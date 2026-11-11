"""Wiki pages, atomic publish, and the append-only run log.

Pages are local HTML and Markdown. Untrusted text is escaped. Tests stay
inside tmp_path and do not open sockets, fixtures over HTTP, or a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from second_brain.consensus.minority import find_minority
from second_brain.errors import IdentityCollision, PublishError, SchemaError
from second_brain.models import (
    Asset,
    ClassifiedItem,
    ConsensusResult,
    FetchEvent,
    LlmEvent,
    Usage,
    build_item,
    claim_slug,
    hash_url,
    make_claim,
    make_ingest_record,
    redact_url,
    topic_slug,
)
from second_brain.review.multi_source import build_topic_reviews
from second_brain.wiki.compile import WikiPublisher

NOW = "2030-01-15T00:00:00.000000Z"
LATER = "2030-01-16T00:00:00.000000Z"
DAY = "2030-01-10T00:00:00.000000Z"
FUTURE = "2030-02-01T00:00:00.000000Z"
POLICY = "ab" * 32
KEY = "机构A|需求同比增长|2030年度|业务B"
FOR = "2030年度业务B需求将同比增长"
AGAINST = "2030年度业务B需求不会同比增长"
BODY = "BODY-SENTINEL-9f3a"
SECRET = "sk-live-SUPERSECRET"
EMPTY = "本次没有新的已分档条目"
FOOTNOTE_PARTS = (
    "非共识页只对照库内句子的分布",
    "它不是市场结论，也不是买卖、仓位或目标价",
    "多数不等于事实，少数不等于机会",
    "无法用短评覆盖具体事实的证据链",
)
ASSESSMENT_LABELS = {
    "insufficient": "样本不足",
    "disputed": "争议（未写机会）",
    "single_source": "单源，未写机会",
    "one_sided": "未出现对立立场",
    "conflicted": "来源自身冲突，已排除",
}
HEADINGS = ("必读", "推荐阅读", "不推荐", "待转写", "日期待确认", "待分类/本次未完成", "非共识", "说明", "脚注")
_SERIAL = 0


def _next() -> int:
    global _SERIAL
    _SERIAL += 1
    return _SERIAL


def _reset() -> None:
    global _SERIAL
    _SERIAL = 0


def _record(
    source: str,
    *,
    title: str,
    body: str = "正文",
    published_at: str | None = DAY,
    comments: tuple[str, ...] = (),
    fetched_at: str = NOW,
) -> Item:
    serial = _next()
    item = build_item(
        source_id=source,
        guid=f"guid-{serial}",
        url=f"https://example.com/{source}/{serial}",
        title=title,
        fetched_at=fetched_at,
        modality="text",
        body_raw=body,
        body_plain=body,
        published_at=published_at,
    )
    asset = Asset(item_id=item.id, transcript_status="not_applicable", top_comments=comments)
    return make_ingest_record(item, asset).item


def _row(
    source: str,
    topic: str,
    *,
    title: str | None = None,
    bucket: str = "must",
    reason: str = "理由写在这里",
    comments: tuple[str, ...] = (),
    published_at: str | None = DAY,
    stance: str | None = None,
    sentence: str | None = None,
    claim_key: str = KEY,
    url: str | None = None,
) -> ClassifiedItem:
    serial = _next()
    item_id = f"{serial:032x}"
    analysis = f"{serial:064x}"
    shown = title if title is not None else f"标题{serial}"
    target = url or f"https://example.com/{source}/{serial}"
    claims = ()
    if stance is not None and sentence is not None:
        claims = (
            make_claim(
                item_id=item_id,
                source_id=source,
                analysis_hash=analysis,
                topic=topic,
                claim_key=claim_key,
                sentence=sentence,
                stance=stance,  # type: ignore[arg-type]
                evidence_kind="body",
                evidence_quote=sentence,
            ),
        )
    return ClassifiedItem(
        id=item_id,
        source_id=source,
        topic=topic,
        url=target,
        title=shown,
        bucket=bucket,  # type: ignore[arg-type]
        reason=reason,
        published_at=published_at,
        analysis_hash=analysis,
        policy_hash=POLICY,
        comments=comments,
        claims=claims,
    )


def _library(rows: list[ClassifiedItem]) -> tuple[TopicReview, ...]:
    return build_topic_reviews(rows)


def _section(html: str, heading: str) -> str:
    marker = f"<h2>{heading}</h2>"
    start = html.index(marker) + len(marker)
    rest = html[start:]
    nxt = rest.find("<h2>")
    return rest if nxt < 0 else rest[:nxt]


def _files(wiki: Path) -> dict[str, tuple[int, bytes]]:
    found: dict[str, tuple[int, bytes]] = {}
    if not wiki.exists():
        return found
    for path in sorted(wiki.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(wiki).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        found[rel] = (path.stat().st_mtime_ns, path.read_bytes())
    return found


def _manifest(wiki: Path, run_id: str) -> dict[str, object]:
    path = wiki / "snapshots" / run_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_complete(wiki: Path, run_id: str) -> None:
    data = _manifest(wiki, run_id)
    files = data["files"]
    assert isinstance(files, list) and files
    paths = []
    for entry in files:
        assert isinstance(entry, dict)
        rel = entry["path"]
        digest = entry["sha256"]
        assert isinstance(rel, str) and isinstance(digest, str)
        target = wiki / rel
        assert target.is_file()
        assert not target.is_symlink()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
        paths.append(rel)
    assert f"digest/{run_id}.html" in paths
    assert (wiki / "snapshots" / run_id / "manifest.json").is_file()


def _current(index: str) -> str:
    for line in index.splitlines():
        if line.startswith("当前run_id: "):
            return line.split(": ", 1)[1]
    raise AssertionError(index)


def _assert_index_safe(wiki: Path) -> None:
    index = wiki / "index.md"
    if not index.exists():
        return
    text = index.read_text(encoding="utf-8")
    current = _current(text)
    _assert_complete(wiki, current)
    listed = text.split("当前快照: ", 1)[1].splitlines()[0]
    assert listed == f"snapshots/{current}/"
    for line in text.splitlines():
        if not line.startswith("历史: "):
            continue
        run_id = line.split(" ", 2)[2]
        if (wiki / "snapshots" / run_id).is_dir():
            _assert_complete(wiki, run_id)


def _claims(rows: list[ClassifiedItem]) -> list[object]:
    found = []
    for row in rows:
        found.extend(row.claims)
    return found


def _case(topic: str, spec: tuple[tuple[str, str], ...]) -> list[ClassifiedItem]:
    rows = []
    for source, stance in spec:
        sentence = FOR if stance == "for" else AGAINST
        rows.append(
            _row(source, topic, stance=stance, sentence=sentence, title=f"{topic}-{source}", reason=f"{topic}的理由")
        )
    return rows


def test_html_labels_and_pending(tmp_path: Path) -> None:
    _reset()
    wiki = tmp_path / "wiki"
    publisher = WikiPublisher(wiki)
    with pytest.raises(TypeError):
        publisher.stage(  # type: ignore[misc]
            "run-html-1",
            NOW,
            (),
            (),
            ConsensusResult((), ()),
            (),
            (),
            (),
            (),
        )

    must = _row("alpha", "阅读", title="必读样本", bucket="must", reason="因为值得读", comments=("观众原话",))
    recommend = _row("beta", "阅读", title="推荐样本", bucket="recommend", reason="可以稍后读")
    skip = _row("gamma", "阅读", title="跳过样本", bucket="skip", reason="不值得读")
    history = [
        _row("hist-a", "历史专题", title="历史甲不应出现在增量", reason="旧理由甲", comments=("旧观众",)),
        _row("hist-b", "历史专题", title="历史乙不应出现在增量", reason="旧理由乙"),
    ]
    pending = _record("pod", title="待转写样本", body=BODY, published_at=DAY)
    held = _record("late", title="日期样本", body=BODY, published_at=FUTURE)
    waiting = _record("wait", title="未分类样本", body=BODY, published_at=None)
    rows = [must, recommend, skip]
    publication = publisher.stage(
        run_id="run-html-1",
        now=NOW,
        delta=rows,
        reviews=_library(rows + history),
        consensus=ConsensusResult((), ()),
        pending=(pending,),
        unclassified=(waiting,),
        held_date=(held,),
        warnings=("来源失败",),
    )
    assert publication.html_path == "digest/run-html-1.html"  # type: ignore[attr-defined]
    assert publication.manifest_path == "snapshots/run-html-1/manifest.json"  # type: ignore[attr-defined]
    assert publication.classified_refs == tuple(  # type: ignore[attr-defined]
        sorted((item.id, item.analysis_hash) for item in rows)
    )
    assert not (wiki / "index.md").exists()
    assert not (wiki / "log.md").exists()

    publisher.publish(publication)  # type: ignore[arg-type]
    html = (wiki / "digest" / "run-html-1.html").read_text(encoding="utf-8")
    positions = [html.index(f"<h2>{heading}</h2>") for heading in HEADINGS]
    assert positions == sorted(positions)
    assert 'charset="utf-8"' in html
    assert "本库当前已收录材料" in html
    for part in FOOTNOTE_PARTS:
        assert part in _section(html, "脚注")
    assert must.title in _section(html, "必读")
    assert "观众原话" in _section(html, "必读")
    assert recommend.title in _section(html, "推荐阅读")
    assert "未提供评论文字" in _section(html, "推荐阅读")
    assert skip.title in _section(html, "不推荐")
    assert pending.title in _section(html, "待转写")
    assert held.title in _section(html, "日期待确认")
    assert "日期待确认" in _section(html, "日期待确认")
    assert FUTURE not in html
    assert waiting.title in _section(html, "待分类/本次未完成")
    assert "发布时间未知" in _section(html, "待分类/本次未完成")
    assert "来源失败" in _section(html, "说明")
    assert EMPTY not in html
    assert "历史甲不应出现在增量" not in html
    assert "历史乙不应出现在增量" not in html
    assert BODY not in html
    topic = (wiki / "snapshots" / "run-html-1" / "topics" / f"{topic_slug('历史专题')}.md").read_text(encoding="utf-8")
    assert "历史甲不应出现在增量" in topic
    assert "历史乙不应出现在增量" in topic
    assert "旧理由甲" in topic
    assert "旧观众" in topic
    assert "未提供评论文字" in topic
    assert "多源对照" in topic
    assert "本库当前已收录材料" in topic
    assert "https://example.com/hist-a/" in topic
    assert BODY not in topic
    index = (wiki / "index.md").read_text(encoding="utf-8")
    assert _current(index) == "run-html-1"
    assert "当前状态: 部分完成" in index
    _assert_index_safe(wiki)

    empty = publisher.stage(
        run_id="run-html-2",
        now=LATER,
        delta=(),
        reviews=_library(rows + history),
        consensus=ConsensusResult((), ()),
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    assert empty.classified_refs == ()  # type: ignore[attr-defined]
    assert (wiki / "index.md").read_bytes() == index.encode("utf-8")
    publisher.publish(empty)  # type: ignore[arg-type]
    second = (wiki / "digest" / "run-html-2.html").read_text(encoding="utf-8")
    assert EMPTY in second
    for title in ("必读样本", "推荐样本", "跳过样本", "历史甲不应出现在增量", "历史乙不应出现在增量"):
        assert title not in second
    for heading in ("必读", "推荐阅读", "不推荐", "待转写", "日期待确认", "待分类/本次未完成", "脚注"):
        assert f"<h2>{heading}</h2>" in second
    assert BODY not in second
    assert _current((wiki / "index.md").read_text(encoding="utf-8")) == "run-html-2"
    assert "当前状态: 已发布" in (wiki / "index.md").read_text(encoding="utf-8")
    assert "历史: 2030-01-15T00:00:00.000000Z run-html-1" in (wiki / "index.md").read_text(encoding="utf-8")
    assert "必读样本" in (wiki / "digest" / "run-html-1.html").read_text(encoding="utf-8")
    _assert_complete(wiki, "run-html-1")
    _assert_complete(wiki, "run-html-2")


def test_assessment_labels_are_distinct(tmp_path: Path) -> None:
    _reset()
    specs = {
        "两则样本": (("a0", "for"), ("a1", "against")),
        "四票平局": (("b0", "for"), ("b1", "for"), ("b2", "against"), ("b3", "against")),
        "单一来源": (("c0", "for"), ("c0", "for"), ("c0", "for")),
        "一边倒": (("d0", "for"), ("d1", "for"), ("d2", "for")),
        "来源冲突": (("e0", "for"), ("e0", "against")),
    }
    rows: list[ClassifiedItem] = []
    for topic, spec in specs.items():
        rows.extend(_case(topic, spec))
    # single_source and conflicted reuse one source id on purpose.
    # Rebuild those two groups with distinct items that still share a source.
    rows = [row for row in rows if row.topic not in {"单一来源", "来源冲突"}]
    rows.extend(
        [
            _row("c0", "单一来源", stance="for", sentence=FOR, title="单一来源-1"),
            _row("c0", "单一来源", stance="for", sentence=FOR, title="单一来源-2"),
            _row("c0", "单一来源", stance="for", sentence=FOR, title="单一来源-3"),
            _row("e0", "来源冲突", stance="for", sentence=FOR, title="来源冲突-1"),
            _row("e0", "来源冲突", stance="against", sentence=AGAINST, title="来源冲突-2"),
        ]
    )
    consensus = find_minority(_claims(rows))
    by_topic = {item.topic: item.status for item in consensus.assessments}
    assert by_topic == {
        "两则样本": "insufficient",
        "四票平局": "disputed",
        "单一来源": "single_source",
        "一边倒": "one_sided",
        "来源冲突": "conflicted",
    }
    assert consensus.opportunities == ()
    wiki = tmp_path / "wiki"
    publisher = WikiPublisher(wiki)
    publication = publisher.stage(
        run_id="run-labels",
        now=NOW,
        delta=tuple(rows),
        reviews=_library(rows),
        consensus=consensus,
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    publisher.publish(publication)  # type: ignore[arg-type]
    snapshot = wiki / "snapshots" / "run-labels"
    assert not (snapshot / "opportunities").exists()
    manifest = _manifest(wiki, "run-labels")
    assert manifest["opportunities"] == []
    for topic, status in by_topic.items():
        text = (snapshot / "topics" / f"{topic_slug(topic)}.md").read_text(encoding="utf-8")
        assert ASSESSMENT_LABELS[status] in text
        for other, label in ASSESSMENT_LABELS.items():
            if other != status:
                assert label not in text
        assert "机会页：" not in text
    single = (snapshot / "topics" / f"{topic_slug('单一来源')}.md").read_text(encoding="utf-8")
    assert "单源材料汇总" in single
    assert "多源对照" not in single
    mixed = (snapshot / "topics" / f"{topic_slug('四票平局')}.md").read_text(encoding="utf-8")
    assert "多源对照" in mixed
    assert "本次未请求短评" in mixed
    assert "支持 2" in mixed
    assert "反对 2" in mixed


def test_digest_escapes_untrusted_text(tmp_path: Path) -> None:
    _reset()
    good = "https://example.com/alpha/safe"
    other = "https://example.com/beta/safe"
    title = "<script>alert(1)</script>"
    reason = (
        '见 <img src="https://evil.example/a.png"> '
        "![img](https://evil.example/a.png) "
        "[x](javascript:alert(1)) data:text/html,hi file:///etc/passwd"
    )
    comment = '<script src="https://evil.example/x.js"></script>'
    warning = "注意 <script>steal</script>"
    bad = _row("alpha", "转义", title=title, reason=reason, comments=(comment,), url=good)
    peer = _row("beta", "转义", title="对照标题", reason="普通理由", url=other)
    wiki = tmp_path / "wiki"
    publisher = WikiPublisher(wiki)
    publication = publisher.stage(
        run_id="run-escape",
        now=NOW,
        delta=(bad, peer),
        reviews=_library([bad, peer]),
        consensus=ConsensusResult((), ()),
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(warning,),
    )
    publisher.publish(publication)  # type: ignore[arg-type]
    html = (wiki / "digest" / "run-escape.html").read_text(encoding="utf-8")
    tags = set(re.findall(r"</?([a-zA-Z0-9]+)", html))
    assert tags <= {"html", "head", "meta", "title", "body", "h1", "h2", "h3", "p", "ul", "li", "article", "a"}
    hrefs = re.findall(r'href="([^"]*)"', html)
    assert set(hrefs) == {good, other}
    assert "href='" not in html
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "<iframe" not in html.lower()
    assert "<link" not in html.lower()
    assert "<style" not in html.lower()
    assert "http-equiv" not in html.lower()
    assert "@import" not in html.lower()
    assert "url(" not in html.lower()
    assert "javascript:" not in html.split("<body>", 1)[0]
    assert all(not href.startswith(("javascript:", "data:", "file:")) for href in hrefs)
    assert "https://evil.example/a.png" not in "".join(hrefs)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=&quot;https://evil.example/a.png&quot;&gt;" in html
    assert "![img](https://evil.example/a.png)" in html
    assert "javascript:alert(1)" in html
    assert "&lt;script&gt;steal&lt;/script&gt;" in html
    assert 'charset="utf-8"' in html
    page = (wiki / "snapshots" / "run-escape" / "topics" / f"{topic_slug('转义')}.md").read_text(encoding="utf-8")
    assert "<script" not in page.lower()
    assert "<img" not in page.lower()
    assert "![" not in page
    assert "](javascript:" not in page
    assert "](https://evil.example" not in page
    assert f"]({good})" in page
    assert f"]({other})" in page
    assert "https://evil.example/a.png" in page
    assert "&lt;script&gt;alert\\(1\\)&lt;/script&gt;" in page
    joined = "\n".join(path.read_text(encoding="utf-8") for path in wiki.rglob("*") if path.is_file())
    assert "BODY-SENTINEL" not in joined


def test_snapshot_removes_stale_current_opportunity(tmp_path: Path) -> None:
    _reset()
    topic = "需求对照"
    first_rows = [
        _row(source, topic, stance="for", sentence=FOR, title=f"多数{source}")
        for source in ("a0", "a1", "a2", "a3")
    ]
    first_rows.append(_row("a4", topic, stance="against", sentence=AGAINST, title="少数a4"))
    first = find_minority(_claims(first_rows))
    assert len(first.opportunities) == 1
    assert first.opportunities[0].minority_sentence == AGAINST
    wiki = tmp_path / "wiki"
    publisher = WikiPublisher(wiki)
    older = publisher.stage(
        run_id="run-old",
        now=NOW,
        delta=tuple(first_rows),
        reviews=_library(first_rows),
        consensus=first,
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    publisher.publish(older)  # type: ignore[arg-type]
    slug = claim_slug(topic, KEY)
    old_page = wiki / "snapshots" / "run-old" / "opportunities" / f"{slug}.md"
    old_bytes = old_page.read_bytes()
    assert AGAINST.encode() in old_bytes
    assert "本条仅为本库材料分歧".encode() in old_bytes
    assert "来源票数".encode() in old_bytes
    for part in FOOTNOTE_PARTS:
        assert part.encode() in old_bytes
    index_before = (wiki / "index.md").read_bytes()
    assert _current(index_before.decode()) == "run-old"

    second_rows = [
        _row("b0", topic, stance="for", sentence=FOR, title="新支持一"),
        _row("b1", topic, stance="for", sentence=FOR, title="新支持二"),
        _row("b2", topic, stance="against", sentence=AGAINST, title="新反对一"),
        _row("b3", topic, stance="against", sentence=AGAINST, title="新反对二"),
    ]
    second = find_minority(_claims(second_rows))
    assert second.opportunities == ()
    assert {item.status for item in second.assessments} == {"disputed"}
    newer = publisher.stage(
        run_id="run-new",
        now=LATER,
        delta=tuple(second_rows),
        reviews=_library(second_rows),
        consensus=second,
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    assert (wiki / "index.md").read_bytes() == index_before
    assert not (wiki / "snapshots" / "run-new").exists()
    publisher.publish(newer)  # type: ignore[arg-type]
    index = (wiki / "index.md").read_text(encoding="utf-8")
    assert _current(index) == "run-new"
    assert "当前快照: snapshots/run-new/" in index
    assert "历史: 2030-01-15T00:00:00.000000Z run-old" in index
    assert "当前快照: snapshots/run-old/" not in index
    assert not (wiki / "snapshots" / "run-new" / "opportunities").exists()
    current_topic = (wiki / "snapshots" / "run-new" / "topics" / f"{topic_slug(topic)}.md").read_text(encoding="utf-8")
    assert "争议（未写机会）" in current_topic
    assert "opportunities/" not in current_topic
    assert old_page.read_bytes() == old_bytes
    _assert_index_safe(wiki)
    _assert_complete(wiki, "run-old")
    _assert_complete(wiki, "run-new")


def test_publish_idempotence_and_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()
    wiki = tmp_path / "wiki"
    publisher = WikiPublisher(wiki)
    rows = [
        _row("alpha", "阅读", title="稳定标题", bucket="must"),
        _row("beta", "阅读", title="稳定对照", bucket="recommend"),
    ]
    publication = publisher.stage(
        run_id="run-idem",
        now=NOW,
        delta=tuple(rows),
        reviews=_library(rows),
        consensus=ConsensusResult((), ()),
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    publisher.publish(publication)  # type: ignore[arg-type]
    before = _files(wiki)
    publisher.publish(publication)  # type: ignore[arg-type]
    again = publisher.stage(
        run_id="run-idem",
        now=NOW,
        delta=tuple(rows),
        reviews=_library(rows),
        consensus=ConsensusResult((), ()),
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    assert again == publication
    publisher.publish(again)  # type: ignore[arg-type]
    assert _files(wiki) == before

    left = [_row(source, "主题甲", stance="for", sentence=FOR) for source in ("m0", "m1", "m2", "m3")]
    left.append(_row("m4", "主题甲", stance="against", sentence=AGAINST))
    right = [_row(source, "主题乙", stance="for", sentence=FOR) for source in ("n0", "n1", "n2", "n3")]
    right.append(_row("n4", "主题乙", stance="against", sentence=AGAINST))
    both = left + right
    result = find_minority(_claims(both))
    assert len(result.opportunities) == 2
    keyed = publisher.stage(
        run_id="run-keys",
        now=LATER,
        delta=tuple(both),
        reviews=_library(both),
        consensus=result,
        pending=(),
        unclassified=(),
        held_date=(),
        warnings=(),
    )
    publisher.publish(keyed)  # type: ignore[arg-type]
    slug_a = claim_slug("主题甲", KEY)
    slug_b = claim_slug("主题乙", KEY)
    assert slug_a != slug_b
    assert (wiki / "snapshots" / "run-keys" / "opportunities" / f"{slug_a}.md").is_file()
    assert (wiki / "snapshots" / "run-keys" / "opportunities" / f"{slug_b}.md").is_file()
    listed = {
        (item["topic"], item["claim_key"], item["slug"])
        for item in _manifest(wiki, "run-keys")["opportunities"]  # type: ignore[index]
    }
    assert listed == {
        (item.topic, item.claim_key, claim_slug(item.topic, item.claim_key))
        for item in result.opportunities
    }
    saved = _files(wiki)

    def _same(*_args: object, **_kwargs: object) -> str:
        return "0123456789ab"

    monkeypatch.setattr("second_brain.wiki.compile.claim_slug", _same)
    collide_rows = [
        _row("p0", "撞车甲", stance="for", sentence=FOR),
        _row("p1", "撞车甲", stance="for", sentence=FOR),
        _row("p2", "撞车甲", stance="for", sentence=FOR),
        _row("p3", "撞车甲", stance="for", sentence=FOR),
        _row("p4", "撞车甲", stance="against", sentence=AGAINST),
        _row("q0", "撞车乙", stance="for", sentence=FOR),
        _row("q1", "撞车乙", stance="for", sentence=FOR),
        _row("q2", "撞车乙", stance="for", sentence=FOR),
        _row("q3", "撞车乙", stance="for", sentence=FOR),
        _row("q4", "撞车乙", stance="against", sentence=AGAINST),
    ]
    collide = find_minority(_claims(collide_rows))
    assert len(collide.opportunities) == 2
    with pytest.raises(IdentityCollision) as caught:
        publisher.stage(
            run_id="run-collide",
            now=LATER,
            delta=tuple(collide_rows),
            reviews=_library(collide_rows),
            consensus=collide,
            pending=(),
            unclassified=(),
            held_date=(),
            warnings=(),
        )
    assert caught.value.code == "identity_collision"
    monkeypatch.undo()
    assert not (wiki / "snapshots" / "run-collide").exists()
    assert not (wiki / ".staging" / "run-collide").exists()
    assert _files(wiki) == saved

    monkeypatch.setattr("second_brain.wiki.compile.topic_slug", _same)
    topic_rows = [
        _row("r0", "页甲", title="页甲一"),
        _row("r1", "页甲", title="页甲二"),
        _row("r2", "页乙", title="页乙一"),
        _row("r3", "页乙", title="页乙二"),
    ]
    with pytest.raises(IdentityCollision):
        publisher.stage(
            run_id="run-topic",
            now=LATER,
            delta=tuple(topic_rows),
            reviews=_library(topic_rows),
            consensus=ConsensusResult((), ()),
            pending=(),
            unclassified=(),
            held_date=(),
            warnings=(),
        )
    monkeypatch.undo()
    assert not (wiki / "snapshots" / "run-topic").exists()
    assert _files(wiki) == saved

    digest = wiki / "digest" / "run-idem.html"
    mutated = digest.read_bytes() + b"\n<!-- mutated -->\n"
    digest.write_bytes(mutated)
    untouched = {path: stat for path, stat in saved.items() if path != "digest/run-idem.html"}
    with pytest.raises(PublishError) as mismatch:
        publisher.publish(publication)  # type: ignore[arg-type]
    assert mismatch.value.code == "hash_mismatch"
    assert digest.read_bytes() == mutated
    after = _files(wiki)
    for path, stat in untouched.items():
        assert after[path] == stat


def test_index_never_points_to_partial_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()

    def _prepare(folder: Path) -> tuple[Path, WikiPublisher, object, bytes]:
        folder.mkdir(parents=True)
        wiki = folder / "wiki"
        publisher = WikiPublisher(wiki)
        old_rows = [_row("old-a", "旧主题", title="旧甲"), _row("old-b", "旧主题", title="旧乙")]
        old = publisher.stage(
            run_id="run-old",
            now=NOW,
            delta=tuple(old_rows),
            reviews=_library(old_rows),
            consensus=ConsensusResult((), ()),
            pending=(),
            unclassified=(),
            held_date=(),
            warnings=(),
        )
        publisher.publish(old)  # type: ignore[arg-type]
        new_rows = [_row("new-a", "新主题", title="新甲"), _row("new-b", "新主题", title="新乙")]
        staged = publisher.stage(
            run_id="run-new",
            now=LATER,
            delta=tuple(new_rows),
            reviews=_library(new_rows),
            consensus=ConsensusResult((), ()),
            pending=(),
            unclassified=(),
            held_date=(),
            warnings=(),
        )
        index = (wiki / "index.md").read_bytes()
        assert _current(index.decode()) == "run-old"
        assert not (wiki / "snapshots" / "run-new").exists()
        return wiki, publisher, staged, index

    def _fail_rename(nth: int) -> None:
        real = os.rename
        seen = {"n": 0}

        def wrapper(src: object, dst: object) -> None:
            seen["n"] += 1
            if seen["n"] == nth:
                raise OSError(nth, "before rename")
            real(src, dst)

        monkeypatch.setattr(os, "rename", wrapper)

    wiki, publisher, staged, index = _prepare(tmp_path / "before-snapshot")
    _fail_rename(1)
    with pytest.raises(PublishError) as first:
        publisher.publish(staged)  # type: ignore[arg-type]
    assert first.value.code == "publish_io"
    monkeypatch.undo()
    assert (wiki / "index.md").read_bytes() == index
    assert not (wiki / "snapshots" / "run-new").exists()
    assert not (wiki / "digest" / "run-new.html").exists()
    _assert_index_safe(wiki)
    publisher.publish(staged)  # type: ignore[arg-type]
    assert _current((wiki / "index.md").read_text(encoding="utf-8")) == "run-new"
    _assert_index_safe(wiki)

    wiki, publisher, staged, index = _prepare(tmp_path / "before-digest")
    _fail_rename(2)
    with pytest.raises(PublishError) as second:
        publisher.publish(staged)  # type: ignore[arg-type]
    assert second.value.code == "publish_io"
    monkeypatch.undo()
    assert (wiki / "index.md").read_bytes() == index
    assert (wiki / "snapshots" / "run-new" / "manifest.json").is_file()
    assert not (wiki / "digest" / "run-new.html").exists()
    _assert_index_safe(wiki)
    publisher.publish(staged)  # type: ignore[arg-type]
    assert _current((wiki / "index.md").read_text(encoding="utf-8")) == "run-new"
    _assert_complete(wiki, "run-new")
    _assert_index_safe(wiki)

    wiki, publisher, staged, index = _prepare(tmp_path / "before-index")
    real_replace = os.replace

    def replace_before(src: object, dst: object) -> None:
        raise OSError(3, "before replace")

    monkeypatch.setattr(os, "replace", replace_before)
    with pytest.raises(PublishError) as third:
        publisher.publish(staged)  # type: ignore[arg-type]
    assert third.value.code == "publish_io"
    monkeypatch.undo()
    assert (wiki / "index.md").read_bytes() == index
    assert (wiki / "digest" / "run-new.html").is_file()
    _assert_index_safe(wiki)
    publisher.publish(staged)  # type: ignore[arg-type]
    assert _current((wiki / "index.md").read_text(encoding="utf-8")) == "run-new"
    _assert_index_safe(wiki)

    wiki, publisher, staged, index = _prepare(tmp_path / "after-index")

    def replace_after(src: object, dst: object) -> None:
        real_replace(src, dst)
        raise OSError(4, "after replace")

    monkeypatch.setattr(os, "replace", replace_after)
    with pytest.raises(PublishError):
        publisher.publish(staged)  # type: ignore[arg-type]
    monkeypatch.undo()
    assert (wiki / "index.md").read_bytes() != index
    assert _current((wiki / "index.md").read_text(encoding="utf-8")) == "run-new"
    _assert_index_safe(wiki)
    publisher.publish(staged)  # type: ignore[arg-type]
    _assert_index_safe(wiki)

    wiki, publisher, staged, _index = _prepare(tmp_path / "sequence")
    calls: list[tuple[str, str, str]] = []
    real_rename = os.rename

    def record_rename(src: object, dst: object) -> None:
        calls.append(("rename", str(src), str(dst)))
        real_rename(src, dst)

    def record_replace(src: object, dst: object) -> None:
        calls.append(("replace", str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "rename", record_rename)
    monkeypatch.setattr(os, "replace", record_replace)
    publisher.publish(staged)  # type: ignore[arg-type]
    monkeypatch.undo()
    kinds = [kind for kind, _src, _dst in calls]
    assert kinds == ["rename", "rename", "replace"]
    assert "/.staging/" in calls[0][1] and calls[0][2].endswith("/snapshots/run-new")
    assert "/.staging/" not in calls[0][2]
    assert calls[1][1].endswith("/digest/run-new.html") and calls[1][2].endswith("/digest/run-new.html")
    assert "/.staging/" in calls[1][1] and "/.staging/" not in calls[1][2]
    assert calls[2][2].endswith("/index.md")
    _assert_index_safe(wiki)
    assert (wiki / "snapshots" / "run-old" / "topics").is_dir()


def test_append_log_once_per_complete_block(tmp_path: Path) -> None:
    _reset()
    wiki = tmp_path / "wiki"
    publisher = WikiPublisher(wiki)
    raw = f"https://feeds.example.com/a.xml?token={SECRET}"
    fetch = FetchEvent(
        source_id="alpha",
        host="feeds.example.com",
        url_redacted=redact_url(raw),
        url_hash=hash_url(raw),
        status=200,
        at="2030-03-15T12:34:56.123456Z",
    )
    llm = LlmEvent(
        call_id="call-1",
        run_id="run-log",
        purpose="classify",
        at="2030-03-15T12:34:56.123456Z",
        status="ok",
        billing_path="test",
        usage=Usage(input_tokens=3, output_tokens=4),
    )
    events = (llm, fetch)
    log = wiki / "log.md"
    half = (
        "<!-- BEGIN run_id=run-log sha256="
        + "ab" * 32
        + " -->\n"
        + SECRET
        + "\n"
    ).encode()
    log.parent.mkdir()
    log.write_bytes(half)
    publisher.append_events("run-log", events)
    data = log.read_bytes()
    assert data.startswith(half)
    added = data[len(half) :]
    assert SECRET.encode() not in added
    assert b"token=" not in added
    assert raw.encode() not in added
    assert b"BODY-SENTINEL" not in added
    assert added.count(b"<!-- BEGIN ") == 1
    assert added.count(b"<!-- END ") == 1
    body = added.split(b"\n", 1)[1].rsplit(b"\n<!-- END ", 1)[0] + b"\n"
    parsed = json.loads(body.decode())
    assert set(parsed) == {"events", "run_id"}
    assert parsed["run_id"] == "run-log"
    assert [item["kind"] for item in parsed["events"]] == ["llm", "fetch"]
    recorded = parsed["events"][0]
    assert recorded["total_cost_usd"] == "UNKNOWN"
    assert recorded["input_tokens"] == 3
    assert recorded["output_tokens"] == 4
    assert recorded["reasoning_tokens"] is None
    assert "0.00" not in body.decode()
    for banned in ("prompt", "stderr", "session", "body", "cookie", "authorization", "secret"):
        assert banned not in recorded
    assert parsed["events"][1]["url_redacted"] == "https://feeds.example.com/\u2026"
    assert SECRET not in parsed["events"][1]["url_redacted"]
    stamped = log.stat().st_mtime_ns
    publisher.append_events("run-log", events)
    assert log.read_bytes() == data
    assert log.stat().st_mtime_ns == stamped

    later = LlmEvent(
        call_id="call-2",
        run_id="run-log",
        purpose="review",
        at=LATER,
        status="failed",
        billing_path="test",
        error_code="unavailable",
        usage=Usage(),
    )
    publisher.append_events("run-log", (later,))
    grown = log.read_bytes()
    assert grown.startswith(data)
    assert grown.count(b"<!-- END ") == 2
    publisher.append_events("run-log", (later,))
    assert log.read_bytes() == grown
    assert SECRET.encode() not in grown[len(half) :]

    with pytest.raises(SchemaError):
        publisher.append_events("run-log", (FetchEvent(
            source_id="alpha",
            host="feeds.example.com",
            url_redacted=redact_url(raw),
            url_hash=hash_url(raw),
            status=200,
            at=NOW,
        ), "not-an-event"))  # type: ignore[arg-type]
