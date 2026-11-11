"""RSS, local records, and transcript hydration.

Parsers receive bytes only. HTTP in these tests is a fake transport, and
fixture reads stay inside tests/fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from second_brain.errors import FetchError, SchemaError
from second_brain.http import HttpClient
from second_brain.models import (
    Asset,
    BatchOutcome,
    Classification,
    HttpResponse,
    KeyPolicy,
    Source,
    format_timestamp,
    item_id,
    parse_timestamp,
    policy_hash,
)
from second_brain.sources.ingest import hydrate_transcripts, persist_feed
from second_brain.sources.rss import parse_local_records, parse_rss
from second_brain.store import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = "2030-01-15T00:00:00.000000Z"
DAY = "2030-01-10T00:00:00.000000Z"
FUTURE = "2030-02-01T00:00:00.000000Z"
POLICY = policy_hash("prefs", KeyPolicy(), {"type": "object"})
FEED_NS = 'xmlns:content="http://purl.org/rss/1.0/modules/content/"'
PODCAST_NS = (
    'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
    'xmlns:podcast="https://podcastindex.org/namespace/1.0"'
)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError(seconds)
        self.sleeps.append(float(seconds))
        self._now += timedelta(seconds=float(seconds))

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class ScriptTransport:
    """Return scripted responses. A URL that was not scripted fails the test."""

    def __init__(self, scripts: list[object] | None = None, *, by_url: dict[str, object] | None = None) -> None:
        self.scripts = list(scripts or [])
        self.by_url = dict(by_url or {})
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: int,
        max_bytes: int,
        purpose: str,
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout_s": timeout_s,
                "max_bytes": max_bytes,
                "purpose": purpose,
            }
        )
        if url in self.by_url:
            action = self.by_url[url]
        elif self.scripts:
            action = self.scripts.pop(0)
        else:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(action, BaseException):
            raise action
        status, response_headers, body = action
        return HttpResponse(status=status, body=body, headers=response_headers, url=url)


def stamp(moment: datetime) -> str:
    return format_timestamp(moment)


def pubdate(year: int, month: int, day: int) -> str:
    return format_datetime(datetime(year, month, day, tzinfo=timezone.utc))


def rss_document(items: str, *, podcast: bool = False) -> bytes:
    namespace = PODCAST_NS if podcast else FEED_NS
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" {namespace}>
  <channel>
    <title>Synthetic</title>
    <link>https://example.com/</link>
    {items}
  </channel>
</rss>
"""
    return xml.encode("utf-8")


def item_xml(
    *,
    title: str,
    guid: str | None = None,
    link: str | None = None,
    published: str | None = None,
    description: str = "",
    encoded: str | None = None,
    enclosure: str | None = None,
    transcript_url: str | None = None,
    transcript_type: str | None = None,
    transcript_text: str | None = None,
) -> str:
    parts = [f"<title>{title}</title>"]
    if link is not None:
        parts.append(f"<link>{link}</link>")
    if guid is not None:
        parts.append(f"<guid isPermaLink=\"false\">{guid}</guid>")
    if published is not None:
        parts.append(f"<pubDate>{published}</pubDate>")
    if description:
        parts.append(f"<description><![CDATA[{description}]]></description>")
    if encoded is not None:
        parts.append(f"<content:encoded><![CDATA[{encoded}]]></content:encoded>")
    if enclosure is not None:
        parts.append(
            f'<enclosure url="{enclosure}" type="audio/mpeg" length="3"/>'
        )
    if transcript_url is not None or transcript_text is not None:
        mime = transcript_type or "text/plain"
        url_attr = f' url="{transcript_url}"' if transcript_url else ""
        body = transcript_text or ""
        parts.append(f'<podcast:transcript{url_attr} type="{mime}">{body}</podcast:transcript>')
    return "<item>" + "".join(parts) + "</item>"


def open_store(tmp_path: Path) -> tuple[Store, Path]:
    raw = tmp_path / "raw"
    raw.mkdir()
    store = Store.connect(str(tmp_path / "brain.sqlite"))
    store.migrate()
    return store, raw


def blog_source(source_id: str = "blog", *, backfill_days: int = 14) -> Source:
    return Source(
        id=source_id,
        kind="rss",
        url=f"https://feeds.example.com/{source_id}.xml",
        title=f"Blog {source_id}",
        backfill_days=backfill_days,
    )


def podcast_source(
    source_id: str = "show",
    *,
    hosts: tuple[str, ...] = (),
    backfill_days: int = 14,
) -> Source:
    return Source(
        id=source_id,
        kind="podcast_rss",
        url=f"https://feeds.example.com/{source_id}.xml",
        title=f"Show {source_id}",
        backfill_days=backfill_days,
        transcript_hosts=hosts,
    )


