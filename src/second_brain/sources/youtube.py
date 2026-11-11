"""Parse a YouTube channel Atom document into a FeedBatch.

The bytes are already in memory. This module does not fetch the watch page,
open thumbnails or media:content, store rows, or drop duplicates across runs.
Description text goes through parse_text. Counts, channel, and the refusal to
read frames go through parse_video. Public Atom has no likes or comments, so
those stay empty even when media:starRating is present.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from second_brain.errors import FetchError, FramesNotAllowed, SchemaError, UnsafeUrl
from second_brain.models import (
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
from second_brain.parse.text import parse_text
from second_brain.parse.video import parse_video
from second_brain.urlguard import guard_public_url

_ATOM = "http://www.w3.org/2005/Atom"
_YOUTUBE = "http://www.youtube.com/xml/schemas/2015"
# The Media RSS vocabulary is published with and without the trailing slash.
_MEDIA = (
    "http://search.yahoo.com/mrss/",
    "http://search.yahoo.com/mrss",
)
_HTML_TYPES = frozenset({"", "text/html", "application/xhtml+xml"})
_BREAKS = ("\n", "\r", "\x00", "\u2028", "\u2029")
_CDATA = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DECLARATION = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_ENCODING = re.compile(r"""encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


def parse_youtube_feed(source: Source, xml_bytes: bytes, *, now: str) -> FeedBatch:
    """Parse one Atom document. One bad entry becomes an EntryIssue.

    Illegal XML, a non-Atom root, or a DTD fails the whole document. A missing
    video id or watch link fails that entry only. A date that does not parse
    leaves published_at unset, and the entry is still returned.
    """
    _require_source(source)
    fetched_at = require_canonical_timestamp(now)
    if not isinstance(xml_bytes, bytes):
        raise SchemaError("invalid_payload")
    root = _root(xml_bytes)
    records: list[IngestRecord] = []
    issues: list[EntryIssue] = []
    seen: set[str] = set()
    for index, entry in enumerate(_direct(root, (_ATOM,), "entry")):
        parsed = _parse_entry(source, root, entry, index, fetched_at)
        if isinstance(parsed, EntryIssue):
            issues.append(parsed)
            continue
        # A rejected earlier copy does not occupy the guid. The first record does.
        if parsed.item.guid in seen:
            issues.append(
                _issue(source.id, "duplicate_guid", video_id=parsed.item.guid, index=index)
            )
            continue
        seen.add(parsed.item.guid)
        records.append(parsed)
    return FeedBatch(tuple(records), tuple(issues))


def _require_source(source: Source) -> None:
    if not isinstance(source, Source):
        raise SchemaError("invalid_source")
    if source.kind == "youtube_rss":
        return
    if source.kind == "file" and source.file_format == "youtube_rss":
        return
    raise SchemaError("invalid_source")


def _root(data: bytes) -> ET.Element:
    if data.strip() == b"":
        raise FetchError("invalid_feed")
    # Reject the declaration before the parser so internal entities never expand.
    if _has_dtd(data):
        raise FetchError("dtd_forbidden")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise FetchError("invalid_feed") from None
    if not _matches(root.tag, (_ATOM,), "feed"):
        raise FetchError("invalid_feed")
    return root


def _parse_entry(
    source: Source,
    root: ET.Element,
    entry: ET.Element,
    index: int,
    fetched_at: str,
) -> IngestRecord | EntryIssue:
    video_id = _video_id(entry)
    hrefs = _hrefs(entry)
    link_raw = hrefs[0] if hrefs else None
    atom_id = _atom_id(entry)
    if video_id and any(mark in video_id for mark in _BREAKS):
        return _issue(source.id, "invalid_identity", video_id=video_id, index=index)
    if video_id == "":
        return _issue(
            source.id,
            "missing_video_id",
            link=link_raw,
            atom_id=atom_id,
            index=index,
        )
    if link_raw is None:
        return _issue(source.id, "missing_link", video_id=video_id, index=index)
    try:
        link = _resolve_link(source, link_raw)
        title = _title(entry)
        if title == "":
            raise SchemaError("invalid_title")
        return _record(
            source,
            root,
            entry,
            video_id=video_id,
            link=link,
            title=title,
            views=_views(entry),
            fetched_at=fetched_at,
        )
    except FramesNotAllowed:
        return _issue(source.id, "frames_not_allowed", video_id=video_id, index=index)
    except UnsafeUrl as exc:
        return _issue(source.id, exc.code, video_id=video_id, link=link_raw, index=index)
    except SchemaError as exc:
        return _issue(source.id, exc.code, video_id=video_id, link=link_raw, index=index)


def _record(
    source: Source,
    root: ET.Element,
    entry: ET.Element,
    *,
    video_id: str,
    link: str,
    title: str,
    views: int | None,
    fetched_at: str,
) -> IngestRecord:
    # starRating is not a like count. Thumbnail, media:content, and player URLs
    # are not passed in, so parse_video cannot treat a watch page as a frame.
    meta = parse_video(
        {
            "title": title,
            "channel": _author_name(entry) or _author_name(root),
            "like_count": None,
            "comment_count": None,
            "view_count": views,
            "comments": (),
            "frame_paths": (),
        }
    )
    description = _description(entry)
    plain, _kind = parse_text(meta.title, description)
    item = build_item(
        source_id=source.id,
        guid=video_id,
        url=link,
        title=meta.title,
        fetched_at=fetched_at,
        modality="video",
        body_raw=description,
        body_plain=plain,
        published_at=_published(entry),
    )
    asset = Asset(
        item_id=item.id,
        transcript_status=meta.transcript_status,
        channel=meta.channel,
        like_count=meta.like_count,
        comment_count=meta.comment_count,
        view_count=meta.view_count,
        top_comments=meta.comments,
    )
    return make_ingest_record(item, asset)


