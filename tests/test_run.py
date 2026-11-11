"""One run: lock, fetch, classify, publish, and recover.

HTTP and the model are fakes. Nothing in this file opens a socket, starts
grok, or reads a secret. Crash points that the orchestrator does not expose
as hooks are simulated on the publisher and store methods it already calls.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from second_brain.consensus.minority import find_minority
from second_brain.errors import ConcurrentRun, GrokUnavailable, PublishError, StorageError
from second_brain.http import HttpClient
from second_brain.models import (
    AppConfig,
    CompletionResult,
    HttpResponse,
    KeyPolicy,
    LlmEvent,
    RuntimePolicy,
    Source,
    Usage,
    format_timestamp,
    item_id,
)
from second_brain.run import BudgetedCompletion, run_once
from second_brain.store import Store
from second_brain.wiki.compile import EMPTY_DELTA, WikiPublisher

PREFERENCES = "只保留合成材料"
QUOTE = "需求将同比增长"
CLAIM_KEY = "机构A|需求同比增长|2030年度|业务B"
FOR_SENTENCE = "2030年度业务B需求将同比增长"
AGAINST_SENTENCE = "2030年度业务B需求不会同比增长"
TITLE_QUOTE = "作者认为需求将增长"
COMMENT_QUOTE = "观众认为不会增长"
EARLY = format_datetime(datetime(2030, 1, 2, tzinfo=timezone.utc))
DAY = format_datetime(datetime(2030, 1, 10, tzinfo=timezone.utc))
STAMP = "2030-01-10T00:00:00.000000Z"
EMPTY_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Synthetic</title>
    <link>https://a.example/</link>
    <description>empty</description>
  </channel>
</rss>
"""


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2030, 1, 15, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError(seconds)
        self._now += timedelta(seconds=float(seconds))

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=float(seconds))


class IdFactory:
    def __init__(self) -> None:
        self._n = 0

    def __call__(self) -> str:
        self._n += 1
        return f"run-{self._n}"


class FakeComplete:
    """Classify from the prompt. Review responses stay invalid on purpose."""

    def __init__(self) -> None:
        self.mode = "plain"
        self.fail_at: int | None = None
        self.prompts: list[str] = []
        self.calls: list[str] = []
        self.classify_calls = 0

    def __call__(self, prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
        if not isinstance(schema, dict):
            raise AssertionError(type(schema))
        self.prompts.append(prompt)
        self.calls.append(purpose)
        if purpose == "review":
            return CompletionResult(
                payload={"topics": []},
                usage=Usage(),
                stop_reason="end_turn",
                billing_path="test",
            )
        self.classify_calls += 1
        if self.fail_at is not None and self.classify_calls >= self.fail_at:
            raise GrokUnavailable("grok_down", usage=Usage(input_tokens=4, output_tokens=1))
        parsed = json.loads(prompt)
        rows = [_row(item, self.mode) for item in parsed["items"]]
        return CompletionResult(
            payload={"items": rows},
            usage=Usage(input_tokens=8, output_tokens=2),
            stop_reason="end_turn",
            billing_path="test",
        )

    def classify_prompts(self) -> list[str]:
        return [prompt for prompt, purpose in zip(self.prompts, self.calls) if purpose == "classify"]


class MapTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.by_url: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.scripts: list[tuple[int, dict[str, str], bytes]] = []

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_s: int,
        max_bytes: int,
        purpose: str,
    ) -> HttpResponse:
        del timeout_s, max_bytes, purpose
        self.calls.append(url)
        self.headers.append(dict(headers))
        if self.scripts:
            status, response_headers, body = self.scripts.pop(0)
        else:
            status, response_headers, body = self.by_url[url]
        return HttpResponse(status=status, body=body, headers=response_headers, url=url)


class BoomTransport:
    def request(self, url: str, **_kwargs: object) -> HttpResponse:
        raise AssertionError(url)


