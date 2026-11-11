"""Contract tests for frozen records, hashes, aliases, and ports."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from second_brain.errors import (
    BudgetExceeded,
    ConfigError,
    GrokUnavailable,
    IdentityCollision,
    InvalidBucket,
    RateLimited,
    SchemaError,
)
from second_brain.models import (
    GROK_ARGV_BEFORE_CWD,
    GROK_ARGV_DYNAMIC_FLAGS,
    HTTP_CIRCUIT_OPEN_S,
    HTTP_MAX_BYTES,
    HTTP_TIMEOUT_S,
    POLICY_TEMPLATE_VERSION,
    PROMPT_BODY_CHARS,
    PROMPT_COMMENT_CHARS,
    PROMPT_COMMENT_MAX,
    PROMPT_TITLE_CHARS,
    SCHEMA_VERSION,
    USER_AGENT,
    AppConfig,
    Asset,
    ClassifiedItem,
    CompletionResult,
    HttpResponse,
    IngestRecord,
    Item,
    KeyPolicy,
    RuntimePolicy,
    Source,
    Usage,
    analysis_hash,
    build_item,
    canonical_json,
    canonical_url,
    claim_id,
    claim_slug,
    content_hash,
    format_timestamp,
    item_id,
    make_claim,
    make_ingest_record,
    normalize_key,
    parse_timestamp,
    policy_hash,
    prompt_fields,
    redact_url,
    resolve_alias,
    topic_slug,
)
from second_brain.ports import Clock, CompletionFn, FixtureReader, HttpStateStore, HttpTransport

NOW = "2030-01-15T00:00:00.000000Z"
EARLIER = "2030-01-01T00:00:00.000000Z"
ROOT = Path(__file__).resolve().parents[1]


def sample_item(**overrides) -> Item:
    values = {
        "source_id": "src",
        "guid": "g1",
        "url": "https://example.com/a",
        "title": "Title",
        "fetched_at": NOW,
        "modality": "text",
        "body_raw": "raw",
        "body_plain": "plain",
        "published_at": EARLIER,
    }
    values.update(overrides)
    return build_item(**values)


def sample_asset(item: Item, **overrides) -> Asset:
    values = {"item_id": item.id, "transcript_status": "not_applicable"}
    values.update(overrides)
    return Asset(**values)


def test_item_id_and_normalization() -> None:
    expected = hashlib.sha256(b"src\nguid-1").hexdigest()[:32]
    assert item_id("src", "guid-1") == expected
    assert item_id("src", "guid-1") == item_id("src", "guid-1")
    assert item_id("src", "guid-1") != item_id("src", "guid-2")
    assert len(expected) == 32
    assert all(char in "0123456789abcdef" for char in expected)
    for source_id, guid in (
        ("src\n", "guid"),
        ("src", "a\nb"),
        ("src", "a\rb"),
        ("src", "a\x00b"),
        ("src", "a\u2028b"),
        ("src", ""),
        ("", "guid"),
        ("bad id", "guid"),
        ("字", "guid"),
        ("a/b", "guid"),
    ):
        with pytest.raises(SchemaError) as caught:
            item_id(source_id, guid)
        assert caught.value.code == "invalid_identity"
    assert normalize_key("\uff21\uff22\uff23  def") == "abc def"
    assert normalize_key("  Foo\t\tBAR\n") == "foo bar"
    assert normalize_key("Straße") == "strasse"
    assert normalize_key("İ") == "i\u0307"
    assert normalize_key("a\n\n b") == "a b"
    assert content_hash("A\r\nB", "raw") == hashlib.sha256(b"A B\nraw").hexdigest()
    assert content_hash("A\n\nB", "raw") == hashlib.sha256(b"A  B\nraw").hexdigest()


def test_canonical_url_keeps_semantic_query() -> None:
    assert canonical_url("https://EXAMPLE.com:443/a?id=1&utm_source=x#z") == "https://example.com/a?id=1"
    assert canonical_url("https://example.com/a?id=2") == "https://example.com/a?id=2"
    assert canonical_url("https://example.com/a?id=1") != canonical_url("https://example.com/a?id=2")
    assert canonical_url("https://EXAMPLE.com/AbC") == "https://example.com/AbC"
    assert canonical_url("https://example.com/a/") == "https://example.com/a/"
    assert canonical_url("http://EXAMPLE.com:80/a") == "http://example.com/a"
    assert canonical_url("http://example.com:443/a") == "http://example.com:443/a"
    assert canonical_url("https://example.com/a?z=1&a=2") == "https://example.com/a?z=1&a=2"
    assert (
        canonical_url("https://example.com/a?utm_source=1&id=2&fbclid=3&gclid=4&UTM_MEDIUM=5")
        == "https://example.com/a?id=2"
    )
    assert canonical_url("https://example.com/a#one") == canonical_url("https://example.com/a#two")
    assert canonical_url("https://10.example.com/feed") == "https://10.example.com/feed"
    assert canonical_url("https://example.com/a?q=a%20b") == "https://example.com/a?q=a%20b"
    assert canonical_url("https://example.com./a") == "https://example.com/a"
    host = "bücher.example".encode("idna").decode("ascii")
    assert canonical_url("https://bücher.example/a") == f"https://{host}/a"
    with pytest.raises(SchemaError) as caught:
        canonical_url("https://user:SUPERSECRET@example.com/a")
    assert caught.value.code == "invalid_url"
    assert "SUPERSECRET" not in str(caught.value)
    assert "SUPERSECRET" not in repr(caught.value)
    for bad in ("ftp://example.com/a", "https:///a", "/a", "https://example.com/a\\b", ""):
        with pytest.raises(SchemaError):
            canonical_url(bad)


def test_alias_cycle_is_rejected() -> None:
    assert resolve_alias("  Chips ", {"chips": "半导体"}) == "半导体"
    assert resolve_alias("CHIPS", {"chips": "半导体", "CHIPS": "半导体"}) == "半导体"
    assert resolve_alias("gpu", {"gpu": "chips", "chips": "半导体"}) == "半导体"
    assert resolve_alias("需求增长", {}) == "需求增长"
    assert resolve_alias("库存过高", {"chips": "半导体"}) == "库存过高"
    with pytest.raises(ConfigError) as cycle:
        resolve_alias("a", {"a": "b", "b": "a"})
    assert cycle.value.code == "alias_cycle"
    with pytest.raises(ConfigError) as untouched:
        resolve_alias("z", {"a": "b", "b": "a"})
    assert untouched.value.code == "alias_cycle"
    with pytest.raises(ConfigError) as conflict:
        resolve_alias("chips", {"chips": "半导体", "CHIPS": "芯片"})
    assert conflict.value.code == "alias_conflict"
    with pytest.raises(ConfigError) as empty:
        resolve_alias("a", {"a": "   "})
    assert empty.value.code == "alias_empty_target"
    with pytest.raises(ConfigError):
        KeyPolicy(topic_aliases={"a": "b", "b": "a"})


def test_hashes_follow_prompt_audience() -> None:
    item = sample_item(body_plain="p" * (PROMPT_BODY_CHARS + 5), body_raw="same-raw")
    other = sample_item(body_plain="p" * PROMPT_BODY_CHARS + "ZZZZZ", body_raw="same-raw", guid="g2")
    channel = "h" * (PROMPT_TITLE_CHARS + 2)
    asset = sample_asset(item, channel=channel, top_comments=("c" * (PROMPT_COMMENT_CHARS + 4),))
    fields = prompt_fields(item, asset)
    assert set(fields) == {"id", "source_id", "title", "modality", "body", "channel", "audience", "truncated"}
    assert fields["body"] == "p" * PROMPT_BODY_CHARS
    assert "True" not in str(fields["body"])
    assert fields["truncated"]["body"] is True
    assert fields["truncated"]["channel"] is True
    assert fields["audience"] == ["c" * PROMPT_COMMENT_CHARS]
    assert fields["truncated"]["audience"] == [True]
    same_tail = sample_asset(item, channel=channel, top_comments=("c" * PROMPT_COMMENT_CHARS + "XXXX",))
    assert analysis_hash(item, asset) == analysis_hash(item, same_tail)
    changed = sample_asset(item, top_comments=("c" * (PROMPT_COMMENT_CHARS - 1) + "Q",))
    assert analysis_hash(item, asset) != analysis_hash(item, changed)
    extra = sample_asset(
        item,
        top_comments=tuple(f"comment-{index}" for index in range(PROMPT_COMMENT_MAX + 1)),
    )
    shorter = sample_asset(item, top_comments=tuple(f"comment-{index}" for index in range(PROMPT_COMMENT_MAX)))
    assert analysis_hash(item, extra) == analysis_hash(item, shorter)
    counted = sample_asset(item, view_count=9, like_count=1, comment_count=4)
    bare = sample_asset(item)
    assert analysis_hash(item, counted) == analysis_hash(item, bare)
    long_channel = sample_asset(item, channel="h" * PROMPT_TITLE_CHARS + "A")
    other_channel = sample_asset(item, channel="h" * PROMPT_TITLE_CHARS + "B")
    assert prompt_fields(item, long_channel)["channel"] == prompt_fields(item, other_channel)["channel"]
    assert analysis_hash(item, long_channel) != analysis_hash(item, other_channel)
    assert analysis_hash(item, bare) != analysis_hash(other, sample_asset(other))
    payload = {
        "content_hash": content_hash(item.title, item.body_raw),
        "body_plain": item.body_plain,
        "modality": item.modality,
        "channel": asset.channel,
        "transcript_status": asset.transcript_status,
        "transcript_url": asset.transcript_url,
        "transcript_type": asset.transcript_type,
        "top_comments": fields["audience"],
    }
    oracle = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert analysis_hash(item, asset) == oracle


def test_identity_collision_does_not_overwrite() -> None:
    registry: dict[str, tuple[str, ...]] = {}
    first = item_id("src", "g1", registry=registry)
    assert item_id("src", "g1", registry=registry) == first
    registry[first] = ("item", "other", "g9")
    with pytest.raises(IdentityCollision) as caught:
        item_id("src", "g1", registry=registry)
    assert caught.value.code == "identity_collision"
    assert "g1" not in str(caught.value)
    assert registry[first] == ("item", "other", "g9")
    left = topic_slug("半导体")
    right = topic_slug("  半导体 ")
    assert left == right == hashlib.sha256(normalize_key("半导体").encode("utf-8")).hexdigest()[:12]
    assert claim_slug("主题A", "机构A|需求同比增长|2030年度|业务B") != claim_slug(
        "主题B", "机构A|需求同比增长|2030年度|业务B"
    )


def test_timestamps_and_bool_rejection() -> None:
    parsed = parse_timestamp("2030-01-01T08:00:00+08:00")
    assert format_timestamp(parsed) == "2030-01-01T00:00:00.000000Z"
    assert format_timestamp(datetime(2030, 1, 15, tzinfo=timezone.utc)) == NOW
    with pytest.raises(SchemaError):
        parse_timestamp("2030-01-01T00:00:00")
    with pytest.raises(SchemaError):
        format_timestamp(datetime(2030, 1, 15))
    with pytest.raises(SchemaError):
        Item(
            id=item_id("src", "g1"),
            source_id="src",
            guid="g1",
            url="https://example.com/a",
            canonical_url="https://example.com/a",
            title="Title",
            published_at="2030-01-01T08:00:00+08:00",
            fetched_at=NOW,
            content_hash=content_hash("Title", "raw"),
            analysis_hash="",
            modality="text",
            body_raw="raw",
            body_plain="plain",
        )
    with pytest.raises(ConfigError) as budget:
        RuntimePolicy(max_classify_batches=True)  # type: ignore[arg-type]
    assert budget.value.code == "invalid_budget"
    with pytest.raises(ConfigError) as flag:
        RuntimePolicy(offline="false")  # type: ignore[arg-type]
    assert flag.value.code == "invalid_bool"
    with pytest.raises(SchemaError):
        sample_asset(sample_item(), like_count=True)  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        Usage(total_cost_usd="1e-3")
    with pytest.raises(SchemaError):
        Usage(input_tokens=True)  # type: ignore[arg-type]
    assert Usage(total_cost_usd="0.10").total_cost_usd == "0.10"
    assert Usage().total_cost_usd is None


def test_ingest_record_rejects_empty_analysis_hash() -> None:
    item = sample_item()
    asset = sample_asset(item)
    assert item.analysis_hash == ""
    with pytest.raises(SchemaError) as caught:
        IngestRecord(item, asset)
    assert caught.value.code == "analysis_hash_mismatch"
    record = make_ingest_record(item, asset)
    assert record.item.analysis_hash == analysis_hash(item, asset)
    assert make_ingest_record(item, asset) == record
    claim = make_claim(
        item_id=item.id,
        source_id=item.source_id,
        analysis_hash=record.item.analysis_hash,
        topic="需求",
        claim_key="机构A|需求同比增长|2030年度|业务B",
        sentence="2030年度业务B需求将同比增长",
        stance="for",
        evidence_kind="body",
        evidence_quote="plain",
    )
    oracle_payload = {
        "analysis_hash": record.item.analysis_hash,
        "claim_key": claim.claim_key,
        "comment_index": None,
        "evidence_kind": "body",
        "evidence_quote": "plain",
        "item_id": item.id,
        "stance": "for",
        "topic": "需求",
    }
    oracle = hashlib.sha256(
        json.dumps(oracle_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    assert claim.id == oracle
    assert claim_id(claim) == claim.id
    with pytest.raises(SchemaError):
        make_claim(
            item_id=item.id,
            source_id=item.source_id,
            analysis_hash=record.item.analysis_hash,
            topic="需求",
            claim_key="只有三段|不够|2030年度",
            sentence="一句",
            stance="for",
            evidence_kind="body",
            evidence_quote="plain",
        )


def test_errors_do_not_echo_secrets() -> None:
    limited = RateLimited("127.0.0.1", 7)
    assert limited.host == "127.0.0.1"
    assert limited.retry_after_s == 7
    assert "127.0.0.1" not in str(limited)
    assert "127.0.0.1" not in repr(limited)
    usage = Usage(input_tokens=3, total_cost_usd=None)
    hidden = GrokUnavailable("timeout SUPERSECRET", usage)
    assert hidden.code == "unsafe_error_code"
    assert hidden.usage == usage
    assert "SUPERSECRET" not in str(hidden)
    assert "SUPERSECRET" not in repr(hidden)
    kept = GrokUnavailable("timeout", usage)
    assert kept.usage == usage
    assert kept.code == "timeout"
    assert "timeout" in str(kept)
    assert BudgetExceeded("classify").purpose == "classify"
    assert BudgetExceeded("drop table").purpose == "unknown"
    with pytest.raises(InvalidBucket):
        sample_classified_bucket("later")
    redacted = redact_url("https://user:SUPERSECRET@example.com/private?token=SUPERSECRET")
    assert redacted == "https://example.com/\u2026"
    assert "SUPERSECRET" not in redacted
    assert "private" not in redacted
    response = HttpResponse(status=200, body=b"ok", headers={"Content-Type": "text/plain"}, url="https://example.com/a")
    assert response.headers["content-type"] == "text/plain"


def sample_classified_bucket(bucket: str) -> ClassifiedItem:
    item = sample_item()
    record = make_ingest_record(item, sample_asset(item))
    return ClassifiedItem(
        id=item.id,
        source_id=item.source_id,
        topic="需求",
        url=item.canonical_url,
        title=item.title,
        bucket=bucket,  # type: ignore[arg-type]
        reason="因为原文这么写",
        published_at=EARLIER,
        analysis_hash=record.item.analysis_hash,
        policy_hash="a" * 64,
    )


def test_ports_are_protocols_only() -> None:
    source = (ROOT / "src" / "second_brain" / "ports.py").read_text(encoding="utf-8")
    for banned in (
        "class HttpClient",
        "class Store",
        "class WikiPublisher",
        "def parse_",
        "def classify_",
        "def run_once",
        "def complete",
        "def guard_",
    ):
        assert banned not in source
    request = inspect.signature(HttpTransport.request)
    assert list(request.parameters) == ["self", "url", "headers", "timeout_s", "max_bytes", "purpose"]
    assert request.parameters["headers"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(Clock.now).parameters) == ["self"]
    assert "state" in inspect.signature(HttpStateStore.put_rate_state).parameters
    complete = inspect.signature(CompletionFn.__call__)
    assert complete.parameters["schema"].kind is inspect.Parameter.KEYWORD_ONLY
    assert complete.parameters["purpose"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(FixtureReader.__call__).parameters) == ["self", "relative_uri"]

    class StubClock:
        def now(self) -> datetime:
            return datetime(2030, 1, 15, tzinfo=timezone.utc)

        def monotonic(self) -> float:
            return 0.0

        def sleep(self, seconds: float) -> None:
            raise AssertionError(seconds)

    class IncompleteClock:
        def now(self) -> datetime:
            return datetime(2030, 1, 15, tzinfo=timezone.utc)

    assert isinstance(StubClock(), Clock)
    assert not isinstance(IncompleteClock(), Clock)


def test_frozen_limits_and_policy_hash() -> None:
    assert SCHEMA_VERSION == "3"
    assert POLICY_TEMPLATE_VERSION == "classify-v3"
    assert USER_AGENT == "SecondBrain/0.3 (local; personal)"
    assert HTTP_TIMEOUT_S == 20
    assert HTTP_MAX_BYTES == 2 * 1024 * 1024
    assert HTTP_CIRCUIT_OPEN_S == 900
    assert PROMPT_BODY_CHARS == 1500
    assert PROMPT_TITLE_CHARS == 300
    assert GROK_ARGV_BEFORE_CWD == (
        "--no-auto-update",
        "--verbatim",
        "--no-plan",
        "--no-subagents",
        "--disable-web-search",
        "--max-turns",
        "1",
        "--disallowed-tools",
        "run_terminal_cmd,web_search,web_fetch,search_replace,Agent",
        "--deny",
        "Bash",
        "--deny",
        "Edit",
        "--deny",
        "Write",
        "--deny",
        "WebFetch",
        "--model",
        "grok-4.6",
        "--reasoning-effort",
        "low",
    )
    assert GROK_ARGV_DYNAMIC_FLAGS == ("--cwd", "--json-schema", "--prompt-file")
    policy = RuntimePolicy()
    assert policy.offline is True
    assert policy.live_authorized is False
    assert policy.billing_path == "test"
    assert policy.billing_path_verified is False
    assert policy.tool_policy_verified is False
    assert policy.review_llm is True
    assert policy.max_classify_batches == 3
    assert policy.max_review_batches == 1
    assert policy.max_transcript_fetches == 20
    keys = KeyPolicy(topic_aliases={"B": "A"}, claim_aliases={})
    reversed_keys = KeyPolicy(topic_aliases={"b": "a"}, claim_aliases={})
    schema = {"type": "object", "additionalProperties": False}
    assert policy_hash("偏好", keys, schema) == policy_hash("偏好", reversed_keys, {"additionalProperties": False, "type": "object"})
    assert "半导体" in canonical_json({"topic": "半导体"})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    with pytest.raises(SchemaError):
        CompletionResult(payload={"ok": True}, usage=Usage(), stop_reason=None, billing_path="test")


def test_source_and_config_rules() -> None:
    blog = Source(id="blog", kind="rss", url="https://example.com/feed.xml", title="示例")
    assert blog.backfill_days == 14
    assert blog.file_format is None
    fixture = Source(
        id="example-blog",
        kind="file",
        url="file:tests/fixtures/rss/feed.xml",
        title="合成博客",
        file_format="rss",
    )
    assert fixture.transcript_hosts == ()
    with pytest.raises(ConfigError) as missing_format:
        Source(id="example-blog", kind="file", url="file:tests/fixtures/rss/feed.xml", title="合成博客")
    assert missing_format.value.code == "file_format_required"
    with pytest.raises(ConfigError) as network_format:
        Source(
            id="blog",
            kind="rss",
            url="https://example.com/feed.xml",
            title="示例",
            file_format="rss",
        )
    assert network_format.value.code == "file_format_forbidden"
    with pytest.raises(ConfigError) as secret:
        Source(id="blog", kind="rss", url="https://example.com/feed.xml?token=SUPERSECRET", title="示例")
    assert secret.value.code == "secret_query"
    assert "SUPERSECRET" not in str(secret.value)
    hosted = Source(
        id="blog",
        kind="rss",
        url="https://example.com/feed.xml",
        title="示例",
        transcript_hosts=("CDN.Example.com.",),
    )
    assert hosted.transcript_hosts == ("cdn.example.com",)
    config = AppConfig(sources=(blog, fixture), preferences="短偏好", keys=KeyPolicy(), runtime=RuntimePolicy())
    assert config.runtime.live_authorized is False
    with pytest.raises(ConfigError) as duplicate:
        AppConfig(sources=(blog, blog), preferences="短偏好", keys=KeyPolicy(), runtime=RuntimePolicy())
    assert duplicate.value.code == "duplicate_source_id"
    with pytest.raises(ConfigError) as long_preferences:
        AppConfig(sources=(blog,), preferences="x" * 4001, keys=KeyPolicy(), runtime=RuntimePolicy())
    assert long_preferences.value.code == "preferences_too_long"
    shifted = parse_timestamp(NOW) + timedelta(0)
    assert format_timestamp(shifted) == NOW
