"""SQLite store. The only SQL writer in the package.

Raw bytes are delegated to RawArchive before the revision row is inserted.
A failed transaction may leave an unreferenced raw file; the next attempt
reuses those bytes instead of rewriting them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from second_brain.errors import ConfigError, IdentityCollision, SchemaError, StorageError
from second_brain.models import (
    SCHEMA_VERSION,
    Asset,
    BatchOutcome,
    Claim,
    ClassifiedItem,
    Classification,
    FeedBatch,
    FetchEvent,
    FetchResult,
    HttpResponse,
    IngestRecord,
    Item,
    LlmEvent,
    Publication,
    RateState,
    Source,
    SourceState,
    Usage,
    build_item,
    canonical_json,
    format_timestamp,
    make_ingest_record,
    parse_timestamp,
    prompt_fields,
    require_canonical_timestamp,
)
from second_brain.storage.raw import RawArchive

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DETAIL_CHARS = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")
_META_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MIME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}"
    r"(/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,127})?$"
)
_PROTECTED_META = frozenset({"schema_version", "last_classified_at", "changed_topics_pending"})
_RUN_ITEM_STATE = {
    "success": "classified",
    "invalid": "invalid",
    "unavailable": "unavailable",
    "budget_skipped": "deferred",
}
_SUMMARY_OWNED = frozenset({"changed_topics", "log_pending", "files"})

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('rss', 'youtube_rss', 'podcast_rss', 'file')),
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        backfill_days INTEGER NOT NULL CHECK (backfill_days >= 0),
        file_format TEXT CHECK (
            file_format IS NULL OR file_format IN (
                'rss', 'podcast_rss', 'youtube_rss', 'video_json', 'podcast_json'
            )
        ),
        transcript_hosts_json TEXT NOT NULL,
        initial_since_at TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        last_fetched_at TEXT,
        last_http_status INTEGER CHECK (
            last_http_status IS NULL OR (last_http_status >= 100 AND last_http_status <= 599)
        ),
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(id),
        guid TEXT NOT NULL,
        url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        title TEXT NOT NULL,
        published_at TEXT,
        fetched_at TEXT NOT NULL,
        content_hash TEXT NOT NULL CHECK (content_hash <> ''),
        analysis_hash TEXT NOT NULL CHECK (analysis_hash <> ''),
        modality TEXT NOT NULL CHECK (modality IN ('text', 'podcast', 'video')),
        body_raw TEXT NOT NULL,
        body_plain TEXT NOT NULL,
        duplicate_of TEXT REFERENCES items(id),
        UNIQUE (source_id, guid),
        UNIQUE (id, source_id),
        CHECK (duplicate_of IS NULL OR duplicate_of <> id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assets (
        item_id TEXT PRIMARY KEY REFERENCES items(id),
        transcript_status TEXT NOT NULL CHECK (
            transcript_status IN ('ready', 'pending_transcript', 'not_applicable')
        ),
        channel TEXT NOT NULL,
        audio_url TEXT,
        transcript_url TEXT,
        transcript_type TEXT,
        transcript_etag TEXT,
        transcript_last_modified TEXT,
        transcript_last_attempt_at TEXT,
        like_count INTEGER CHECK (like_count IS NULL OR like_count >= 0),
        comment_count INTEGER CHECK (comment_count IS NULL OR comment_count >= 0),
        view_count INTEGER CHECK (view_count IS NULL OR view_count >= 0),
        top_comments_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        since_at TEXT,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (
            status IN ('running', 'publishing', 'success', 'partial', 'failed')
        ),
        publication_json TEXT,
        summary_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_items (
        run_id TEXT NOT NULL REFERENCES runs(id),
        item_id TEXT NOT NULL REFERENCES items(id),
        analysis_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('selected', 'classified', 'invalid', 'unavailable', 'deferred', 'published')
        ),
        error_code TEXT,
        PRIMARY KEY (run_id, item_id, analysis_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classifications (
        item_id TEXT PRIMARY KEY REFERENCES items(id),
        run_id TEXT NOT NULL REFERENCES runs(id),
        topic TEXT NOT NULL,
        bucket TEXT NOT NULL CHECK (bucket IN ('must', 'recommend', 'skip')),
        reason TEXT NOT NULL,
        analysis_hash TEXT NOT NULL,
        policy_hash TEXT NOT NULL,
        classified_at TEXT NOT NULL,
        published_run_id TEXT REFERENCES runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claims (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        analysis_hash TEXT NOT NULL,
        topic TEXT NOT NULL,
        claim_key TEXT NOT NULL,
        sentence TEXT NOT NULL,
        stance TEXT NOT NULL CHECK (stance IN ('for', 'against')),
        evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('title', 'body', 'comment')),
        evidence_quote TEXT NOT NULL,
        comment_index INTEGER,
        FOREIGN KEY (item_id, source_id) REFERENCES items(id, source_id),
        CHECK (
            (evidence_kind = 'comment' AND comment_index IS NOT NULL AND comment_index >= 0)
            OR (evidence_kind <> 'comment' AND comment_index IS NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_revisions (
        item_id TEXT NOT NULL REFERENCES items(id),
        analysis_hash TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (item_id, analysis_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fetch_log (
        id INTEGER PRIMARY KEY,
        run_id TEXT REFERENCES runs(id),
        source_id TEXT REFERENCES sources(id),
        host TEXT NOT NULL,
        url_redacted TEXT NOT NULL,
        url_hash TEXT NOT NULL,
        status INTEGER CHECK (status IS NULL OR (status >= 100 AND status <= 599)),
        at TEXT NOT NULL,
        error_code TEXT,
        retry_after_s REAL CHECK (retry_after_s IS NULL OR retry_after_s >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_limits (
        host TEXT PRIMARY KEY,
        blocked_until TEXT,
        consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
        last_request_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        call_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        purpose TEXT NOT NULL CHECK (purpose IN ('classify', 'review')),
        at TEXT NOT NULL,
        status TEXT NOT NULL,
        billing_path TEXT NOT NULL CHECK (billing_path IN ('test', 'session', 'api', 'unknown')),
        error_code TEXT,
        error_detail TEXT,
        input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
        reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
        cache_read_input_tokens INTEGER CHECK (
            cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0
        ),
        cache_creation_input_tokens INTEGER CHECK (
            cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0
        ),
        total_cost_usd TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS items_fetched_at_idx ON items(fetched_at)",
    "CREATE INDEX IF NOT EXISTS items_canonical_url_idx ON items(canonical_url)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS items_one_primary_idx
    ON items(canonical_url) WHERE duplicate_of IS NULL
    """,
    "CREATE INDEX IF NOT EXISTS classifications_run_id_idx ON classifications(run_id)",
    """
    CREATE INDEX IF NOT EXISTS classifications_published_run_id_idx
    ON classifications(published_run_id)
    """,
    "CREATE INDEX IF NOT EXISTS claims_topic_key_idx ON claims(topic, claim_key)",
    "CREATE INDEX IF NOT EXISTS claims_item_id_idx ON claims(item_id)",
)