class Lab:
    def __init__(self, tmp_path: Path) -> None:
        self.wiki = tmp_path / "wiki"
        self.raw = tmp_path / "raw"
        self.lock = tmp_path / "data" / "run.lock"
        self.wiki.mkdir()
        self.raw.mkdir()
        self.lock.parent.mkdir()
        self.store = Store.connect(str(tmp_path / "brain.sqlite"))
        self.clock = FakeClock()
        self.ids = IdFactory()
        self.files: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.complete = FakeComplete()

    def close(self) -> None:
        self.store.close()

    def reader(self, uri: str) -> bytes:
        self.reads.append(uri)
        if uri not in self.files:
            raise AssertionError(uri)
        return self.files[uri]

    def http(self, transport: object) -> HttpClient:
        return HttpClient(transport, self.clock, self.store, min_interval_s=0)

    def run(
        self,
        sources: list[Source],
        *,
        offline: bool = True,
        http: HttpClient | None = None,
    ):
        config = AppConfig(
            sources=tuple(sources),
            preferences=PREFERENCES,
            keys=KeyPolicy(),
            runtime=RuntimePolicy(offline=offline),
        )
        return run_once(
            self.store,
            config=config,
            clock=self.clock,
            run_id_factory=self.ids,
            complete_fn=self.complete,
            wiki_dir=self.wiki,
            raw_dir=self.raw,
            lock_path=self.lock,
            http=http,
            fixture_reader=self.reader,
        )


def _row(item: dict, mode: str) -> dict:
    claims: list[dict] = []
    if mode == "body":
        if QUOTE not in item["body"]:
            raise AssertionError(item["body"])
        claims.append(
            {
                "claim_key": CLAIM_KEY,
                "sentence": FOR_SENTENCE,
                "stance": "for",
                "evidence_kind": "body",
                "evidence_quote": QUOTE,
                "comment_index": None,
            }
        )
    elif mode == "comments":
        audience = item["audience"]
        if not audience or audience[0] != COMMENT_QUOTE or item["title"] != TITLE_QUOTE:
            raise AssertionError(item)
        claims.append(
            {
                "claim_key": CLAIM_KEY,
                "sentence": FOR_SENTENCE,
                "stance": "for",
                "evidence_kind": "title",
                "evidence_quote": TITLE_QUOTE,
                "comment_index": None,
            }
        )
        claims.append(
            {
                "claim_key": CLAIM_KEY,
                "sentence": AGAINST_SENTENCE,
                "stance": "against",
                "evidence_kind": "comment",
                "evidence_quote": COMMENT_QUOTE,
                "comment_index": 0,
            }
        )
    return {
        "id": item["id"],
        "topic": "需求",
        "bucket": "must",
        "reason": "分档依据",
        "claims": claims,
    }


def file_source(source_id: str, uri: str, *, kind: str, title: str) -> Source:
    return Source(id=source_id, kind="file", url=uri, title=title, file_format=kind)


def rss_bytes(items: list[tuple[str, str, str, str]], *, published: str = DAY) -> bytes:
    rows = []
    for guid, title, url, body in items:
        rows.append(
            "\n".join(
                (
                    "    <item>",
                    f"      <title>{title}</title>",
                    f"      <link>{url}</link>",
                    f"      <guid isPermaLink=\"false\">{guid}</guid>",
                    f"      <pubDate>{published}</pubDate>",
                    f"      <content:encoded><![CDATA[<p>{body}</p>]]></content:encoded>",
                    "    </item>",
                )
            )
        )
    body = "\n".join(rows)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Synthetic</title>
    <link>https://example.com/</link>
    <description>synthetic</description>
{body}
  </channel>
