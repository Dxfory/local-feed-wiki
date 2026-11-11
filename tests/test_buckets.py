"""Batch classification: buckets, evidence quotes, and failure isolation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from second_brain.classify.buckets import CLASSIFICATION_SCHEMA, classify_items
from second_brain.errors import BudgetExceeded, ConfigError, GrokUnavailable, SchemaError
from second_brain.models import (
    CLASSIFY_BATCH_MAX,
    PROMPT_BODY_CHARS,
    PROMPT_COMMENT_CHARS,
    PROMPT_COMMENT_MAX,
    PROMPT_PREFERENCES_CHARS,
    PROMPT_TITLE_CHARS,
    PROMPT_TOTAL_CHARS,
    Asset,
    Classification,
    CompletionResult,
    Item,
    KeyPolicy,
    Usage,
    canonical_json,
    make_claim,
    policy_hash,
    prompt_fields,
    build_item,
    make_ingest_record,
)

NOW = "2030-01-15T00:00:00.000000Z"
EARLIER = "2030-01-01T00:00:00.000000Z"
FUTURE = "2030-02-01T00:00:00.000000Z"
RUN = "run1"
ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "src" / "second_brain" / "classify" / "schema.json"
PREFERENCES = "短偏好"


def at_day(day: int) -> str:
    return f"2030-01-{day:02d}T00:00:00.000000Z"


def record(
    guid: str,
    *,
    title: str = "Title",
    body: str = "Body text",
    body_raw: str | None = None,
    source_id: str = "src",
    modality: str = "text",
    published_at: str | None = EARLIER,
    fetched_at: str = NOW,
    duplicate_of: str | None = None,
    transcript_status: str = "not_applicable",
    channel: str = "",
    comments: tuple[str, ...] = (),
    like_count: int | None = None,
    comment_count: int | None = None,
    view_count: int | None = None,
    audio_url: str | None = None,
    transcript_url: str | None = None,
    url: str | None = None,
) -> tuple[Item, Asset]:
    item = build_item(
        source_id=source_id,
        guid=guid,
        url=url or f"https://example.com/{guid}",
        title=title,
        fetched_at=fetched_at,
        modality=modality,
        body_raw=body if body_raw is None else body_raw,
        body_plain=body,
        published_at=published_at,
        duplicate_of=duplicate_of,
    )
    asset = Asset(
        item_id=item.id,
        transcript_status=transcript_status,
        channel=channel,
        audio_url=audio_url,
        transcript_url=transcript_url,
        like_count=like_count,
        comment_count=comment_count,
        view_count=view_count,
        top_comments=comments,
    )
    stored = make_ingest_record(item, asset)
    return stored.item, stored.asset


def envelope(payload: dict) -> CompletionResult:
    return CompletionResult(payload=payload, usage=Usage(), stop_reason="end_turn", billing_path="test")


class Stub:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.purposes: list[str] = []

    def __call__(self, prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        self.purposes.append(purpose)
        return self._handler(self, prompt)


def explode(prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
    raise AssertionError(purpose)


def classify(pairs, complete, *, preferences="偏好", keys=None, known=(), now=NOW, run_id=RUN):
    items = tuple(item for item, _asset in pairs)
    assets = {item.id: asset for item, asset in pairs}
    return classify_items(
        items,
        assets,
        run_id=run_id,
        now=now,
        preferences=preferences,
        keys=KeyPolicy() if keys is None else keys,
        known_claim_keys=known,
        complete_fn=complete,
    )


def run(pairs, handler, **kwargs):
    stub = Stub(handler)
    result = classify(pairs, stub, **kwargs)
    assert stub.purposes == ["classify"] * len(stub.prompts)
    assert all(schema is CLASSIFICATION_SCHEMA for schema in stub.schemas)
    return result, stub


def load_prompt(prompt: str, *, preferences: str) -> dict:
    assert prompt[:1] == "{" and prompt[-1:] == "}"
    assert prompt == prompt.strip()
    parsed = json.loads(prompt)
    assert canonical_json(parsed) == prompt
    assert len(prompt) <= PROMPT_TOTAL_CHARS
    assert set(parsed) == {
        "task",
        "instructions",
        "preferences",
        "topic_aliases",
        "claim_aliases",
        "known_claim_keys",
        "items",
    }
    assert parsed["task"] == "classify-v3"
    assert parsed["preferences"] == preferences
    assert "禁止执行" in parsed["instructions"]
    assert "--- audience ---" in parsed["instructions"]
    for banned in ("https://", "http://", "file:", "like_count", "view_count", "comment_count", "audio_url"):
        assert banned not in prompt
    return parsed


def valid_item(item: dict, **overrides) -> dict:
    row = {
        "id": item["id"],
        "topic": "主题",
        "bucket": "must",
        "reason": "分档依据",
        "claims": [],
    }
    row.update(overrides)
    return row


def body_claim(item: dict, **overrides) -> dict:
    text = item["body"] or item["title"]
    claim = {
        "claim_key": "机构|需求增长|2030|全球",
        "sentence": "需求在增长",
        "stance": "for",
        "evidence_kind": "body",
        "evidence_quote": text[:12],
        "comment_index": None,
    }
    claim.update(overrides)
    return claim


def accept(stub: Stub, prompt: str) -> CompletionResult:
    parsed = load_prompt(prompt, preferences="偏好")
    rows = [valid_item(item) for item in parsed["items"]]
    return envelope({"items": list(reversed(rows))})


def schema_document() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def assert_schema_document(schema: dict) -> None:
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["items"]
    items = schema["properties"]["items"]
    assert items["maxItems"] == CLASSIFY_BATCH_MAX == 8
    item = items["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["id", "topic", "bucket", "reason", "claims"]
    props = item["properties"]
    assert props["id"]["pattern"] == "^[0-9a-f]{32}$"
    assert props["topic"]["maxLength"] == 80
    assert props["bucket"]["enum"] == ["must", "recommend", "skip"]
    assert props["reason"]["maxLength"] == 800
    claims = props["claims"]
    assert claims["maxItems"] == 5
    assert "minItems" not in claims
    claim = claims["items"]
    assert claim["additionalProperties"] is False
    assert claim["required"] == [
        "claim_key",
        "sentence",
        "stance",
        "evidence_kind",
        "evidence_quote",
        "comment_index",
    ]
    claim_props = claim["properties"]
    assert claim_props["stance"]["enum"] == ["for", "against"]
    assert claim_props["evidence_kind"]["enum"] == ["title", "body", "comment"]
    assert claim_props["claim_key"]["maxLength"] == 200
    assert claim_props["sentence"]["maxLength"] == 400
    assert claim_props["evidence_quote"]["maxLength"] == 300
    index = claim_props["comment_index"]
    assert {"type": "integer", "minimum": 0} in index["anyOf"]
    assert {"type": "null"} in index["anyOf"]
    assert "audience" in index["description"]


def test_maps_three_buckets() -> None:
    schema = schema_document()
    assert CLASSIFICATION_SCHEMA == schema
    assert_schema_document(schema)
    keys = KeyPolicy(
        topic_aliases={"Chips": "半导体"},
        claim_aliases={"Org|Demand Grows|2030|Global": "Org|Demand Rises|2030|Global"},
    )
    must = record("must", title="必须篇", body="必须  阅读原文", fetched_at=at_day(2))
    recommend = record("recommend", title="推荐篇", body="推荐阅读的原文句子", fetched_at=at_day(3))
    skip = record("skip", title="跳过篇", body="可以跳过的原文", fetched_at=at_day(4))
    overlong = "0" * 201
    numbered = tuple(f"{index:02d}|subj|when|where" for index in range(33))
    catalog = ("", overlong, numbered[0]) + numbered

    def handler(_stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences=PREFERENCES)
        by_title = {item["title"]: item for item in parsed["items"]}
        first = {
            "claim_key": "Org|Demand Grows|2030|Global",
            "sentence": "来源认为需求在上升",
            "stance": "for",
            "evidence_kind": "body",
            "evidence_quote": "必须 阅读原文",
            "comment_index": None,
        }
        rows = [
            {
                "id": by_title["跳过篇"]["id"],
                "topic": "  Chips  ",
                "bucket": "skip",
                "reason": "跳过的理由",
                "claims": [],
            },
            {
                "id": by_title["推荐篇"]["id"],
                "topic": "  Chips  ",
                "bucket": "recommend",
                "reason": "推荐的理由",
                "claims": [
                    {
                        "claim_key": "机构|建议阅读|2030|全球",
                        "sentence": "来源建议阅读",
                        "stance": "for",
                        "evidence_kind": "body",
                        "evidence_quote": "推荐阅读的原文句子",
                        "comment_index": None,
                    }
                ],
            },
            {
                "id": by_title["必须篇"]["id"],
                "topic": "  Chips  ",
                "bucket": "must",
                "reason": "必须的理由",
                "claims": [
                    first,
                    dict(first),
                    {
                        "claim_key": "机构|需求下降|UNKNOWN|全球",
                        "sentence": "来源认为需求在下降",
                        "stance": "against",
                        "evidence_kind": "body",
                        "evidence_quote": "阅读原文",
                        "comment_index": None,
                    },
                ],
            },
        ]
        return envelope({"items": rows})

    result, stub = run((skip, recommend, must), handler, preferences=PREFERENCES, keys=keys, known=catalog)
    parsed = load_prompt(stub.prompts[0], preferences=PREFERENCES)
    assert parsed["topic_aliases"] == {"chips": "半导体"}
    assert parsed["claim_aliases"] == {"org|demand grows|2030|global": "org|demand rises|2030|global"}
    assert parsed["known_claim_keys"] == [f"{index:02d}|subj|when|where" for index in range(32)]
    assert overlong not in stub.prompts[0]
    assert parsed["items"] == [prompt_fields(item, asset) for item, asset in (must, recommend, skip)]
    digest = policy_hash(PREFERENCES, keys, schema)
    expected = []
    expected_claims = []
    specs = (
        (must, "must", "必须的理由", ("alias", "unknown")),
        (recommend, "recommend", "推荐的理由", ("recommend",)),
        (skip, "skip", "跳过的理由", ()),
    )
    claim_specs = {
        "alias": ("org|demand rises|2030|global", "来源认为需求在上升", "for", "必须 阅读原文"),
        "unknown": ("机构|需求下降|unknown|全球", "来源认为需求在下降", "against", "阅读原文"),
        "recommend": ("机构|建议阅读|2030|全球", "来源建议阅读", "for", "推荐阅读的原文句子"),
    }
    for pair, bucket, reason, names in specs:
        item, _asset = pair
        expected.append(
            Classification(
                item_id=item.id,
                run_id=RUN,
                topic="半导体",
                bucket=bucket,
                reason=reason,
                analysis_hash=item.analysis_hash,
                policy_hash=digest,
                classified_at=NOW,
            )
        )
        for name in names:
            key, sentence, stance, quote = claim_specs[name]
            expected_claims.append(
                make_claim(
                    item_id=item.id,
                    source_id=item.source_id,
                    analysis_hash=item.analysis_hash,
                    topic="半导体",
                    claim_key=key,
                    sentence=sentence,
                    stance=stance,
                    evidence_kind="body",
                    evidence_quote=quote,
                )
            )
    assert result.classifications == tuple(expected)
    assert result.claims == tuple(expected_claims)
    assert len(stub.prompts) == 1
    assert result.batches[0].batch_id == "b0"
    assert result.batches[0].status == "success"
    assert result.batches[0].error_code is None
    assert result.batches[0].item_ids == tuple(item.id for item, _asset in (must, recommend, skip))


def test_exact_ids_and_batch_atomicity() -> None:
    pairs = [
        record("left", title="左", body="左文足够长", fetched_at=at_day(2)),
        record("right", title="右", body="右文足够长", fetched_at=at_day(3)),
    ]
    result, stub = run(pairs, accept)
    assert [row.item_id for row in result.classifications] == [item.id for item, _asset in pairs]
    assert result.claims == ()
    assert result.batches[0].status == "success"
    assert result.batches[0].error_code is None
    assert len(stub.prompts) == 1

    owned = {item.id for item, _asset in pairs}
    fake = next(candidate for candidate in ("0" * 32, "f" * 32, "1" * 32) if candidate not in owned)
    long_key = "s|" + ("p" * 195) + "|t|u"
    assert len(long_key) == 201

    def payload_for(name: str, parsed: dict) -> dict:
        items = parsed["items"]
        if name == "missing":
            return {"items": [valid_item(items[0])]}
        if name == "duplicate":
            row = valid_item(items[0])
            return {"items": [row, dict(row)]}
        if name == "unknown":
            rows = [valid_item(item) for item in items]
            rows[1]["id"] = fake
            return {"items": rows}
        if name == "later":
            return {"items": [valid_item(items[0], bucket="later"), valid_item(items[1])]}
        if name == "stance":
            return {
                "items": [
                    valid_item(items[0], claims=[body_claim(items[0], stance="abstain")]),
                    valid_item(items[1], claims=[body_claim(items[1])]),
                ]
            }
        if name == "extra":
            rows = [valid_item(item) for item in items]
            rows[0]["analysis_hash"] = "ab" * 32
            return {"items": rows}
        if name == "claim_extra":
            claim = body_claim(items[0])
            claim["note"] = "nope"
            return {"items": [valid_item(items[0], claims=[claim]), valid_item(items[1])]}
        if name == "top_extra":
            return {"items": [valid_item(item) for item in items], "note": "nope"}
        if name == "six":
            return {
                "items": [
                    valid_item(items[0], claims=[body_claim(items[0])] * 6),
                    valid_item(items[1]),
                ]
            }
        if name == "three_parts":
            return {
                "items": [
                    valid_item(items[0], claims=[body_claim(items[0], claim_key="only|three|parts")]),
                    valid_item(items[1]),
                ]
            }
        if name == "long_key":
            return {
                "items": [
                    valid_item(items[0], claims=[body_claim(items[0], claim_key=long_key)]),
                    valid_item(items[1]),
                ]
            }
        if name == "blank_topic":
            return {"items": [valid_item(items[0], topic="   "), valid_item(items[1])]}
        if name == "long_topic":
            return {"items": [valid_item(items[0], topic="x" * 81), valid_item(items[1])]}
        if name == "long_reason":
            return {"items": [valid_item(items[0], reason="r" * 801), valid_item(items[1])]}
        if name == "evidence":
            return {
                "items": [
                    valid_item(items[0], claims=[body_claim(items[0], evidence_kind="caption")]),
                    valid_item(items[1]),
                ]
            }
        raise AssertionError(name)

    cases = (
        ("missing", "missing_id"),
        ("duplicate", "duplicate_id"),
        ("unknown", "unknown_id"),
        ("later", "invalid_bucket"),
        ("stance", "invalid_stance"),
        ("extra", "extra_field"),
        ("claim_extra", "extra_field"),
        ("top_extra", "extra_field"),
        ("six", "too_many_claims"),
        ("three_parts", "invalid_claim_key"),
        ("long_key", "invalid_claim_key"),
        ("blank_topic", "invalid_topic"),
        ("long_topic", "invalid_topic"),
        ("long_reason", "invalid_reason"),
        ("evidence", "invalid_evidence"),
    )
    for name, code in cases:
        def handler(stub: Stub, prompt: str, *, expected_name: str = name) -> CompletionResult:
            parsed = load_prompt(prompt, preferences="偏好")
            return envelope(payload_for(expected_name, parsed))

        result, stub = run(pairs, handler)
        assert result.classifications == (), name
        assert result.claims == (), name
        assert len(result.batches) == 1, name
        assert result.batches[0].status == "invalid", name
        assert result.batches[0].error_code == code, name
        assert result.batches[0].item_ids == tuple(item.id for item, _asset in pairs), name
        assert len(stub.prompts) == 1, name

    item, asset = record("solo")
    with pytest.raises(SchemaError) as missing_asset:
        classify_items(
            (item,),
            {},
            run_id=RUN,
            now=NOW,
            preferences="偏好",
            keys=KeyPolicy(),
            known_claim_keys=(),
            complete_fn=explode,
        )
    assert missing_asset.value.code == "missing_asset"
    other, _other_asset = record("other")
    wrong = Asset(item_id=other.id, transcript_status="not_applicable")
    with pytest.raises(SchemaError) as mismatch:
        classify_items(
            (item,),
            {item.id: wrong},
            run_id=RUN,
            now=NOW,
            preferences="偏好",
            keys=KeyPolicy(),
            known_claim_keys=(),
            complete_fn=explode,
        )
    assert mismatch.value.code == "asset_item_mismatch"
    stale = replace(item, analysis_hash="ab" * 32)
    with pytest.raises(SchemaError) as stale_hash:
        classify_items(
            (stale,),
            {stale.id: asset},
            run_id=RUN,
            now=NOW,
            preferences="偏好",
            keys=KeyPolicy(),
            known_claim_keys=(),
            complete_fn=explode,
        )
    assert stale_hash.value.code == "analysis_hash_mismatch"
    with pytest.raises(SchemaError) as duplicate_input:
        classify(((item, asset), (item, asset)), explode)
    assert duplicate_input.value.code == "duplicate_item"
    with pytest.raises(SchemaError) as bad_run:
        classify(((item, asset),), explode, run_id="bad id")
    assert bad_run.value.code == "invalid_run_id"
    with pytest.raises(SchemaError) as bad_now:
        classify(((item, asset),), explode, now="2030-01-15T00:00:00")
    assert bad_now.value.code == "invalid_timestamp"


def test_batches_by_eight() -> None:
    assert CLASSIFY_BATCH_MAX == 8
    pairs = [
        record(f"n{index:02d}", title=f"标题{index:02d}", body="短正文", fetched_at=at_day(index + 1))
        for index in range(9)
    ]
    ordered = [item.id for item, _asset in pairs]

    def invalid_then_ok(stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences="偏好")
        if len(stub.prompts) == 1:
            return envelope({"items": []})
        return envelope({"items": [valid_item(item) for item in parsed["items"]]})

    result, stub = run(pairs, invalid_then_ok)
    assert [len(json.loads(prompt)["items"]) for prompt in stub.prompts] == [8, 1]
    assert len(stub.prompts) == 2
    assert [batch.batch_id for batch in result.batches] == ["b0", "b1"]
    assert [batch.status for batch in result.batches] == ["invalid", "success"]
    assert result.batches[0].error_code == "missing_id"
    assert result.batches[0].item_ids == tuple(ordered[:8])
    assert result.batches[1].item_ids == tuple(ordered[8:])
    assert result.batches[1].error_code is None
    assert [row.item_id for row in result.classifications] == ordered[8:]
    assert result.claims == ()


def test_prompt_bounds_and_quote_validation() -> None:
    assert PROMPT_BODY_CHARS == 1500
    assert PROMPT_TITLE_CHARS == 300
    assert PROMPT_TOTAL_CHARS == 32000
    assert PROMPT_PREFERENCES_CHARS == 4000
    plain = "A" * 1500 + "UNSENT-TAIL"
    long_item = record(
        "long",
        title="T" * 300 + "TITLE-TAIL",
        body=plain,
        body_raw="RAW-ONLY-SENTINEL" + plain,
        fetched_at=at_day(2),
    )

    def respond(quote: str, kind: str = "body"):
        def handler(_stub: Stub, prompt: str) -> CompletionResult:
            parsed = load_prompt(prompt, preferences="偏好")
            item = parsed["items"][0]
            assert item["body"] == "A" * 1500
            assert item["title"] == "T" * 300
            assert item["truncated"]["body"] is True
            assert item["truncated"]["title"] is True
            assert item == prompt_fields(long_item[0], long_item[1])
            assert "UNSENT-TAIL" not in prompt
            assert "TITLE-TAIL" not in prompt
            assert "RAW-ONLY-SENTINEL" not in prompt
            claim = body_claim(item, evidence_quote=quote, evidence_kind=kind)
            return envelope({"items": [valid_item(item, claims=[claim])]})

        return handler

    rejected, _stub = run([long_item], respond("UNSENT-TAIL"))
    assert rejected.classifications == ()
    assert rejected.claims == ()
    assert rejected.batches[0].status == "invalid"
    assert rejected.batches[0].error_code == "invalid_quote"
    rejected, _stub = run([long_item], respond("TITLE-TAIL", "title"))
    assert rejected.batches[0].error_code == "invalid_quote"
    assert rejected.classifications == ()
    rejected, _stub = run([long_item], respond("RAW-ONLY-SENTINEL"))
    assert rejected.batches[0].error_code == "invalid_quote"
    kept, _stub = run([long_item], respond("A" * 20))
    assert kept.claims[0].evidence_quote == "A" * 20
    assert kept.claims[0].evidence_kind == "body"
    assert kept.classifications[0].analysis_hash == long_item[0].analysis_hash
    kept, _stub = run([long_item], respond("T" * 20, "title"))
    assert kept.claims[0].evidence_quote == "T" * 20
    assert kept.claims[0].evidence_kind == "title"

    alpha = record("alpha", title="大小写", body="Alpha", fetched_at=at_day(2))
    rejected, _stub = run([alpha], respond_quote("alpha"))
    assert rejected.batches[0].error_code == "invalid_quote"
    assert rejected.classifications == ()
    spaced = record("spaced", title="空白", body="Alpha  Beta", fetched_at=at_day(2))
    kept, _stub = run([spaced], respond_quote("Alpha Beta"))
    assert kept.claims[0].evidence_quote == "Alpha Beta"
    wide = record("wide", body="Q" * 400, fetched_at=at_day(2))
    rejected, _stub = run([wide], respond_quote("Q" * 301))
    assert rejected.batches[0].error_code == "invalid_quote"
    assert rejected.classifications == ()

    secret = "P" * 4000 + "SUPERSECRET"
    with pytest.raises(ConfigError) as too_long:
        classify([alpha], explode, preferences=secret)
    assert too_long.value.code == "preferences_too_long"
    assert "SUPERSECRET" not in str(too_long.value)
    assert "SUPERSECRET" not in repr(too_long.value)
    with pytest.raises(ConfigError) as preferred_first:
        classify([alpha, alpha], explode, preferences=secret)
    assert preferred_first.value.code == "preferences_too_long"
    with pytest.raises(ConfigError) as nul:
        classify([alpha], explode, preferences="bad\x00value")
    assert nul.value.code == "invalid_preferences"

    fat = [
        record(
            f"fat{index:02d}",
            title="T" * 300,
            body="B" * 1500,
            channel="C" * 300,
            comments=tuple("M" * 300 for _ in range(5)),
            modality="video",
            fetched_at=at_day(index + 1),
        )
        for index in range(9)
    ]
    pad = KeyPolicy(topic_aliases={"pad": "A" * 20000})

    def accept_fat(_stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences="P" * 4000)
        return envelope({"items": [valid_item(item) for item in parsed["items"]]})

    result, stub = run(fat, accept_fat, preferences="P" * 4000, keys=pad)
    sizes = [len(json.loads(prompt)["items"]) for prompt in stub.prompts]
    seen: list[str] = []
    for prompt in stub.prompts:
        assert len(prompt) <= 32000
        seen.extend(item["id"] for item in json.loads(prompt)["items"])
    assert sizes[0] < 8
    assert max(sizes) <= 8
    assert seen == [item.id for item, _asset in fat]
    assert [row.item_id for row in result.classifications] == seen
    assert all(batch.status == "success" for batch in result.batches)

    tiny = record("tiny", title="短", body="短正文")
    with pytest.raises(SchemaError) as overflow:
        classify([tiny], explode, keys=KeyPolicy(topic_aliases={"pad": "A" * 40000}))
    assert overflow.value.code == "prompt_too_long"
    assert str(overflow.value) == "prompt_too_long"


def respond_quote(quote: str):
    def handler(_stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences="偏好")
        item = parsed["items"][0]
        return envelope({"items": [valid_item(item, claims=[body_claim(item, evidence_quote=quote)])]})

    return handler


def test_comments_reach_prompt_not_votes() -> None:
    assert PROMPT_COMMENT_MAX == 5
    assert PROMPT_COMMENT_CHARS == 300
    comments = (
        "观众认为值得读",
        "第二句观众",
        "y" * 400 + "TAIL",
        "第四句",
        "第五句",
        "UNSENT-COMMENT",
    )
    video = record(
        "clip",
        title="视频标题",
        body="作者正文",
        modality="video",
        url="https://videos.example/watch/unique-item",
        channel="频道甲",
        comments=comments,
        like_count=4,
        view_count=6,
        comment_count=100,
        audio_url="https://cdn.example/files/unique-audio.mp3",
        transcript_url="file:tests/fixtures/video/unique-transcript.vtt",
        fetched_at=at_day(2),
    )

    def comment_row(item: dict, *, quote: str, index, kind: str = "comment", stance: str = "against") -> dict:
        return valid_item(
            item,
            bucket="recommend",
            reason="评论里有对照",
            claims=[
                {
                    "claim_key": "观众|值得阅读|2030|评论区",
                    "sentence": "观众认为值得阅读",
                    "stance": stance,
                    "evidence_kind": kind,
                    "evidence_quote": quote,
                    "comment_index": index,
                }
            ],
        )

    def handler(_stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences="偏好")
        assert parsed["items"] == [prompt_fields(video[0], video[1])]
        assert parsed["items"][0]["audience"] == [
            "观众认为值得读",
            "第二句观众",
            "y" * 300,
            "第四句",
            "第五句",
        ]
        assert "UNSENT-COMMENT" not in prompt
        assert "TAIL" not in prompt
        assert "y" * 301 not in prompt
        assert "频道甲" in prompt
        assert "unique-item" not in prompt
        assert "unique-audio" not in prompt
        assert "unique-transcript" not in prompt
        return envelope({"items": [comment_row(parsed["items"][0], quote="观众认为值得读", index=0)]})

    result, _stub = run([video], handler)
    claim = result.claims[0]
    assert claim == make_claim(
        item_id=video[0].id,
        source_id=video[0].source_id,
        analysis_hash=video[0].analysis_hash,
        topic="主题",
        claim_key="观众|值得阅读|2030|评论区",
        sentence="观众认为值得阅读",
        stance="against",
        evidence_kind="comment",
        evidence_quote="观众认为值得读",
        comment_index=0,
    )
    assert claim.evidence_kind == "comment"
    assert claim.comment_index == 0

    def bad(quote: str, index, kind: str = "comment"):
        def inner(_stub: Stub, prompt: str) -> CompletionResult:
            parsed = load_prompt(prompt, preferences="偏好")
            return envelope({"items": [comment_row(parsed["items"][0], quote=quote, index=index, kind=kind)]})

        return inner

    rejected, _stub = run([video], bad("TAIL", 2))
    assert rejected.classifications == ()
    assert rejected.claims == ()
    assert rejected.batches[0].error_code == "invalid_quote"
    rejected, _stub = run([video], bad("UNSENT-COMMENT", 5))
    assert rejected.batches[0].error_code == "invalid_comment_index"
    assert rejected.classifications == ()
    kept, _stub = run([video], bad("y" * 300, 2, "comment"))
    assert kept.claims[0].evidence_kind == "comment"
    assert kept.claims[0].comment_index == 2
    assert kept.claims[0].evidence_quote == "y" * 300
    rejected, _stub = run([video], bad("y" * 301, 2))
    assert rejected.batches[0].error_code == "invalid_quote"
    rejected, _stub = run([video], bad("作者正文", 0))
    assert rejected.batches[0].error_code == "invalid_quote"
    rejected, _stub = run([video], bad("作者正文", 0, "body"))
    assert rejected.batches[0].error_code == "invalid_comment_index"
    rejected, _stub = run([video], bad("观众认为值得读", None))
    assert rejected.batches[0].error_code == "invalid_comment_index"
    rejected, _stub = run([video], bad("观众认为值得读", -1))
    assert rejected.batches[0].error_code == "invalid_comment_index"
    rejected, _stub = run([video], bad("观众认为值得读", "0"))
    assert rejected.batches[0].error_code == "invalid_comment_index"

    def mutated(bad_index):
        def inner(_stub: Stub, prompt: str) -> CompletionResult:
            parsed = load_prompt(prompt, preferences="偏好")
            result = envelope({"items": [comment_row(parsed["items"][0], quote="观众认为值得读", index=0)]})
            result.payload["items"][0]["claims"][0]["comment_index"] = bad_index
            return result

        return inner

    for bad_index in (True, 1.0, float("nan"), float("inf")):
        rejected, _stub = run([video], mutated(bad_index))
        assert rejected.batches[0].status == "invalid"
        assert rejected.batches[0].error_code == "invalid_comment_index"
        assert rejected.classifications == ()

    fewer = record(
        "few-count",
        title="计数更小",
        body="正文乙",
        modality="video",
        comments=("c0 text", "c1 text", "c2 text"),
        comment_count=1,
        fetched_at=at_day(2),
    )

    def allow_index_two(_stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences="偏好")
        assert parsed["items"][0]["audience"] == ["c0 text", "c1 text", "c2 text"]
        return envelope({"items": [comment_row(parsed["items"][0], quote="c2 text", index=2)]})

    kept, _stub = run([fewer], allow_index_two)
    assert kept.claims[0].comment_index == 2
    assert kept.claims[0].evidence_kind == "comment"

    larger = record(
        "many-count",
        title="计数更大",
        body="正文丙",
        modality="video",
        comments=("only one", "only two"),
        comment_count=90,
        fetched_at=at_day(2),
    )
    rejected, _stub = run([larger], bad("only two", 2))
    assert rejected.batches[0].error_code == "invalid_comment_index"
    assert rejected.classifications == ()


def test_pending_and_duplicates_skipped() -> None:
    valid = record("valid", title="VALID-MARKER", body="有效正文", fetched_at=at_day(2))
    boundary = record("boundary", title="TODAY-MARKER", body="今天发布", published_at=NOW, fetched_at=at_day(3))
    empty = record("empty", title="EMPTY-BODY", body="", fetched_at=at_day(4))
    pending = record(
        "pending",
        title="PENDING-MARKER",
        body="",
        modality="podcast",
        transcript_status="pending_transcript",
        fetched_at=at_day(5),
    )
    future = record("future", title="FUTURE-MARKER", body="未来正文", published_at=FUTURE, fetched_at=at_day(6))
    nodate = record("nodate", title="NODATE-MARKER", body="无日期正文", published_at=None, fetched_at=at_day(7))
    duplicate = record("dup", title="DUP-MARKER", body="重复正文", duplicate_of=valid[0].id, fetched_at=at_day(8))
    kept = (valid, boundary, empty)
    items = tuple(item for item, _asset in kept + (pending, future, nodate, duplicate))
    assets = {item.id: asset for item, asset in kept + (pending, future, nodate)}

    def handler(_stub: Stub, prompt: str) -> CompletionResult:
        parsed = load_prompt(prompt, preferences="偏好")
        return envelope({"items": [valid_item(item) for item in parsed["items"]]})

    stub = Stub(handler)
    result = classify_items(
        items,
        assets,
        run_id=RUN,
        now=NOW,
        preferences="偏好",
        keys=KeyPolicy(),
        known_claim_keys=(),
        complete_fn=stub,
    )
    parsed = load_prompt(stub.prompts[0], preferences="偏好")
    assert [item["id"] for item in parsed["items"]] == [item.id for item, _asset in kept]
    assert parsed["items"] == [prompt_fields(item, asset) for item, asset in kept]
    for marker in ("PENDING-MARKER", "FUTURE-MARKER", "NODATE-MARKER", "DUP-MARKER"):
        assert marker not in stub.prompts[0]
    for marker in ("VALID-MARKER", "TODAY-MARKER", "EMPTY-BODY"):
        assert marker in stub.prompts[0]
    assert [row.item_id for row in result.classifications] == [item.id for item, _asset in kept]
    assert len(stub.prompts) == 1

    skipped_items = tuple(item for item, _asset in (pending, future, nodate, duplicate))
    skipped_assets = {pending[0].id: pending[1], future[0].id: future[1], nodate[0].id: nodate[1]}
    quiet = Stub(handler)
    result = classify_items(
        skipped_items,
        skipped_assets,
        run_id=RUN,
        now=NOW,
        preferences="偏好",
        keys=KeyPolicy(),
        known_claim_keys=(),
        complete_fn=quiet,
    )
    assert quiet.prompts == []
    assert result.classifications == ()
    assert result.claims == ()
    assert result.batches == ()


def test_grok_unavailable_stops_later_calls() -> None:
    short = [
        record(f"s{index:02d}", title=f"短{index:02d}", body="短正文", fetched_at=at_day(index + 1)) for index in range(9)
    ]
    ordered = [item.id for item, _asset in short]

    def unavailable(stub: Stub, prompt: str) -> CompletionResult:
        load_prompt(prompt, preferences="偏好")
        raise GrokUnavailable("auth_failed")

    result, stub = run(short, unavailable)
    assert len(stub.prompts) == 1
    assert result.classifications == ()
    assert result.claims == ()
    assert [batch.status for batch in result.batches] == ["unavailable", "unavailable"]
    assert [batch.error_code for batch in result.batches] == ["auth_failed", "auth_failed"]
    assert [batch.item_ids for batch in result.batches] == [tuple(ordered[:8]), tuple(ordered[8:])]
    assert [batch.batch_id for batch in result.batches] == ["b0", "b1"]

    def budget_first(stub: Stub, prompt: str) -> CompletionResult:
        load_prompt(prompt, preferences="偏好")
        raise BudgetExceeded("classify")

    result, stub = run(short, budget_first)
    assert len(stub.prompts) == 1
    assert result.classifications == ()
    assert [batch.status for batch in result.batches] == ["budget_skipped", "budget_skipped"]
    assert [batch.error_code for batch in result.batches] == ["budget_exceeded", "budget_exceeded"]

    longer = [
        record(f"l{index:02d}", title=f"长列{index:02d}", body="短正文", fetched_at=at_day(index + 1)) for index in range(17)
    ]
    ids = [item.id for item, _asset in longer]

    def stop_on_second(exc: Exception):
        def handler(stub: Stub, prompt: str) -> CompletionResult:
            parsed = load_prompt(prompt, preferences="偏好")
            if len(stub.prompts) == 2:
                raise exc
            return envelope({"items": [valid_item(item) for item in parsed["items"]]})

        return handler

    result, stub = run(longer, stop_on_second(GrokUnavailable("timeout")))
    assert len(stub.prompts) == 2
    assert [row.item_id for row in result.classifications] == ids[:8]
    assert result.classifications[0].run_id == RUN
    assert result.classifications[0].bucket == "must"
    assert [batch.status for batch in result.batches] == ["success", "unavailable", "unavailable"]
    assert [batch.error_code for batch in result.batches] == [None, "timeout", "timeout"]
    assert [batch.item_ids for batch in result.batches] == [tuple(ids[:8]), tuple(ids[8:16]), tuple(ids[16:])]

    result, stub = run(longer, stop_on_second(BudgetExceeded("review")))
    assert len(stub.prompts) == 2
    assert [row.item_id for row in result.classifications] == ids[:8]
    assert [batch.status for batch in result.batches] == ["success", "budget_skipped", "budget_skipped"]
    assert [batch.error_code for batch in result.batches] == [None, "budget_exceeded", "budget_exceeded"]
    assert result.batches[2].item_ids == tuple(ids[16:])
