"""YouTube Atom becomes video records. The parser never fetches media."""

from __future__ import annotations

import builtins
import hashlib
import http.client
import inspect
import io
import os
import socket
import urllib.request
from pathlib import Path
from typing import get_type_hints

import pytest

from second_brain.errors import FetchError, SchemaError
from second_brain.models import (
    EntryIssue,
    FeedBatch,
    IngestRecord,
    Source,
    analysis_hash,
    canonical_url,
    content_hash,
    item_id,
)
from second_brain.parse.text import parse_text
from second_brain.sources.youtube import parse_youtube_feed

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "youtube"
NOW = "2026-11-10T00:00:00.000000Z"
SOURCE_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_SYNTHETIC_WP11"
WATCH = "https://www.youtube.com/watch?v=view13931aa"
PLAIN = "说明 & 重点 notes about clip.mp4 later"
EDGES = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
  <author><name>订阅作者</name></author>
  <entry>
    <yt:videoId>relative0001</yt:videoId>
    <title>相对链接</title>
    <link rel="alternate" href="/watch?v=relative0001"/>
    <author><name>相对频道</name></author>
    <published>2024-03-01T07:30:00-05:00</published>
  </entry>
  <entry>
    <yt:videoId>utmlink0001</yt:videoId>
    <title>跟踪参数</title>
    <link rel="alternate" href="https://www.YouTube.com/watch?v=utmlink0001&amp;utm_source=wp11"/>
    <author><name>跟踪频道</name></author>
  </entry>
  <entry>
    <yt:videoId>zeroviews01</yt:videoId>
    <title>零播放</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=zeroviews01"/>
    <author><name>零频道</name></author>
    <media:group>
      <media:community>
        <media:starRating count="8" average="1.00" min="1" max="5"/>
        <media:statistics views="0"/>
      </media:community>
    </media:group>
  </entry>
  <entry>
    <yt:videoId>badviews001</yt:videoId>
    <title>坏播放量</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=badviews001"/>
    <media:group><media:statistics views="1,393"/></media:group>
  </entry>
  <entry>
    <yt:videoId>dupdupdupd1</yt:videoId>
    <title>第一份</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=dupdupdupd1"/>
    <author><name>重复频道</name></author>
  </entry>
  <entry>
    <yt:videoId>dupdupdupd1</yt:videoId>
    <title>第二份</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=dupdupdupd1"/>
  </entry>
  <entry>
    <yt:videoId>dupskip0001</yt:videoId>
    <title>坏的先到</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=dupskip0001"/>
    <media:group><media:statistics views="nope"/></media:group>
  </entry>
  <entry>
    <yt:videoId>dupskip0001</yt:videoId>
    <title>保留的</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=dupskip0001"/>
    <author><name>保留频道</name></author>
    <media:group><media:statistics views="5"/></media:group>
  </entry>
  <entry>
    <yt:videoId>baddate0001</yt:videoId>
    <title>坏日期</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=baddate0001"/>
    <published>tomorrow</published>
  </entry>
  <entry>
    <yt:videoId>naivedate01</yt:videoId>
    <title>无时区</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=naivedate01"/>
    <published>2024-01-02T03:04:05</published>
  </entry>
  <entry>
    <yt:videoId>futuredate1</yt:videoId>
    <title>未来</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=futuredate1"/>
    <published>2099-01-01T00:00:00Z</published>
  </entry>
  <entry>
    <yt:videoId>mediabody01</yt:videoId>
    <title>正文来源</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=mediabody01"/>
    <summary>ATOM ONLY</summary>
    <media:group><media:description>MEDIA ONLY</media:description></media:group>
  </entry>
  <entry>
    <yt:videoId>summaryonly</yt:videoId>
    <title>只有摘要</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=summaryonly1"/>
    <summary>ATOM ONLY</summary>
  </entry>
  <entry>
    <yt:videoId>feedauthor1</yt:videoId>
    <title>用订阅作者</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=feedauthor1"/>
  </entry>
  <entry>
    <yt:videoId>norel000001</yt:videoId>
    <title>默认关系</title>
    <link href="https://www.youtube.com/watch?v=norel000001"/>
    <author><name>默认频道</name></author>
  </entry>
  <entry>
    <yt:videoId>mismatch001</yt:videoId>
    <title>链接不必同号</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=othermismatch"/>
    <author><name>错位频道</name></author>
  </entry>
  <entry>
    <yt:videoId>emptyviews1</yt:videoId>
    <title>空统计</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=emptyviews1"/>
    <media:group><media:statistics views=""/></media:group>
  </entry>
  <entry>
    <yt:videoId>protorel001</yt:videoId>
    <title>协议相对</title>
    <link rel="alternate" href="//www.youtube.com/watch?v=protorel001"/>
    <author><name>协议频道</name></author>
  </entry>
  <entry>
    <yt:videoId>htmlpref001</yt:videoId>
    <title>优先网页</title>
    <link rel="alternate" type="application/atom+xml" href="https://www.youtube.com/feeds/videos.xml?channel_id=UC_SYNTHETIC_WP11"/>
    <link rel="alternate" type="text/html" href="https://www.youtube.com/watch?v=htmlpref001"/>
    <author><name>网页频道</name></author>
  </entry>
  <entry>
    <yt:videoId>loopback001</yt:videoId>
    <title>回环</title>
    <link rel="alternate" href="http://127.0.0.1/watch?v=loopback001"/>
  </entry>
  <entry>
    <yt:videoId>blanktitle1</yt:videoId>
    <title>   </title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=blanktitle1"/>
  </entry>
  <entry>
    <yt:videoId>notitle0001</yt:videoId>
    <link rel="alternate" href="https://www.youtube.com/watch?v=notitle0001"/>
  </entry>