</rss>
"""
    return xml.encode("utf-8")


def video_bytes(source_name: str) -> bytes:
    payload = {
        "items": [
            {
                "guid": "clip",
                "url": f"https://example.com/videos/{source_name}",
                "published_at": STAMP,
                "title": TITLE_QUOTE,
                "channel": source_name,
                "like_count": 4,
                "comment_count": 6,
                "view_count": 20,
                "comments": [
                    COMMENT_QUOTE,
                    "观众二",
                    "观众三",
                    "观众四",
                    "观众五",
                    "第六条不进入提示",
                ],
                "frame_paths": [],
            }
        ]
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def prompt_ids(prompts: list[str]) -> list[str]:
    found: list[str] = []
    for prompt in prompts:
        for item in json.loads(prompt)["items"]:
            found.append(item["id"])
    return found


def section(html: str, heading: str) -> str:
    marker = f"<h2>{heading}</h2>"
    start = html.index(marker) + len(marker)
    rest = html[start:]
    nxt = rest.find("<h2>")
    return rest if nxt < 0 else rest[:nxt]


def digest(lab: Lab, run_id: str) -> str:
    return (lab.wiki / "digest" / f"{run_id}.html").read_text(encoding="utf-8")


def cursor(lab: Lab) -> str | None:
    return lab.store.get_meta("last_classified_at")


def run_row(lab: Lab, run_id: str) -> tuple[str, str]:
    row = lab.store._conn.execute(
        "SELECT status, started_at FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    return row["status"], row["started_at"]


def llm_rows(lab: Lab, run_id: str) -> list[tuple[str, str]]:
    rows = lab.store._conn.execute(
        "SELECT purpose, status FROM llm_calls WHERE run_id=? ORDER BY rowid",
        (run_id,),
    ).fetchall()
    return [(row["purpose"], row["status"]) for row in rows]


def llm_count(lab: Lab) -> int:
    return lab.store._conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]


def test_classifies_only_unclassified(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/blog.xml"
    source = file_source("blog", uri, kind="rss", title="合成博客")
    lab.files[uri] = rss_bytes(
        [
            ("stable", "稳定旧文", "https://example.com/posts/stable", "稳定正文不改"),
            ("revise", "改稿旧文", "https://example.com/posts/revise", "改稿前的正文"),
        ],
        published=EARLY,
    )
    try:
        first = lab.run([source])
        assert first.status == "success"
        assert first.warnings == ()
        stable = item_id("blog", "stable")
        revise = item_id("blog", "revise")
        assert set(prompt_ids(lab.complete.classify_prompts())) == {stable, revise}
        seen = cursor(lab)
        assert seen == run_row(lab, first.run_id)[1]
        lab.clock.advance(60)
        before = len(lab.complete.classify_prompts())
        lab.files[uri] = rss_bytes(
            [
                ("stable", "稳定旧文", "https://example.com/posts/stable", "稳定正文不改"),
                ("revise", "改稿旧文", "https://example.com/posts/revise", "改稿后的正文"),
                ("new-a", "新增甲", "https://example.com/posts/new-a", "新增甲的正文"),
                ("new-b", "新增乙", "https://example.com/posts/new-b", "新增乙的正文"),
            ],
            published=EARLY,
        )
        second = lab.run([source])
        sent = prompt_ids(lab.complete.classify_prompts()[before:])
        assert set(sent) == {revise, item_id("blog", "new-a"), item_id("blog", "new-b")}
        assert stable not in sent
        assert second.inserted == 2
        assert second.updated == 1
        assert second.classified == 3
        assert second.status == "success"
        assert cursor(lab) == run_row(lab, second.run_id)[1]
        assert seen < cursor(lab)
    finally:
        lab.close()


def test_skips_pending_transcript(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/pod.json"
    source = file_source("pod", uri, kind="podcast_json", title="合成播客")
    lab.files[uri] = json.dumps(
        {
            "items": [
                {
                    "guid": "ready-ep",
                    "url": "https://example.com/pod/ready",
                    "title": "已有文稿",
                    "published_at": STAMP,
                    "transcript": f"这里写{QUOTE}。",
                    "audio_url": "https://cdn.example.com/ready.mp3",
                    "transcript_url": None,
                },
                {
                    "guid": "audio-ep",
                    "url": "https://example.com/pod/audio",
                    "title": "只有音频",
                    "published_at": STAMP,
                    "transcript": None,
                    "audio_url": "https://cdn.example.com/audio.mp3",
                    "transcript_url": None,
                },
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    lab.complete.mode = "body"
    try:
        report = lab.run([source], http=lab.http(BoomTransport()))
        ready = item_id("pod", "ready-ep")
        pending = item_id("pod", "audio-ep")
        assert prompt_ids(lab.complete.classify_prompts()) == [ready]
        assert report.pending == 1
        assert report.classified == 1
        assert report.status == "success"
        claims = lab.store.get_current_claims(now=format_timestamp(lab.clock.now()))
        assert [claim.item_id for claim in claims] == [ready]
        assert pending not in {claim.item_id for claim in claims}
        vote = find_minority(claims)
        assert vote.opportunities == ()
        assert [(row.n_items, row.n_sources) for row in vote.assessments] == [(1, 1)]
        page = digest(lab, report.run_id)
        assert "只有音频" in section(page, "待转写")
        assert "已有文稿" not in section(page, "待转写")
        assert "已有文稿" in section(page, "必读")
        assert "只有音频" not in section(page, "必读")
        assert lab.reads == [uri]
        assert all(not read.endswith(".mp3") and "ffmpeg" not in read for read in lab.reads)
    finally:
        lab.close()


def test_grok_failure_keeps_items_and_cursor(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/grok.xml"
    source = file_source("grok", uri, kind="rss", title="合成失败")
    lab.files[uri] = rss_bytes([("old", "旧成功", "https://example.com/grok/old", "旧成功正文")])
    try:
        first = lab.run([source])
        assert first.status == "success"
        kept = cursor(lab)
        assert kept is not None
        lab.clock.advance(60)
        rows = [("old", "旧成功", "https://example.com/grok/old", "旧成功正文")]
        for index in range(1, 10):
            rows.append(
                (f"new-{index}", f"新条目{index}", f"https://example.com/grok/{index}", f"新正文{index}")
            )
        lab.files[uri] = rss_bytes(rows)
        lab.complete.fail_at = lab.complete.classify_calls + 2
        before = len(lab.complete.classify_prompts())
        second = lab.run([source])
        sent = lab.complete.classify_prompts()[before:]
        assert len(sent) == 2
        assert second.status == "partial"
        assert second.classified == 8
        assert len(second.failed_item_ids) == 1
        assert "模型不可用，未完成分类" in second.warnings
        assert cursor(lab) == kept
        assert run_row(lab, second.run_id)[0] == "partial"
        assert lab.store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 10
        assert lab.store._conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 9
        queued = {item.id for item in lab.store.items_for_classify(now=format_timestamp(lab.clock.now()))}
        assert queued == set(second.failed_item_ids)
        assert llm_rows(lab, second.run_id) == [("classify", "ok"), ("classify", "unavailable")]
        unavailable = [
            event
            for event in lab.store.events_for_run(second.run_id)
            if isinstance(event, LlmEvent) and event.status == "unavailable"
        ]
        assert len(unavailable) == 1
        assert unavailable[0].usage is not None
        assert unavailable[0].usage.input_tokens == 4
        page = digest(lab, second.run_id)
        assert second.failed_item_ids[0] and "待分类" in page
        seen: list[str] = []

        def boom(prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
            del prompt, schema
            seen.append(purpose)
            raise GrokUnavailable("grok_down", usage=Usage(input_tokens=2))

        lab.store.begin_run("latch", started_at=format_timestamp(lab.clock.now()))
        wrapper = BudgetedCompletion(boom, RuntimePolicy(), lab.store, "latch", lab.clock)
        with pytest.raises(GrokUnavailable):
            wrapper("{}", schema={}, purpose="classify")
        with pytest.raises(GrokUnavailable):
            wrapper("{}", schema={}, purpose="review")
        assert seen == ["classify"]
        latched = [event for event in lab.store.events_for_run("latch") if isinstance(event, LlmEvent)]
        assert [(event.purpose, event.status) for event in latched] == [("classify", "unavailable")]
        assert latched[0].usage is not None and latched[0].usage.input_tokens == 2
        lab.store.fail_run("latch", finished_at=format_timestamp(lab.clock.now()), code="grok_down")
    finally:
        lab.close()


def test_rate_limit_skips_one_source(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    kept_uri = "file:tests/fixtures/run/kept.xml"
    file_uri = "file:tests/fixtures/run/ok.xml"
    kept = file_source("kept", kept_uri, kind="rss", title="已有来源")
    fixture = file_source("b-file", file_uri, kind="rss", title="可归档来源")
    network = Source(id="a-net", kind="rss", url="https://a.example/feed.xml", title="限流来源")
    lab.files[kept_uri] = rss_bytes([("old", "已有游标", "https://example.com/kept/old", "已有正文")])
    lab.files[file_uri] = rss_bytes([("b-item", "乙可归档", "https://example.com/file/b", "乙的正文")])
    transport = MapTransport()
    transport.by_url["https://a.example/feed.xml"] = (
        429,
        {"content-type": "text/plain", "retry-after": "30"},
        b"slow",
    )
    try:
        first = lab.run([kept])
        assert first.status == "success"
        marked = cursor(lab)
        lab.clock.advance(60)
        before = len(lab.complete.classify_prompts())
        second = lab.run([kept, fixture, network], offline=False, http=lab.http(transport))
        assert transport.calls == ["https://a.example/feed.xml"]
        assert second.failed_source_ids == ("a-net",)
        assert second.status == "partial"
        assert second.inserted == 1
        assert "来源取数失败：a-net" in second.warnings
        assert cursor(lab) == marked
        sent = set(prompt_ids(lab.complete.classify_prompts()[before:]))
        assert sent == {item_id("b-file", "b-item")}
        published = lab.store._conn.execute(
            """
            SELECT c.published_run_id
            FROM classifications c
            JOIN items i ON i.id = c.item_id
            WHERE i.guid = 'b-item'
            """
        ).fetchone()
        assert published["published_run_id"] == second.run_id
        page = digest(lab, second.run_id)
        assert "乙可归档" in section(page, "必读")
        assert "a-net" in page
        assert run_row(lab, second.run_id)[0] == "partial"
    finally:
        lab.close()


def test_budget_stops_before_spawn(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/budget.xml"
    source = file_source("budget", uri, kind="rss", title="预算来源")
    items = [
        (f"n{index:02d}", f"条目{index:02d}", f"https://example.com/budget/{index:02d}", f"预算正文{index:02d}")
        for index in range(1, 26)
    ]
    lab.files[uri] = rss_bytes(items)
    try:
        first = lab.run([source])
        first_ids = prompt_ids(lab.complete.classify_prompts())
        assert lab.complete.classify_calls == 3
        assert len(first_ids) == 24
        assert first.classified == 24
        assert first.deferred == 1
        assert first.status == "partial"
        assert "分类预算已用完，其余延后" in first.warnings
        assert cursor(lab) is None
        assert llm_rows(lab, first.run_id) == [("classify", "ok")] * 3
        deferred = {
            item.id for item in lab.store.items_for_classify(now=format_timestamp(lab.clock.now()))
        }
        assert deferred.isdisjoint(first_ids)
        assert len(deferred) == 1
        states = {
            row["state"]: row["total"]
            for row in lab.store._conn.execute(
                "SELECT state, COUNT(*) AS total FROM run_items WHERE run_id=? GROUP BY state",
                (first.run_id,),
            )
        }
        assert states == {"published": 24, "deferred": 1}
        assert states["deferred"] == 1
        calls_before = lab.complete.classify_calls
        ledger = llm_count(lab)
        second = lab.run([source])
        resent = prompt_ids(lab.complete.classify_prompts()[3:])
        assert lab.complete.classify_calls == calls_before + 1
        assert set(resent) == deferred
        assert set(resent).isdisjoint(first_ids)
        assert second.classified == 1
        assert second.deferred == 0
        assert second.status == "success"
        assert llm_count(lab) == ledger + 1
        assert lab.store.items_for_classify(now=format_timestamp(lab.clock.now())) == ()
        assert cursor(lab) == run_row(lab, second.run_id)[1]
    finally:
        lab.close()


def test_publish_failure_replays_without_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/stage.xml"
    source = file_source("stage", uri, kind="rss", title="发布来源")
    lab.files[uri] = rss_bytes([("one", "待补发", "https://example.com/stage/one", "待补发正文")])
    original = WikiPublisher.stage
    calls = {"n": 0}

    def stage(self: WikiPublisher, **kwargs: object):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PublishError("publish_io")
        return original(self, **kwargs)

    monkeypatch.setattr(WikiPublisher, "stage", stage)
    try:
        failed = lab.run([source])
        assert failed.status == "failed"
        assert failed.html_path is None
        assert failed.classified == 1
        assert "发布未完成" in failed.warnings
        assert cursor(lab) is None
        assert run_row(lab, failed.run_id)[0] == "failed"
        assert lab.store.items_for_classify(now=format_timestamp(lab.clock.now())) == ()
        assert len(lab.store.get_unpublished_classified(now=format_timestamp(lab.clock.now()))) == 1
        spent = lab.complete.classify_calls
        ledger = llm_count(lab)
        lab.clock.advance(60)
        replayed = lab.run([source])
        assert lab.complete.classify_calls == spent
        assert llm_count(lab) == ledger
        assert replayed.status == "success"
        assert replayed.classified == 0
        assert "待补发" in section(digest(lab, replayed.run_id), "必读")
        assert lab.store.get_unpublished_classified(now=format_timestamp(lab.clock.now())) == ()
        published = lab.store._conn.execute(
            "SELECT published_run_id FROM classifications"
        ).fetchone()
        assert published["published_run_id"] == replayed.run_id
        assert cursor(lab) == run_row(lab, replayed.run_id)[1]
    finally:
        lab.close()


def test_recover_after_index_before_db_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/index.xml"
    source = file_source("index", uri, kind="rss", title="索引来源")
    lab.files[uri] = rss_bytes([("one", "索引前正文", "https://example.com/index/one", "索引前的句子")])
    original = Store.finish_publication
    calls = {"n": 0}

    def finish(self: Store, publication: object, *, finished_at: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageError("io")
        return original(self, publication, finished_at=finished_at)

    monkeypatch.setattr(Store, "finish_publication", finish)
    try:
        with pytest.raises(StorageError) as caught:
            lab.run([source])
        assert caught.value.code == "io"
        assert calls["n"] == 1
        crashed = "run-1"
        assert run_row(lab, crashed)[0] == "publishing"
        assert "索引前正文" in digest(lab, crashed)
        index = (lab.wiki / "index.md").read_text(encoding="utf-8")
        assert f"当前run_id: {crashed}" in index
        assert f"当前摘要: digest/{crashed}.html" in index
        assert cursor(lab) is None
        published = lab.store._conn.execute(
            "SELECT published_run_id FROM classifications"
        ).fetchone()
        assert published["published_run_id"] is None
        spent = lab.complete.classify_calls
        ledger = llm_count(lab)
        assert ledger == 1
        lab.clock.advance(60)
        recovered = lab.run([source])
        assert lab.complete.classify_calls == spent
        assert llm_count(lab) == ledger
        assert run_row(lab, crashed)[0] == "success"
        published = lab.store._conn.execute(
            "SELECT published_run_id FROM classifications"
        ).fetchone()
        assert published["published_run_id"] == crashed
        assert "索引前正文" in digest(lab, crashed)
        replay = digest(lab, recovered.run_id)
        assert EMPTY_DELTA in replay
        assert "索引前正文" not in replay
        assert recovered.classified == 0
        assert recovered.status == "success"
        assert lab.lock.exists()
        assert not lab.lock.is_symlink()
    finally:
        lab.close()


def test_canonical_duplicate_integration(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    alpha_uri = "file:tests/fixtures/run/alpha.xml"
    beta_uri = "file:tests/fixtures/run/beta.xml"
    alpha = file_source("alpha", alpha_uri, kind="rss", title="来源甲")
    beta = file_source("beta", beta_uri, kind="rss", title="来源乙")
    shared = "https://example.com/shared/article"
    lab.files[alpha_uri] = rss_bytes(
        [("alpha-guid", "甲文", shared, f"来源甲记录{QUOTE}")]
    )
    lab.files[beta_uri] = rss_bytes(
        [("beta-guid", "乙文", shared, f"来源乙记录{QUOTE}")]
    )
    lab.complete.mode = "body"
    try:
        first = lab.run([alpha, beta])
        assert first.inserted == 2
        assert first.classified == 1
        rows = list(
            lab.store._conn.execute(
                "SELECT id, source_id, duplicate_of FROM items ORDER BY source_id"
            )
        )
        assert len(rows) == 2
        primary = next(row for row in rows if row["duplicate_of"] is None)
        alias = next(row for row in rows if row["duplicate_of"] is not None)
        assert alias["duplicate_of"] == primary["id"]
        assert prompt_ids(lab.complete.classify_prompts()) == [primary["id"]]
        now = format_timestamp(lab.clock.now())
        current = lab.store.get_current_classified(now=now)
        assert [item.id for item in current] == [primary["id"]]
        claims = lab.store.get_current_claims(now=now)
        assert [claim.source_id for claim in claims] == [primary["source_id"]]
        old_ids = {claim.id for claim in claims}
        vote = find_minority(claims)
        assert vote.opportunities == ()
        assert [(row.status, row.n_items, row.n_sources, row.n_for) for row in vote.assessments] == [
            ("insufficient", 1, 1, 1)
        ]
        survivor = beta if primary["source_id"] == "alpha" else alpha
        lab.clock.advance(60)
        before = len(lab.complete.classify_prompts())
        second = lab.run([survivor])
        assert second.classified == 1
        sent = prompt_ids(lab.complete.classify_prompts()[before:])
        assert sent == [alias["id"]]
        now = format_timestamp(lab.clock.now())
        promoted = lab.store.get_current_classified(now=now)
        assert [item.id for item in promoted] == [alias["id"]]
        assert [item.source_id for item in promoted] == [alias["source_id"]]
        fresh = lab.store.get_current_claims(now=now)
        assert {claim.id for claim in fresh}.isdisjoint(old_ids)
        assert [claim.item_id for claim in fresh] == [alias["id"]]
        leftover = lab.store._conn.execute(
            "SELECT COUNT(*) FROM claims WHERE item_id=?",
            (primary["id"],),
        ).fetchone()[0]
        assert leftover == 0
        again = find_minority(fresh)
        assert [(row.status, row.n_items, row.n_sources) for row in again.assessments] == [
            ("insufficient", 1, 1)
        ]
        graph = list(lab.store._conn.execute("SELECT id, duplicate_of FROM items"))
        assert sum(row["duplicate_of"] is None for row in graph) == 1
        promoted_row = next(row for row in graph if row["id"] == alias["id"])
        demoted_row = next(row for row in graph if row["id"] == primary["id"])
        assert promoted_row["duplicate_of"] is None
        assert demoted_row["duplicate_of"] == alias["id"]
    finally:
        lab.close()


def test_comments_and_review_end_to_end(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    sources = []
    for name in ("vid-a", "vid-b", "vid-c"):
        uri = f"file:tests/fixtures/run/{name}.json"
        lab.files[uri] = video_bytes(name)
        sources.append(file_source(name, uri, kind="video_json", title=f"频道{name}"))
    lab.complete.mode = "comments"
    try:
        report = lab.run(sources)
        prompts = lab.complete.classify_prompts()
        assert len(prompts) == 1
        parsed = json.loads(prompts[0])
        assert len(parsed["items"]) == 3
        for item in parsed["items"]:
            assert item["audience"] == ["观众认为不会增长", "观众二", "观众三", "观众四", "观众五"]
            assert "第六条不进入提示" not in prompts[0]
            assert "--- audience ---" in parsed["instructions"]
        assert lab.complete.calls.count("review") == 1
        assert report.status == "success"
        assert "短评不可用" in report.warnings
        now = format_timestamp(lab.clock.now())
        claims = lab.store.get_current_claims(now=now)
        assert len(claims) == 6
        comments = [claim for claim in claims if claim.evidence_kind == "comment"]
        titles = [claim for claim in claims if claim.evidence_kind == "title"]
        assert len(comments) == 3 and {claim.stance for claim in comments} == {"against"}
        assert len(titles) == 3 and {claim.stance for claim in titles} == {"for"}
        vote = find_minority(claims)
        assert vote.opportunities == ()
        assert [(row.status, row.n_for, row.n_against, row.n_items, row.n_sources) for row in vote.assessments] == [
            ("one_sided", 3, 0, 3, 3)
        ]
        page = digest(lab, report.run_id)
        assert "未出现对立立场" in page
        assert "来源自身冲突，已排除" not in page
        assert "短评不可用" in page
        assert "观众认为不会增长" in page
        opportunity_dir = lab.wiki / "snapshots" / report.run_id / "opportunities"
        assert list(opportunity_dir.glob("*.md")) == []
        topics = list((lab.wiki / "snapshots" / report.run_id / "topics").glob("*.md"))
        assert len(topics) == 1
        text = topics[0].read_text(encoding="utf-8")
        assert "多源对照" in text
        assert "观众摘句" in text
        assert COMMENT_QUOTE in text
        assert TITLE_QUOTE in text
        for name in ("vid-a", "vid-b", "vid-c"):
            assert name in text
        assert report.html_path == f"digest/{report.run_id}.html"
        assert (lab.wiki / report.html_path).is_file()
    finally:
        lab.close()


def test_two_runs_are_mutually_exclusive(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/lock.xml"
    network = Source(id="a-net", kind="rss", url="https://a.example/feed.xml", title="锁网络")
    local = file_source("b-file", uri, kind="rss", title="锁文件")
    lab.files[uri] = rss_bytes([("locked", "锁内条目", "https://example.com/lock/one", "锁内正文")])
    transport = MapTransport()
    transport.by_url["https://a.example/feed.xml"] = (200, {"content-type": "text/xml"}, EMPTY_RSS)
    client = lab.http(transport)
    gate = threading.Event()
    release = threading.Event()
    box: list[BaseException] = []
    held: dict[str, int] = {}

    def reader(relative: str) -> bytes:
        held["ino"] = lab.lock.stat().st_ino
        gate.set()
        assert release.wait(5)
        return lab.reader(relative)

    def other() -> None:
        try:
            assert gate.wait(5)
            http_before = len(transport.calls)
            llm_before = len(lab.complete.calls)
            with pytest.raises(ConcurrentRun) as caught:
                run_once(
                    lab.store,
                    config=AppConfig(
                        sources=(network, local),
                        preferences=PREFERENCES,
                        keys=KeyPolicy(),
                        runtime=RuntimePolicy(offline=False),
                    ),
                    clock=lab.clock,
                    run_id_factory=IdFactory(),
                    complete_fn=lab.complete,
                    wiki_dir=lab.wiki,
                    raw_dir=lab.raw,
                    lock_path=lab.lock,
                    http=client,
                    fixture_reader=reader,
                )
            assert caught.value.code == "run_locked"
            assert len(transport.calls) == http_before
            assert len(lab.complete.calls) == llm_before
        except BaseException as exc:
            box.append(exc)
        finally:
            release.set()

    worker = threading.Thread(target=other)
    worker.start()
    try:
        report = run_once(
            lab.store,
            config=AppConfig(
                sources=(network, local),
                preferences=PREFERENCES,
                keys=KeyPolicy(),
                runtime=RuntimePolicy(offline=False),
            ),
            clock=lab.clock,
            run_id_factory=lab.ids,
            complete_fn=lab.complete,
            wiki_dir=lab.wiki,
            raw_dir=lab.raw,
            lock_path=lab.lock,
            http=client,
            fixture_reader=reader,
        )
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert box == []
        assert report.status == "success"
        assert transport.calls == ["https://a.example/feed.xml"]
        assert held["ino"] == lab.lock.stat().st_ino
        again = lab.run([network, local], offline=False, http=client)
        assert again.status == "success"
        assert lab.lock.exists()
        assert not lab.lock.is_symlink()
        assert lab.lock.stat().st_ino == held["ino"]
    finally:
        release.set()
        worker.join(timeout=10)
        lab.close()


def test_log_failure_is_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = Lab(tmp_path)
    uri = "file:tests/fixtures/run/log.xml"
    source = file_source("log", uri, kind="rss", title="日志来源")
    lab.files[uri] = rss_bytes([("one", "日志条目", "https://example.com/log/one", "日志正文")])
    original = WikiPublisher.append_events
    calls = {"n": 0}

    def append(self: WikiPublisher, run_id: str, events: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PublishError("publish_io")
        return original(self, run_id, events)

    monkeypatch.setattr(WikiPublisher, "append_events", append)
    try:
        first = lab.run([source])
        assert first.status == "success"
        assert "日志尚未写入" in first.warnings
        assert cursor(lab) == run_row(lab, first.run_id)[1]
        assert run_row(lab, first.run_id)[0] == "success"
        assert lab.store.pending_log_runs() == (first.run_id,)
        log_path = lab.wiki / "log.md"
        assert not log_path.exists()
        event = next(
            event
            for event in lab.store.events_for_run(first.run_id)
            if isinstance(event, LlmEvent) and event.status == "ok"
        )
        ledger = llm_count(lab)
        assert ledger == 1
        spent = lab.complete.classify_calls
        lab.clock.advance(60)
        second = lab.run([source])
        assert lab.complete.classify_calls == spent
        assert llm_count(lab) == ledger
        assert first.run_id not in lab.store.pending_log_runs()
        assert second.run_id not in lab.store.pending_log_runs()
        assert run_row(lab, first.run_id)[0] == "success"
        assert cursor(lab) == run_row(lab, second.run_id)[1]
        assert cursor(lab) > run_row(lab, first.run_id)[1]
        text = log_path.read_text(encoding="utf-8")
        assert text.count(f"<!-- BEGIN run_id={first.run_id} ") == 1
        assert text.count(event.call_id) == 1
        assert second.status == "success"
        published = lab.store._conn.execute(
            "SELECT published_run_id FROM classifications"
        ).fetchone()
        assert published["published_run_id"] == first.run_id
    finally:
        lab.close()


def test_not_modified_keeps_the_feed(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    source = Source(id="feed", kind="rss", url="https://news.example/feed.xml", title="合成网络")
    body = rss_bytes([("one", "网络文", "https://news.example/posts/one", "网络正文")])
    transport = MapTransport()
    transport.scripts = [
        (200, {"content-type": "text/xml", "etag": '"v1"'}, body),
        (304, {"etag": '"v1"'}, b""),
    ]
    try:
        first = lab.run([source], offline=False, http=lab.http(transport))
        assert first.status == "success"
        assert first.inserted == 1
        assert lab.complete.classify_calls == 1
        state = lab.store.get_source_state("feed")
        assert state.etag == '"v1"'
        assert state.last_http_status == 200
        lab.clock.advance(60)
        second = lab.run([source], offline=False, http=lab.http(transport))
        assert second.status == "success"
        assert second.failed_source_ids == ()
        assert second.inserted == 0
        assert second.classified == 0
        assert lab.complete.classify_calls == 1
        assert transport.headers[1]["If-None-Match"] == '"v1"'
        state = lab.store.get_source_state("feed")
        assert state.etag == '"v1"'
        assert state.last_http_status == 304
        assert lab.store._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert "网络文" in digest(lab, first.run_id)
    finally:
        lab.close()
