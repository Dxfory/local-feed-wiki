"""Parse RSS, Atom, and local JSON into feed batches.

parse_rss and parse_local_records accept bytes only. They do not fetch
feeds, articles, audio, or transcript URLs. Podcast show notes stay out of
body_plain. An audio enclosure is stored as a link and is never opened.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import feedparser

from second_brain.errors import FetchError, FramesNotAllowed, SchemaError
from second_brain.models import (
    HTTP_MAX_BYTES,
    Asset,
    EntryIssue,
    FeedBatch,
    IngestRecord,
    Source,
    build_item,
    canonical_url,
    format_timestamp,
    make_ingest_record,
    parse_timestamp,
    require_canonical_timestamp,
)
from second_brain.parse.podcast import parse_podcast
from second_brain.parse.text import parse_text
from second_brain.parse.video import parse_video

_RSS_KINDS = frozenset({"rss", "podcast_rss"})
_LOCAL_KINDS = frozenset({"podcast_json", "video_json"})
_PODCAST_FIELDS = frozenset(
    {"guid", "url", "title", "published_at", "transcript", "audio_url", "transcript_url"}
)
_VIDEO_FIELDS = frozenset(
    {
        "guid",
        "url",
        "published_at",
        "title",
        "channel",
        "like_count",
        "comment_count",
        "view_count",
        "comments",
        "frame_paths",
    }
)
_AUDIO_EXTENSIONS = frozenset(
    {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".mpga", ".ogg", ".opus", ".wav", ".wma"}
)
_TYPE_RANK = {"text/plain": 0, "text/vtt": 1, "application/x-subrip": 2}
_EXT_TYPE = {".txt": "text/plain", ".vtt": "text/vtt", ".srt": "application/x-subrip"}
_FORMAT = {"text/plain": "plain", "text/vtt": "vtt", "application/x-subrip": "srt"}
_DTD = ("<!doctype", "<!entity", "<!element", "<!attlist", "<!notation")
_IDENTITY_BREAKS = ("\n", "\r", "\x00", "\u2028", "\u2029")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")
_MIME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,127})?"
)


def parse_rss(source: Source, xml_bytes: bytes, *, now: str) -> FeedBatch:
    """Turn one RSS or Atom document into records and per-entry issues.

    A broken document or a DTD raises FetchError so the caller can keep the
    previous validators. One bad entry becomes an EntryIssue and does not
    drop the other entries. Podcast notes are not a transcript.
    """
    fetched_at = require_canonical_timestamp(now)
    kind = _require_kind(source, _RSS_KINDS)
    text = _xml_text(xml_bytes)
    root = _xml_root(text)
    # Bytes, never a URL. feedparser must not open a network connection.
    parsed = feedparser.parse(text.encode("utf-8"), resolve_relative_uris=False, sanitize_html=False)
    elements = _entry_elements(root)
    entries = list(parsed.entries)
    if len(elements) != len(entries):
        raise FetchError("invalid_feed")
    podcast = kind == "podcast_rss"
    channel = _channel(parsed.feed.get("title")) if podcast else ""
    records: list[IngestRecord] = []
    issues: list[EntryIssue] = []
    seen: set[str] = set()
    for index, (element, entry) in enumerate(zip(elements, entries)):
        record, issue = _rss_entry(
            source,
            element,
            entry,
            now=fetched_at,
            podcast=podcast,
            channel=channel,
            index=index,
        )
        if issue is not None:
            issues.append(issue)
            continue
        if record is None:
            continue
        guid = record.item.guid
        if guid in seen:
            issues.append(_issue(source.id, f"duplicate:{index}:{guid}", "duplicate_guid"))
            continue
        seen.add(guid)
        records.append(record)
    return FeedBatch(tuple(records), tuple(issues))


def parse_local_records(source: Source, payload: bytes, *, now: str) -> FeedBatch:
    """Turn podcast_json or video_json bytes into one feed batch.

    Unknown fields reject that entry. A document that is not the closed
    top-level shape raises FetchError and saves nothing.
    """
    fetched_at = require_canonical_timestamp(now)
    kind = _require_kind(source, _LOCAL_KINDS)
    document = _json_document(payload)
    items = document["items"]
    if isinstance(items, (str, bytes)) or not isinstance(items, list):
        raise FetchError("invalid_payload")
    fields = _PODCAST_FIELDS if kind == "podcast_json" else _VIDEO_FIELDS
    records: list[IngestRecord] = []
    issues: list[EntryIssue] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        record, issue = _local_entry(source, item, fields=fields, kind=kind, now=fetched_at, index=index)
        if issue is not None:
            issues.append(issue)
            continue
        if record is None:
            continue
        guid = record.item.guid
        if guid in seen:
            issues.append(_issue(source.id, f"duplicate:{index}:{guid}", "duplicate_guid"))
            continue
        seen.add(guid)
        records.append(record)
    return FeedBatch(tuple(records), tuple(issues))


def _require_kind(source: object, allowed: frozenset[str]) -> str:
    if not isinstance(source, Source):
        raise FetchError("invalid_source")
    kind = source.file_format if source.kind == "file" else source.kind
    if kind not in allowed:
        raise FetchError("invalid_source")
    return kind


def _xml_text(payload: object) -> str:
    if not isinstance(payload, (bytes, bytearray)):
        raise FetchError("invalid_feed")
    raw = bytes(payload)
    if len(raw) > HTTP_MAX_BYTES or b"\x00" in raw:
        raise FetchError("body_too_large" if len(raw) > HTTP_MAX_BYTES else "invalid_feed")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise FetchError("invalid_feed") from None
    folded = text.lower()
    if any(token in folded for token in _DTD):
        raise FetchError("xml_dtd")
    return text


def _xml_root(text: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise FetchError("invalid_feed") from None


def _json_document(payload: object) -> dict[str, object]:
    if not isinstance(payload, (bytes, bytearray)):
        raise FetchError("invalid_payload")
    raw = bytes(payload)
    if len(raw) > HTTP_MAX_BYTES:
        raise FetchError("body_too_large")
    if b"\x00" in raw:
        raise FetchError("invalid_payload")
    try:
        text = raw.decode("utf-8-sig")
        loaded = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (UnicodeError, ValueError):
        raise FetchError("invalid_payload") from None
    if not isinstance(loaded, dict) or set(loaded) != {"items"}:
        raise FetchError("invalid_payload")
    return loaded


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate")
        obj[key] = value
    return obj


def _reject_constant(_name: str) -> object:
    raise ValueError("constant")


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _entry_elements(root: ElementTree.Element) -> list[ElementTree.Element]:
    found: list[ElementTree.Element] = []
    for parent in root.iter():
        if _local(parent.tag) not in {"channel", "feed"}:
            continue
        for child in list(parent):
            if _local(child.tag) in {"item", "entry"}:
                found.append(child)
    return found


def _rss_entry(
    source: Source,
    element: ElementTree.Element,
    entry: object,
    *,
    now: str,
    podcast: bool,
    channel: str,
    index: int,
) -> tuple[IngestRecord | None, EntryIssue | None]:
    marker = f"index:{index}"
    try:
        if not isinstance(entry, dict):
            raise SchemaError("invalid_payload")
        guid = _explicit_guid(entry)
        link = entry.get("link") if isinstance(entry.get("link"), str) else ""
        if guid is None and link == "":
            raise SchemaError("missing_identity")
        url = _item_url(source, link if isinstance(link, str) else "")
        if guid is None:
            guid = url
        _reject_guid(guid)
        marker = guid
        title = entry.get("title") if isinstance(entry.get("title"), str) else ""
        published = _published(entry)
        notes = _html_body(entry)
        audio = _audio_url(source, entry)
        if podcast:
            transcript_url, transcript_type, transcript_text, transcript_format = _transcript(source, element)
            record = _podcast_record(
                source,
                guid=guid,
                url=url,
                title=title,
                published=published,
                now=now,
                channel=channel,
                notes=notes,
                audio=audio,
                transcript_url=transcript_url,
                transcript_type=transcript_type,
                transcript_text=transcript_text,
                transcript_format=transcript_format,
            )
        else:
            plain, _modality = parse_text(title, notes)
            record = _text_record(
                source,
                guid=guid,
                url=url,
                title=title,
                published=published,
                now=now,
                body_raw=notes,
                body_plain=plain,
                audio=audio,
            )
    except SchemaError as exc:
        return None, _issue(source.id, marker, exc.code)
    return record, None


def _local_entry(
    source: Source,
    item: object,
    *,
    fields: frozenset[str],
    kind: str,
    now: str,
    index: int,
) -> tuple[IngestRecord | None, EntryIssue | None]:
    marker = f"index:{index}"
    try:
        if not isinstance(item, dict):
            raise SchemaError("invalid_payload")
        unknown = [key for key in item if not isinstance(key, str) or key not in fields]
        if unknown:
            raise SchemaError("invalid_field")
        url = _public_item_url(item.get("url"))
        guid = _json_guid(item.get("guid"), url)
        marker = guid
        published = _json_time(item["published_at"]) if "published_at" in item else None
        if kind == "podcast_json":
            record = _podcast_json_record(source, item, guid=guid, url=url, published=published, now=now)
        else:
            record = _video_record(source, item, guid=guid, url=url, published=published, now=now)
    except FramesNotAllowed:
        return None, _issue(source.id, marker, "frames_not_allowed")
    except SchemaError as exc:
        return None, _issue(source.id, marker, exc.code)
    return record, None


def _text_record(
    source: Source,
    *,
    guid: str,
    url: str,
    title: str,
    published: str | None,
    now: str,
    body_raw: str,
    body_plain: str,
    audio: str | None,
) -> IngestRecord:
    item = build_item(
        source_id=source.id,
        guid=guid,
        url=url,
        title=title,
        fetched_at=now,
        modality="text",
        body_raw=body_raw,
        body_plain=body_plain,
        published_at=published,
    )
    asset = Asset(
        item_id=item.id,
        transcript_status="not_applicable",
        audio_url=audio,
    )
    return make_ingest_record(item, asset)


def _podcast_record(
    source: Source,
    *,
    guid: str,
    url: str,
    title: str,
    published: str | None,
    now: str,
    channel: str,
    notes: str,
    audio: str | None,
    transcript_url: str | None,
    transcript_type: str | None,
    transcript_text: str | None,
    transcript_format: str,
) -> IngestRecord:
    # Show notes are raw evidence only. They never become the transcript.
    body_raw = notes
    body_plain = ""
    status = "pending_transcript"
    if transcript_text is not None and transcript_text.strip() != "":
        parsed = parse_podcast(
            title,
            transcript_text,
            audio,
            transcript_url,
            transcript_format=transcript_format,
        )
        if parsed.transcript_status == "ready":
            body_raw = transcript_text
            body_plain = parsed.body_plain
            status = "ready"
    item = build_item(
        source_id=source.id,
        guid=guid,
        url=url,
        title=title,
        fetched_at=now,
        modality="podcast",
        body_raw=body_raw,
        body_plain=body_plain,
        published_at=published,
    )
    asset = Asset(
        item_id=item.id,
        transcript_status=status,
        channel=channel,
        audio_url=audio,
        transcript_url=transcript_url,
        transcript_type=transcript_type if status == "ready" or transcript_url else None,
    )
    return make_ingest_record(item, asset)


def _podcast_json_record(
    source: Source,
    item: dict[str, object],
    *,
    guid: str,
    url: str,
    published: str | None,
    now: str,
) -> IngestRecord:
    title = _json_title(item.get("title"))
    audio = _optional_public(item.get("audio_url"), allow_missing=True)
    transcript_url = _optional_reference(item.get("transcript_url"))
    transcript = item.get("transcript")
    if transcript is not None and not isinstance(transcript, str):
        raise SchemaError("invalid_text")
    text = transcript if isinstance(transcript, str) and transcript.strip() != "" else None
    return _podcast_record(
        source,
        guid=guid,
        url=url,
        title=title,
        published=published,
        now=now,
        channel="",
        notes="",
        audio=audio,
        transcript_url=transcript_url,
        transcript_type="text/plain" if text is not None else _type_for_url(transcript_url),
        transcript_text=text,
        transcript_format="plain",
    )


def _video_record(
    source: Source,
    item: dict[str, object],
    *,
    guid: str,
    url: str,
    published: str | None,
    now: str,
) -> IngestRecord:
    payload: dict[str, object] = {}
    for key in ("title", "channel", "like_count", "comment_count", "view_count", "comments", "frame_paths"):
        if key in item:
            payload[key] = item[key]
    meta = parse_video(payload)
    built = build_item(
        source_id=source.id,
        guid=guid,
        url=url,
        title=meta.title,
        fetched_at=now,
        modality="video",
        body_raw="",
        body_plain="",
        published_at=published,
    )
    asset = Asset(
        item_id=built.id,
        transcript_status="not_applicable",
        channel=meta.channel,
        like_count=meta.like_count,
        comment_count=meta.comment_count,
        view_count=meta.view_count,
        top_comments=meta.comments,
    )
    return make_ingest_record(built, asset)


def _explicit_guid(entry: dict[str, object]) -> str | None:
    for key in ("guid", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip() != "":
            return value.strip()
    return None


def _reject_guid(guid: str) -> None:
    if guid == "" or any(char in guid for char in _IDENTITY_BREAKS):
        raise SchemaError("invalid_identity")


def _item_url(source: Source, link: str) -> str:
    if link.strip() == "":
        raise SchemaError("invalid_url")
    absolute = link.strip()
    if urlsplit(absolute).scheme == "":
        if source.kind == "file":
            raise SchemaError("invalid_url")
        absolute = urljoin(source.url, absolute)
    return canonical_url(absolute)


def _public_item_url(value: object) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise SchemaError("invalid_url")
    if value.startswith("file:") or urlsplit(value).scheme == "":
        raise SchemaError("invalid_url")
    return canonical_url(value.strip())


def _json_guid(value: object, url: str) -> str:
    if value is None:
        return url
    if not isinstance(value, str) or value.strip() == "":
        return url
    guid = value.strip()
    _reject_guid(guid)
    return guid


def _json_title(value: object) -> str:
    if not isinstance(value, str):
        raise SchemaError("invalid_title")
    return value


def _json_time(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("invalid_timestamp")
    if value.strip() == "":
        return None
    try:
        return format_timestamp(parse_timestamp(value))
    except SchemaError:
        return None


def _published(entry: dict[str, object]) -> str | None:
    # Read the stored keys directly. feedparser warns if code asks for the
    # updated alias when only published_parsed is present.
    parsed = _stored_struct(entry, "published_parsed")
    if parsed is None:
        parsed = _stored_struct(entry, "updated_parsed")
    if parsed is None:
        return None
    return _published_moment(parsed)


def _stored_struct(entry: dict[str, object], key: str) -> time.struct_time | None:
    if not dict.__contains__(entry, key):
        return None
    value = dict.__getitem__(entry, key)
    if isinstance(value, time.struct_time):
        return value
    return None


def _published_moment(parsed: time.struct_time) -> str | None:
    try:
        moment = datetime(
            parsed.tm_year,
            parsed.tm_mon,
            parsed.tm_mday,
            parsed.tm_hour,
            parsed.tm_min,
            parsed.tm_sec,
            tzinfo=timezone.utc,
        )
    except (ValueError, OverflowError):
        return None
    return format_timestamp(moment)


def _html_body(entry: dict[str, object]) -> str:
    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            value = block.get("value")
            if isinstance(value, str) and value.strip() != "":
                return value
    summary = entry.get("summary")
    if isinstance(summary, str):
        return summary
    return ""


def _audio_url(source: Source, entry: dict[str, object]) -> str | None:
    enclosures = entry.get("enclosures")
    if not isinstance(enclosures, list):
        return None
    for enclosure in enclosures:
        if not isinstance(enclosure, dict):
            continue
        href = enclosure.get("href")
        if not isinstance(href, str) or href.strip() == "":
            continue
        media = _media_type(enclosure.get("type") if isinstance(enclosure.get("type"), str) else None)
        extension = _extension(href)
        if media is not None and (media.startswith("video/") or media.startswith("image/")):
            continue
        if (media is not None and media.startswith("audio/")) or extension in _AUDIO_EXTENSIONS:
            try:
                return _resolve_public(source, href)
            except SchemaError:
                return None
    return None


def _transcript(
    source: Source,
    element: ElementTree.Element,
) -> tuple[str | None, str | None, str | None, str]:
    choices: list[tuple[int, int, int, str | None, str | None, str | None]] = []
    for index, node in enumerate(_transcript_nodes(element)):
        raw_url = node.attrib.get("url") or node.attrib.get("href")
        mime = _media_type(node.attrib.get("type"))
        text = "".join(node.itertext()).strip()
        stored_url: str | None = None
        if isinstance(raw_url, str) and raw_url.strip() != "":
            stored_url = _resolve_reference(source, raw_url.strip())
        inline = text if text != "" else None
        choices.append(
            (
                0 if inline is not None else 1,
                _type_rank(mime, stored_url),
                index,
                stored_url,
                mime,
                inline,
            )
        )
    if not choices:
        return None, None, None, "plain"
    choices.sort()
    _prefer, _rank, _index, url, mime, inline = choices[0]
    declared = mime if mime in _FORMAT else _type_for_url(url)
    transcript_format = _FORMAT.get(declared or "", "plain")
    if inline is None:
        return url, declared, None, transcript_format
    return url, declared if declared is not None else "text/plain", inline, transcript_format


def _transcript_nodes(element: ElementTree.Element) -> list[ElementTree.Element]:
    return [node for node in element.iter() if node is not element and _local(node.tag) == "transcript"]


def _type_rank(mime: str | None, url: str | None) -> int:
    if mime in _TYPE_RANK:
        return _TYPE_RANK[mime]
    derived = _type_for_url(url)
    if derived in _TYPE_RANK:
        return _TYPE_RANK[derived]
    return 9


def _type_for_url(url: str | None) -> str | None:
    if url is None:
        return None
    return _EXT_TYPE.get(_extension(url))


def _resolve_reference(source: Source, raw: str) -> str:
    if raw.startswith("file:"):
        return raw
    return _resolve_public(source, raw)


def _resolve_public(source: Source, raw: str) -> str:
    absolute = raw.strip()
    if urlsplit(absolute).scheme == "":
        if source.kind == "file":
            raise SchemaError("invalid_url")
        absolute = urljoin(source.url, absolute)
    if absolute.startswith("file:"):
        raise SchemaError("invalid_url")
    return canonical_url(absolute)


def _optional_public(value: object, *, allow_missing: bool) -> str | None:
    if value is None:
        if allow_missing:
            return None
        raise SchemaError("invalid_url")
    if not isinstance(value, str) or value.strip() == "":
        raise SchemaError("invalid_url")
    if value.startswith("file:") or urlsplit(value).scheme == "":
        raise SchemaError("invalid_url")
    return canonical_url(value.strip())


def _optional_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise SchemaError("invalid_url")
    text = value.strip()
    if text.startswith("file:"):
        return text
    if urlsplit(text).scheme == "":
        raise SchemaError("invalid_url")
    return canonical_url(text)


def _media_type(value: str | None) -> str | None:
    if value is None:
        return None
    base = value.split(";", 1)[0].strip().lower()
    if base == "" or _MIME.fullmatch(base) is None:
        return None
    return base


def _extension(url: str) -> str:
    path = urlsplit(url).path
    slash = path.rfind("/")
    dot = path.rfind(".")
    if dot <= slash:
        return ""
    ext = path[dot:].lower()
    if _EXTENSION.fullmatch(ext) is None:
        return ""
    return ext


def _channel(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        return ""
    return value


def _issue(source_id: str, marker: str, code: str) -> EntryIssue:
    digest = hashlib.sha256(f"{source_id}\n{marker}".encode("utf-8")).hexdigest()
    return EntryIssue(entry_key_hash=digest, code=code)