</feed>
"""
MEDIA_TRAP = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
  <entry>
    <yt:videoId>goodmedia01</yt:videoId>
    <title>看说明里的 clip.mp4 即可</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=goodmedia01"/>
    <author><name>媒体频道</name></author>
    <media:group>
      <media:content url="https://www.youtube.com/watch?v=goodmedia01" type="text/html"/>
      <media:content url="file:///tmp/wp11-do-not-open.mp4" type="video/mp4"/>
      <media:thumbnail url="file:///tmp/wp11-do-not-open.jpg"/>
      <media:player url="https://www.youtube.com/watch?v=goodmedia01"/>
      <media:description>播放页 https://www.youtube.com/watch?v=goodmedia01 不是帧</media:description>
    </media:group>
  </entry>
  <entry>
    <yt:videoId>badpath0001</yt:videoId>
    <title>/tmp/wp11-do-not-open.jpg</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=badpath0001"/>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/badpath0001/hqdefault.jpg"/>
      <media:content url="file:///tmp/wp11-do-not-open.mp4"/>
    </media:group>
  </entry>
  <entry>
    <yt:videoId>afterpath01</yt:videoId>
    <title>后面仍在</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=afterpath01"/>
    <author><name>后续频道</name></author>
  </entry>
</feed>
"""
LITERAL_DECLARATION = """<?xml version="1.0" encoding="UTF-8"?>
<!-- <!DOCTYPE feed [<!ENTITY x "BOOM">]> -->
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
  <entry>
    <yt:videoId>cdatadoc001</yt:videoId>
    <title>字面声明</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=cdatadoc001"/>
    <media:group><media:description><![CDATA[before <!DOCTYPE feed [<!ENTITY x "BOOM">]> after]]></media:description></media:group>
  </entry>
</feed>
"""
INTERNAL_DTD = """<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY x "BOOM">]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>
"""
EXTERNAL_DTD = """<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&xxe;</title></feed>
"""


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def network_source(**overrides: object) -> Source:
    payload: dict[str, object] = {
        "id": "example-youtube",
        "kind": "youtube_rss",
        "url": SOURCE_URL,
        "title": "合成频道",
    }
    payload.update(overrides)
    return Source(**payload)  # type: ignore[arg-type]


def file_source() -> Source:
    return Source(
        id="example-youtube",
        kind="file",
        file_format="youtube_rss",
        url="file:tests/fixtures/youtube/feed.xml",
        title="合成频道",
    )


def issue_hash(source_id: str, kind: str, value: str) -> str:
    """Digest stored on an EntryIssue: kind, source id, and value, no raw URL."""
    return hashlib.sha256(f"{kind}\n{source_id}\n{value}".encode("utf-8")).hexdigest()


