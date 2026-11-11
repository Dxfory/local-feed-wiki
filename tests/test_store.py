"""Store API and immutable raw archive."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from second_brain.errors import ConfigError, IdentityCollision, SchemaError, StorageError
from second_brain.models import (
    SCHEMA_VERSION,
    Asset,
    BatchOutcome,
    Claim,
    Classification,
    EntryIssue,
    FeedBatch,
    FetchEvent,
    HttpResponse,
    IngestRecord,
    Item,
    KeyPolicy,
    LlmEvent,
    Publication,
    RateState,
    Source,
    Usage,
    build_item,
    format_timestamp,
    hash_url,
    make_claim,
    make_ingest_record,
    parse_timestamp,
    policy_hash,
)
from second_brain.storage.raw import RawArchive
from second_brain.store import Store

NOW = "2030-01-15T00:00:00.000000Z"
DAY = "2030-01-10T00:00:00.000000Z"
OLD = "2029-12-01T00:00:00.000000Z"
FUTURE = "2030-02-01T00:00:00.000000Z"
LATER = "2030-01-16T00:00:00.000000Z"
FINISH = "2030-01-17T00:00:00.000000Z"
POLICY = policy_hash("prefs", KeyPolicy(), {"type": "object"})
SHARED_URL = "https://example.com/post"
MOVED_URL = "https://example.com/moved"


def make_source(source_id: str, *, backfill_days: int = 14) -> Source:
    return Source(
        id=source_id,
        kind="rss",
        url=f"https://feeds.example.com/{source_id}.xml",
        title=f"Source {source_id}",
        backfill_days=backfill_days,
    )


def make_record(
    source_id: str,
    guid: str,
    *,
    url: str | None = None,
    title: str = "Title",
    body: str = "Body",
    published_at: str | None = DAY,
    modality: str = "text",
    transcript_status: str = "not_applicable",
    channel: str = "",
    comments: tuple[str, ...] = (),
    view_count: int | None = None,
    like_count: int | None = None,
    comment_count: int | None = None,
    transcript_url: str | None = None,
    transcript_type: str | None = None,
    fetched_at: str = NOW,
) -> IngestRecord:
    item = build_item(
        source_id=source_id,
        guid=guid,
        url=url or f"https://example.com/{source_id}/{guid}",
        title=title,
        fetched_at=fetched_at,
        modality=modality,
        body_raw=body,
        body_plain=body,
        published_at=published_at,
    )
    asset = Asset(
        item_id=item.id,
        transcript_status=transcript_status,
        channel=channel,
        transcript_url=transcript_url,
        transcript_type=transcript_type,
        like_count=like_count,
        comment_count=comment_count,
        view_count=view_count,
        top_comments=comments,
    )
    return make_ingest_record(item, asset)


def response(status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> HttpResponse:
    return HttpResponse(
        status=status,
        body=body,
        headers=headers or {},
        url="https://feeds.example.com/alpha.xml",
    )


def classification(item: Item, run_id: str, *, topic: str = "chips") -> Classification:
    return Classification(
        item_id=item.id,
        run_id=run_id,
        topic=topic,
        bucket="must",
        reason="Worth reading.",
        analysis_hash=item.analysis_hash,
        policy_hash=POLICY,
        classified_at=NOW,
    )


def claim_for(item: Item, *, topic: str = "chips", quote: str = "Body") -> Claim:
    return make_claim(
        item_id=item.id,
        source_id=item.source_id,
        analysis_hash=item.analysis_hash,
        topic=topic,
        claim_key="author|says|something|2026",
        sentence="The author says something.",
        stance="for",
        evidence_kind="body",
        evidence_quote=quote,
    )


def outcome(batch_id: str, items: tuple[Item, ...], status: str = "success", error_code: str | None = None) -> BatchOutcome:
    return BatchOutcome(
        batch_id=batch_id,
        item_ids=tuple(item.id for item in items),
        status=status,
        error_code=error_code,
    )


def open_store(tmp_path: Path) -> tuple[Store, Path, Path]:
    database = tmp_path / "second_brain.sqlite"
    raw = tmp_path / "raw"
    raw.mkdir()
    store = Store.connect(str(database))
    store.migrate()
    return store, raw, database


def ingest(store: Store, source: Source, raw: Path, record: IngestRecord, *, now: str = NOW, http: HttpResponse | None = None):
    return store.ingest_feed(
        source,
        FeedBatch(records=(record,)),
        now=now,
        raw_dir=raw,
        response=http,
    )


def raw_files(raw: Path) -> list[str]:
    return sorted(path.relative_to(raw).as_posix() for path in raw.rglob("*.md") if path.is_file())


def graph(store: Store) -> list[sqlite3.Row]:
    return list(
        store._conn.execute(
            "SELECT id, source_id, canonical_url, duplicate_of FROM items ORDER BY id"
        )
    )


def assert_no_cycles(store: Store) -> None:
    rows = graph(store)
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        assert row["duplicate_of"] != row["id"]
        if row["duplicate_of"] is not None:
            head = by_id[row["duplicate_of"]]
            assert head["duplicate_of"] is None
            assert head["canonical_url"] == row["canonical_url"]


def flip(digest: str) -> str:
    return digest[:-1] + ("0" if digest[-1] != "0" else "1")


def test_schema_pragmas_and_idempotence(tmp_path: Path) -> None:
    database = tmp_path / "brain.sqlite"
    store = Store.connect(str(database))
    store.migrate()
    store.migrate()
    assert store.get_meta("schema_version") == SCHEMA_VERSION == "3"
    assert store.get_meta("last_classified_at") is None
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert str(store._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert store.items_for_classify(now=NOW) == ()
    assert store.pending_transcript_items(now=NOW) == ()
    assert store.held_date_items(now=NOW) == ()
    assert store.get_current_classified(now=NOW) == ()
    assert store.get_current_claims(now=NOW) == ()
    assert store.get_unpublished_classified(now=NOW) == ()
    assert store.transcript_candidates(now=NOW) == ()
    assert store.unfinished_publications() == ()
    assert store.pending_log_runs() == ()
    assert store.known_claim_keys() == ()
    store.set_meta("writer", "store")
    assert store.get_meta("writer") == "store"
    with pytest.raises(StorageError) as protected:
        store.set_meta("schema_version", "9")
    assert protected.value.code == "protected_meta"
    assert store.get_meta("schema_version") == "3"
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO assets(item_id, transcript_status, channel, top_comments_json) VALUES (?, ?, ?, ?)",
            ("ab" * 16, "ready", "", "[]"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO rate_limits(host, consecutive_failures) VALUES (?, ?)",
            ("example.com", -1),
        )
    raw = tmp_path / "raw"
    raw.mkdir()
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    ingest(store, source, raw, make_record("alpha", "kept"))
    store.migrate()
    assert [item.guid for item in store.items_for_classify(now=NOW)] == ["kept"]
    store.close()

    legacy_path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute("CREATE TABLE legacy(id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy(note) VALUES ('keep')")
    legacy.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    legacy.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '2')")
    legacy.commit()
    before = legacy.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    legacy.close()
    old = Store.connect(str(legacy_path))
    with pytest.raises(ConfigError) as rejected:
        old.migrate()
    assert rejected.value.code == "unsupported_schema"
    old.close()
    legacy = sqlite3.connect(legacy_path)
    assert legacy.execute("SELECT note FROM legacy").fetchone()[0] == "keep"
    assert legacy.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    after = legacy.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    assert after == before
    assert legacy.execute("SELECT name FROM sqlite_master WHERE name='items'").fetchone() is None
    legacy.close()


def test_updated_clears_classification(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    first = make_record("alpha", "g1", body="first body")
    assert ingest(store, source, raw, first).inserted == 1
    again = ingest(store, source, raw, first)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 1)
    item = store.items_for_classify(now=NOW)[0]
    old_path = raw / item.id / f"{item.analysis_hash}.md"
    old_bytes = old_path.read_bytes()
    old_mtime = old_path.stat().st_mtime_ns
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", (item,))
    store.save_batch(
        outcome("batch-1", (item,)),
        (classification(item, "run-1", topic="alpha"),),
        (claim_for(item, topic="alpha", quote="first body"),),
        run_id="run-1",
    )
    publication = Publication(
        run_id="run-1",
        html_path="digest/run-1.html",
        manifest_path="snapshots/run-1/manifest.json",
        classified_refs=((item.id, item.analysis_hash),),
    )
    store.mark_publishing(publication, status_after_publish="success", summary={})
    store.finish_publication(publication, finished_at=LATER)
    revised = make_record("alpha", "g1", body="revised body")
    store.begin_run("run-2", started_at=LATER)
    updated = ingest(store, source, raw, revised, now=LATER)
    assert (updated.inserted, updated.updated, updated.unchanged) == (0, 1, 0)
    assert store.get_current_classified(now=LATER) == ()
    assert store.get_current_claims(now=LATER) == ()
    assert old_path.read_bytes() == old_bytes
    assert old_path.stat().st_mtime_ns == old_mtime
    queued = store.items_for_classify(now=LATER)
    assert [row.guid for row in queued] == ["g1"]
    assert queued[0].analysis_hash != item.analysis_hash
    assert queued[0].body_plain == "revised body"
    asset = store.assets_for([queued[0].id])[queued[0].id]
    from second_brain.models import analysis_hash

    assert analysis_hash(queued[0], asset) == queued[0].analysis_hash
    new_path = raw / queued[0].id / f"{queued[0].analysis_hash}.md"
    assert new_path.is_file()
    assert new_path.read_bytes() != old_bytes
    assert store.get_changed_topics("run-2") == frozenset({"alpha"})
    revisions = store._conn.execute("SELECT COUNT(*) FROM raw_revisions").fetchone()[0]
    assert revisions == 2
    store.close()


def test_metric_refresh_does_not_reclassify(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    comments = ("a" * 300 + "TAIL", "second", "third", "fourth", "fifth", "sixth")
    first = make_record("alpha", "g1", channel="Newsroom", comments=comments)
    ingest(store, source, raw, first)
    item = store.items_for_classify(now=NOW)[0]
    raw_path = raw / item.id / f"{item.analysis_hash}.md"
    document = raw_path.read_text(encoding="utf-8")
    assert document.startswith("# raw\n")
    payload = json.loads(document.split("\n", 1)[1])
    assert payload["title"] == "Title"
    assert payload["body"] == "Body"
    assert payload["channel"] == "Newsroom"
    assert payload["audience"] == ["a" * 300, "second", "third", "fourth", "fifth"]
    assert payload["transcript_status"] == "not_applicable"
    assert "view_count" not in payload
    assert "TAIL" not in document
    assert "sixth" not in document
    stored = store._conn.execute(
        "SELECT path, sha256 FROM raw_revisions WHERE item_id=?",
        (item.id,),
    ).fetchone()
    assert stored["path"] == f"{item.id}/{item.analysis_hash}.md"
    assert stored["sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", (item,))
    store.save_batch(
        outcome("batch-1", (item,)),
        (classification(item, "run-1"),),
        (claim_for(item),),
        run_id="run-1",
    )
    mtime = raw_path.stat().st_mtime_ns
    counted = make_record(
        "alpha",
        "g1",
        channel="Newsroom",
        comments=comments,
        view_count=424242,
        like_count=7,
        comment_count=11,
    )
    counted_result = ingest(store, source, raw, counted, now=LATER)
    assert (counted_result.inserted, counted_result.updated, counted_result.unchanged) == (0, 0, 1)
    current = store.get_current_classified(now=LATER)
    assert len(current) == 1
    assert current[0].analysis_hash == item.analysis_hash
    assert current[0].comments == ("a" * 300, "second", "third", "fourth", "fifth")
    assert "TAIL" not in "".join(current[0].comments)
    asset = store.assets_for([item.id])[item.id]
    assert asset.view_count == 424242
    assert asset.like_count == 7
    assert asset.top_comments[0].endswith("TAIL")
    assert b"424242" not in raw_path.read_bytes()
    assert raw_path.stat().st_mtime_ns == mtime
    fetched = store._conn.execute("SELECT fetched_at FROM items WHERE id=?", (item.id,)).fetchone()[0]
    assert fetched == NOW
    tail = ("a" * 300 + "ZZZZ", "second", "third", "fourth", "fifth", "sixth-b")
    tail_result = ingest(
        store,
        source,
        raw,
        make_record("alpha", "g1", channel="Newsroom", comments=tail, view_count=424242, like_count=7),
        now=FINISH,
    )
    assert tail_result.unchanged == 1
    assert store.get_current_classified(now=FINISH)[0].analysis_hash == item.analysis_hash
    changed = ("b" + "a" * 299 + "TAIL", "second", "third", "fourth", "fifth", "sixth")
    changed_result = ingest(
        store,
        source,
        raw,
        make_record("alpha", "g1", channel="Newsroom", comments=changed),
        now=FINISH,
    )
    assert changed_result.updated == 1
    assert store.get_current_classified(now=FINISH) == ()
    assert store.get_current_claims(now=FINISH) == ()
    assert raw_path.read_bytes()
    queued = store.items_for_classify(now=FINISH)
    assert len(raw_files(raw)) == 2
    assert queued[0].analysis_hash != item.analysis_hash
    store.close()


def test_canonical_duplicate_and_promotion(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    alpha = make_source("alpha")
    beta = make_source("beta")
    store.sync_sources([alpha, beta], now=NOW, raw_dir=raw)
    first = make_record("alpha", "a", url=SHARED_URL, body="same")
    second = make_record("beta", "b", url=SHARED_URL, body="same")
    assert ingest(store, alpha, raw, first).inserted == 1
    duplicate = ingest(store, beta, raw, second)
    assert (duplicate.inserted, duplicate.updated, duplicate.unchanged, duplicate.skipped_dup) == (1, 0, 0, 1)
    assert len(raw_files(raw)) == 1
    primary = store.items_for_classify(now=NOW)
    assert [item.source_id for item in primary] == ["alpha"]
    alpha_id = primary[0].id
    alpha_mtime = (raw / raw_files(raw)[0]).stat().st_mtime_ns
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", primary)
    store.save_batch(
        outcome("batch-1", primary),
        (classification(primary[0], "run-1"),),
        (claim_for(primary[0], quote="same"),),
        run_id="run-1",
    )
    assert [row.source_id for row in store.get_current_classified(now=NOW)] == ["alpha"]
    store.sync_sources([beta], now=NOW, raw_dir=raw)
    assert_no_cycles(store)
    rows = {row["source_id"]: row for row in graph(store)}
    assert rows["beta"]["duplicate_of"] is None
    assert rows["alpha"]["duplicate_of"] == rows["beta"]["id"]
    assert store.get_current_classified(now=NOW) == ()
    assert store.get_current_claims(now=NOW) == ()
    assert len(raw_files(raw)) == 2
    assert [item.source_id for item in store.items_for_classify(now=NOW)] == ["beta"]
    moved = ingest(store, beta, raw, make_record("beta", "b", url=MOVED_URL, body="same"), now=LATER)
    assert moved.unchanged == 1
    assert_no_cycles(store)
    rows = {row["source_id"]: row for row in graph(store)}
    assert rows["alpha"]["canonical_url"] == SHARED_URL
    assert rows["alpha"]["duplicate_of"] is None
    assert rows["beta"]["canonical_url"] == MOVED_URL
    assert rows["beta"]["duplicate_of"] is None
    store.sync_sources([alpha, beta], now=LATER, raw_dir=raw)
    queued = store.items_for_classify(now=LATER)
    assert alpha_id in {item.id for item in queued}
    assert {item.source_id for item in queued} == {"alpha", "beta"}
    assert (raw / f"{alpha_id}" / f"{primary[0].analysis_hash}.md").stat().st_mtime_ns == alpha_mtime
    assert_no_cycles(store)
    store.close()


def test_save_batch_is_atomic(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    ingest(store, source, raw, make_record("alpha", "g1", body="one"))
    ingest(store, source, raw, make_record("alpha", "g2", body="two"))
    ingest(store, source, raw, make_record("alpha", "g3", body="three"))
    items = store.items_for_classify(now=NOW)
    assert [item.guid for item in items] == ["g1", "g2", "g3"]
    first, second, third = items
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", items)
    bad = Classification(
        item_id=second.id,
        run_id="run-1",
        topic="chips",
        bucket="recommend",
        reason="Wrong hash.",
        analysis_hash=flip(second.analysis_hash),
        policy_hash=POLICY,
        classified_at=NOW,
    )
    with pytest.raises(SchemaError) as caught:
        store.save_batch(
            outcome("batch-bad", (first, second)),
            (classification(first, "run-1"), bad),
            (claim_for(first, quote="one"),),
            run_id="run-1",
        )
    assert caught.value.code == "analysis_hash_mismatch"
    assert store._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    states = [
        row["state"]
        for row in store._conn.execute("SELECT state FROM run_items ORDER BY item_id")
    ]
    assert states == ["selected", "selected", "selected"]
    assert [item.guid for item in store.items_for_classify(now=NOW)] == ["g1", "g2", "g3"]
    store.save_batch(
        outcome("batch-ok", (first, second)),
        (classification(first, "run-1", topic="one"), classification(second, "run-1", topic="two")),
        (claim_for(first, topic="one", quote="one"), claim_for(second, topic="two", quote="two")),
        run_id="run-1",
    )
    store.save_batch(
        outcome("batch-budget", (third,), status="budget_skipped", error_code="budget_exceeded"),
        (),
        (),
        run_id="run-1",
    )
    assert store._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2
    assert store._conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
    saved = {
        row["item_id"]: row["state"]
        for row in store._conn.execute("SELECT item_id, state FROM run_items")
    }
    assert saved[first.id] == "classified"
    assert saved[second.id] == "classified"
    assert saved[third.id] == "deferred"
    assert [item.guid for item in store.items_for_classify(now=NOW)] == ["g3"]
    assert {row.topic for row in store.get_current_classified(now=NOW)} == {"one", "two"}
    store.close()


def test_raw_db_failure_reuses_revision(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    record = make_record("alpha", "g1", body="kept")
    store._conn.execute(
        """
        CREATE TRIGGER fail_raw_revision BEFORE INSERT ON raw_revisions
        BEGIN
          SELECT RAISE(ABORT, 'simulated');
        END
        """
    )
    with pytest.raises(StorageError) as caught:
        ingest(store, source, raw, record)
    assert caught.value.code == "constraint"
    path = raw / record.item.id / f"{record.item.analysis_hash}.md"
    payload = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    inode = path.stat().st_ino
    assert store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM raw_revisions").fetchone()[0] == 0
    store._conn.execute("DROP TRIGGER fail_raw_revision")
    result = ingest(store, source, raw, record)
    assert result.inserted == 1
    assert path.read_bytes() == payload
    assert path.stat().st_mtime_ns == mtime
    assert path.stat().st_ino == inode
    assert store._conn.execute("SELECT sha256 FROM raw_revisions").fetchone()[0] == hashlib.sha256(payload).hexdigest()
    path.write_bytes(payload + b"\nchanged")
    with pytest.raises(IdentityCollision):
        RawArchive(raw).put(record)
    assert path.read_bytes() == payload + b"\nchanged"
    with pytest.raises(IdentityCollision):
        ingest(store, source, raw, record, now=LATER)
    assert store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert path.read_bytes() == payload + b"\nchanged"
    store.close()


def test_old_unclassified_is_not_lost(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    skipped = ingest(store, source, raw, make_record("alpha", "ancient", published_at=OLD))
    assert (skipped.skipped_old, skipped.inserted, skipped.updated, skipped.unchanged) == (1, 0, 0, 0)
    assert store._conn.execute("SELECT COUNT(*) FROM items WHERE guid='ancient'").fetchone()[0] == 0
    held = ingest(store, source, raw, make_record("alpha", "future", published_at=FUTURE))
    assert held.inserted == 1 and held.held_date == 1
    pending = make_record(
        "alpha",
        "pending",
        modality="podcast",
        body="",
        transcript_status="pending_transcript",
        transcript_url="https://cdn.example.com/pending.txt",
    )
    assert ingest(store, source, raw, pending).inserted == 1
    early = make_record("alpha", "early", body="early")
    later = make_record("alpha", "later", body="later")
    ingest(store, source, raw, early, now=NOW)
    ingest(store, source, raw, later, now=NOW)
    early_item = next(item for item in store.items_for_classify(now=NOW) if item.guid == "early")
    later_item = next(item for item in store.items_for_classify(now=NOW) if item.guid == "later")
    assert early_item.fetched_at == NOW
    store.begin_run("run-1", started_at=LATER)
    store.select_run_items("run-1", (later_item,))
    store.save_batch(
        outcome("batch-1", (later_item,)),
        (classification(later_item, "run-1"),),
        (claim_for(later_item, quote="later"),),
        run_id="run-1",
    )
    publication = Publication(
        run_id="run-1",
        html_path="digest/run-1.html",
        manifest_path="snapshots/run-1/manifest.json",
        classified_refs=((later_item.id, later_item.analysis_hash),),
    )
    store.mark_publishing(publication, status_after_publish="success", summary={"files": []})
    store.finish_publication(publication, finished_at=FINISH)
    assert store.get_meta("last_classified_at") == LATER
    queued = store.items_for_classify(now=FINISH)
    assert [item.guid for item in queued] == ["early"]
    assert queued[0].fetched_at < store.get_meta("last_classified_at")
    assert [item.guid for item in store.pending_transcript_items(now=FINISH)] == ["pending"]
    assert [item.guid for item in store.held_date_items(now=FINISH)] == ["future"]
    assert "early" not in {item.guid for item in store.held_date_items(now=FINISH)}
    assert "pending" not in {item.guid for item in store.held_date_items(now=FINISH)}
    partial = Publication(
        run_id="run-2",
        html_path="digest/run-2.html",
        manifest_path="snapshots/run-2/manifest.json",
        classified_refs=(),
    )
    store.begin_run("run-2", started_at=FINISH)
    store.mark_publishing(partial, status_after_publish="partial", summary={})
    store.finish_publication(partial, finished_at="2030-01-18T00:00:00.000000Z")
    assert store.get_meta("last_classified_at") == LATER
    store.close()


def test_unpublished_query_survives_restart(tmp_path: Path) -> None:
    store, raw, database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    ingest(store, source, raw, make_record("alpha", "g1", body="body"))
    item = store.items_for_classify(now=NOW)[0]
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", (item,))
    store.save_batch(
        outcome("batch-1", (item,)),
        (classification(item, "run-1"),),
        (claim_for(item),),
        run_id="run-1",
    )
    publication = Publication(
        run_id="run-1",
        html_path="digest/run-1.html",
        manifest_path="snapshots/run-1/manifest.json",
        classified_refs=((item.id, item.analysis_hash),),
    )
    store.mark_publishing(
        publication,
        status_after_publish="success",
        summary={"files": [{"path": "digest/run-1.html", "sha256": "ab" * 32}], "changed_topics": ["smuggled"]},
    )
    store.close()
    reopened = Store.connect(str(database))
    reopened.migrate()
    unpublished = reopened.get_unpublished_classified(now=NOW)
    assert len(unpublished) == 1
    assert unpublished[0].id == item.id
    assert unpublished[0].comments == ()
    assert len(unpublished[0].claims) == 1
    assert reopened.unfinished_publications() == (publication,)
    assert reopened.get_changed_topics("run-1") == frozenset({"chips"})
    reopened.finish_publication(publication, finished_at=LATER)
    reopened.finish_publication(publication, finished_at=FINISH)
    assert reopened.get_unpublished_classified(now=LATER) == ()
    assert [row.id for row in reopened.get_current_classified(now=LATER)] == [item.id]
    assert reopened.get_meta("last_classified_at") == NOW
    assert reopened.pending_log_runs() == ("run-1",)
    reopened.mark_log_written("run-1")
    assert reopened.pending_log_runs() == ()
    assert reopened.items_for_classify(now=LATER) == ()
    reopened.close()


def test_interrupted_usage_is_unknown(tmp_path: Path) -> None:
    store, raw, database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    ingest(store, source, raw, make_record("alpha", "g1"))
    item = store.items_for_classify(now=NOW)[0]
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", (item,))
    store.save_batch(
        outcome("batch-1", (item,)),
        (classification(item, "run-1"),),
        (claim_for(item),),
        run_id="run-1",
    )
    store.append_llm_event(
        LlmEvent(
            call_id="call-a",
            run_id="run-1",
            purpose="classify",
            at=NOW,
            status="started",
            billing_path="test",
        )
    )
    store.append_llm_event(
        LlmEvent(
            call_id="call-a",
            run_id="run-1",
            purpose="classify",
            at=LATER,
            status="success",
            billing_path="test",
            usage=Usage(input_tokens=3),
        )
    )
    with pytest.raises(StorageError) as charged:
        store.append_llm_event(
            LlmEvent(
                call_id="call-a",
                run_id="run-1",
                purpose="classify",
                at=FINISH,
                status="success",
                billing_path="test",
                usage=Usage(output_tokens=9, total_cost_usd="0"),
            )
        )
    assert charged.value.code == "llm_event_final"
    assert store._conn.execute("SELECT COUNT(*) FROM llm_calls WHERE call_id='call-a'").fetchone()[0] == 1
    store.append_llm_event(
        LlmEvent(
            call_id="call-b",
            run_id="run-1",
            purpose="review",
            at=NOW,
            status="started",
            billing_path="unknown",
        )
    )
    publication = Publication(
        run_id="run-1",
        html_path="digest/run-1.html",
        manifest_path="snapshots/run-1/manifest.json",
        classified_refs=((item.id, item.analysis_hash),),
    )
    store.mark_publishing(publication, status_after_publish="success", summary={})
    other = Store.connect(str(database))
    other.migrate()
    other.begin_run("run-2", started_at=LATER)
    other.close()
    store.recover_interrupted_runs(now=FINISH)
    kept = store._conn.execute(
        "SELECT status, input_tokens, output_tokens, total_cost_usd FROM llm_calls WHERE call_id='call-a'"
    ).fetchone()
    assert kept["status"] == "success"
    assert kept["input_tokens"] == 3
    assert kept["output_tokens"] is None
    assert kept["total_cost_usd"] is None
    interrupted = store._conn.execute(
        "SELECT status, input_tokens, output_tokens, reasoning_tokens, total_cost_usd FROM llm_calls WHERE call_id='call-b'"
    ).fetchone()
    assert interrupted["status"] == "interrupted_unknown"
    assert interrupted["input_tokens"] is None
    assert interrupted["output_tokens"] is None
    assert interrupted["reasoning_tokens"] is None
    assert interrupted["total_cost_usd"] is None
    assert store._conn.execute("SELECT status FROM runs WHERE id='run-1'").fetchone()[0] == "publishing"
    assert store._conn.execute("SELECT status FROM runs WHERE id='run-2'").fetchone()[0] == "failed"
    assert store.unfinished_publications() == (publication,)
    assert [row.id for row in store.get_unpublished_classified(now=FINISH)] == [item.id]
    assert store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
    store.close()


def test_backfill_keeps_known_guid_and_skips_old(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha", backfill_days=14)
    store.sync_sources([source], now=NOW, raw_dir=raw)
    since = format_timestamp(parse_timestamp(NOW) - timedelta(days=14))
    assert store.get_source_state("alpha").initial_since_at == since
    equal = ingest(store, source, raw, make_record("alpha", "edge", published_at=since))
    assert equal.inserted == 1 and equal.skipped_old == 0
    skipped = ingest(store, source, raw, make_record("alpha", "old", published_at=OLD))
    assert skipped.skipped_old == 1 and skipped.inserted == 0
    known = make_record("alpha", "known", body="original", published_at=DAY)
    ingest(store, source, raw, known)
    store.sync_sources([make_source("alpha", backfill_days=1)], now=LATER, raw_dir=raw)
    assert store.get_source_state("alpha").initial_since_at == since
    revised = ingest(
        store,
        source,
        raw,
        make_record("alpha", "known", body="revised", published_at=OLD),
        now=LATER,
    )
    assert revised.updated == 1 and revised.skipped_old == 0
    fresh = ingest(store, source, raw, make_record("alpha", "fresh-old", published_at=OLD), now=LATER)
    assert fresh.skipped_old == 1
    guids = {
        row["guid"]
        for row in store._conn.execute("SELECT guid FROM items")
    }
    assert guids == {"edge", "known"}
    store.close()


def test_accept_not_modified_preserves_validators(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    empty = store.ingest_feed(
        source,
        FeedBatch(records=()),
        now=NOW,
        raw_dir=raw,
        response=response(200, {"ETag": "abc", "Last-Modified": "Mon, 01 Jan 2030 00:00:00 GMT"}),
    )
    assert empty.inserted == 0
    state = store.get_source_state("alpha")
    assert state.etag == "abc"
    assert state.last_modified == "Mon, 01 Jan 2030 00:00:00 GMT"
    assert state.last_http_status == 200
    assert state.last_fetched_at == NOW
    pending = make_record(
        "alpha",
        "ep",
        modality="podcast",
        body="notes",
        transcript_status="pending_transcript",
        transcript_url="https://cdn.example.com/ep.txt",
    )
    ingest(store, source, raw, pending)
    assert [record.item.guid for record in store.transcript_candidates(now=NOW)] == ["ep"]
    store.accept_not_modified(source.id, now=LATER, response=response(304))
    state = store.get_source_state("alpha")
    assert state.etag == "abc"
    assert state.last_modified == "Mon, 01 Jan 2030 00:00:00 GMT"
    assert state.last_http_status == 304
    assert state.last_fetched_at == LATER
    assert [record.item.guid for record in store.transcript_candidates(now=LATER)] == ["ep"]
    store.accept_not_modified(source.id, now=FINISH, response=response(304, {"ETag": "def"}))
    state = store.get_source_state("alpha")
    assert state.etag == "def"
    assert state.last_modified == "Mon, 01 Jan 2030 00:00:00 GMT"
    issue = EntryIssue(entry_key_hash="ab" * 32, code="invalid_entry")
    partial = store.ingest_feed(
        source,
        FeedBatch(records=(make_record("alpha", "new"),), issues=(issue,)),
        now=FINISH,
        raw_dir=raw,
        response=response(200, {"ETag": "nope", "Last-Modified": "Tue, 02 Jan 2030 00:00:00 GMT"}),
    )
    assert partial.invalid == 1 and partial.inserted == 1
    state = store.get_source_state("alpha")
    assert state.etag == "def"
    assert state.last_modified == "Mon, 01 Jan 2030 00:00:00 GMT"
    assert state.last_http_status == 304
    store.close()


def test_ready_transcript_survives_show_notes(tmp_path: Path) -> None:
    store, raw, _database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    transcript_url = "https://cdn.example.com/a.txt"
    ready = make_record(
        "alpha",
        "ep1",
        modality="podcast",
        body="Hello from the transcript.",
        transcript_status="ready",
        transcript_url=transcript_url,
        transcript_type="text/plain",
        channel="Desk",
    )
    ingest(store, source, raw, ready)
    item = store.items_for_classify(now=NOW)[0]
    old_hash = item.analysis_hash
    store.begin_run("run-1", started_at=NOW)
    store.select_run_items("run-1", (item,))
    store.save_batch(
        outcome("batch-1", (item,)),
        (classification(item, "run-1"),),
        (claim_for(item, quote="Hello from the transcript."),),
        run_id="run-1",
    )
    notes = make_record(
        "alpha",
        "ep1",
        modality="podcast",
        body="Show notes only.",
        transcript_status="pending_transcript",
        transcript_url=transcript_url,
        channel="Desk",
        view_count=5,
    )
    kept = ingest(store, source, raw, notes, now=LATER)
    assert kept.unchanged == 1
    asset = store.assets_for([item.id])[item.id]
    assert asset.transcript_status == "ready"
    assert asset.view_count == 5
    assert store._conn.execute("SELECT body_plain FROM items WHERE id=?", (item.id,)).fetchone()[0] == "Hello from the transcript."
    assert store.get_current_classified(now=LATER)[0].analysis_hash == old_hash
    moved = make_record(
        "alpha",
        "ep1",
        modality="podcast",
        body="Show notes only.",
        transcript_status="pending_transcript",
        transcript_url="https://cdn.example.com/b.txt",
        channel="Desk",
    )
    changed = ingest(store, source, raw, moved, now=LATER)
    assert changed.updated == 1
    asset = store.assets_for([item.id])[item.id]
    assert asset.transcript_status == "pending_transcript"
    assert asset.transcript_url == "https://cdn.example.com/b.txt"
    assert store.get_current_classified(now=LATER) == ()
    assert (raw / item.id / f"{old_hash}.md").is_file()
    pending = store.transcript_candidates(now=LATER)[0]
    store.record_transcript_attempt(item.id, now=LATER)
    assert store.assets_for([item.id])[item.id].transcript_last_attempt_at == LATER
    with pytest.raises(SchemaError) as mismatch:
        store.update_transcript(
            item.id,
            expected_hash="0" * 64,
            text="New transcript.",
            response=response(200, {"ETag": "t1"}),
            now=FINISH,
            raw_dir=raw,
        )
    assert mismatch.value.code == "analysis_hash_mismatch"
    with pytest.raises(SchemaError) as empty:
        store.update_transcript(
            item.id,
            expected_hash=pending.item.analysis_hash,
            text="  ",
            response=response(200),
            now=FINISH,
            raw_dir=raw,
        )
    assert empty.value.code == "empty_transcript"
    assert store.assets_for([item.id])[item.id].transcript_status == "pending_transcript"
    store.update_transcript(
        item.id,
        expected_hash=pending.item.analysis_hash,
        text="New transcript.",
        response=response(
            200,
            {
                "ETag": "t-etag",
                "Last-Modified": "Tue, 02 Jan 2030 00:00:00 GMT",
                "Content-Type": "text/plain",
            },
        ),
        now=FINISH,
        raw_dir=raw,
    )
    restored = store.assets_for([item.id])[item.id]
    assert restored.transcript_status == "ready"
    assert restored.transcript_etag == "t-etag"
    assert restored.transcript_type == "text/plain"
    body = store._conn.execute("SELECT body_plain, analysis_hash FROM items WHERE id=?", (item.id,)).fetchone()
    assert body["body_plain"] == "New transcript."
    assert body["analysis_hash"] != pending.item.analysis_hash
    assert (raw / item.id / f"{body['analysis_hash']}.md").is_file()
    assert (raw / item.id / f"{old_hash}.md").is_file()
    store.close()


def test_symlink_raw_is_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    record = make_record("alpha", "g1")
    linked_root = tmp_path / "linked-raw"
    linked_root.symlink_to(raw, target_is_directory=True)
    with pytest.raises(StorageError) as root_link:
        RawArchive(linked_root).put(record)
    assert root_link.value.code == "symlink_escape"
    outside = tmp_path / "outside"
    outside.mkdir()
    (raw / record.item.id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError) as directory_link:
        RawArchive(raw).put(record)
    assert directory_link.value.code == "symlink_escape"
    assert list(outside.iterdir()) == []
    (raw / record.item.id).unlink()
    other = make_record("alpha", "g2")
    item_dir = raw / other.item.id
    item_dir.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_bytes(b"secret")
    (item_dir / f"{other.item.analysis_hash}.md").symlink_to(secret)
    with pytest.raises(StorageError) as file_link:
        RawArchive(raw).put(other)
    assert file_link.value.code == "symlink_escape"
    assert secret.read_bytes() == b"secret"


def test_fetch_event_binds_active_run(tmp_path: Path) -> None:
    store, _raw, database = open_store(tmp_path)
    source = make_source("alpha")
    store.sync_sources([source], now=NOW, raw_dir=tmp_path / "raw")
    assert store.get_rate_state("feeds.example.com") == RateState(host="feeds.example.com")
    store.put_rate_state(
        RateState(host="feeds.example.com", blocked_until=LATER, consecutive_failures=2, last_request_at=NOW)
    )
    assert store.get_rate_state("feeds.example.com").consecutive_failures == 2
    store.begin_run("run-1", started_at=NOW)
    event = FetchEvent(
        source_id="alpha",
        host="feeds.example.com",
        url_redacted="https://feeds.example.com/\u2026",
        url_hash=hash_url("https://feeds.example.com/alpha.xml"),
        status=200,
        at=NOW,
    )
    store.append_fetch_event(event)
    assert store.events_for_run("run-1") == (event,)
    store.close()
    reopened = Store.connect(str(database))
    reopened.migrate()
    later = FetchEvent(
        source_id=None,
        host="feeds.example.com",
        url_redacted="https://feeds.example.com/\u2026",
        url_hash=hash_url("https://feeds.example.com/alpha.xml"),
        status=304,
        at=LATER,
    )
    reopened.append_fetch_event(later)
    assert reopened.events_for_run("run-1") == (event,)
    assert reopened._conn.execute("SELECT run_id FROM fetch_log WHERE status=304").fetchone()[0] is None
    reopened.close()


def test_migrate_adds_llm_error_detail(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '3')")
    conn.execute(
        """
        CREATE TABLE llm_calls (
            call_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            at TEXT NOT NULL,
            status TEXT NOT NULL,
            billing_path TEXT NOT NULL,
            error_code TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    store = Store.connect(str(database))
    store.migrate()
    store.migrate()
    names = [row["name"] for row in store._conn.execute("PRAGMA table_info(llm_calls)")]
    assert "error_detail" in names
    assert store.get_meta("schema_version") == "3"
    store.close()