class Store:
    """Durable source, item, run, and ledger state. One connection, explicit transactions."""

    def __init__(self, connection: sqlite3.Connection, path: str) -> None:
        self._conn: sqlite3.Connection | None = connection
        self._path = path
        self._active_run_id: str | None = None
        self._tx = False

    @classmethod
    def connect(cls, path: str) -> Store:
        """Open a database and apply the connection pragmas. Does not migrate."""
        if not isinstance(path, str) or path == "" or "\x00" in path:
            raise StorageError("invalid_path")
        try:
            connection = sqlite3.connect(path, isolation_level=None)
        except sqlite3.OperationalError:
            raise StorageError("io") from None
        connection.row_factory = sqlite3.Row
        store = cls(connection, path)
        try:
            store._apply_pragmas()
        except Exception:
            store.close()
            raise
        return store

    def migrate(self) -> None:
        """Create schema 3, or refuse a non-empty database that is not already version 3."""
        self._require_open()
        if not self._foreign_keys_on():
            raise StorageError("foreign_keys_off")
        tables = self._user_tables()
        if not tables:
            with self._transaction():
                self._create_schema()
                self._ensure_llm_error_detail()
                self._exec(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            return
        version = self._read_version()
        if version != SCHEMA_VERSION:
            raise ConfigError("unsupported_schema")
        with self._transaction():
            self._create_schema()
            self._ensure_llm_error_detail()

    def close(self) -> None:
        """Release the connection. A second call is a no-op."""
        self._active_run_id = None
        connection = self._conn
        self._conn = None
        if connection is None:
            return
        try:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        connection.close()

    def get_meta(self, key: str) -> str | None:
        """Return one meta value, including store-owned keys."""
        self._require_schema()
        if not isinstance(key, str) or "\x00" in key:
            raise SchemaError("invalid_meta")
        return self._meta_value(key)

    def set_meta(self, key: str, value: str) -> None:
        """Store a caller meta string. Schema and cursor keys are rejected."""
        self._require_schema()
        if not isinstance(key, str) or _META_KEY.fullmatch(key) is None:
            raise SchemaError("invalid_meta")
        if key in _PROTECTED_META:
            raise StorageError("protected_meta")
        if not isinstance(value, str) or "\x00" in value:
            raise SchemaError("invalid_meta")
        with self._transaction():
            self._put_meta(key, value)

    def sync_sources(self, sources: Sequence[Source], *, now: str, raw_dir: Path) -> None:
        """Register, refresh, and disable sources, then rebalance canonical groups."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        raw_dir = _require_path(raw_dir)
        cleaned = _typed_sequence(sources, Source, "invalid_record")
        seen: set[str] = set()
        for source in cleaned:
            if source.id in seen:
                raise ConfigError("duplicate_source")
            seen.add(source.id)
        with self._transaction():
            before = {
                row["id"]: int(row["enabled"])
                for row in self._exec("SELECT id, enabled FROM sources")
            }
            for source in cleaned:
                hosts = canonical_json(list(source.transcript_hosts))
                if source.id not in before:
                    initial = _initial_since(now, source.backfill_days)
                    self._exec(
                        """
                        INSERT INTO sources (
                            id, kind, url, title, backfill_days, file_format, transcript_hosts_json,
                            initial_since_at, etag, last_modified, last_fetched_at,
                            last_http_status, enabled
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 1)
                        """,
                        (
                            source.id,
                            source.kind,
                            source.url,
                            source.title,
                            source.backfill_days,
                            source.file_format,
                            hosts,
                            initial,
                        ),
                    )
                else:
                    self._exec(
                        """
                        UPDATE sources
                        SET kind=?, url=?, title=?, backfill_days=?, file_format=?,
                            transcript_hosts_json=?, enabled=1
                        WHERE id=?
                        """,
                        (
                            source.kind,
                            source.url,
                            source.title,
                            source.backfill_days,
                            source.file_format,
                            hosts,
                            source.id,
                        ),
                    )
            kept = tuple(source.id for source in cleaned)
            if kept:
                marks = ",".join("?" for _ in kept)
                self._exec(f"UPDATE sources SET enabled=0 WHERE id NOT IN ({marks})", kept)
            else:
                self._exec("UPDATE sources SET enabled=0")
            after = {
                row["id"]: int(row["enabled"])
                for row in self._exec("SELECT id, enabled FROM sources")
            }
            urls = [
                row["canonical_url"]
                for row in self._exec("SELECT DISTINCT canonical_url FROM items")
            ]
            for url in urls:
                self._rebalance(url, now, raw_dir)
            flipped = [
                source_id
                for source_id, enabled in after.items()
                if source_id in before and before[source_id] != enabled
            ]
            if flipped:
                marks = ",".join("?" for _ in flipped)
                rows = self._exec(
                    f"""
                    SELECT c.topic
                    FROM classifications c
                    JOIN items i ON i.id = c.item_id
                    WHERE i.source_id IN ({marks})
                    """,
                    tuple(flipped),
                )
                for row in rows:
                    self._note_topic_on(self._active_run_id, row["topic"])

    def get_source_state(self, source_id: str) -> SourceState:
        """Return the fixed backfill bound and the last committed validators."""
        self._require_schema()
        row = self._source_row(_require_id(source_id, "invalid_source_id"))
        if row is None:
            raise StorageError("unknown_source")
        return SourceState(
            source_id=row["id"],
            initial_since_at=row["initial_since_at"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_fetched_at=row["last_fetched_at"],
            last_http_status=row["last_http_status"],
        )

    def ingest_feed(
        self,
        source: Source,
        batch: FeedBatch,
        *,
        now: str,
        raw_dir: Path,
        response: HttpResponse | None,
    ) -> FetchResult:
        """Commit one feed. Validators move only when the batch has no entry issues."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        raw_dir = _require_path(raw_dir)
        if not isinstance(source, Source) or not isinstance(batch, FeedBatch):
            raise SchemaError("invalid_record")
        if response is not None and not isinstance(response, HttpResponse):
            raise SchemaError("invalid_record")
        if response is not None and response.status != 200:
            raise SchemaError("invalid_status")
        ordered = tuple(sorted(batch.records, key=lambda record: record.item.guid))
        guids = [record.item.guid for record in ordered]
        if len(guids) != len(set(guids)):
            raise SchemaError("duplicate_guid")
        for record in ordered:
            if record.item.source_id != source.id:
                raise SchemaError("source_mismatch")
        counts = {
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped_old": 0,
            "skipped_dup": 0,
            "held_date": 0,
        }
        with self._transaction():
            row = self._source_row(source.id)
            if row is None:
                raise StorageError("unknown_source")
            if int(row["enabled"]) != 1:
                raise StorageError("source_disabled")
            for record in ordered:
                for name, amount in self._apply_record(record, now, raw_dir, row["initial_since_at"]).items():
                    counts[name] += amount
            if not batch.issues:
                self._touch_validators(source.id, now, response)
        return FetchResult(
            inserted=counts["inserted"],
            updated=counts["updated"],
            unchanged=counts["unchanged"],
            skipped_old=counts["skipped_old"],
            skipped_dup=counts["skipped_dup"],
            held_date=counts["held_date"],
            invalid=len(batch.issues),
        )

    def accept_not_modified(self, source_id: str, *, now: str, response: HttpResponse) -> None:
        """Record a 304. Missing validators are left unchanged."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        source_id = _require_id(source_id, "invalid_source_id")
        if not isinstance(response, HttpResponse):
            raise SchemaError("invalid_record")
        if response.status != 304:
            raise SchemaError("invalid_status")
        with self._transaction():
            row = self._source_row(source_id)
            if row is None:
                raise StorageError("unknown_source")
            etag = row["etag"]
            modified = row["last_modified"]
            if "etag" in response.headers:
                etag = response.headers["etag"]
            if "last-modified" in response.headers:
                modified = response.headers["last-modified"]
            self._exec(
                """
                UPDATE sources
                SET etag=?, last_modified=?, last_fetched_at=?, last_http_status=?
                WHERE id=?
                """,
                (etag, modified, now, 304, source_id),
            )

    def transcript_candidates(self, *, now: str) -> tuple[IngestRecord, ...]:
        """Enabled primary items still waiting for a transcript, including future dates."""
        self._require_schema()
        require_canonical_timestamp(now)
        items = self._select_items(
            """
            s.enabled = 1
            AND i.duplicate_of IS NULL
            AND a.transcript_status = 'pending_transcript'
            """
        )
        assets = self.assets_for(tuple(item.id for item in items))
        return tuple(make_ingest_record(item, assets[item.id]) for item in items)

    def update_transcript(
        self,
        item_id: str,
        *,
        expected_hash: str,
        text: str,
        response: HttpResponse,
        now: str,
        raw_dir: Path,
    ) -> None:
        """Replace show notes with a downloaded transcript when the hash still matches."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        raw_dir = _require_path(raw_dir)
        if not isinstance(text, str) or text.strip() == "" or "\x00" in text:
            raise SchemaError("empty_transcript")
        if not isinstance(response, HttpResponse):
            raise SchemaError("invalid_record")
        if response.status != 200:
            raise SchemaError("invalid_status")
        if not isinstance(expected_hash, str) or _HEX64.fullmatch(expected_hash) is None:
            raise SchemaError("invalid_hash")
        with self._transaction():
            item = self._load_item(item_id)
            if item is None:
                raise StorageError("unknown_item")
            if item.analysis_hash != expected_hash:
                raise SchemaError("analysis_hash_mismatch")
            asset = self._require_asset(item.id)
            mime = _content_type(response.headers.get("content-type"), asset.transcript_type)
            etag = response.headers["etag"] if "etag" in response.headers else asset.transcript_etag
            modified = (
                response.headers["last-modified"]
                if "last-modified" in response.headers
                else asset.transcript_last_modified
            )
            built = build_item(
                source_id=item.source_id,
                guid=item.guid,
                url=item.url,
                title=item.title,
                fetched_at=now,
                modality=item.modality,
                body_raw=text,
                body_plain=text,
                published_at=item.published_at,
                duplicate_of=item.duplicate_of,
            )
            merged = Asset(
                item_id=built.id,
                transcript_status="ready",
                channel=asset.channel,
                audio_url=asset.audio_url,
                transcript_url=asset.transcript_url,
                transcript_type=mime,
                transcript_etag=etag,
                transcript_last_modified=modified,
                transcript_last_attempt_at=now,
                like_count=asset.like_count,
                comment_count=asset.comment_count,
                view_count=asset.view_count,
                top_comments=asset.top_comments,
            )
            record = make_ingest_record(built, merged)
            if record.item.analysis_hash != item.analysis_hash:
                self._drop_classification(item.id, self._active_run_id)
                final_item = record.item
            else:
                final_item = replace(record.item, fetched_at=item.fetched_at)
                record = IngestRecord(final_item, record.asset)
            self._update_item(final_item)
            self._update_asset(record.asset)
            if self._is_primary(item.id):
                self._ensure_raw(item.id, raw_dir, now)

    def record_transcript_attempt(self, item_id: str, *, now: str) -> None:
        """Remember a transcript attempt without changing the stored text."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        with self._transaction():
            if self._load_item(item_id) is None:
                raise StorageError("unknown_item")
            self._exec(
                "UPDATE assets SET transcript_last_attempt_at=? WHERE item_id=?",
                (now, item_id),
            )

    def items_for_classify(self, *, now: str) -> tuple[Item, ...]:
        """Enabled primaries with a usable date and no current classification."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        return self._select_items(
            """
            s.enabled = 1
            AND i.duplicate_of IS NULL
            AND i.published_at IS NOT NULL
            AND i.published_at <= ?
            AND a.transcript_status <> 'pending_transcript'
            AND NOT EXISTS (
                SELECT 1 FROM classifications c
                WHERE c.item_id = i.id AND c.analysis_hash = i.analysis_hash
            )
            """,
            (now,),
        )

    def assets_for(self, item_ids: Sequence[str]) -> dict[str, Asset]:
        """Return assets for the requested ids. Missing ids are omitted."""
        self._require_schema()
        ids = _id_list(item_ids)
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self._exec(f"SELECT * FROM assets WHERE item_id IN ({marks})", tuple(ids))
        return {row["item_id"]: self._asset_from_row(row) for row in rows}

    def pending_transcript_items(self, *, now: str) -> tuple[Item, ...]:
        """Enabled primaries waiting on a transcript. Pending wins over a date hold."""
        self._require_schema()
        require_canonical_timestamp(now)
        return self._select_items(
            """
            s.enabled = 1
            AND i.duplicate_of IS NULL
            AND a.transcript_status = 'pending_transcript'
            """
        )

    def held_date_items(self, *, now: str) -> tuple[Item, ...]:
        """Enabled primaries whose date is missing or still in the future."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        return self._select_items(
            """
            s.enabled = 1
            AND i.duplicate_of IS NULL
            AND a.transcript_status <> 'pending_transcript'
            AND (i.published_at IS NULL OR i.published_at > ?)
            """,
            (now,),
        )

    def begin_run(self, run_id: str, *, started_at: str) -> None:
        """Open a run on this connection. since_at copies the current cursor."""
        self._require_schema()
        run_id = _require_id(run_id)
        started_at = require_canonical_timestamp(started_at)
        if self._active_run_id is not None:
            raise StorageError("run_active")
        with self._transaction():
            if self._run_row(run_id) is not None:
                raise StorageError("duplicate_run")
            pending = self._stash_topics()
            summary = {"changed_topics": sorted(pending), "log_pending": False}
            self._exec(
                """
                INSERT INTO runs (
                    id, started_at, since_at, finished_at, status, publication_json, summary_json
                ) VALUES (?, ?, ?, NULL, 'running', NULL, ?)
                """,
                (run_id, started_at, self._meta_value("last_classified_at"), canonical_json(summary)),
            )
            if pending:
                self._exec("DELETE FROM meta WHERE key='changed_topics_pending'")
        self._active_run_id = run_id

    def select_run_items(self, run_id: str, items: Sequence[Item]) -> None:
        """Mark the current analysis hash selected. An existing state is kept."""
        self._require_schema()
        run_id = _require_id(run_id)
        selected = _typed_sequence(items, Item, "invalid_record")
        with self._transaction():
            if self._run_row(run_id) is None:
                raise StorageError("unknown_run")
            for item in selected:
                stored = self._load_item(item.id)
                if stored is None:
                    raise SchemaError("missing_item")
                if stored.source_id != item.source_id or stored.guid != item.guid:
                    raise IdentityCollision()
                if stored.analysis_hash != item.analysis_hash:
                    raise SchemaError("analysis_hash_mismatch")
                self._exec(
                    """
                    INSERT INTO run_items (run_id, item_id, analysis_hash, state, error_code)
                    VALUES (?, ?, ?, 'selected', NULL)
                    ON CONFLICT (run_id, item_id, analysis_hash) DO NOTHING
                    """,
                    (run_id, item.id, item.analysis_hash),
                )

    def save_batch(
        self,
        outcome: BatchOutcome,
        classifications: Sequence[Classification],
        claims: Sequence[Claim],
        *,
        run_id: str,
    ) -> None:
        """Commit one batch atomically. A hash mismatch leaves the earlier rows unwritten."""
        self._require_schema()
        if not isinstance(outcome, BatchOutcome):
            raise SchemaError("invalid_record")
        run_id = _require_id(run_id)
        stored_classes = _typed_sequence(classifications, Classification, "invalid_record")
        stored_claims = _typed_sequence(claims, Claim, "invalid_record")
        with self._transaction():
            if self._run_row(run_id) is None:
                raise StorageError("unknown_run")
            if outcome.status != "success":
                if stored_classes or stored_claims:
                    raise SchemaError("batch_payload")
                self._defer_outcome(outcome, run_id)
                return
            self._write_success(outcome, stored_classes, stored_claims, run_id)

    def get_current_classified(self, *, now: str) -> tuple[ClassifiedItem, ...]:
        """Current primaries whose classification still matches the stored text."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        return self._classified(now, unpublished_only=False)

    def get_current_claims(self, *, now: str) -> tuple[Claim, ...]:
        """Claims attached to the current classified items, ordered by id."""
        claims = [claim for item in self.get_current_classified(now=now) for claim in item.claims]
        claims.sort(key=lambda claim: claim.id)
        return tuple(claims)

    def get_unpublished_classified(self, *, now: str) -> tuple[ClassifiedItem, ...]:
        """Current classifications that have not been marked published by any run."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        return self._classified(now, unpublished_only=True)

    def get_changed_topics(self, run_id: str) -> frozenset[str]:
        """Topics this run invalidated or classified. Callers cannot overwrite the set."""
        self._require_schema()
        run = self._require_run(_require_id(run_id))
        summary = self._load_summary(run["summary_json"])
        return frozenset(summary["changed_topics"])

    def known_claim_keys(self) -> tuple[str, ...]:
        """Sorted claim keys for enabled current primaries. This method has no clock."""
        self._require_schema()
        rows = self._exec(
            """
            SELECT DISTINCT c.claim_key
            FROM claims c
            JOIN items i ON i.id = c.item_id AND i.analysis_hash = c.analysis_hash
            JOIN classifications k ON k.item_id = i.id AND k.analysis_hash = i.analysis_hash
            JOIN sources s ON s.id = i.source_id
            JOIN assets a ON a.item_id = i.id
            WHERE s.enabled = 1
              AND i.duplicate_of IS NULL
              AND i.published_at IS NOT NULL
              AND a.transcript_status <> 'pending_transcript'
            ORDER BY c.claim_key
            """
        )
        return tuple(row["claim_key"] for row in rows)

    def append_llm_event(self, event: LlmEvent) -> None:
        """Insert a call or finish a started row. A second terminal payload is refused."""
        self._require_schema()
        if not isinstance(event, LlmEvent):
            raise SchemaError("invalid_record")
        with self._transaction():
            if self._run_row(event.run_id) is None:
                raise StorageError("unknown_run")
            row = self._exec(
                "SELECT * FROM llm_calls WHERE call_id=?",
                (event.call_id,),
            ).fetchone()
            if row is None:
                self._insert_llm(event)
                return
            if row["run_id"] != event.run_id or row["purpose"] != event.purpose:
                raise StorageError("llm_event_final")
            if row["status"] != "started":
                if _same_llm(row, event):
                    return
                raise StorageError("llm_event_final")
            if event.status == "started" and _same_llm(row, event):
                return
            usage = _usage_values(event.usage)
            self._exec(
                """
                UPDATE llm_calls
                SET status=?, error_code=?, billing_path=?, at=?,
                    input_tokens=?, output_tokens=?, reasoning_tokens=?,
                    cache_read_input_tokens=?, cache_creation_input_tokens=?, total_cost_usd=?
                WHERE call_id=?
                """,
                (event.status, event.error_code, event.billing_path, event.at, *usage, event.call_id),
            )

    def set_llm_error_detail(self, call_id: str, detail: str) -> None:
        """Store one redacted stderr line on an existing model call."""

        self._require_schema()
        call_id = _require_id(call_id)
        if not isinstance(detail, str) or _DETAIL_CHARS.fullmatch(detail) is None:
            raise SchemaError("invalid_text")
        with self._transaction():
            row = self._exec(
                "SELECT error_detail FROM llm_calls WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if row is None:
                raise StorageError("llm_event_final")
            current = row["error_detail"]
            if current is not None:
                if current == detail:
                    return
                raise StorageError("llm_event_final")
            self._exec(
                "UPDATE llm_calls SET error_detail=? WHERE call_id=?",
                (detail, call_id),
            )

    def events_for_run(self, run_id: str) -> tuple[LlmEvent | FetchEvent, ...]:
        """Return this run's model and fetch events, ordered by time."""
        self._require_schema()
        run_id = _require_id(run_id)
        if self._run_row(run_id) is None:
            raise StorageError("unknown_run")
        events: list[LlmEvent | FetchEvent] = []
        for row in self._exec("SELECT * FROM llm_calls WHERE run_id=?", (run_id,)):
            events.append(_llm_from_row(row))
        for row in self._exec("SELECT * FROM fetch_log WHERE run_id=? ORDER BY id", (run_id,)):
            events.append(_fetch_from_row(row))
        events.sort(key=_event_sort_key)
        return tuple(events)

    def mark_publishing(
        self,
        publication: Publication,
        *,
        status_after_publish: str,
        summary: Mapping[str, object],
    ) -> None:
        """Store the publication snapshot. Caller keys cannot replace changed topics."""
        self._require_schema()
        if not isinstance(publication, Publication):
            raise SchemaError("invalid_record")
        if status_after_publish not in {"success", "partial"}:
            raise SchemaError("invalid_run_status")
        if not isinstance(summary, Mapping):
            raise SchemaError("invalid_summary")
        files = _file_list(summary["files"]) if "files" in summary else []
        caller = {
            key: value
            for key, value in summary.items()
            if key not in _SUMMARY_OWNED
        }
        caller_json = _json_object(caller)
        with self._transaction():
            run = self._require_run(publication.run_id)
            stored = self._load_summary(run["summary_json"])
            merged = dict(stored)
            merged.update(caller_json)
            merged["changed_topics"] = list(stored["changed_topics"])
            merged["log_pending"] = True if stored["log_pending"] is True else False
            payload = canonical_json(
                {
                    "run_id": publication.run_id,
                    "html_path": publication.html_path,
                    "manifest_path": publication.manifest_path,
                    "classified_refs": [list(ref) for ref in publication.classified_refs],
                    "status_after_publish": status_after_publish,
                    "files": files,
                }
            )
            if run["status"] == "publishing":
                if run["publication_json"] == payload:
                    return
                raise StorageError("publication_exists")
            if run["status"] != "running":
                raise StorageError("invalid_run_state")
            self._exec(
                """
                UPDATE runs
                SET status='publishing', publication_json=?, summary_json=?
                WHERE id=?
                """,
                (payload, canonical_json(merged), publication.run_id),
            )

    def finish_publication(self, publication: Publication, *, finished_at: str) -> None:
        """Mark eligible refs published. Success advances the cursor to started_at."""
        self._require_schema()
        finished_at = require_canonical_timestamp(finished_at)
        if not isinstance(publication, Publication):
            raise SchemaError("invalid_record")
        with self._transaction():
            run = self._require_run(publication.run_id)
            payload = _load_publication(run["publication_json"])
            if not _same_publication(payload, publication):
                raise StorageError("publication_mismatch")
            status_after = payload.get("status_after_publish")
            if status_after not in {"success", "partial"}:
                raise StorageError("corrupt")
            if run["status"] in {"success", "partial"}:
                if run["status"] != status_after:
                    raise StorageError("invalid_run_state")
            elif run["status"] != "publishing":
                raise StorageError("invalid_run_state")
            else:
                for item_id, digest in publication.classified_refs:
                    self._mark_published(publication.run_id, item_id, digest, finished_at)
                summary = self._load_summary(run["summary_json"])
                summary["log_pending"] = True
                self._exec(
                    "UPDATE runs SET status=?, finished_at=?, summary_json=? WHERE id=?",
                    (status_after, finished_at, canonical_json(summary), publication.run_id),
                )
                if status_after == "success":
                    self._put_meta("last_classified_at", run["started_at"])
        if self._active_run_id == publication.run_id:
            self._active_run_id = None

    def unfinished_publications(self) -> tuple[Publication, ...]:
        """Publications whose files may exist while the run is still publishing."""
        self._require_schema()
        rows = self._exec(
            """
            SELECT id, publication_json
            FROM runs
            WHERE status = 'publishing' AND publication_json IS NOT NULL
            ORDER BY started_at, id
            """
        )
        found: list[Publication] = []
        for row in rows:
            payload = _load_publication(row["publication_json"])
            found.append(_publication_from_payload(row["id"], payload))
        return tuple(found)

    def fail_run(self, run_id: str, *, finished_at: str, code: str) -> None:
        """Fail a running run. A publishing run keeps its snapshot and status."""
        self._require_schema()
        run_id = _require_id(run_id)
        finished_at = require_canonical_timestamp(finished_at)
        if not isinstance(code, str) or _TOKEN.fullmatch(code) is None:
            raise SchemaError("invalid_error_code")
        with self._transaction():
            run = self._require_run(run_id)
            summary = self._load_summary(run["summary_json"])
            summary["error_code"] = code
            if run["status"] == "publishing":
                self._exec(
                    "UPDATE runs SET summary_json=? WHERE id=?",
                    (canonical_json(summary), run_id),
                )
                return
            if run["status"] != "running":
                raise StorageError("invalid_run_state")
            summary["log_pending"] = True
            self._exec(
                "UPDATE runs SET status='failed', finished_at=?, summary_json=? WHERE id=?",
                (finished_at, canonical_json(summary), run_id),
            )
        if self._active_run_id == run_id:
            self._active_run_id = None

    def recover_interrupted_runs(self, *, now: str) -> None:
        """Fail leftover running rows and mark started model calls unknown. Publishing stays."""
        self._require_schema()
        now = require_canonical_timestamp(now)
        cleared: list[str] = []
        with self._transaction():
            rows = self._exec("SELECT id, summary_json FROM runs WHERE status='running'")
            for row in rows:
                summary = self._load_summary(row["summary_json"])
                summary["error_code"] = "interrupted"
                summary["log_pending"] = True
                self._exec(
                    "UPDATE runs SET status='failed', finished_at=?, summary_json=? WHERE id=?",
                    (now, canonical_json(summary), row["id"]),
                )
                cleared.append(row["id"])
            self._exec(
                "UPDATE llm_calls SET status='interrupted_unknown' WHERE status='started'"
            )
        if self._active_run_id in cleared:
            self._active_run_id = None

    def pending_log_runs(self) -> tuple[str, ...]:
        """Run ids whose log block still needs to be appended, oldest first."""
        self._require_schema()
        rows = self._exec("SELECT id, summary_json FROM runs ORDER BY started_at, id")
        found: list[str] = []
        for row in rows:
            summary = self._load_summary(row["summary_json"])
            if summary["log_pending"] is True:
                found.append(row["id"])
        return tuple(found)

    def mark_log_written(self, run_id: str) -> None:
        """Clear the log-pending flag. The run status is left as it was."""
        self._require_schema()
        run_id = _require_id(run_id)
        with self._transaction():
            run = self._require_run(run_id)
            summary = self._load_summary(run["summary_json"])
            summary["log_pending"] = False
            self._exec(
                "UPDATE runs SET summary_json=? WHERE id=?",
                (canonical_json(summary), run_id),
            )

    def get_rate_state(self, host: str) -> RateState:
        """Return the saved host state, or a fresh idle state that is not inserted."""
        self._require_schema()
        probe = RateState(host=host)
        row = self._exec("SELECT * FROM rate_limits WHERE host=?", (probe.host,)).fetchone()
        if row is None:
            return probe
        return RateState(
            host=row["host"],
            blocked_until=row["blocked_until"],
            consecutive_failures=row["consecutive_failures"],
            last_request_at=row["last_request_at"],
        )

    def put_rate_state(self, state: RateState) -> None:
        """Replace the saved state for one host."""
        self._require_schema()
        if not isinstance(state, RateState):
            raise SchemaError("invalid_record")
        with self._transaction():
            self._exec(
                """
                INSERT INTO rate_limits (host, blocked_until, consecutive_failures, last_request_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (host) DO UPDATE SET
                    blocked_until=excluded.blocked_until,
                    consecutive_failures=excluded.consecutive_failures,
                    last_request_at=excluded.last_request_at
                """,
                (state.host, state.blocked_until, state.consecutive_failures, state.last_request_at),
            )

    def append_fetch_event(self, event: FetchEvent) -> None:
        """Append one redacted fetch. The active run id is attached when present."""
        self._require_schema()
        if not isinstance(event, FetchEvent):
            raise SchemaError("invalid_record")
        with self._transaction():
            if event.source_id is not None and self._source_row(event.source_id) is None:
                raise StorageError("unknown_source")
            self._exec(
                """
                INSERT INTO fetch_log (
                    run_id, source_id, host, url_redacted, url_hash, status, at, error_code, retry_after_s
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._active_run_id,
                    event.source_id,
                    event.host,
                    event.url_redacted,
                    event.url_hash,
                    event.status,
                    event.at,
                    event.error_code,
                    event.retry_after_s,
                ),
            )

    def _apply_pragmas(self) -> None:
        connection = self._conn
        if connection is None:
            raise StorageError("closed")
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            connection.execute("PRAGMA busy_timeout=5000")
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        except sqlite3.OperationalError:
            raise StorageError("pragma_failed") from None
        if int(foreign_keys) != 1 or str(journal).lower() != "wal" or int(timeout) != 5000:
            raise StorageError("pragma_failed")

    def _apply_record(
        self,
        incoming: IngestRecord,
        now: str,
        raw_dir: Path,
        initial_since: str,
    ) -> dict[str, int]:
        stored = self._exec(
            "SELECT id FROM items WHERE source_id=? AND guid=?",
            (incoming.item.source_id, incoming.item.guid),
        ).fetchone()
        if stored is None:
            return self._insert_new(incoming, now, raw_dir, initial_since)
        return self._update_existing(stored["id"], incoming, now, raw_dir)

    def _insert_new(
        self,
        incoming: IngestRecord,
        now: str,
        raw_dir: Path,
        initial_since: str,
    ) -> dict[str, int]:
        if _is_old(incoming.item.published_at, initial_since):
            return {"skipped_old": 1}
        duplicate_of = self._primary_id(incoming.item.canonical_url, incoming.item.id)
        record = _recompute(incoming, fetched_at=now, duplicate_of=duplicate_of)
        self._insert_item(record.item)
        self._insert_asset(record.asset)
        self._rebalance(record.item.canonical_url, now, raw_dir)
        counts = {"inserted": 1}
        if not self._is_primary(record.item.id):
            counts["skipped_dup"] = 1
        else:
            self._ensure_raw(record.item.id, raw_dir, now)
        if _is_held(record.item.published_at, now, record.asset.transcript_status):
            counts["held_date"] = 1
        return counts

    def _update_existing(
        self,
        item_id: str,
        incoming: IngestRecord,
        now: str,
        raw_dir: Path,
    ) -> dict[str, int]:
        stored_item = self._require_loaded_item(item_id)
        stored_asset = self._require_asset(item_id)
        merged_item, merged_asset = _merge_feed(stored_item, stored_asset, incoming, now)
        provisional = make_ingest_record(merged_item, merged_asset)
        hash_changed = provisional.item.analysis_hash != stored_item.analysis_hash
        if hash_changed:
            self._drop_classification(item_id, self._active_run_id)
        new_dup = stored_item.duplicate_of
        url_changed = provisional.item.canonical_url != stored_item.canonical_url
        if url_changed:
            new_dup = self._moving_duplicate(
                item_id,
                stored_item.canonical_url,
                provisional.item.canonical_url,
            )
        fetched_at = now if hash_changed else stored_item.fetched_at
        final_item = replace(provisional.item, fetched_at=fetched_at, duplicate_of=new_dup)
        record = IngestRecord(final_item, provisional.asset)
        self._update_item(final_item)
        self._update_asset(record.asset)
        if url_changed:
            self._rebalance(stored_item.canonical_url, now, raw_dir)
        self._rebalance(final_item.canonical_url, now, raw_dir)
        published_changed = final_item.published_at != stored_item.published_at
        if published_changed or url_changed:
            topic = self._exec(
                "SELECT topic FROM classifications WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if topic is not None:
                self._note_topic_on(self._active_run_id, topic["topic"])
        if self._is_primary(item_id):
            self._ensure_raw(item_id, raw_dir, now)
        counts = {"updated": 1} if hash_changed else {"unchanged": 1}
        if _is_held(final_item.published_at, now, record.asset.transcript_status):
            counts["held_date"] = 1
        return counts

    def _moving_duplicate(self, item_id: str, old_url: str, new_url: str) -> str | None:
        """Point a moving primary at a real row before its URL changes.

        The partial unique index is checked at the end of the update, so the
        row must not arrive as a second primary. A temporary cycle is repaired
        by the following rebalance.
        """
        current = self._require_loaded_item(item_id)
        occupant = self._primary_id(new_url, item_id)
        if current.duplicate_of is None and occupant is None:
            others = self._member_ids(old_url, item_id)
            if others:
                return min(others)
            return None
        if occupant is not None:
            return occupant
        return None

    def _rebalance(self, canonical: str, now: str, raw_dir: Path) -> None:
        rows = list(
            self._exec(
                """
                SELECT i.id, i.duplicate_of, s.enabled
                FROM items i
                JOIN sources s ON s.id = i.source_id
                WHERE i.canonical_url=?
                ORDER BY i.id
                """,
                (canonical,),
            )
        )
        if not rows:
            return
        enabled = [row["id"] for row in rows if int(row["enabled"]) == 1]
        pool = enabled or [row["id"] for row in rows]
        chosen = min(pool)
        primaries = [row["id"] for row in rows if row["duplicate_of"] is None]
        aliases_ok = all(row["duplicate_of"] == chosen for row in rows if row["id"] != chosen)
        chosen_row = next(row for row in rows if row["id"] == chosen)
        if len(primaries) == 1 and chosen_row["duplicate_of"] is None and aliases_ok:
            for row in rows:
                if row["id"] != chosen:
                    self._drop_classification(row["id"], self._active_run_id)
            return
        # Demote every other primary before promoting, so the unique slot is free
        # and the chosen row never points at itself.
        for row in rows:
            if row["id"] != chosen and row["duplicate_of"] is None:
                self._exec("UPDATE items SET duplicate_of=? WHERE id=?", (chosen, row["id"]))
                self._drop_classification(row["id"], self._active_run_id)
        self._exec("UPDATE items SET duplicate_of=NULL WHERE id=?", (chosen,))
        self._ensure_raw(chosen, raw_dir, now)
        for row in rows:
            if row["id"] == chosen:
                continue
            self._exec("UPDATE items SET duplicate_of=? WHERE id=?", (chosen, row["id"]))
            self._drop_classification(row["id"], self._active_run_id)
        self._assert_group(canonical, chosen)

    def _assert_group(self, canonical: str, chosen: str) -> None:
        rows = list(
            self._exec(
                "SELECT id, duplicate_of FROM items WHERE canonical_url=?",
                (canonical,),
            )
        )
        primaries = [row["id"] for row in rows if row["duplicate_of"] is None]
        if primaries != [chosen]:
            raise StorageError("corrupt")
        for row in rows:
            if row["id"] == chosen:
                continue
            if row["duplicate_of"] != chosen:
                raise StorageError("corrupt")

    def _defer_outcome(self, outcome: BatchOutcome, run_id: str) -> None:
        state = _RUN_ITEM_STATE[outcome.status]
        seen: set[str] = set()
        for item_id in outcome.item_ids:
            if item_id in seen:
                raise SchemaError("batch_payload")
            seen.add(item_id)
            item = self._load_item(item_id)
            if item is None:
                raise SchemaError("missing_item")
            self._require_run_item(run_id, item.id, item.analysis_hash)
            self._exec(
                """
                UPDATE run_items
                SET state=?, error_code=?
                WHERE run_id=? AND item_id=? AND analysis_hash=?
                """,
                (state, outcome.error_code, run_id, item.id, item.analysis_hash),
            )

    def _write_success(
        self,
        outcome: BatchOutcome,
        classifications: tuple[Classification, ...],
        claims: tuple[Claim, ...],
        run_id: str,
    ) -> None:
        if len(set(outcome.item_ids)) != len(outcome.item_ids):
            raise SchemaError("batch_payload")
        by_item: dict[str, Classification] = {}
        for classification in classifications:
            if classification.item_id in by_item:
                raise SchemaError("batch_payload")
            by_item[classification.item_id] = classification
        if set(by_item) != set(outcome.item_ids):
            raise SchemaError("batch_payload")
        grouped: dict[str, list[Claim]] = {item_id: [] for item_id in outcome.item_ids}
        for claim in claims:
            if claim.item_id not in grouped:
                raise SchemaError("claim_batch_mismatch")
            grouped[claim.item_id].append(claim)
        prepared: list[tuple[Item, Asset, Classification, tuple[Claim, ...]]] = []
        for item_id in outcome.item_ids:
            item = self._load_item(item_id)
            if item is None:
                raise SchemaError("missing_item")
            classification = by_item[item_id]
            if classification.run_id != run_id:
                raise SchemaError("batch_mismatch")
            if classification.analysis_hash != item.analysis_hash:
                raise SchemaError("analysis_hash_mismatch")
            if item.duplicate_of is not None:
                raise SchemaError("not_primary")
            self._require_run_item(run_id, item.id, item.analysis_hash)
            asset = self._require_asset(item.id)
            audience = prompt_fields(item, asset)["audience"]
            if not isinstance(audience, list):
                raise StorageError("corrupt")
            item_claims = tuple(grouped[item_id])
            if len(item_claims) > 5:
                raise SchemaError("batch_payload")
            for claim in item_claims:
                if claim.source_id != item.source_id or claim.analysis_hash != item.analysis_hash:
                    raise SchemaError("claim_batch_mismatch")
                if claim.comment_index is not None and claim.comment_index >= len(audience):
                    raise SchemaError("invalid_comment_index")
            prepared.append((item, asset, classification, item_claims))
        for item, _asset, classification, item_claims in prepared:
            self._drop_classification(item.id, run_id)
            self._insert_classification(classification)
            for claim in item_claims:
                self._insert_claim(claim)
            self._exec(
                """
                UPDATE run_items
                SET state='classified', error_code=NULL
                WHERE run_id=? AND item_id=? AND analysis_hash=?
                """,
                (run_id, item.id, item.analysis_hash),
            )
            self._note_topic_on(run_id, classification.topic)

    def _classified(self, now: str, *, unpublished_only: bool) -> tuple[ClassifiedItem, ...]:
        unpublished = "AND c.published_run_id IS NULL" if unpublished_only else ""
        rows = self._exec(
            f"""
            SELECT i.id AS item_id, c.topic, c.bucket, c.reason, c.policy_hash
            FROM items i
            JOIN sources s ON s.id = i.source_id
            JOIN assets a ON a.item_id = i.id
            JOIN classifications c ON c.item_id = i.id AND c.analysis_hash = i.analysis_hash
            WHERE s.enabled = 1
              AND i.duplicate_of IS NULL
              AND i.published_at IS NOT NULL
              AND i.published_at <= ?
              AND a.transcript_status <> 'pending_transcript'
              {unpublished}
            ORDER BY c.topic, i.source_id, i.id
            """,
            (now,),
        )
        found: list[ClassifiedItem] = []
        for row in rows:
            item = self._require_loaded_item(row["item_id"])
            asset = self._require_asset(item.id)
            audience = prompt_fields(item, asset)["audience"]
            if not isinstance(audience, list):
                raise StorageError("corrupt")
            claim_rows = self._exec(
                """
                SELECT * FROM claims
                WHERE item_id=? AND analysis_hash=?
                ORDER BY id
                """,
                (item.id, item.analysis_hash),
            )
            claim_list = tuple(self._claim_from_row(claim) for claim in claim_rows)
            found.append(
                ClassifiedItem(
                    id=item.id,
                    source_id=item.source_id,
                    topic=row["topic"],
                    url=item.canonical_url,
                    title=item.title,
                    bucket=row["bucket"],
                    reason=row["reason"],
                    published_at=item.published_at,
                    analysis_hash=item.analysis_hash,
                    policy_hash=row["policy_hash"],
                    comments=tuple(audience),
                    claims=claim_list,
                )
            )
        return tuple(found)

    def _mark_published(self, run_id: str, item_id: str, digest: str, finished_at: str) -> None:
        updated = self._exec(
            """
            UPDATE classifications
            SET published_run_id=?
            WHERE item_id=?
              AND analysis_hash=?
              AND published_run_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM items i
                  JOIN sources s ON s.id = i.source_id
                  JOIN assets a ON a.item_id = i.id
                  WHERE i.id = classifications.item_id
                    AND i.analysis_hash = ?
                    AND i.duplicate_of IS NULL
                    AND s.enabled = 1
                    AND a.transcript_status <> 'pending_transcript'
                    AND i.published_at IS NOT NULL
                    AND i.published_at <= ?
              )
            """,
            (run_id, item_id, digest, digest, finished_at),
        )
        if updated.rowcount != 1:
            return
        self._exec(
            """
            INSERT INTO run_items (run_id, item_id, analysis_hash, state, error_code)
            VALUES (?, ?, ?, 'published', NULL)
            ON CONFLICT (run_id, item_id, analysis_hash)
            DO UPDATE SET state='published', error_code=NULL
            """,
            (run_id, item_id, digest),
        )

    def _ensure_raw(self, item_id: str, raw_dir: Path, now: str) -> None:
        item = self._require_loaded_item(item_id)
        if item.duplicate_of is not None:
            return
        asset = self._require_asset(item_id)
        record = make_ingest_record(item, asset)
        relative, digest = RawArchive(raw_dir).put(record)
        payload = _read_nofollow(raw_dir / relative)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise StorageError("raw_checksum")
        self._exec(
            """
            INSERT INTO raw_revisions (item_id, analysis_hash, path, sha256, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (item_id, analysis_hash) DO NOTHING
            """,
            (item.id, item.analysis_hash, relative, digest, now),
        )
        row = self._exec(
            "SELECT path, sha256 FROM raw_revisions WHERE item_id=? AND analysis_hash=?",
            (item.id, item.analysis_hash),
        ).fetchone()
        if row is None or row["path"] != relative or row["sha256"] != digest:
            raise IdentityCollision()

    def _drop_classification(self, item_id: str, note_run: str | None) -> None:
        row = self._exec(
            "SELECT topic FROM classifications WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            return
        self._exec("DELETE FROM claims WHERE item_id=?", (item_id,))
        self._exec("DELETE FROM classifications WHERE item_id=?", (item_id,))
        self._note_topic_on(note_run, row["topic"])

    def _note_topic_on(self, run_id: str | None, topic: str) -> None:
        if run_id is None:
            pending = self._stash_topics()
            pending.add(topic)
            self._put_meta("changed_topics_pending", canonical_json(sorted(pending)))
            return
        run = self._require_run(run_id)
        summary = self._load_summary(run["summary_json"])
        topics = set(summary["changed_topics"])
        topics.add(topic)
        summary["changed_topics"] = sorted(topics)
        self._exec(
            "UPDATE runs SET summary_json=? WHERE id=?",
            (canonical_json(summary), run_id),
        )

    def _insert_item(self, item: Item) -> None:
        self._exec(
            """
            INSERT INTO items (
                id, source_id, guid, url, canonical_url, title, published_at, fetched_at,
                content_hash, analysis_hash, modality, body_raw, body_plain, duplicate_of
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.source_id,
                item.guid,
                item.url,
                item.canonical_url,
                item.title,
                item.published_at,
                item.fetched_at,
                item.content_hash,
                item.analysis_hash,
                item.modality,
                item.body_raw,
                item.body_plain,
                item.duplicate_of,
            ),
        )

    def _update_item(self, item: Item) -> None:
        self._exec(
            """
            UPDATE items
            SET url=?, canonical_url=?, title=?, published_at=?, fetched_at=?,
                content_hash=?, analysis_hash=?, modality=?, body_raw=?, body_plain=?,
                duplicate_of=?
            WHERE id=?
            """,
            (
                item.url,
                item.canonical_url,
                item.title,
                item.published_at,
                item.fetched_at,
                item.content_hash,
                item.analysis_hash,
                item.modality,
                item.body_raw,
                item.body_plain,
                item.duplicate_of,
                item.id,
            ),
        )

    def _insert_asset(self, asset: Asset) -> None:
        self._exec(
            """
            INSERT INTO assets (
                item_id, transcript_status, channel, audio_url, transcript_url, transcript_type,
                transcript_etag, transcript_last_modified, transcript_last_attempt_at,
                like_count, comment_count, view_count, top_comments_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _asset_values(asset),
        )

    def _update_asset(self, asset: Asset) -> None:
        values = _asset_values(asset)
        self._exec(
            """
            UPDATE assets
            SET transcript_status=?, channel=?, audio_url=?, transcript_url=?, transcript_type=?,
                transcript_etag=?, transcript_last_modified=?, transcript_last_attempt_at=?,
                like_count=?, comment_count=?, view_count=?, top_comments_json=?
            WHERE item_id=?
            """,
            values[1:] + (values[0],),
        )

    def _insert_classification(self, classification: Classification) -> None:
        self._exec(
            """
            INSERT INTO classifications (
                item_id, run_id, topic, bucket, reason, analysis_hash, policy_hash,
                classified_at, published_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                classification.item_id,
                classification.run_id,
                classification.topic,
                classification.bucket,
                classification.reason,
                classification.analysis_hash,
                classification.policy_hash,
                classification.classified_at,
            ),
        )

    def _insert_claim(self, claim: Claim) -> None:
        self._exec(
            """
            INSERT INTO claims (
                id, item_id, source_id, analysis_hash, topic, claim_key, sentence, stance,
                evidence_kind, evidence_quote, comment_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim.id,
                claim.item_id,
                claim.source_id,
                claim.analysis_hash,
                claim.topic,
                claim.claim_key,
                claim.sentence,
                claim.stance,
                claim.evidence_kind,
                claim.evidence_quote,
                claim.comment_index,
            ),
        )

    def _insert_llm(self, event: LlmEvent) -> None:
        usage = _usage_values(event.usage)
        self._exec(
            """
            INSERT INTO llm_calls (
                call_id, run_id, purpose, at, status, billing_path, error_code,
                input_tokens, output_tokens, reasoning_tokens,
                cache_read_input_tokens, cache_creation_input_tokens, total_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.call_id,
                event.run_id,
                event.purpose,
                event.at,
                event.status,
                event.billing_path,
                event.error_code,
                *usage,
            ),
        )

    def _select_items(self, where: str, params: tuple[object, ...] = ()) -> tuple[Item, ...]:
        rows = self._exec(
            f"""
            SELECT i.*
            FROM items i
            JOIN sources s ON s.id = i.source_id
            JOIN assets a ON a.item_id = i.id
            WHERE {where}
            ORDER BY i.source_id, i.guid
            """,
            params,
        )
        return tuple(self._item_from_row(row) for row in rows)

    def _touch_validators(self, source_id: str, now: str, http: HttpResponse | None) -> None:
        if http is None:
            self._exec(
                "UPDATE sources SET last_fetched_at=? WHERE id=?",
                (now, source_id),
            )
            return
        self._exec(
            """
            UPDATE sources
            SET etag=?, last_modified=?, last_fetched_at=?, last_http_status=?
            WHERE id=?
            """,
            (
                http.headers.get("etag"),
                http.headers.get("last-modified"),
                now,
                http.status,
                source_id,
            ),
        )

    def _create_schema(self) -> None:
        for statement in _SCHEMA:
            self._exec(statement)

    def _ensure_llm_error_detail(self) -> None:
        rows = self._exec("PRAGMA table_info(llm_calls)").fetchall()
        if any(row["name"] == "error_detail" for row in rows):
            return
        self._exec("ALTER TABLE llm_calls ADD COLUMN error_detail TEXT")

    def _user_tables(self) -> list[str]:
        rows = self._exec(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        return [row["name"] for row in rows]

    def _read_version(self) -> str | None:
        if "meta" not in self._user_tables():
            return None
        try:
            row = self._exec(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None or not isinstance(row["value"], str):
            return None
        return row["value"]

    def _foreign_keys_on(self) -> bool:
        row = self._exec("PRAGMA foreign_keys").fetchone()
        return row is not None and int(row[0]) == 1

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("closed")
        return self._conn

    def _require_schema(self) -> None:
        self._require_open()
        if not self._user_tables():
            raise StorageError("not_migrated")
        if self._read_version() != SCHEMA_VERSION:
            raise ConfigError("unsupported_schema")

    def _exec(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self._require_open().execute(sql, params)

    def _meta_value(self, key: str) -> str | None:
        row = self._exec("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        value = row["value"]
        if not isinstance(value, str):
            raise StorageError("corrupt")
        return value

    def _put_meta(self, key: str, value: str) -> None:
        self._exec(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _stash_topics(self) -> set[str]:
        raw = self._meta_value("changed_topics_pending")
        if raw is None:
            return set()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise StorageError("corrupt") from None
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise StorageError("corrupt")
        return set(value)

    def _source_row(self, source_id: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def _run_row(self, run_id: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def _require_run(self, run_id: str) -> sqlite3.Row:
        row = self._run_row(run_id)
        if row is None:
            raise StorageError("unknown_run")
        return row

    def _load_item(self, item_id: str) -> Item | None:
        row = self._exec("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            return None
        return self._item_from_row(row)

    def _require_loaded_item(self, item_id: str) -> Item:
        item = self._load_item(item_id)
        if item is None:
            raise StorageError("unknown_item")
        return item

    def _require_asset(self, item_id: str) -> Asset:
        row = self._exec("SELECT * FROM assets WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise StorageError("corrupt")
        return self._asset_from_row(row)

    def _require_run_item(self, run_id: str, item_id: str, analysis_hash: str) -> None:
        row = self._exec(
            """
            SELECT state FROM run_items
            WHERE run_id=? AND item_id=? AND analysis_hash=?
            """,
            (run_id, item_id, analysis_hash),
        ).fetchone()
        if row is None:
            raise SchemaError("item_not_selected")

    def _is_primary(self, item_id: str) -> bool:
        row = self._exec("SELECT duplicate_of FROM items WHERE id=?", (item_id,)).fetchone()
        return row is not None and row["duplicate_of"] is None

    def _primary_id(self, canonical: str, exclude: str) -> str | None:
        row = self._exec(
            """
            SELECT id FROM items
            WHERE canonical_url=? AND duplicate_of IS NULL AND id<>?
            """,
            (canonical, exclude),
        ).fetchone()
        if row is None:
            return None
        return row["id"]

    def _member_ids(self, canonical: str, exclude: str) -> list[str]:
        rows = self._exec(
            "SELECT id FROM items WHERE canonical_url=? AND id<>? ORDER BY id",
            (canonical, exclude),
        )
        return [row["id"] for row in rows]

    def _item_from_row(self, row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            source_id=row["source_id"],
            guid=row["guid"],
            url=row["url"],
            canonical_url=row["canonical_url"],
            title=row["title"],
            published_at=row["published_at"],
            fetched_at=row["fetched_at"],
            content_hash=row["content_hash"],
            analysis_hash=row["analysis_hash"],
            modality=row["modality"],
            body_raw=row["body_raw"],
            body_plain=row["body_plain"],
            duplicate_of=row["duplicate_of"],
        )

    def _asset_from_row(self, row: sqlite3.Row) -> Asset:
        try:
            comments = json.loads(row["top_comments_json"])
        except json.JSONDecodeError:
            raise StorageError("corrupt") from None
        if not isinstance(comments, list) or any(not isinstance(item, str) for item in comments):
            raise StorageError("corrupt")
        return Asset(
            item_id=row["item_id"],
            transcript_status=row["transcript_status"],
            channel=row["channel"],
            audio_url=row["audio_url"],
            transcript_url=row["transcript_url"],
            transcript_type=row["transcript_type"],
            transcript_etag=row["transcript_etag"],
            transcript_last_modified=row["transcript_last_modified"],
            transcript_last_attempt_at=row["transcript_last_attempt_at"],
            like_count=row["like_count"],
            comment_count=row["comment_count"],
            view_count=row["view_count"],
            top_comments=tuple(comments),
        )

    def _claim_from_row(self, row: sqlite3.Row) -> Claim:
        return Claim(
            id=row["id"],
            item_id=row["item_id"],
            source_id=row["source_id"],
            analysis_hash=row["analysis_hash"],
            topic=row["topic"],
            claim_key=row["claim_key"],
            sentence=row["sentence"],
            stance=row["stance"],
            evidence_kind=row["evidence_kind"],
            evidence_quote=row["evidence_quote"],
            comment_index=row["comment_index"],
        )

    def _load_summary(self, text: str) -> dict[str, object]:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise StorageError("corrupt") from None
        if not isinstance(value, dict):
            raise StorageError("corrupt")
        topics = value.get("changed_topics", [])
        if not isinstance(topics, list) or any(not isinstance(topic, str) for topic in topics):
            raise StorageError("corrupt")
        flag = value.get("log_pending", False)
        if not isinstance(flag, bool):
            raise StorageError("corrupt")
        loaded = dict(value)
        loaded["changed_topics"] = list(topics)
        loaded["log_pending"] = flag
        return loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._require_open()
        if self._tx:
            raise StorageError("io")
        self._tx = True
        try:
            self._exec("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            self._tx = False
            raise StorageError("io") from None
        try:
            yield
            self._require_open().commit()
        except sqlite3.IntegrityError:
            self._rollback()
            raise StorageError("constraint") from None
        except sqlite3.OperationalError:
            self._rollback()
            raise StorageError("io") from None
        except Exception:
            self._rollback()
            raise
        finally:
            self._tx = False

    def _rollback(self) -> None:
        connection = self._conn
        if connection is None:
            return
        try:
            connection.rollback()
        except sqlite3.Error:
            pass


def _initial_since(now: str, backfill_days: int) -> str:
    return format_timestamp(parse_timestamp(now) - timedelta(days=backfill_days))


def _is_old(published_at: str | None, initial_since: str) -> bool:
    return published_at is not None and published_at < initial_since


def _is_held(published_at: str | None, now: str, transcript_status: str) -> bool:
    if transcript_status == "pending_transcript":
        return False
    return published_at is None or published_at > now


def _recompute(record: IngestRecord, *, fetched_at: str, duplicate_of: str | None) -> IngestRecord:
    item = record.item
    built = build_item(
        source_id=item.source_id,
        guid=item.guid,
        url=item.url,
        title=item.title,
        fetched_at=fetched_at,
        modality=item.modality,
        body_raw=item.body_raw,
        body_plain=item.body_plain,
        published_at=item.published_at,
        duplicate_of=duplicate_of,
    )
    asset = replace(record.asset, item_id=built.id)
    return make_ingest_record(built, asset)


def _merge_feed(
    stored_item: Item,
    stored_asset: Asset,
    incoming: IngestRecord,
    now: str,
) -> tuple[Item, Asset]:
    """Keep a downloaded transcript when the feed only repeats its URL."""
    inc_item = incoming.item
    inc_asset = incoming.asset
    same_url = stored_asset.transcript_url == inc_asset.transcript_url
    keep_ready = (
        stored_asset.transcript_status == "ready"
        and inc_asset.transcript_status != "ready"
        and same_url
    )
    clear_ready = (
        stored_asset.transcript_status == "ready"
        and inc_asset.transcript_status != "ready"
        and not same_url
    )
    if keep_ready:
        body_raw = stored_item.body_raw
        body_plain = stored_item.body_plain
        status = stored_asset.transcript_status
        transcript_url = stored_asset.transcript_url
        transcript_type = stored_asset.transcript_type
        etag = stored_asset.transcript_etag
        modified = stored_asset.transcript_last_modified
    elif clear_ready:
        body_raw = inc_item.body_raw
        body_plain = inc_item.body_plain
        status = "pending_transcript"
        transcript_url = inc_asset.transcript_url
        transcript_type = None
        etag = None
        modified = None
    else:
        body_raw = inc_item.body_raw
        body_plain = inc_item.body_plain
        status = inc_asset.transcript_status
        transcript_url = inc_asset.transcript_url
        transcript_type = inc_asset.transcript_type
        if same_url:
            etag = inc_asset.transcript_etag
            if etag is None:
                etag = stored_asset.transcript_etag
            modified = inc_asset.transcript_last_modified
            if modified is None:
                modified = stored_asset.transcript_last_modified
        else:
            etag = inc_asset.transcript_etag
            modified = inc_asset.transcript_last_modified
    built = build_item(
        source_id=inc_item.source_id,
        guid=inc_item.guid,
        url=inc_item.url,
        title=inc_item.title,
        fetched_at=now,
        modality=inc_item.modality,
        body_raw=body_raw,
        body_plain=body_plain,
        published_at=inc_item.published_at,
        duplicate_of=stored_item.duplicate_of,
    )
    asset = Asset(
        item_id=built.id,
        transcript_status=status,
        channel=inc_asset.channel,
        audio_url=inc_asset.audio_url,
        transcript_url=transcript_url,
        transcript_type=transcript_type,
        transcript_etag=etag,
        transcript_last_modified=modified,
        transcript_last_attempt_at=stored_asset.transcript_last_attempt_at,
        like_count=inc_asset.like_count,
        comment_count=inc_asset.comment_count,
        view_count=inc_asset.view_count,
        top_comments=inc_asset.top_comments,
    )
    return built, asset


def _asset_values(asset: Asset) -> tuple[object, ...]:
    return (
        asset.item_id,
        asset.transcript_status,
        asset.channel,
        asset.audio_url,
        asset.transcript_url,
        asset.transcript_type,
        asset.transcript_etag,
        asset.transcript_last_modified,
        asset.transcript_last_attempt_at,
        asset.like_count,
        asset.comment_count,
        asset.view_count,
        canonical_json(list(asset.top_comments)),
    )


def _usage_values(usage: Usage | None) -> tuple[object, ...]:
    if usage is None:
        return (None, None, None, None, None, None)
    return (
        usage.input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
        usage.total_cost_usd,
    )


def _same_llm(row: sqlite3.Row, event: LlmEvent) -> bool:
    return (
        row["run_id"] == event.run_id
        and row["purpose"] == event.purpose
        and row["status"] == event.status
        and row["at"] == event.at
        and row["billing_path"] == event.billing_path
        and row["error_code"] == event.error_code
        and (
            row["input_tokens"],
            row["output_tokens"],
            row["reasoning_tokens"],
            row["cache_read_input_tokens"],
            row["cache_creation_input_tokens"],
            row["total_cost_usd"],
        )
        == _usage_values(event.usage)
    )


def _llm_from_row(row: sqlite3.Row) -> LlmEvent:
    usage_fields = _usage_values(None)
    stored = (
        row["input_tokens"],
        row["output_tokens"],
        row["reasoning_tokens"],
        row["cache_read_input_tokens"],
        row["cache_creation_input_tokens"],
        row["total_cost_usd"],
    )
    usage = None if stored == usage_fields else Usage(
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        cache_read_input_tokens=row["cache_read_input_tokens"],
        cache_creation_input_tokens=row["cache_creation_input_tokens"],
        total_cost_usd=row["total_cost_usd"],
    )
    return LlmEvent(
        call_id=row["call_id"],
        run_id=row["run_id"],
        purpose=row["purpose"],
        at=row["at"],
        status=row["status"],
        billing_path=row["billing_path"],
        error_code=row["error_code"],
        usage=usage,
    )


def _fetch_from_row(row: sqlite3.Row) -> FetchEvent:
    return FetchEvent(
        source_id=row["source_id"],
        host=row["host"],
        url_redacted=row["url_redacted"],
        url_hash=row["url_hash"],
        status=row["status"],
        at=row["at"],
        error_code=row["error_code"],
        retry_after_s=row["retry_after_s"],
    )


def _event_sort_key(event: LlmEvent | FetchEvent) -> tuple[object, ...]:
    if isinstance(event, FetchEvent):
        return (event.at, 0, event.url_hash, event.status or 0)
    return (event.at, 1, event.call_id, 0)


def _load_publication(text: str | None) -> dict[str, object]:
    if not isinstance(text, str):
        raise StorageError("publication_mismatch")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise StorageError("corrupt") from None
    if not isinstance(value, dict):
        raise StorageError("corrupt")
    return value


def _publication_from_payload(run_id: str, payload: Mapping[str, object]) -> Publication:
    refs = payload.get("classified_refs")
    html_path = payload.get("html_path")
    manifest_path = payload.get("manifest_path")
    if not isinstance(refs, list) or not isinstance(html_path, str) or not isinstance(manifest_path, str):
        raise StorageError("corrupt")
    try:
        parsed = tuple(tuple(ref) for ref in refs)
        return Publication(
            run_id=run_id,
            html_path=html_path,
            manifest_path=manifest_path,
            classified_refs=parsed,
        )
    except (SchemaError, TypeError):
        raise StorageError("corrupt") from None


def _same_publication(payload: Mapping[str, object], publication: Publication) -> bool:
    refs = payload.get("classified_refs")
    if not isinstance(refs, list):
        return False
    try:
        parsed = tuple(tuple(ref) for ref in refs)
    except TypeError:
        return False
    return (
        payload.get("html_path") == publication.html_path
        and payload.get("manifest_path") == publication.manifest_path
        and parsed == publication.classified_refs
    )


def _file_list(value: object) -> list[dict[str, str]]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SchemaError("invalid_record")
    files: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise SchemaError("invalid_record")
        path = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise SchemaError("invalid_path")
        parts = path.split("/")
        if (
            path.startswith("/")
            or "\\" in path
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise SchemaError("invalid_path")
        if _HEX64.fullmatch(digest) is None:
            raise SchemaError("invalid_hash")
        files.append({"path": path, "sha256": digest})
    return files


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    try:
        loaded = json.loads(canonical_json(dict(value)))
    except SchemaError:
        raise SchemaError("invalid_summary") from None
    if not isinstance(loaded, dict):
        raise SchemaError("invalid_summary")
    return loaded


def _content_type(value: str | None, fallback: str | None) -> str | None:
    if value is None:
        return fallback
    token = value.split(";", 1)[0].strip()
    if _MIME.fullmatch(token) is None:
        return fallback
    return token


def _require_path(value: Path) -> Path:
    if not isinstance(value, Path):
        raise StorageError("invalid_path")
    return value


def _require_id(value: str, code: str = "invalid_run_id") -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise SchemaError(code)
    return value


def _typed_sequence(value: object, kind: type, code: str) -> tuple:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SchemaError(code)
    items = tuple(value)
    if any(not isinstance(item, kind) for item in items):
        raise SchemaError(code)
    return items


def _id_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SchemaError("invalid_hash")
    items = tuple(value)
    if any(not isinstance(item, str) for item in items):
        raise SchemaError("invalid_hash")
    return items


def _read_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise StorageError("raw_io") from None
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
    finally:
        os.close(fd)