def assert_video(record: IngestRecord, *, guid: str, views: int | None) -> None:
    assert isinstance(record, IngestRecord)
    assert isinstance(record.asset.top_comments, tuple)
    assert record.item.guid == guid
    assert record.item.modality == "video"
    assert record.asset.view_count == views
    assert record.asset.like_count is None
    assert record.asset.comment_count is None
    assert record.asset.top_comments == ()
    assert record.asset.transcript_status == "not_applicable"
    assert record.asset.audio_url is None
    assert record.asset.transcript_url is None
    assert record.asset.transcript_type is None
    assert record.item.duplicate_of is None
    assert record.item.fetched_at == NOW
    assert record.item.published_at != NOW
    assert record.item.id == item_id(record.item.source_id, guid)
    assert record.item.content_hash == content_hash(record.item.title, record.item.body_raw)
    assert record.item.analysis_hash == analysis_hash(record.item, record.asset)
    assert record.item.canonical_url == canonical_url(record.item.url)
    assert record.asset.item_id == record.item.id
    plain, kind = parse_text(record.item.title, record.item.body_raw)
    assert kind == "text"
    assert record.item.body_plain == plain


def assert_safe_issue(issue: EntryIssue, *forbidden: str) -> None:
    assert isinstance(issue, EntryIssue)
    assert len(issue.entry_key_hash) == 64
    text = f"{issue} {issue!r}"
    for needle in forbidden:
        assert needle not in text
    assert "http" not in text
    assert "BOOM" not in text
    assert "/tmp/" not in text


def install_guards(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]) -> None:
    def opened(*_args: object, **_kwargs: object) -> None:
        counts["open"] += 1
        raise AssertionError("file opened")

    def networked(*_args: object, **_kwargs: object) -> None:
        counts["network"] += 1
        raise AssertionError("network used")

    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setattr(io, "open", opened)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(urllib.request, "urlopen", networked)
    monkeypatch.setattr(urllib.request, "urlretrieve", networked)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", networked)
    monkeypatch.setattr(socket.socket, "connect", networked)
    monkeypatch.setattr(socket.socket, "connect_ex", networked)
    monkeypatch.setattr(socket, "create_connection", networked)
    monkeypatch.setattr(socket, "getaddrinfo", networked)
    monkeypatch.setattr(socket, "gethostbyname", networked)
    monkeypatch.setattr(socket, "gethostbyname_ex", networked)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", networked)
    monkeypatch.setattr(http.client.HTTPConnection, "request", networked)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", networked)
    monkeypatch.setattr(http.client.HTTPSConnection, "request", networked)


def test_youtube_views_without_likes() -> None:
    signature = inspect.signature(parse_youtube_feed)
    assert list(signature.parameters) == ["source", "xml_bytes", "now"]
    assert signature.parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["now"].default is inspect.Parameter.empty
    hints = get_type_hints(parse_youtube_feed)
    assert hints["source"] is Source
    assert hints["xml_bytes"] is bytes
    assert hints["now"] is str
    assert hints["return"] is FeedBatch

    payload = load("feed.xml")
    source = network_source()
    batch = parse_youtube_feed(source, payload, now=NOW)
    again = parse_youtube_feed(source=source, xml_bytes=payload, now=NOW)
    from_file = parse_youtube_feed(file_source(), payload, now=NOW)
    assert isinstance(batch, FeedBatch)
    assert batch == again == from_file
    assert batch.issues == ()
    assert len(batch.records) == 1
    record = batch.records[0]
    assert_video(record, guid="view13931aa", views=13931)
    assert record.item.guid != "yt:video:view13931aa"
    assert record.item.title == "合成视频"
    assert record.asset.channel == "合成频道"
    assert record.item.url == WATCH
    assert record.item.canonical_url == WATCH
    assert "feeds/videos.xml" not in record.item.url
    assert not record.item.url.endswith(".jpg")
    assert record.item.published_at == "2024-03-01T12:30:00.000000Z"
    assert record.item.published_at != "2024-03-02T01:00:00.000000Z"
    assert record.item.body_plain == PLAIN
    assert record.item.body_raw != record.item.body_plain
    assert "steal()" in record.item.body_raw
    assert "hqdefault.jpg" in record.item.body_raw
    assert "/tmp/wp11-do-not-open.jpg" in record.item.body_raw
    assert "steal" not in record.item.body_plain
    assert "hqdefault" not in record.item.body_plain
    assert "wp11-do-not-open" not in record.item.body_plain
    assert record.asset.like_count != 999
    assert record.asset.comment_count != 999
    assert record.asset.view_count != 999
    assert record.item.modality != "text"


