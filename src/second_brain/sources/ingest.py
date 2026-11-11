"""Persist feed batches and retry pending transcripts.

Every insert goes through Store. This module does not write SQL or raw
files itself. Transcript bytes come from HttpClient or from the injected
fixture reader. Audio enclosures are not requested.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from second_brain.errors import ConfigError, FetchError, SchemaError, UnsafeUrl
from second_brain.http import HttpClient
from second_brain.models import (
    HTTP_MAX_BYTES,
    EntryIssue,
    FeedBatch,
    FetchResult,
    HttpResponse,
    Source,
    require_canonical_timestamp,
)
from second_brain.parse.podcast import parse_podcast
from second_brain.ports import FixtureReader
from second_brain.store import Store
from second_brain.urlguard import parse_public_url

_ACCEPTED = {"text/plain": "plain", "text/vtt": "vtt", "application/x-subrip": "srt"}
_EXT_TYPE = {".txt": "text/plain", ".vtt": "text/vtt", ".srt": "application/x-subrip"}
_BLOCKED = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/xhtml+xml",
        "text/html",
    }
)
_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")
_FIXTURE_PART = re.compile(r"[A-Za-z0-9_.-]+")


def persist_feed(
    store: Store,
    source: Source,
    batch: FeedBatch,
    *,
    now: str,
    raw_dir: Path,
    response: HttpResponse | None,
) -> FetchResult:
    """Commit one parsed batch. A 304 response belongs to accept_not_modified."""
    if not isinstance(store, Store):
        raise SchemaError("invalid_record")
    return store.ingest_feed(source, batch, now=now, raw_dir=raw_dir, response=response)


def hydrate_transcripts(
    store: Store,
    *,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
    sources: tuple[Source, ...],
    now: str,
    raw_dir: Path,
    limit: int,
) -> tuple[int, tuple[EntryIssue, ...]]:
    """Download pending transcript URLs, oldest item id first, up to limit.

    The returned count is how many items became ready. Failures stay pending.
    A file URI is read only for a file source. Network items cannot point the
    reader at the fixture tree. http is None means no network transcript fetch.
    """
    moment = require_canonical_timestamp(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise SchemaError("invalid_budget")
    if http is not None and not isinstance(http, HttpClient):
        raise SchemaError("invalid_record")
    by_id = _sources(sources)
    ready = 0
    issues: list[EntryIssue] = []
    attempts = 0
    pending = store.transcript_candidates(now=moment)
    for record in sorted(pending, key=lambda item: item.item.id):
        transcript_url = record.asset.transcript_url
        if transcript_url is None:
            continue
        if attempts >= limit:
            issues.append(_issue(record.item.source_id, record.item.guid, "transcript_fetch_limit"))
            continue
        attempts += 1
        source = by_id.get(record.item.source_id)
        if source is None:
            store.record_transcript_attempt(record.item.id, now=moment)
            issues.append(_issue(record.item.source_id, record.item.guid, "unknown_source"))
            continue
        try:
            text, response, transcript_format = _load(
                source,
                record.asset.transcript_url,
                record.asset.transcript_type,
                record.asset.transcript_etag,
                record.asset.transcript_last_modified,
                http=http,
                fixture_reader=fixture_reader,
            )
            parsed = parse_podcast(
                record.item.title,
                text,
                record.asset.audio_url,
                transcript_url,
                transcript_format=transcript_format,
            )
            if parsed.transcript_status != "ready":
                raise SchemaError("empty_transcript")
            store.update_transcript(
                record.item.id,
                expected_hash=record.item.analysis_hash,
                text=parsed.body_plain,
                response=response,
                now=moment,
                raw_dir=raw_dir,
            )
        except (FetchError, UnsafeUrl, SchemaError) as exc:
            store.record_transcript_attempt(record.item.id, now=moment)
            issues.append(_issue(record.item.source_id, record.item.guid, exc.code))
            continue
        ready += 1
    return ready, tuple(issues)


def _sources(sources: Sequence[Source]) -> dict[str, Source]:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise SchemaError("invalid_record")
    found: dict[str, Source] = {}
    for source in sources:
        if not isinstance(source, Source):
            raise SchemaError("invalid_record")
        if source.id in found:
            raise ConfigError("duplicate_source")
        found[source.id] = source
    return found


def _load(
    source: Source,
    url: str,
    declared: str | None,
    etag: str | None,
    modified: str | None,
    *,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
) -> tuple[str, HttpResponse, str]:
    if url.startswith("file:"):
        if source.kind != "file":
            raise FetchError("file_transcript_forbidden")
        if not _fixture_uri(url):
            raise SchemaError("invalid_fixture_url")
        payload = fixture_reader(url)
        if not isinstance(payload, bytes):
            raise FetchError("invalid_body")
        if len(payload) > HTTP_MAX_BYTES:
            raise FetchError("body_too_large")
        media = _chosen_type(declared, url)
        text = _decode(payload)
        response = HttpResponse(
            status=200,
            body=payload,
            headers={"content-type": media},
            url=url,
        )
        return text, response, _ACCEPTED[media]
    if http is None:
        raise FetchError("offline_transcript")
    # Fetch only a declared transcript type or a .txt/.vtt/.srt path.
    # Anything else fails here, before HttpClient.
    if not _candidate_declared(declared, url):
        raise FetchError("bad_content_type")
    response = http.fetch(
        url,
        source_id=source.id,
        etag=etag,
        last_modified=modified,
        purpose="transcript",
        allowed_hosts=_allowed_hosts(source),
    )
    if response.status != 200:
        raise FetchError("transcript_not_modified", response.status)
    media = _media_type(response.headers.get("content-type"))
    if media not in _ACCEPTED:
        if media is None and _extension(url) in _EXT_TYPE:
            media = _EXT_TYPE[_extension(url)]
        else:
            raise FetchError("bad_content_type", response.status)
    return _decode(response.body), response, _ACCEPTED[media]


def _allowed_hosts(source: Source) -> frozenset[str]:
    hosts = set(source.transcript_hosts)
    if source.kind != "file":
        host = _feed_host(source.url)
        if host is not None:
            hosts.add(host)
    return frozenset(hosts)


def _feed_host(url: str) -> str | None:
    """Return the guard's hostname, so the allow list matches HttpClient.

    ``urlsplit().hostname`` drops the brackets around an IPv6 literal.
    ``parse_public_url`` keeps the same bracketed form the client compares.
    A feed URL that is not a public http(s) target adds nothing; listed
    transcript hosts still apply, and the client still refuses the fetch.
    """

    try:
        return parse_public_url(url).hostname
    except UnsafeUrl:
        return None


def _fixture_uri(url: str) -> bool:
    prefix = "file:tests/fixtures/"
    if not url.startswith(prefix):
        return False
    parts = url[len(prefix) :].split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    return all(_FIXTURE_PART.fullmatch(part) for part in parts)


def _candidate_declared(declared: str | None, url: str) -> bool:
    if _media_type(declared) in _ACCEPTED:
        return True
    return _extension(url) in _EXT_TYPE


def _chosen_type(declared: str | None, url: str) -> str:
    media = _media_type(declared)
    if _blocked(media):
        raise FetchError("bad_content_type")
    if media in _ACCEPTED:
        return media
    if media is None and _extension(url) in _EXT_TYPE:
        return _EXT_TYPE[_extension(url)]
    raise FetchError("bad_content_type")


def _blocked(media: str | None) -> bool:
    if media is None:
        return False
    if media.startswith(("audio/", "video/", "image/")):
        return True
    return media in _BLOCKED


def _decode(payload: bytes) -> str:
    if b"\x00" in payload:
        raise FetchError("bad_content_type")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeError:
        raise FetchError("bad_content_type") from None


def _media_type(value: str | None) -> str | None:
    if value is None:
        return None
    base = value.split(";", 1)[0].strip().lower()
    if base == "":
        return None
    return base


def _extension(url: str) -> str:
    path = urlsplit(url).path
    slash = path.rfind("/")
    dot = path.rfind(".")
    if dot <= slash:
        return ""
    found = path[dot:].lower()
    if _EXTENSION.fullmatch(found) is None:
        return ""
    return found


def _issue(source_id: str, guid: str, code: str) -> EntryIssue:
    digest = hashlib.sha256(f"{source_id}\n{guid}\n{code}".encode("utf-8")).hexdigest()
    return EntryIssue(entry_key_hash=digest, code=code)