def file_source(source_id: str, file_format: str, url: str) -> Source:
    return Source(
        id=source_id,
        kind="file",
        url=url,
        title=f"File {source_id}",
        file_format=file_format,
        backfill_days=14,
    )


def http_response(status: int, body: bytes, headers: dict[str, str], url: str) -> HttpResponse:
    return HttpResponse(status=status, body=body, headers=headers, url=url)


def items_by_guid(store: Store, now: str) -> dict[str, object]:
    rows = (
        store.items_for_classify(now=now)
        + store.held_date_items(now=now)
        + store.pending_transcript_items(now=now)
    )
    return {row.guid: row for row in rows}


def asset_of(store: Store, item_id_value: str) -> Asset:
    return store.assets_for([item_id_value])[item_id_value]


def fixture_reader(uri: str) -> bytes:
    prefix = "file:tests/fixtures/"
    if not uri.startswith(prefix) or ".." in uri.split("/"):
        raise SchemaError("invalid_fixture_url")
    relative = uri[len("file:") :]
    path = ROOT / relative
    root = (ROOT / "tests" / "fixtures").resolve()
    if path.is_symlink():
        raise SchemaError("symlink_escape")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise SchemaError("invalid_fixture_url")
    return resolved.read_bytes()


def test_atom_dates_and_unparseable_pubdate() -> None:
    source = blog_source("atom")
    document = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Blog</title>
  <entry>
    <title>Post</title>
    <id>urn:example:post</id>
    <link href="https://example.com/post"/>
    <updated>2030-01-10T00:00:00Z</updated>
    <content type="html">&lt;p&gt;Hello&lt;/p&gt;&lt;script&gt;nope()&lt;/script&gt;</content>
    <summary>SUM</summary>
  </entry>
  <entry>
    <title>Link only</title>
    <link href="https://example.com/only?utm_source=atom"/>
    <updated>2030-02-01T00:00:00Z</updated>
    <summary>body</summary>
  </entry>