def test_youtube_missing_stats_and_namespaces() -> None:
    payload = load("namespaces.xml")
    source = network_source()
    batch = parse_youtube_feed(source, payload, now=NOW)
    # Backfill and the subscription title belong to later packages.
    assert parse_youtube_feed(network_source(backfill_days=0), payload, now=NOW) == batch
    assert parse_youtube_feed(network_source(title="别的订阅名"), payload, now=NOW) == batch
    assert [record.item.guid for record in batch.records] == [
        "nostats0001",
        "namespace01",
        "staronly001",
    ]
    assert [issue.code for issue in batch.issues] == [
        "missing_video_id",
        "missing_link",
        "missing_video_id",
    ]
    missing_id, missing_link, bare = batch.issues
    assert missing_id.entry_key_hash == issue_hash(
        source.id,
        "link",
        "https://www.youtube.com/watch?v=missingid01",
    )
    assert missing_link.entry_key_hash == issue_hash(source.id, "video", "missinglink")
    assert bare.entry_key_hash == issue_hash(source.id, "index", "4")
    for issue in batch.issues:
        assert_safe_issue(issue, "缺视频号", "缺链接", "无稳定键", "missinglink", "hqdefault", "watch?v=")

    by_guid = {record.item.guid: record for record in batch.records}
    quiet = by_guid["nostats0001"]
    named = by_guid["namespace01"]
    stars = by_guid["staronly001"]
    assert_video(quiet, guid="nostats0001", views=None)
    assert_video(named, guid="namespace01", views=42)
    assert_video(stars, guid="staronly001", views=None)
    assert quiet.asset.view_count is None
    assert stars.asset.view_count is None
    assert stars.asset.like_count is None
    assert quiet.item.title == "没有统计"
    assert quiet.asset.channel == "无统计频道"
    assert quiet.item.body_plain == "没有播放量"
    assert quiet.item.published_at == "2001-01-01T00:00:00.000000Z"
    assert quiet.item.url == "https://www.youtube.com/watch?v=nostats0001"
    assert named.item.title == "前缀无关"
    assert named.asset.channel == "前缀频道"
    assert named.item.body_plain == "命名空间正文"
    assert named.item.body_raw == "命名空间正文"
    assert named.item.published_at is None
    assert named.item.url == "https://www.youtube.com/watch?v=namespace01"
    assert stars.item.title == "只有星级"
    assert stars.asset.channel == "星级频道"
    assert stars.item.body_plain == ""
    assert stars.item.published_at is None
    assert stars.asset.like_count != 50
    assert stars.asset.view_count != 50
    urls = [record.item.url for record in batch.records]
    assert all("missinglink" not in url for url in urls)
    assert all("hqdefault" not in url for url in urls)
    assert all(not url.endswith(".jpg") for url in urls)
    assert all(record.item.title != "订阅标题" for record in batch.records)
    assert all(record.asset.channel != "订阅作者" for record in batch.records)