def _issue(
    source_id: str,
    code: str,
    *,
    video_id: str | None = None,
    link: str | None = None,
    atom_id: str | None = None,
    index: int,
) -> EntryIssue:
    digest = _entry_key_hash(source_id, video_id, link, atom_id, index)
    return EntryIssue(entry_key_hash=digest, code=code)


def _entry_key_hash(
    source_id: str,
    video_id: str | None,
    link: str | None,
    atom_id: str | None,
    index: int,
) -> str:
    """Digest only. The issue does not keep the URL or the title."""
    if video_id:
        kind, value = "video", video_id
    elif link:
        kind, value = "link", link
    elif atom_id:
        kind, value = "atom", atom_id
    else:
        kind, value = "index", str(index)
    return hashlib.sha256(f"{kind}\n{source_id}\n{value}".encode("utf-8")).hexdigest()


def _resolve_link(source: Source, href: str) -> str:
    text = href.strip()
    parts = urlsplit(text)
    # A network source may complete a relative watch link. A file source may not.
    if parts.scheme == "":
        if source.kind != "youtube_rss":
            raise SchemaError("invalid_url")
        text = urljoin(source.url, text)
    guard_public_url(text)
    canonical_url(text)
    return text


def _video_id(entry: ET.Element) -> str:
    nodes = _direct(entry, (_YOUTUBE,), "videoId")
    if not nodes:
        return ""
    return _text(nodes[0])


def _atom_id(entry: ET.Element) -> str | None:
    nodes = _direct(entry, (_ATOM,), "id")
    if not nodes:
        return None
    text = _text(nodes[0])
    return text or None


def _hrefs(entry: ET.Element) -> list[str]:
    """Alternate watch links. rel=self is the feed, not the video page."""
    html: list[str] = []
    other: list[str] = []
    for link in _direct(entry, (_ATOM,), "link"):
        rel = link.attrib.get("rel", "alternate").strip().casefold()
        if rel != "alternate":
            continue
        href = link.attrib.get("href", "").strip()
        if href == "":
            continue
        media_type = link.attrib.get("type", "").split(";", 1)[0].strip().casefold()
        if media_type in _HTML_TYPES:
            html.append(href)
        else:
            other.append(href)
    return html + other


def _title(entry: ET.Element) -> str:
    for node in _direct(entry, (_ATOM,), "title"):
        text = _text(node)
        if text:
            return text
    for node in _walk(entry, _MEDIA, "title"):
        text = _text(node)
        if text:
            return text
    return ""


def _author_name(scope: ET.Element) -> str:
    authors = _direct(scope, (_ATOM,), "author")
    if not authors:
        return ""
    names = _direct(authors[0], (_ATOM,), "name")
    if not names:
        return ""
    return _text(names[0])


def _description(entry: ET.Element) -> str:
    # atom:summary is not the video description. Only media:description is.
    nodes = _walk(entry, _MEDIA, "description")
    if not nodes:
        return ""
    return _markup(nodes[0])


def _views(entry: ET.Element) -> int | None:
    nodes = _walk(entry, _MEDIA, "statistics")
    if not nodes:
        return None
    raw = nodes[0].attrib.get("views")
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    if not text.isascii() or not text.isdigit():
        raise SchemaError("invalid_count")
    return int(text)


def _published(entry: ET.Element) -> str | None:
    # updated is a revision time. It does not stand in for a missing publish time.
    nodes = _direct(entry, (_ATOM,), "published")
    if not nodes:
        return None
    raw = _text(nodes[0])
    if raw == "":
        return None
    try:
        return format_timestamp(parse_timestamp(raw))
    except SchemaError:
        return None


def _markup(elem: ET.Element) -> str:
    children = list(elem)
    if not children:
        return elem.text or ""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in children:
        local = _parts(child.tag)[1]
        parts.append(f"<{local}>{_markup(child)}</{local}>")
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _text(elem: ET.Element) -> str:
    return "".join(elem.itertext()).strip()


def _direct(elem: ET.Element, namespaces: tuple[str, ...], local: str) -> list[ET.Element]:
    return [child for child in list(elem) if _matches(child.tag, namespaces, local)]


def _walk(elem: ET.Element, namespaces: tuple[str, ...], local: str) -> list[ET.Element]:
    return [node for node in elem.iter() if _matches(node.tag, namespaces, local)]


def _matches(tag: str, namespaces: tuple[str, ...], local: str) -> bool:
    namespace, name = _parts(tag)
    return name == local and namespace in namespaces


def _parts(tag: str) -> tuple[str, str]:
    if tag.startswith("{") and "}" in tag:
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return "", tag


def _has_dtd(data: bytes) -> bool:
    stripped = _COMMENT.sub("", _CDATA.sub("", _decode_scan(data)))
    return _DECLARATION.search(stripped) is not None


def _decode_scan(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    sample = data[:160]
    if len(sample) >= 4:
        odd = sum(byte == 0 for byte in sample[1::2])
        even = sum(byte == 0 for byte in sample[0::2])
        half = max(len(sample) // 2, 1)
        if odd >= half * 0.8 and even < half * 0.2:
            return data.decode("utf-16-le", "replace")
        if even >= half * 0.8 and odd < half * 0.2:
            return data.decode("utf-16-be", "replace")
    body = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    head = body[:200].decode("ascii", "replace")
    match = _ENCODING.search(head)
    if match is not None:
        try:
            return body.decode(match.group(1), "replace")
        except LookupError:
            pass
    return body.decode("utf-8", "replace")