</feed>
""".encode("utf-8")
    batch = parse_rss(source, document, now=NOW)
    assert batch.issues == ()
    by_guid = {record.item.guid: record for record in batch.records}
    post = by_guid["urn:example:post"]
    assert post.item.body_plain == "Hello"
    assert "nope" not in post.item.body_plain
    assert "SUM" not in post.item.body_plain
    assert post.item.published_at == DAY
    assert post.item.modality == "text"
    linked = by_guid["https://example.com/only"]
    assert linked.item.published_at == FUTURE
    assert linked.item.body_plain == "body"
    bad = parse_rss(
        source,
        rss_document(
            item_xml(
                title="Broken date",
                guid="broken-date",
                link="https://example.com/broken-date",
                published="not-a-date",
                encoded="<p>kept</p>",
            )
        ),
        now=NOW,
    )
    assert bad.issues == ()
    assert bad.records[0].item.published_at is None
    assert bad.records[0].item.body_plain == "kept"


def test_second_fetch_inserts_only_new_items(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = blog_source()
    store.sync_sources([source], now=NOW, raw_dir=raw)
    published = pubdate(2030, 1, 10)
    alpha = item_xml(
        title="Alpha",
        guid="a",
        link="https://example.com/a",
        published=published,
        description="ignore me",
        encoded="<p>Body A</p><script>alert(1)</script>",
    )
    beta = item_xml(
        title="Beta",
        guid="b",
        link="https://example.com/b",
        published=published,
        encoded="<p>Body B</p>",
    )
    gamma = item_xml(
        title="Gamma",
        guid="c",
        link="https://example.com/c",
        published=published,
        encoded="<p>Body C</p>",
    )
    try:
        first = parse_rss(source, rss_document(alpha), now=NOW)
        assert [record.item.guid for record in first.records] == ["a"]
        assert first.records[0].item.body_plain == "Body A"
        assert "alert" not in first.records[0].item.body_plain
        assert "ignore me" not in first.records[0].item.body_plain
        assert first.records[0].item.modality == "text"
        assert first.records[0].asset.transcript_status == "not_applicable"
        saved = persist_feed(
            store,
            source,
            first,
            now=NOW,
            raw_dir=raw,
            response=http_response(200, rss_document(alpha), {"etag": "v1"}, source.url),
        )
        assert (saved.inserted, saved.updated, saved.unchanged) == (1, 0, 0)
        alpha_id = item_id("blog", "a")
        raw_path = next((raw / alpha_id).glob("*.md"))
        raw_bytes = raw_path.read_bytes()
        raw_mtime = raw_path.stat().st_mtime_ns
        second = parse_rss(source, rss_document(beta + alpha + gamma), now=NOW)
        again = persist_feed(
            store,
            source,
            second,
            now=NOW,
            raw_dir=raw,
            response=http_response(200, b"<rss/>", {"etag": "v2"}, source.url),
        )
        assert (again.inserted, again.updated, again.unchanged) == (2, 0, 1)
        assert raw_path.read_bytes() == raw_bytes
        assert raw_path.stat().st_mtime_ns == raw_mtime
        assert sorted(items_by_guid(store, NOW)) == ["a", "b", "c"]
        assert len(list((raw / alpha_id).glob("*.md"))) == 1
    finally:
        store.close()


def test_backfill_cutoff_is_fixed(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = blog_source(backfill_days=14)
    store.sync_sources([source], now=NOW, raw_dir=raw)
    initial = format_timestamp(parse_timestamp(NOW) - timedelta(days=14))
    assert initial == "2030-01-01T00:00:00.000000Z"
    assert store.get_source_state(source.id).initial_since_at == initial
    old_xml = item_xml(
        title="Too old",
        guid="too-old",
        link="https://example.com/too-old",
        published=pubdate(2029, 12, 26),
        encoded="<p>ancient</p>",
    )
    edge_xml = item_xml(
        title="Edge",
        guid="edge",
        link="https://example.com/edge",
        published=pubdate(2030, 1, 1),
        encoded="<p>first</p>",
    )
    try:
        first = persist_feed(
            store,
            source,
            parse_rss(source, rss_document(old_xml + edge_xml), now=NOW),
            now=NOW,
            raw_dir=raw,
            response=None,
        )
        assert first.skipped_old == 1
        assert first.inserted == 1
        assert items_by_guid(store, NOW)["edge"].body_plain == "first"
        assert "too-old" not in items_by_guid(store, NOW)
        later = format_timestamp(parse_timestamp(NOW) + timedelta(days=20))
        rolling = format_timestamp(parse_timestamp(later) - timedelta(days=14))
        caught_at = "2030-01-05T00:00:00.000000Z"
        assert caught_at >= initial
        assert caught_at < rolling
        store.sync_sources([source], now=later, raw_dir=raw)
        assert store.get_source_state(source.id).initial_since_at == initial
        revised = item_xml(
            title="Edge",
            guid="edge",
            link="https://example.com/edge",
            published=pubdate(2030, 1, 1),
            encoded="<p>revised</p>",
        )
        caught = item_xml(
            title="Caught up",
            guid="during-outage",
            link="https://example.com/during-outage",
            published=pubdate(2030, 1, 5),
            encoded="<p>caught up</p>",
        )
        second = persist_feed(
            store,
            source,
            parse_rss(source, rss_document(old_xml + revised + caught), now=later),
            now=later,
            raw_dir=raw,
            response=None,
        )
        assert second.skipped_old == 1
        assert second.updated == 1
        assert second.inserted == 1
        found = items_by_guid(store, later)
        assert "too-old" not in found
        assert found["edge"].body_plain == "revised"
        assert found["during-outage"].body_plain == "caught up"
        assert store.get_source_state(source.id).initial_since_at == initial
    finally:
        store.close()


def test_missing_future_date_and_guid(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = blog_source()
    store.sync_sources([source], now=NOW, raw_dir=raw)
    published = pubdate(2030, 1, 10)
    undated = item_xml(title="Undated", guid="undated", link="https://example.com/undated", encoded="<p>no date</p>")
    future = item_xml(
        title="Future",
        guid="future",
        link="https://example.com/future",
        published=pubdate(2030, 2, 1),
        encoded="<p>later</p>",
    )
    linked = item_xml(
        title="Linked",
        link="https://example.com/stable?utm_source=rss",
        published=published,
        encoded="<p>stable</p>",
    )
    named = item_xml(
        title="Same Name",
        guid="named",
        link="https://example.com/named",
        published=published,
        encoded="<p>named body</p>",
    )
    title_only = item_xml(title="Same Name")
    relative = item_xml(
        title="Relative",
        link="/posts/rel",
        published=published,
        encoded="<p>relative</p>",
    )
    document = rss_document(undated + future + linked + title_only + named + relative)
    try:
        batch = parse_rss(source, document, now=NOW)
        assert [issue.code for issue in batch.issues] == ["missing_identity"]
        guids = [record.item.guid for record in batch.records]
        assert "https://example.com/stable" in guids
        assert "https://feeds.example.com/posts/rel" in guids
        saved = persist_feed(store, source, batch, now=NOW, raw_dir=raw, response=None)
        assert saved.invalid == 1
        assert saved.held_date == 2
        assert saved.inserted == 5
        found = items_by_guid(store, NOW)
        assert found["undated"].published_at is None
        assert found["future"].published_at == FUTURE
        assert found["named"].title == "Same Name"
        stable = found["https://example.com/stable"]
        assert stable.canonical_url == "https://example.com/stable"
        assert stable.id == item_id(source.id, "https://example.com/stable")
        relative_item = found["https://feeds.example.com/posts/rel"]
        assert relative_item.url == "https://feeds.example.com/posts/rel"
        linked_again = item_xml(
            title="Linked",
            link="https://example.com/stable?fbclid=abc",
            published=published,
            encoded="<p>stable</p>",
        )
        title_again = item_xml(title="Same Name")
        second = parse_rss(source, rss_document(linked_again + title_again + named), now=NOW)
        assert [issue.code for issue in second.issues] == ["missing_identity"]
        again = persist_feed(store, source, second, now=NOW, raw_dir=raw, response=None)
        assert again.inserted == 0
        assert again.unchanged == 2
        assert again.invalid == 1
        after = items_by_guid(store, NOW)
        assert after["named"].analysis_hash == found["named"].analysis_hash
        assert after["named"].body_plain == "named body"
        assert "Same Name" not in {row.guid for row in after.values()}
        file_blog = file_source("clip", "rss", "file:tests/fixtures/rss/feed.xml")
        rejected = parse_rss(
            file_blog,
            rss_document(item_xml(title="Rel", link="/posts/rel", published=published, encoded="<p>x</p>")),
            now=NOW,
        )
        assert rejected.records == ()
        assert rejected.issues[0].code == "invalid_url"
    finally:
        store.close()


def test_podcast_does_not_treat_notes_as_transcript(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = podcast_source()
    store.sync_sources([source], now=NOW, raw_dir=raw)
    published = pubdate(2030, 1, 10)
    notes = item_xml(
        title="Notes",
        guid="notes",
        link="https://show.example.com/notes",
        published=published,
        description="<p>Show notes mention a transcript</p>",
        enclosure="https://cdn.example.com/notes.mp3",
    )
    explicit = item_xml(
        title="Explicit",
        guid="explicit",
        link="https://show.example.com/explicit",
        published=published,
        description="<p>These notes are not the transcript</p>",
        enclosure="https://cdn.example.com/explicit.mp3",
        transcript_text="alpha beta",
    )
    transport = ScriptTransport()
    clock = FakeClock(datetime(2030, 1, 15, tzinfo=timezone.utc))
    http = HttpClient(transport, clock, store, min_interval_s=0)
    try:
        batch = parse_rss(source, rss_document(notes + explicit, podcast=True), now=NOW)
        assert batch.issues == ()
        by_guid = {record.item.guid: record for record in batch.records}
        assert by_guid["notes"].item.body_plain == ""
        assert by_guid["notes"].item.modality == "podcast"
        assert by_guid["notes"].asset.transcript_status == "pending_transcript"
        assert by_guid["notes"].asset.audio_url == "https://cdn.example.com/notes.mp3"
        assert by_guid["notes"].asset.transcript_url is None
        assert "Show notes" not in by_guid["notes"].item.body_plain
        assert by_guid["explicit"].item.body_plain == "alpha beta"
        assert by_guid["explicit"].asset.transcript_status == "ready"
        assert by_guid["explicit"].asset.audio_url == "https://cdn.example.com/explicit.mp3"
        persist_feed(store, source, batch, now=NOW, raw_dir=raw, response=None)
        ready, issues = hydrate_transcripts(
            store,
            http=http,
            fixture_reader=fixture_reader,
            sources=(source,),
            now=NOW,
            raw_dir=raw,
            limit=20,
        )
        assert ready == 0
        assert transport.calls == []
        assert issues == ()
        stored = items_by_guid(store, NOW)
        assert stored["notes"].body_plain == ""
        assert asset_of(store, stored["notes"].id).transcript_status == "pending_transcript"
        assert stored["explicit"].body_plain == "alpha beta"
        payload = {
            "items": [
                {
                    "guid": "json-ready",
                    "url": "https://show.example.com/json-ready",
                    "title": "JSON",
                    "published_at": DAY,
                    "transcript": "alpha beta",
                    "audio_url": "https://cdn.example.com/json.mp3",
                    "transcript_url": None,
                }
            ]
        }
        local = file_source("episodes", "podcast_json", "file:tests/fixtures/podcast/episodes.json")
        store.sync_sources([source, local], now=NOW, raw_dir=raw)
        local_batch = parse_local_records(local, json.dumps(payload).encode("utf-8"), now=NOW)
        assert local_batch.issues == ()
        assert local_batch.records[0].item.body_plain == "alpha beta"
        assert local_batch.records[0].asset.transcript_status == "ready"
        assert local_batch.records[0].asset.audio_url == "https://cdn.example.com/json.mp3"
        persist_feed(store, local, local_batch, now=NOW, raw_dir=raw, response=None)
        fixture_payload = (ROOT / "tests" / "fixtures" / "podcast" / "episodes.json").read_bytes()
        example = parse_local_records(local, fixture_payload, now=NOW)
        statuses = {record.item.guid: record.asset.transcript_status for record in example.records}
        assert statuses == {"episode-ready": "ready", "episode-audio": "pending_transcript"}
        assert example.records[0].item.body_plain == "这是一段合成文稿。"
        assert next(record.item.body_plain for record in example.records if record.item.guid == "episode-audio") == ""
        pending_file = {
            "items": [
                {
                    "guid": "file-ep",
                    "url": "https://example.com/podcast/file-ep",
                    "title": "Fixture",
                    "published_at": DAY,
                    "transcript": None,
                    "audio_url": "https://cdn.example.com/file.mp3",
                    "transcript_url": "file:tests/fixtures/podcast/episode.txt",
                }
            ]
        }
        file_batch = parse_local_records(local, json.dumps(pending_file).encode("utf-8"), now=NOW)
        persist_feed(store, local, file_batch, now=NOW, raw_dir=raw, response=None)
        ready_count, file_issues = hydrate_transcripts(
            store,
            http=None,
            fixture_reader=fixture_reader,
            sources=(source, local),
            now=NOW,
            raw_dir=raw,
            limit=20,
        )
        assert ready_count == 1
        assert file_issues == ()
        assert transport.calls == []
        hydrated = items_by_guid(store, NOW)["file-ep"]
        assert hydrated.body_plain == "fixture transcript"
        assert asset_of(store, hydrated.id).transcript_status == "ready"
        blog = (ROOT / "tests" / "fixtures" / "rss" / "feed.xml").read_bytes()
        blog_source_file = file_source("example-blog", "rss", "file:tests/fixtures/rss/feed.xml")
        blog_batch = parse_rss(blog_source_file, blog, now=NOW)
        assert blog_batch.issues == ()
        assert [record.item.guid for record in blog_batch.records] == ["alpha", "beta"]
        assert blog_batch.records[0].item.body_plain == "正文甲"
        assert "secret" not in blog_batch.records[0].item.body_plain
        assert blog_batch.records[1].item.body_plain == "正文乙"
    finally:
        store.close()


def test_transcript_retry_after_feed_304(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = podcast_source(hosts=("transcripts.example.com",))
    store.sync_sources([source], now=NOW, raw_dir=raw)
    transcript_url = "https://transcripts.example.com/ep.txt"
    document = rss_document(
        item_xml(
            title="Episode",
            guid="ep-1",
            link="https://show.example.com/ep-1",
            published=pubdate(2030, 1, 10),
            description="<p>show notes only</p>",
            enclosure="https://cdn.example.com/ep-1.mp3",
            transcript_url=transcript_url,
            transcript_type="text/plain",
        ),
        podcast=True,
    )
    clock = FakeClock(datetime(2030, 1, 15, tzinfo=timezone.utc))
    transport = ScriptTransport(
        [
            (200, {"etag": "feed-v1", "content-type": "application/rss+xml", "last-modified": "Thu, 10 Jan 2030 00:00:00 GMT"}, document),
            (503, {"content-type": "text/plain"}, b"down"),
            (304, {}, b""),
            (200, {"content-type": "text/plain", "etag": "tr-1"}, b"hello transcript"),
            (304, {}, b""),
        ]
    )
    http = HttpClient(transport, clock, store, min_interval_s=0)

    def pull(now: str) -> tuple[int, tuple[object, ...]]:
        state = store.get_source_state(source.id)
        response = http.fetch(
            source.url,
            source_id=source.id,
            etag=state.etag,
            last_modified=state.last_modified,
            purpose="feed",
        )
        if response.status == 304:
            store.accept_not_modified(source.id, now=now, response=response)
        else:
            batch = parse_rss(source, response.body, now=now)
            persist_feed(store, source, batch, now=now, raw_dir=raw, response=response)
        return hydrate_transcripts(
            store,
            http=http,
            fixture_reader=fixture_reader,
            sources=(source,),
            now=now,
            raw_dir=raw,
            limit=20,
        )

    try:
        ready, issues = pull(NOW)
        assert ready == 0
        assert [issue.code for issue in issues] == ["server_error"]
        pending = items_by_guid(store, NOW)["ep-1"]
        assert pending.body_plain == ""
        assert asset_of(store, pending.id).transcript_status == "pending_transcript"
        assert asset_of(store, pending.id).transcript_url == transcript_url
        assert store.get_source_state(source.id).etag == "feed-v1"
        store.begin_run("run-1", started_at=NOW)
        store.select_run_items("run-1", (pending,))
        store.save_batch(
            BatchOutcome(batch_id="batch-1", item_ids=(pending.id,), status="success"),
            (
                Classification(
                    item_id=pending.id,
                    run_id="run-1",
                    topic="show",
                    bucket="must",
                    reason="Old bucket from the notes version.",
                    analysis_hash=pending.analysis_hash,
                    policy_hash=POLICY,
                    classified_at=NOW,
                ),
            ),
            (),
            run_id="run-1",
        )
        assert store._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
        clock.advance(24 * 60 * 60)
        later = stamp(clock.now())
        ready, issues = pull(later)
        assert ready == 1
        assert issues == ()
        restored = items_by_guid(store, later)["ep-1"]
        assert restored.body_plain == "hello transcript"
        assert asset_of(store, restored.id).transcript_status == "ready"
        assert store.get_current_classified(now=later) == ()
        assert store._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0
        assert [row.guid for row in store.items_for_classify(now=later)] == ["ep-1"]
        assert store.get_source_state(source.id).etag == "feed-v1"
        assert store.get_source_state(source.id).last_http_status == 304
        calls_before = len(transport.calls)
        transcript_calls = [call["url"] for call in transport.calls if call["url"] == transcript_url]
        assert transcript_calls == [transcript_url, transcript_url]
        ready, issues = pull(later)
        assert ready == 0
        assert issues == ()
        assert [call["url"] for call in transport.calls[calls_before:]] == [source.url]
        assert all(call["url"] != "https://cdn.example.com/ep-1.mp3" for call in transport.calls)
        assert restored.body_plain == items_by_guid(store, later)["ep-1"].body_plain
    finally:
        store.close()


def test_transcript_mime_and_host_guard(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = podcast_source("pod")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    guids = [f"m{index}" for index in range(8)]
    ordered = sorted(guids, key=lambda guid: item_id(source.id, guid))
    roles = ["audio", "html", "foreign", "file", "plain", "vtt", "srt", "later"]
    role_of = dict(zip(ordered, roles, strict=True))
    vtt_bytes = (ROOT / "tests" / "fixtures" / "podcast" / "episode.vtt").read_bytes()
    srt_bytes = (ROOT / "tests" / "fixtures" / "podcast" / "episode.srt").read_bytes()
    urls: dict[str, str] = {}
    items: list[str] = []
    published = pubdate(2030, 1, 10)
    for guid, role in role_of.items():
        if role == "foreign":
            url = "https://cdn.example.net/clip.txt"
        elif role == "file":
            url = "file:tests/fixtures/podcast/episode.txt"
        else:
            suffix = {"vtt": "vtt", "srt": "srt"}.get(role, "txt")
            url = f"https://feeds.example.com/{role}-{guid}.{suffix}"
        urls[role] = url
        mime = {"vtt": "text/vtt", "srt": "application/x-subrip"}.get(role, "text/plain")
        items.append(
            item_xml(
                title=role,
                guid=guid,
                link=f"https://show.example.com/{guid}",
                published=published,
                description="<p>notes</p>",
                enclosure="https://audio.example.com/episode.mp3",
                transcript_url=url,
                transcript_type=mime,
            )
        )
    responses = {
        urls["audio"]: (200, {"content-type": "audio/mpeg"}, b"AUDIOBYTES"),
        urls["html"]: (200, {"content-type": "text/html"}, b"HTMLBYTES"),
        urls["plain"]: (200, {"content-type": "text/plain"}, b"plain words"),
        urls["vtt"]: (200, {"content-type": "text/vtt"}, vtt_bytes),
        urls["srt"]: (200, {"content-type": "application/x-subrip"}, srt_bytes),
        urls["later"]: (200, {"content-type": "text/plain"}, b"not yet"),
    }
    transport = ScriptTransport(by_url=responses)
    clock = FakeClock(datetime(2030, 1, 15, tzinfo=timezone.utc))
    http = HttpClient(transport, clock, store, min_interval_s=0)
    reads: list[str] = []

    def reader(uri: str) -> bytes:
        reads.append(uri)
        return fixture_reader(uri)

    try:
        batch = parse_rss(source, rss_document("".join(items), podcast=True), now=NOW)
        assert batch.issues == ()
        persist_feed(store, source, batch, now=NOW, raw_dir=raw, response=None)
        escape = parse_local_records(
            file_source("escape", "podcast_json", "file:tests/fixtures/podcast/episodes.json"),
            json.dumps(
                {
                    "items": [
                        {
                            "guid": "escape",
                            "url": "https://example.com/podcast/escape",
                            "title": "Escape",
                            "published_at": DAY,
                            "transcript": None,
                            "audio_url": None,
                            "transcript_url": "file:tests/fixtures/podcast/../../secret.txt",
                        }
                    ]
                }
            ).encode("utf-8"),
            now=NOW,
        )
        assert escape.records == ()
        assert escape.issues[0].code == "invalid_fixture_url"
        ready, issues = hydrate_transcripts(
            store,
            http=http,
            fixture_reader=reader,
            sources=(source,),
            now=NOW,
            raw_dir=raw,
            limit=7,
        )
        assert ready == 3
        assert reads == []
        called = [call["url"] for call in transport.calls]
        assert urls["foreign"] not in called
        assert urls["later"] not in called
        assert "https://audio.example.com/episode.mp3" not in called
        assert set(called) == {urls["audio"], urls["html"], urls["plain"], urls["vtt"], urls["srt"]}
        codes = sorted(issue.code for issue in issues)
        assert codes.count("bad_content_type") == 2
        assert "host_not_allowed" in codes
        assert "file_transcript_forbidden" in codes
        assert "transcript_fetch_limit" in codes
        found = items_by_guid(store, NOW)
        bodies = {role_of[guid]: item.body_plain for guid, item in found.items()}
        assert bodies["plain"] == "plain words"
        assert bodies["vtt"] == "fixture vtt line"
        assert bodies["srt"] == "fixture srt line"
        for role in ("audio", "html", "foreign", "file", "later"):
            guid = next(key for key, value in role_of.items() if value == role)
            assert found[guid].body_plain == ""
            assert asset_of(store, found[guid].id).transcript_status == "pending_transcript"
            assert "AUDIOBYTES" not in found[guid].body_raw
            assert "HTMLBYTES" not in found[guid].body_raw
        for role in ("plain", "vtt", "srt"):
            guid = next(key for key, value in role_of.items() if value == role)
            assert asset_of(store, found[guid].id).transcript_status == "ready"
    finally:
        store.close()


def test_ipv6_feed_host_allows_same_host_transcript(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    feed = "https://[2001:4860:4860::8888]/show.xml"
    same = "https://[2001:4860:4860::8888]/ep.txt"
    other = "https://cdn.example.net/ep.txt"
    source = Source(id="ipv6", kind="podcast_rss", url=feed, title="IPv6 show")
    store.sync_sources([source], now=NOW, raw_dir=raw)
    published = pubdate(2030, 1, 10)
    items = "".join(
        item_xml(
            title=title,
            guid=title,
            link=f"https://show.example.com/{title}",
            published=published,
            description="<p>notes</p>",
            enclosure=f"https://audio.example.com/{title}.mp3",
            transcript_url=url,
            transcript_type="text/plain",
        )
        for title, url in (("same", same), ("other", other))
    )
    transport = ScriptTransport(by_url={same: (200, {"content-type": "text/plain"}, b"ipv6 transcript")})
    clock = FakeClock(datetime(2030, 1, 15, tzinfo=timezone.utc))
    http = HttpClient(transport, clock, store, min_interval_s=0)
    try:
        batch = parse_rss(source, rss_document(items, podcast=True), now=NOW)
        assert batch.issues == ()
        persist_feed(store, source, batch, now=NOW, raw_dir=raw, response=None)
        ready, issues = hydrate_transcripts(
            store,
            http=http,
            fixture_reader=lambda _uri: b"",
            sources=(source,),
            now=NOW,
            raw_dir=raw,
            limit=5,
        )
        assert ready == 1
        assert [call["url"] for call in transport.calls] == [same]
        assert [issue.code for issue in issues] == ["host_not_allowed"]
        found = items_by_guid(store, NOW)
        assert found["same"].body_plain == "ipv6 transcript"
        assert found["other"].body_plain == ""
        assert asset_of(store, found["same"].id).transcript_status == "ready"
        assert asset_of(store, found["other"].id).transcript_status == "pending_transcript"
    finally:
        store.close()


def test_invalid_feed_preserves_validators(tmp_path: Path) -> None:
    store, raw = open_store(tmp_path)
    source = blog_source()
    store.sync_sources([source], now=NOW, raw_dir=raw)
    published = pubdate(2030, 1, 10)
    first_xml = rss_document(
        item_xml(
            title="Kept",
            guid="kept",
            link="https://example.com/kept",
            published=published,
            encoded="<p>kept</p>",
        )
    )
    try:
        first = persist_feed(
            store,
            source,
            parse_rss(source, first_xml, now=NOW),
            now=NOW,
            raw_dir=raw,
            response=http_response(
                200,
                first_xml,
                {"etag": "v1", "last-modified": "Thu, 10 Jan 2030 00:00:00 GMT"},
                source.url,
            ),
        )
        assert first.inserted == 1
        assert store.get_source_state(source.id).etag == "v1"
        broken = b"<rss><channel><item><title>Broken</title></channel></rss>"
        with pytest.raises(FetchError) as bad_xml:
            parse_rss(source, broken, now=NOW)
        assert bad_xml.value.code == "invalid_feed"
        assert "Broken" not in repr(bad_xml.value)
        dtd = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY secret "EXPANDED">]>
<rss version="2.0"><channel><title>&secret;</title></channel></rss>"""
        with pytest.raises(FetchError) as bad_dtd:
            parse_rss(source, dtd, now=NOW)
        assert bad_dtd.value.code == "xml_dtd"
        assert "EXPANDED" not in repr(bad_dtd.value)
        assert store.get_source_state(source.id).etag == "v1"
        assert store.get_source_state(source.id).last_modified == "Thu, 10 Jan 2030 00:00:00 GMT"
        partial_xml = rss_document(
            item_xml(
                title="Kept",
                guid="kept",
                link="https://example.com/kept",
                published=published,
                encoded="<p>kept</p>",
            )
            + item_xml(title="Kept")
            + item_xml(
                title="Fresh",
                guid="fresh",
                link="https://example.com/fresh",
                published=published,
                encoded="<p>fresh</p>",
            )
        )
        partial = parse_rss(source, partial_xml, now=NOW)
        assert [issue.code for issue in partial.issues] == ["missing_identity"]
        assert sorted(record.item.guid for record in partial.records) == ["fresh", "kept"]
        mixed = persist_feed(
            store,
            source,
            partial,
            now=NOW,
            raw_dir=raw,
            response=http_response(200, partial_xml, {"etag": "v2", "last-modified": "Fri, 11 Jan 2030 00:00:00 GMT"}, source.url),
        )
        assert mixed.inserted == 1
        assert mixed.unchanged == 1
        assert mixed.invalid == 1
        state = store.get_source_state(source.id)
        assert state.etag == "v1"
        assert state.last_modified == "Thu, 10 Jan 2030 00:00:00 GMT"
        assert state.last_http_status == 200
        assert items_by_guid(store, NOW)["fresh"].body_plain == "fresh"
        assert items_by_guid(store, NOW)["kept"].body_plain == "kept"
        store.accept_not_modified(
            source.id,
            now=NOW,
            response=http_response(304, b"", {}, source.url),
        )
        state = store.get_source_state(source.id)
        assert state.etag == "v1"
        assert state.last_modified == "Thu, 10 Jan 2030 00:00:00 GMT"
        assert state.last_http_status == 304
        video = file_source("clips", "video_json", "file:tests/fixtures/video/keeps_five.json")
        store.sync_sources([source, video], now=NOW, raw_dir=raw)
        video_body = json.dumps(
            {
                "items": [
                    {
                        "guid": "clip-ok",
                        "url": "https://example.com/videos/ok",
                        "title": "Ok",
                        "channel": "Desk",
                        "published_at": DAY,
                        "comments": ["hello"],
                        "frame_paths": [],
                    },
                    {
                        "guid": "clip-bad",
                        "url": "https://example.com/videos/bad",
                        "title": "Bad",
                        "channel": "Desk",
                        "frame_paths": ["frames/clip.mp4"],
                    },
                ]
            }
        ).encode("utf-8")
        video_batch = parse_local_records(video, video_body, now=NOW)
        assert [issue.code for issue in video_batch.issues] == ["frames_not_allowed"]
        assert video_batch.records[0].item.modality == "video"
        assert video_batch.records[0].asset.transcript_status == "not_applicable"
        assert video_batch.records[0].asset.top_comments == ("hello",)
        saved_video = persist_feed(
            store,
            video,
            video_batch,
            now=NOW,
            raw_dir=raw,
            response=http_response(200, video_body, {"etag": "video-v1"}, "https://feeds.example.com/video.xml"),
        )
        assert saved_video.inserted == 1
        assert saved_video.invalid == 1
        assert store.get_source_state(video.id).etag is None
        unknown = json.dumps({"items": [{"guid": "x", "url": "https://example.com/x", "title": "X", "cookie": "no"}]}).encode()
        unknown_batch = parse_local_records(
            file_source("pods", "podcast_json", "file:tests/fixtures/podcast/episodes.json"),
            unknown,
            now=NOW,
        )
        assert unknown_batch.records == ()
        assert unknown_batch.issues[0].code == "invalid_field"
        with pytest.raises(FetchError) as foreign:
            parse_local_records(
                file_source("pods", "podcast_json", "file:tests/fixtures/podcast/episodes.json"),
                b'{"items": [], "api_key": "no"}',
                now=NOW,
            )
        assert foreign.value.code == "invalid_payload"
        assert "api_key" not in repr(foreign.value)
    finally:
        store.close()