def test_youtube_never_fetches_media(monkeypatch: pytest.MonkeyPatch, tripwire: object) -> None:
    payload = load("feed.xml")
    extra = MEDIA_TRAP.encode("utf-8")
    counts = {"open": 0, "network": 0}
    start = len(tripwire.events)  # type: ignore[attr-defined]
    install_guards(monkeypatch, counts)
    try:
        batch = parse_youtube_feed(network_source(), payload, now=NOW)
        trapped = parse_youtube_feed(network_source(), extra, now=NOW)
    finally:
        monkeypatch.undo()
    assert counts == {"open": 0, "network": 0}
    assert tripwire.events[start:] == []  # type: ignore[attr-defined]
    assert len(batch.records) == 1
    assert batch.records[0].item.url == WATCH
    assert "hqdefault.jpg" not in batch.records[0].item.url
    assert "/v/view13931aa" not in batch.records[0].item.url
    assert batch.records[0].item.body_plain == PLAIN
    assert [record.item.guid for record in trapped.records] == ["goodmedia01", "afterpath01"]
    assert [issue.code for issue in trapped.issues] == ["frames_not_allowed"]
    good = trapped.records[0]
    assert_video(good, guid="goodmedia01", views=None)
    assert good.item.title == "看说明里的 clip.mp4 即可"
    assert good.item.url == "https://www.youtube.com/watch?v=goodmedia01"
    assert good.item.url.startswith("https://")
    assert not good.item.url.startswith("file:")
    assert "不是帧" in good.item.body_plain
    assert "file://" not in good.item.url
    assert trapped.records[1].item.title == "后面仍在"
    assert_safe_issue(trapped.issues[0], "wp11-do-not-open", "badpath0001", "hqdefault")
    assert all(not record.item.url.startswith("file:") for record in trapped.records)


def test_youtube_links_dates_and_counts() -> None:
    payload = EDGES.encode("utf-8")
    batch = parse_youtube_feed(network_source(), payload, now=NOW)
    assert [issue.code for issue in batch.issues] == [
        "invalid_count",
        "duplicate_guid",
        "invalid_count",
        "non_global_address",
        "invalid_title",
        "invalid_title",
    ]
    by_guid = {record.item.guid: record for record in batch.records}
    assert set(by_guid) == {
        "relative0001",
        "utmlink0001",
        "zeroviews01",
        "dupdupdupd1",
        "dupskip0001",
        "baddate0001",
        "naivedate01",
        "futuredate1",
        "mediabody01",
        "summaryonly",
        "feedauthor1",
        "norel000001",
        "mismatch001",
        "emptyviews1",
        "protorel001",
        "htmlpref001",
    }
    relative = by_guid["relative0001"]
    assert_video(relative, guid="relative0001", views=None)
    assert relative.item.url == "https://www.youtube.com/watch?v=relative0001"
    assert relative.item.published_at == "2024-03-01T12:30:00.000000Z"
    assert relative.asset.channel == "相对频道"
    tracked = by_guid["utmlink0001"]
    assert tracked.item.url == "https://www.YouTube.com/watch?v=utmlink0001&utm_source=wp11"
    assert tracked.item.canonical_url == "https://www.youtube.com/watch?v=utmlink0001"
    zeros = by_guid["zeroviews01"]
    assert_video(zeros, guid="zeroviews01", views=0)
    assert zeros.asset.view_count == 0
    assert zeros.asset.like_count is None
    assert zeros.asset.like_count != 8
    assert by_guid["dupdupdupd1"].item.title == "第一份"
    assert "第二份" not in {record.item.title for record in batch.records}
    kept = by_guid["dupskip0001"]
    assert kept.item.title == "保留的"
    assert_video(kept, guid="dupskip0001", views=5)
    assert by_guid["baddate0001"].item.published_at is None
    assert by_guid["naivedate01"].item.published_at is None
    assert by_guid["futuredate1"].item.published_at == "2099-01-01T00:00:00.000000Z"
    assert by_guid["mediabody01"].item.body_raw == "MEDIA ONLY"
    assert by_guid["mediabody01"].item.body_plain == "MEDIA ONLY"
    assert "ATOM ONLY" not in by_guid["mediabody01"].item.body_raw
    assert by_guid["summaryonly"].item.body_raw == ""
    assert by_guid["summaryonly"].item.body_plain == ""
    assert by_guid["feedauthor1"].asset.channel == "订阅作者"
    assert by_guid["norel000001"].item.url == "https://www.youtube.com/watch?v=norel000001"
    assert by_guid["mismatch001"].item.guid == "mismatch001"
    assert by_guid["mismatch001"].item.url == "https://www.youtube.com/watch?v=othermismatch"
    assert_video(by_guid["emptyviews1"], guid="emptyviews1", views=None)
    assert by_guid["protorel001"].item.url == "https://www.youtube.com/watch?v=protorel001"
    page = by_guid["htmlpref001"]
    assert page.item.url == "https://www.youtube.com/watch?v=htmlpref001"
    assert "feeds/videos.xml" not in page.item.url
    assert "loopback001" not in by_guid
    assert "blanktitle1" not in by_guid
    assert "notitle0001" not in by_guid
    assert "badviews001" not in by_guid
    assert all("127.0.0.1" not in record.item.url for record in batch.records)

    filed = parse_youtube_feed(file_source(), payload, now=NOW)
    filed_ids = {record.item.guid for record in filed.records}
    assert "relative0001" not in filed_ids
    assert "protorel001" not in filed_ids
    assert "htmlpref001" in filed_ids
    assert "utmlink0001" in filed_ids
    assert [issue.code for issue in filed.issues].count("invalid_url") == 2


def test_youtube_bad_document_fails(monkeypatch: pytest.MonkeyPatch, tripwire: object) -> None:
    payload = load("feed.xml")
    counts = {"open": 0, "network": 0}
    start = len(tripwire.events)  # type: ignore[attr-defined]
    install_guards(monkeypatch, counts)
    try:
        _expect_feed(b"", "invalid_feed")
        _expect_feed(b" \n\t", "invalid_feed")
        _expect_feed(
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>\xe5\x85\x88\xe5\x87\xba\xe7\x8e\xb0</title></entry><entry><title>\xe5\x9d\x8f</feed>',
            "invalid_feed",
        )
        _expect_feed(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>',
            "invalid_feed",
        )
        _expect_feed(
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>abcdefghijk</yt:videoId><title>t</title></entry></feed>',
            "invalid_feed",
        )
        _expect_feed(
            b'<entry xmlns="http://www.w3.org/2005/Atom"><title>x</title></entry>',
            "invalid_feed",
        )
        _expect_feed(b"tests/fixtures/youtube/feed.xml", "invalid_feed")
        _expect_feed(INTERNAL_DTD.encode("utf-8"), "dtd_forbidden")
        _expect_feed(EXTERNAL_DTD.encode("utf-8"), "dtd_forbidden")
        _expect_feed(
            (
                '<?xml version="1.0" encoding="UTF-16"?>\n'
                '<!DOCTYPE feed [<!ENTITY x "BOOM">]>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>\n'
            ).encode("utf-16"),
            "dtd_forbidden",
        )
        with pytest.raises(SchemaError) as payload_error:
            parse_youtube_feed(network_source(), SOURCE_URL, now=NOW)  # type: ignore[arg-type]
        assert payload_error.value.code == "invalid_payload"
        blog = Source(id="example-blog", kind="rss", url="https://example.com/rss.xml", title="博客")
        with pytest.raises(SchemaError) as source_error:
            parse_youtube_feed(blog, payload, now=NOW)
        assert source_error.value.code == "invalid_source"
        file_blog = Source(
            id="example-blog",
            kind="file",
            file_format="rss",
            url="file:tests/fixtures/youtube/feed.xml",
            title="博客",
        )
        with pytest.raises(SchemaError) as format_error:
            parse_youtube_feed(file_blog, payload, now=NOW)
        assert format_error.value.code == "invalid_source"
        with pytest.raises(SchemaError) as clock_error:
            parse_youtube_feed(network_source(), payload, now="2026-11-10T00:00:00Z")
        assert clock_error.value.code == "invalid_timestamp"
        literal = parse_youtube_feed(network_source(), LITERAL_DECLARATION.encode("utf-8"), now=NOW)
    finally:
        monkeypatch.undo()
    assert counts == {"open": 0, "network": 0}
    assert tripwire.events[start:] == []  # type: ignore[attr-defined]
    assert len(literal.records) == 1
    record = literal.records[0]
    assert_video(record, guid="cdatadoc001", views=None)
    assert record.item.title == "字面声明"
    assert "<!DOCTYPE" in record.item.body_raw
    assert "BOOM" in record.item.body_raw
    assert record.item.title != "BOOM"


def _expect_feed(payload: bytes, code: str) -> None:
    with pytest.raises(FetchError) as caught:
        parse_youtube_feed(network_source(), payload, now=NOW)
    assert caught.value.code == code
    assert caught.value.status is None
    text = f"{caught.value} {caught.value!r}"
    assert text == f"{code} FetchError({code!r})"
    assert "BOOM" not in text
    assert "passwd" not in text
    assert "先出现" not in text
