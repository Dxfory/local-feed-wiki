"""Multi-source reviews: deterministic rows, optional short model notes, no wiki writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from second_brain.errors import BudgetExceeded, GrokUnavailable, SchemaError
from second_brain.models import (
    PROMPT_TOTAL_CHARS,
    REVIEW_ROWS_MAX,
    REVIEW_SENTENCE_CHARS,
    REVIEW_SENTENCE_MAX,
    REVIEW_SENTENCE_MIN,
    REVIEW_TEMPLATE_VERSION,
    REVIEW_TOPIC_BATCH_MAX,
    TOPIC_CHARS,
    ClassifiedItem,
    CompletionResult,
    ReviewSentence,
    Usage,
    canonical_json,
    make_claim,
    normalize_key,
)
from second_brain.review.multi_source import (
    INPUT_TOO_LARGE,
    NOT_REQUESTED_NOTE,
    NO_CONFIRMED_DIFFERENCE_NOTE,
    REVIEW_SCHEMA,
    SELECTION_LIMIT_NOTE,
    build_topic_reviews,
    enrich_topic_reviews,
    summary_note,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "review" / "synthetic_topics.json"
SCHEMA_PATH = ROOT / "src" / "second_brain" / "review" / "schema.json"
NOW = "2030-01-15T00:00:00.000000Z"
POLICY = "ab" * 32


def _stamp(day: int) -> str:
    return f"2030-01-{day:02d}T00:00:00.000000Z"


def _row(
    *,
    source: str,
    topic: str,
    serial: int,
    title: str = "标题",
    bucket: str = "must",
    reason: str = "理由写在这里",
    comments: tuple[str, ...] = (),
    published_at: str | None = NOW,
    policy_hash: str = POLICY,
    analysis_serial: int | None = None,
    item_serial: int | None = None,
    claim: dict[str, object] | None = None,
) -> ClassifiedItem:
    item_hex = f"{(serial if item_serial is None else item_serial):032x}"
    analysis = f"{(serial if analysis_serial is None else analysis_serial):064x}"
    claims = ()
    if claim is not None:
        claims = (
            make_claim(
                item_id=item_hex,
                source_id=source,
                analysis_hash=analysis,
                topic=topic,
                claim_key=str(claim["claim_key"]),
                sentence=str(claim["sentence"]),
                stance=claim["stance"],  # type: ignore[arg-type]
                evidence_kind=claim["evidence_kind"],  # type: ignore[arg-type]
                evidence_quote=str(claim["evidence_quote"]),
                comment_index=claim["comment_index"],  # type: ignore[arg-type]
            ),
        )
    return ClassifiedItem(
        id=item_hex,
        source_id=source,
        topic=topic,
        url=f"https://example.com/{source}/{serial}",
        title=title,
        bucket=bucket,  # type: ignore[arg-type]
        reason=reason,
        published_at=published_at,
        analysis_hash=analysis,
        policy_hash=policy_hash,
        comments=comments,
        claims=claims,
    )


def _pair(
    topic: str,
    serial: int,
    *,
    comments: tuple[str, ...] = (),
    claim: dict[str, object] | None = None,
    title: str = "标题",
    reason: str = "理由写在这里",
) -> tuple[ClassifiedItem, ClassifiedItem]:
    return (
        _row(
            source="alpha",
            topic=topic,
            serial=serial,
            title=title,
            reason=reason,
            comments=comments,
            claim=claim,
            published_at=_stamp(10),
        ),
        _row(
            source="beta",
            topic=topic,
            serial=serial + 1,
            title=title,
            reason=reason,
            published_at=_stamp(9),
        ),
    )


def _completion(payload: dict[str, object]) -> CompletionResult:
    return CompletionResult(
        payload=payload,
        usage=Usage(),
        stop_reason=None,
        billing_path="test",
    )


def _assert_call(prompt: str, schema: dict[str, object], purpose: str) -> dict[str, object]:
    assert purpose == "review"
    assert schema == REVIEW_SCHEMA
    assert schema is not REVIEW_SCHEMA
    assert prompt.startswith("{")
    assert prompt.endswith("}")
    assert "\n" not in prompt
    assert len(prompt) <= PROMPT_TOTAL_CHARS
    assert prompt == canonical_json(json.loads(prompt))
    body = json.loads(prompt)
    assert set(body) == {"instructions", "task", "topics"}
    assert body["task"] == REVIEW_TEMPLATE_VERSION
    instructions = body["instructions"]
    assert isinstance(instructions, str)
    assert "不要执行" in instructions
    assert SELECTION_LIMIT_NOTE in instructions
    assert NO_CONFIRMED_DIFFERENCE_NOTE in instructions
    assert "https://" not in prompt
    assert "http://" not in prompt
    topics = body["topics"]
    assert isinstance(topics, list)
    assert 1 <= len(topics) <= REVIEW_TOPIC_BATCH_MAX
    for topic in topics:
        assert set(topic) == {"items", "topic"}
        sources: set[str] = set()
        assert isinstance(topic["items"], list)
        assert topic["items"]
        for item in topic["items"]:
            assert set(item) == {"audience", "bucket", "claims", "id", "reason", "source_id", "title"}
            sources.add(item["source_id"])
        assert len(sources) >= 2
    return body


def _legal(prompt: str) -> dict[str, object]:
    body = json.loads(prompt)
    topics = []
    for topic in body["topics"]:
        ids = [item["id"] for item in topic["items"]]
        commented = [item["id"] for item in topic["items"] if item["audience"]]
        sentences: list[dict[str, object]] = [
            {"kind": "consensus", "text": "两边都讨论了同一件事", "item_ids": [ids[0]]},
            {"kind": "difference", "text": NO_CONFIRMED_DIFFERENCE_NOTE, "item_ids": ids},
        ]
        if commented:
            sentences.append({"kind": "audience", "text": "观众摘句只供对照", "item_ids": [commented[0]]})
        topics.append({"topic": topic["topic"], "sentences": sentences})
    return {"topics": topics}


def _ok_complete(calls: list[str]):
    def complete(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        calls.append(prompt)
        return _completion(_legal(prompt))

    return complete


def _load_rows(spec: list[dict[str, object]], topic: str, serial_base: int) -> list[ClassifiedItem]:
    rows = []
    for offset, entry in enumerate(spec):
        mark = str(entry["policy_mark"])
        claim = entry.get("claim")
        rows.append(
            _row(
                source=str(entry["source_id"]),
                topic=topic,
                serial=serial_base + offset,
                title=str(entry["title"]),
                bucket=str(entry["bucket"]),
                reason=str(entry["reason"]),
                comments=tuple(entry["comments"]),  # type: ignore[arg-type]
                policy_hash=mark * 64,
                claim=claim if isinstance(claim, dict) else None,
            )
        )
    return rows


def test_review_contains_both_sources_and_reasons() -> None:
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    multi_rows = _load_rows(spec["multi"], spec["multi_topic"], 1)
    single_rows = _load_rows(spec["single"], spec["single_topic"], 20)
    reviews = build_topic_reviews([*reversed(multi_rows), *reversed(single_rows)])

    assert [review.topic for review in reviews] == sorted(
        (spec["multi_topic"], spec["single_topic"]),
        key=lambda topic: (normalize_key(topic), topic),
    )
    by_topic = {review.topic: review for review in reviews}
    multi = by_topic[spec["multi_topic"]]
    single = by_topic[spec["single_topic"]]

    assert multi.summary_status == "not_requested"
    assert multi.sentences == ()
    assert [row.source_id for row in multi.rows] == ["alpha", "beta"]
    assert [(row.source_id, row.id) for row in multi.rows] == sorted(
        (row.source_id, row.id) for row in multi.rows
    )
    alpha, beta = multi.rows
    assert alpha.url == "https://example.com/alpha/2"
    assert alpha.title == "甲源标题"
    assert alpha.bucket == "must"
    assert alpha.reason == "甲源写了全年增长"
    assert alpha.comments == ("观众说要看分季",)
    assert alpha.policy_hash == "a" * 64
    assert len(alpha.claims) == 1
    assert alpha.claims[0].evidence_kind == "comment"
    assert alpha.claims[0].comment_index == 0
    assert alpha.claims[0].evidence_quote == "观众说要看分季"
    assert beta.url == "https://example.com/beta/1"
    assert beta.title == "乙源标题"
    assert beta.bucket == "recommend"
    assert beta.reason == "乙源只写了区间"
    assert beta.comments == ()
    assert beta.policy_hash == "b" * 64
    assert beta.claims == ()
    assert {id(row) for row in multi.rows} == {id(row) for row in multi_rows}

    assert single.summary_status == "single_source"
    assert single.sentences == ()
    assert {row.source_id for row in single.rows} == {"alpha"}
    assert [row.title for row in single.rows] == ["同题第二条", "同题第一条"]
    assert single.rows[0].comments == ()
    assert single.rows[1].comments == ("观众甲",)
    assert {row.policy_hash for row in single.rows} == {"c" * 64, "d" * 64}
    assert build_topic_reviews(()) == ()


def test_review_failure_preserves_rows() -> None:
    stale_text = "旧短评不能沿用"
    timeout_items = [item for index in range(5) for item in _pair(f"t{index}", 100 + index * 2)]
    timeout_reviews = list(build_topic_reviews(timeout_items))
    steady_items = _pair("steady", 200)
    steady = build_topic_reviews(steady_items)[0]
    seeded = timeout_reviews[0]
    timeout_reviews[0] = type(seeded)(
        topic=seeded.topic,
        rows=seeded.rows,
        sentences=(ReviewSentence(kind="limit", text=stale_text, item_ids=(seeded.rows[0].id,)),),
        summary_status="ok",
    )
    changed = frozenset(review.topic for review in timeout_reviews)
    calls: list[str] = []

    def timeout(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        calls.append(prompt)
        raise GrokUnavailable("timeout")

    timed = enrich_topic_reviews(
        [*timeout_reviews, steady],
        changed_topics=changed,
        complete_fn=timeout,
    )
    assert len(calls) == 1
    assert len(json.loads(calls[0])["topics"]) <= REVIEW_TOPIC_BATCH_MAX
    for review, original in zip(timed[:5], timeout_reviews, strict=True):
        assert review.rows is original.rows
        assert review.summary_status == "failed"
        assert review.sentences == ()
        assert stale_text not in "".join(sentence.text for sentence in review.sentences)
    assert timed[5].rows is steady.rows
    assert timed[5].summary_status == "not_requested"
    assert timed[5].sentences == ()

    bad_review = build_topic_reviews(_pair("bounds", 300))[0]
    sentinel = "SENTINEL_BAD_SENTENCE"
    selected_id = bad_review.rows[0].id

    def bad_topic(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        return _completion(
            {
                "topics": [
                    {
                        "topic": "不在请求里",
                        "sentences": [
                            {"kind": "consensus", "text": sentinel, "item_ids": [selected_id]},
                            {"kind": "difference", "text": "另一句", "item_ids": [selected_id]},
                        ],
                    }
                ]
            }
        )

    rejected = enrich_topic_reviews(
        (bad_review,),
        changed_topics=frozenset({bad_review.topic}),
        complete_fn=bad_topic,
    )[0]
    assert rejected.rows is bad_review.rows
    assert rejected.summary_status == "failed"
    assert rejected.sentences == ()
    assert sentinel not in rejected.topic

    def bad_id(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        return _completion(
            {
                "topics": [
                    {
                        "topic": bad_review.topic,
                        "sentences": [
                            {"kind": "consensus", "text": sentinel, "item_ids": ["f" * 32]},
                            {"kind": "difference", "text": "另一句", "item_ids": [selected_id]},
                        ],
                    }
                ]
            }
        )

    missed = enrich_topic_reviews(
        (bad_review,),
        changed_topics=frozenset({bad_review.topic}),
        complete_fn=bad_id,
    )[0]
    assert missed.rows is bad_review.rows
    assert missed.summary_status == "failed"
    assert missed.sentences == ()
    assert all(sentence.text != sentinel for sentence in missed.sentences)


def test_review_schema_bounds() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema == REVIEW_SCHEMA
    sentence_schema = schema["properties"]["topics"]["items"]["properties"]["sentences"]
    sentence_item = sentence_schema["items"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["topics"]
    assert schema["properties"]["topics"]["maxItems"] == REVIEW_TOPIC_BATCH_MAX
    assert sentence_schema["minItems"] == REVIEW_SENTENCE_MIN
    assert sentence_schema["maxItems"] == REVIEW_SENTENCE_MAX
    assert sentence_item["additionalProperties"] is False
    assert sentence_item["properties"]["kind"]["enum"] == ["consensus", "difference", "audience", "limit"]
    assert sentence_item["properties"]["text"]["maxLength"] == REVIEW_SENTENCE_CHARS
    assert sentence_item["properties"]["item_ids"]["minItems"] == 1
    assert sentence_item["properties"]["item_ids"]["uniqueItems"] is True
    assert schema["properties"]["topics"]["items"]["properties"]["topic"]["maxLength"] == TOPIC_CHARS
    assert schema["properties"]["topics"]["items"]["additionalProperties"] is False

    base = build_topic_reviews(_pair("shape", 400))[0]
    ids = [row.id for row in base.rows]
    failures: list[str] = []

    def run_case(name: str, payload: dict[str, object], expected: str, review: object = base) -> None:
        calls: list[str] = []

        def complete(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
            _assert_call(prompt, schema, purpose)
            calls.append(prompt)
            return _completion(payload)

        try:
            result = enrich_topic_reviews(
                (review,),  # type: ignore[arg-type]
                changed_topics=frozenset({review.topic}),  # type: ignore[attr-defined]
                complete_fn=complete,
            )[0]
            assert result.summary_status == expected, result.summary_status
            assert result.rows is review.rows  # type: ignore[attr-defined]
            if expected == "ok":
                assert REVIEW_SENTENCE_MIN <= len(result.sentences) <= REVIEW_SENTENCE_MAX
            else:
                assert result.sentences == ()
                assert calls
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")

    def sentences(count: int, *, kind_override: str | None = None, text: str | None = None, item_ids: list[str] | None = None) -> list[dict[str, object]]:
        built = [
            {"kind": "consensus", "text": "3.14 是同一数字", "item_ids": [ids[0]]},
            {"kind": "difference", "text": "等待...", "item_ids": ids},
        ]
        while len(built) < count:
            built.append({"kind": "limit", "text": f"补充{len(built)}", "item_ids": [ids[0]]})
        if kind_override is not None:
            built[0]["kind"] = kind_override
        if text is not None:
            built[0]["text"] = text
        if item_ids is not None:
            built[0]["item_ids"] = item_ids
        return built[:count]

    for count in range(REVIEW_SENTENCE_MIN, REVIEW_SENTENCE_MAX + 1):
        run_case(f"accept_{count}", {"topics": [{"topic": base.topic, "sentences": sentences(count)}]}, "ok")

    run_case(
        "accept_300_chars",
        {
            "topics": [
                {
                    "topic": base.topic,
                    "sentences": [
                        {"kind": "consensus", "text": ("句" * 299) + "。", "item_ids": [ids[0]]},
                        {"kind": "difference", "text": "等待...", "item_ids": ids},
                    ],
                }
            ]
        },
        "ok",
    )
    commented = build_topic_reviews(_pair("heard", 420, comments=("观众甲",)))[0]
    commented_ids = [row.id for row in commented.rows]
    run_case(
        "accept_audience",
        {
            "topics": [
                {
                    "topic": commented.topic,
                    "sentences": [
                        {"kind": "consensus", "text": "结论。", "item_ids": [commented_ids[0]]},
                        {"kind": "difference", "text": "等待...", "item_ids": commented_ids},
                        {"kind": "audience", "text": "观众只是摘句", "item_ids": [commented_ids[0]]},
                    ],
                }
            ]
        },
        "ok",
        commented,
    )

    run_case("nine_sentences", {"topics": [{"topic": base.topic, "sentences": sentences(9)}]}, "failed")
    run_case("one_sentence", {"topics": [{"topic": base.topic, "sentences": sentences(1)}]}, "failed")
    run_case(
        "missing_consensus",
        {
            "topics": [
                {
                    "topic": base.topic,
                    "sentences": [
                        {"kind": "difference", "text": "只有差异", "item_ids": ids},
                        {"kind": "difference", "text": "还是差异", "item_ids": ids},
                    ],
                }
            ]
        },
        "failed",
    )
    run_case(
        "missing_difference",
        {
            "topics": [
                {
                    "topic": base.topic,
                    "sentences": [
                        {"kind": "consensus", "text": "只有一致", "item_ids": [ids[0]]},
                        {"kind": "consensus", "text": "仍然一致", "item_ids": ids},
                    ],
                }
            ]
        },
        "failed",
    )
    run_case("empty_item_ids", {"topics": [{"topic": base.topic, "sentences": sentences(2, item_ids=[])}]}, "failed")
    run_case(
        "duplicate_item_ids",
        {"topics": [{"topic": base.topic, "sentences": sentences(2, item_ids=[ids[0], ids[0]])}]},
        "failed",
    )
    run_case(
        "two_terminators",
        {"topics": [{"topic": base.topic, "sentences": sentences(2, text="结论。还有。")}]},
        "failed",
    )
    run_case(
        "newline_sentence",
        {"topics": [{"topic": base.topic, "sentences": sentences(2, text="第一句\n第二句")}]},
        "failed",
    )
    run_case(
        "text_too_long",
        {"topics": [{"topic": base.topic, "sentences": sentences(2, text="句" * (REVIEW_SENTENCE_CHARS + 1))}]},
        "failed",
    )
    run_case(
        "extra_field",
        {
            "topics": [
                {
                    "topic": base.topic,
                    "sentences": [
                        {"kind": "consensus", "text": "一致", "item_ids": [ids[0]], "note": "no"},
                        {"kind": "difference", "text": "等待...", "item_ids": ids},
                    ],
                }
            ]
        },
        "failed",
    )
    run_case(
        "foreign_topic",
        {
            "topics": [
                {
                    "topic": "请求外主题",
                    "sentences": sentences(2),
                }
            ]
        },
        "failed",
    )
    run_case(
        "bad_kind",
        {"topics": [{"topic": base.topic, "sentences": sentences(2, kind_override="Consensus")}]},
        "failed",
    )
    run_case(
        "missing_audience",
        {
            "topics": [
                {
                    "topic": commented.topic,
                    "sentences": [
                        {"kind": "consensus", "text": "结论。", "item_ids": [commented_ids[0]]},
                        {"kind": "difference", "text": "等待...", "item_ids": commented_ids},
                    ],
                }
            ]
        },
        "failed",
        commented,
    )
    run_case(
        "audience_without_comments",
        {
            "topics": [
                {
                    "topic": base.topic,
                    "sentences": [
                        {"kind": "consensus", "text": "一致", "item_ids": [ids[0]]},
                        {"kind": "difference", "text": "等待...", "item_ids": ids},
                        {"kind": "audience", "text": "不该有观众", "item_ids": [ids[0]]},
                    ],
                }
            ]
        },
        "failed",
    )
    run_case(
        "audience_points_at_silent_item",
        {
            "topics": [
                {
                    "topic": commented.topic,
                    "sentences": [
                        {"kind": "consensus", "text": "结论。", "item_ids": [commented_ids[0]]},
                        {"kind": "difference", "text": "等待...", "item_ids": commented_ids},
                        {"kind": "audience", "text": "指错了材料", "item_ids": [commented_ids[1]]},
                    ],
                }
            ]
        },
        "failed",
        commented,
    )

    pair_items = [*_pair("left", 440), *_pair("right", 460)]
    pair_reviews = build_topic_reviews(pair_items)
    left, right = pair_reviews
    good_left = [
        {"kind": "consensus", "text": "左边一致", "item_ids": [left.rows[0].id]},
        {"kind": "difference", "text": "等待...", "item_ids": [row.id for row in left.rows]},
    ]
    good_right = [
        {"kind": "consensus", "text": "右边一致", "item_ids": [right.rows[0].id]},
        {"kind": "difference", "text": "等待...", "item_ids": [row.id for row in right.rows]},
    ]

    def run_pair(name: str, payload: dict[str, object], expected: tuple[str, str]) -> None:
        def complete(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
            _assert_call(prompt, schema, purpose)
            return _completion(payload)

        try:
            result = enrich_topic_reviews(
                pair_reviews,
                changed_topics=frozenset({left.topic, right.topic}),
                complete_fn=complete,
            )
            assert tuple(review.summary_status for review in result) == expected
            assert result[0].rows is left.rows
            assert result[1].rows is right.rows
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")

    run_pair(
        "missing_requested_topic",
        {"topics": [{"topic": left.topic, "sentences": good_left}]},
        ("failed", "failed"),
    )
    run_pair(
        "duplicate_topic",
        {
            "topics": [
                {"topic": left.topic, "sentences": good_left},
                {"topic": left.topic, "sentences": good_left},
            ]
        },
        ("failed", "failed"),
    )
    run_pair(
        "extra_topic_beside_request",
        {
            "topics": [
                {"topic": left.topic, "sentences": good_left},
                {"topic": right.topic, "sentences": good_right},
                {"topic": "请求外主题", "sentences": good_left},
            ]
        },
        ("failed", "failed"),
    )

    continued_items = [item for index in range(5) for item in _pair(f"c{index}", 500 + index * 2)]
    continued = build_topic_reviews(continued_items)
    calls: list[str] = []

    def continue_after_bad(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        calls.append(prompt)
        if len(calls) == 1:
            return _completion({"topics": [{"topic": "请求外主题", "sentences": sentences(2)}]})
        return _completion(_legal(prompt))

    continued_result = enrich_topic_reviews(
        continued,
        changed_topics=frozenset(review.topic for review in continued),
        complete_fn=continue_after_bad,
    )
    assert len(calls) == 2
    assert sum(review.summary_status == "failed" for review in continued_result) == REVIEW_TOPIC_BATCH_MAX
    assert sum(review.summary_status == "ok" for review in continued_result) == 1
    assert all(review.rows is original.rows for review, original in zip(continued_result, continued, strict=True))
    assert not failures, "\n".join(failures)


def test_review_batches_and_input_scope() -> None:
    claim = {
        "claim_key": "机构A|需求同比增长|2030年度|业务B",
        "sentence": "2030年度业务B需求将同比增长",
        "stance": "for",
        "evidence_kind": "comment",
        "evidence_quote": "观众说要看分季",
        "comment_index": 0,
    }
    items: list[ClassifiedItem] = []
    for index, topic in enumerate(("t1", "t2", "t3", "t4", "t5", "steady")):
        extra_comments = ("观众说要看分季",) if topic == "t1" else ()
        extra_claim = claim if topic == "t1" else None
        items.extend(
            _pair(topic, 600 + index * 2, comments=extra_comments, claim=extra_claim, title=f"{topic}标题")
        )
    items.append(_row(source="solo", topic="solo", serial=680, title="单源一"))
    items.append(_row(source="solo", topic="solo", serial=681, title="单源二"))
    built = list(build_topic_reviews(items))
    by_topic = {review.topic: review for review in built}
    steady = by_topic["steady"]
    stale = ReviewSentence(kind="limit", text="旧段不能复用", item_ids=(steady.rows[0].id,))
    by_topic["steady"] = type(steady)(
        topic=steady.topic,
        rows=steady.rows,
        sentences=(stale,),
        summary_status="ok",
    )
    order = ["t3", "steady", "t1", "solo", "t5", "t2", "t4"]
    shuffled = tuple(by_topic[topic] for topic in order)
    calls: list[str] = []

    def budget_on_second(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        calls.append(prompt)
        if len(calls) > 1:
            raise BudgetExceeded("review")
        return _completion(_legal(prompt))

    result = enrich_topic_reviews(
        shuffled,
        changed_topics=frozenset({"t1", "t2", "t3", "t4", "t5", "solo"}),
        complete_fn=budget_on_second,
    )
    assert [review.topic for review in result] == order
    assert len(calls) == 2
    first_topics = [topic["topic"] for topic in json.loads(calls[0])["topics"]]
    second_topics = [topic["topic"] for topic in json.loads(calls[1])["topics"]]
    assert first_topics == ["t1", "t2", "t3", "t4"]
    assert second_topics == ["t5"]
    assert "steady" not in first_topics + second_topics
    assert "solo" not in first_topics + second_topics
    assert "https://example.com/" not in "".join(calls)
    first_body = json.loads(calls[0])
    t1_items = next(topic["items"] for topic in first_body["topics"] if topic["topic"] == "t1")
    assert t1_items[0]["audience"] == ["观众说要看分季"]
    assert t1_items[0]["claims"][0]["evidence_kind"] == "comment"
    assert t1_items[0]["claims"][0]["comment_index"] == 0
    assert t1_items[0]["claims"][0]["sentence"] == "2030年度业务B需求将同比增长"
    assert "url" not in t1_items[0]
    statuses = {review.topic: review.summary_status for review in result}
    assert statuses == {
        "t1": "ok",
        "t2": "ok",
        "t3": "ok",
        "t4": "ok",
        "t5": "budget_skipped",
        "steady": "not_requested",
        "solo": "single_source",
    }
    steady_result = next(review for review in result if review.topic == "steady")
    assert steady_result.sentences == ()
    assert steady_result.rows is by_topic["steady"].rows
    assert all(review.rows is by_topic[review.topic].rows for review in result)
    assert summary_note("not_requested") == NOT_REQUESTED_NOTE == "本次未请求短评"

    early_calls: list[str] = []

    def budget_immediately(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        _assert_call(prompt, schema, purpose)
        early_calls.append(prompt)
        raise BudgetExceeded("review")

    early = enrich_topic_reviews(
        tuple(by_topic[topic] for topic in ("t1", "t2", "t3", "t4", "t5")),
        changed_topics=frozenset({"t1", "t2", "t3", "t4", "t5"}),
        complete_fn=budget_immediately,
    )
    assert len(early_calls) == 1
    assert len(json.loads(early_calls[0])["topics"]) == REVIEW_TOPIC_BATCH_MAX
    assert {review.summary_status for review in early} == {"budget_skipped"}

    cap_items = [
        _row(source=f"s{day}", topic="cap", serial=700 + day, published_at=_stamp(day), title=f"源{day}")
        for day in range(1, 9)
    ]
    cap_items.append(_row(source="undated", topic="cap", serial=720, published_at=None, title="没有日期"))
    cap_review = build_topic_reviews(cap_items)[0]
    cap_calls: list[str] = []
    cap_result = enrich_topic_reviews(
        (cap_review,),
        changed_topics=frozenset({"cap"}),
        complete_fn=_ok_complete(cap_calls),
    )[0]
    cap_body = json.loads(cap_calls[0])
    cap_prompt_items = cap_body["topics"][0]["items"]
    assert len(cap_prompt_items) == REVIEW_ROWS_MAX
    assert {item["source_id"] for item in cap_prompt_items} == {f"s{day}" for day in range(1, 9)}
    assert "undated" not in {item["source_id"] for item in cap_prompt_items}
    assert len(cap_result.rows) == 9
    assert cap_result.summary_status == "ok"
    tie_items = [
        _row(source="alpha", topic="tie", serial=730, item_serial=2, published_at=_stamp(12), title="较新编号"),
        _row(source="alpha", topic="tie", serial=731, item_serial=1, published_at=_stamp(12), title="较小编号"),
        _row(source="beta", topic="tie", serial=732, published_at=_stamp(11), title="乙源"),
    ]
    tie_review = build_topic_reviews(tie_items)[0]
    tie_calls: list[str] = []
    enrich_topic_reviews(
        (tie_review,),
        changed_topics=frozenset({"tie"}),
        complete_fn=_ok_complete(tie_calls),
    )
    tie_ids = [item["id"] for item in json.loads(tie_calls[0])["topics"][0]["items"]]
    assert tie_ids.index(f"{1:032x}") < tie_ids.index(f"{2:032x}")

    filler_items = []
    for source, serial_base in (("alpha", 740), ("beta", 760)):
        for offset in range(6):
            filler_items.append(
                _row(
                    source=source,
                    topic="filler",
                    serial=serial_base + offset,
                    title="题" * 4500,
                    reason="因" * 40,
                    published_at=_stamp(20 - offset),
                )
            )
    filler_review = build_topic_reviews(filler_items)[0]
    filler_calls: list[str] = []
    filler_result = enrich_topic_reviews(
        (filler_review,),
        changed_topics=frozenset({"filler"}),
        complete_fn=_ok_complete(filler_calls),
    )[0]
    filler_prompt = json.loads(filler_calls[0])["topics"][0]["items"]
    filler_sources = {item["source_id"] for item in filler_prompt}
    filler_ids = {item["id"] for item in filler_prompt}
    newest = {
        source: max(
            (row for row in filler_review.rows if row.source_id == source),
            key=lambda row: row.published_at or "",
        )
        for source in ("alpha", "beta")
    }
    assert filler_sources == {"alpha", "beta"}
    assert len(filler_prompt) < REVIEW_ROWS_MAX
    assert newest["alpha"].id in filler_ids
    assert newest["beta"].id in filler_ids
    assert len(filler_result.rows) == 12
    assert filler_result.rows is filler_review.rows
    assert filler_result.summary_status == "ok"

    drop_items = [
        _row(source=f"s{index}", topic="drop", serial=800 + index, title="长" * 12000, published_at=_stamp(10 + index))
        for index in range(3)
    ]
    drop_review = build_topic_reviews(drop_items)[0]
    drop_calls: list[str] = []
    drop_result = enrich_topic_reviews(
        (drop_review,),
        changed_topics=frozenset({"drop"}),
        complete_fn=_ok_complete(drop_calls),
    )[0]
    drop_items_prompt = json.loads(drop_calls[0])["topics"][0]["items"]
    drop_sources = {item["source_id"] for item in drop_items_prompt}
    assert drop_sources == {"s1", "s2"}
    assert "s0" not in drop_sources
    assert len(drop_result.rows) == 3
    assert drop_result.summary_status == "ok"

    huge_items = [
        _row(source="alpha", topic="huge", serial=900, title="超" * 20000),
        _row(source="beta", topic="huge", serial=901, title="超" * 20000),
    ]
    huge_review = build_topic_reviews(huge_items)[0]
    huge_calls: list[str] = []

    def refuse(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        huge_calls.append(prompt)
        raise AssertionError("model started")

    huge_result = enrich_topic_reviews(
        (huge_review,),
        changed_topics=frozenset({"huge"}),
        complete_fn=refuse,
    )[0]
    assert huge_calls == []
    assert huge_result.summary_status == "failed"
    assert huge_result.sentences == ()
    assert huge_result.rows is huge_review.rows
    assert INPUT_TOO_LARGE == "input_too_large"

    outside_items = []
    for source, serial_base in (("alpha", 910), ("beta", 930)):
        for offset in range(6):
            outside_items.append(
                _row(
                    source=source,
                    topic="outside",
                    serial=serial_base + offset,
                    published_at=_stamp(28 - offset),
                    title=f"{source}-{offset}",
                )
            )
    outside_review = build_topic_reviews(outside_items)[0]
    oldest = min(outside_review.rows, key=lambda row: (row.published_at is None, row.published_at or "", row.id))
    seen_ids: list[str] = []

    def cite_oldest(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        body = _assert_call(prompt, schema, purpose)
        prompt_ids = [item["id"] for item in body["topics"][0]["items"]]
        seen_ids.extend(prompt_ids)
        return _completion(
            {
                "topics": [
                    {
                        "topic": "outside",
                        "sentences": [
                            {"kind": "consensus", "text": "引用了没送进短评的行", "item_ids": [oldest.id]},
                            {"kind": "difference", "text": "等待...", "item_ids": [oldest.id]},
                        ],
                    }
                ]
            }
        )

    outside_result = enrich_topic_reviews(
        (outside_review,),
        changed_topics=frozenset({"outside"}),
        complete_fn=cite_oldest,
    )[0]
    assert oldest.id not in seen_ids
    assert len(seen_ids) <= REVIEW_ROWS_MAX
    assert outside_result.summary_status == "failed"
    assert outside_result.rows is outside_review.rows
    assert len(outside_result.rows) == 12


def test_review_groups_normalized_topic_and_keeps_one_row_per_id() -> None:
    spaced = _row(source="alpha", topic=" Growth", serial=1, policy_hash="a" * 64)
    folded = _row(source="beta", topic="growth", serial=2, policy_hash="b" * 64)
    alone = _row(source="gamma", topic="lonely", serial=3)
    ligature = "\ufb01" * 41
    mixed = "fi" + ("\ufb01" * 40)
    wide_a = _row(source="alpha", topic=ligature, serial=4)
    wide_b = _row(source="beta", topic=mixed, serial=5)
    shared_id = 9
    dup_low = _row(
        source="a",
        topic="dedupe",
        serial=6,
        item_serial=shared_id,
        analysis_serial=1,
        policy_hash="1" * 64,
    )
    dup_high = _row(
        source="b",
        topic="dedupe",
        serial=7,
        item_serial=shared_id,
        analysis_serial=2,
        policy_hash="2" * 64,
    )
    keeper = _row(source="c", topic="dedupe", serial=8)
    items = [folded, alone, wide_b, dup_high, spaced, wide_a, keeper, dup_low]
    forward = build_topic_reviews(items)
    backward = build_topic_reviews(list(reversed(items)))
    assert forward == backward
    topics = {review.topic: review for review in forward}
    assert "lonely" not in topics
    assert topics["growth"].summary_status == "not_requested"
    assert {row.policy_hash for row in topics["growth"].rows} == {"a" * 64, "b" * 64}
    assert normalize_key(ligature) == normalize_key(mixed)
    assert len(normalize_key(ligature)) > TOPIC_CHARS
    wide = topics[min(ligature, mixed)]
    assert wide.topic == min(ligature, mixed)
    assert {row.topic for row in wide.rows} == {ligature, mixed}
    deduped = topics["dedupe"]
    assert [row.source_id for row in deduped.rows] == ["a", "c"]
    assert deduped.rows[0].policy_hash == "1" * 64
    with pytest.raises(SchemaError) as caught:
        build_topic_reviews("not-items")  # type: ignore[arg-type]
    assert caught.value.code == "invalid_record"


def test_review_disabled_when_model_is_off() -> None:
    multi = build_topic_reviews(_pair("open", 1))[0]
    single = build_topic_reviews(
        (
            _row(source="alpha", topic="solo", serial=3),
            _row(source="alpha", topic="solo", serial=4),
        )
    )[0]
    stale = type(multi)(
        topic=multi.topic,
        rows=multi.rows,
        sentences=(ReviewSentence(kind="limit", text="关掉之后不保留旧段", item_ids=(multi.rows[0].id,)),),
        summary_status="ok",
    )

    def explode(prompt: str, *, schema: dict[str, object], purpose: str) -> CompletionResult:
        raise AssertionError(purpose)

    disabled = enrich_topic_reviews(
        (stale, single),
        changed_topics=frozenset(),
        complete_fn=None,
    )
    assert disabled[0].summary_status == "disabled"
    assert disabled[0].sentences == ()
    assert disabled[0].rows is stale.rows
    assert disabled[1].summary_status == "single_source"
    assert disabled[1].sentences == ()
    assert summary_note("disabled") is None
    assert summary_note("failed") is None
    assert summary_note("ok") is None
    assert summary_note("budget_skipped") is None
    assert summary_note("single_source") is None
    assert summary_note(None) is None  # type: ignore[arg-type]
    with pytest.raises(SchemaError) as caught:
        enrich_topic_reviews((multi,), changed_topics={"open"}, complete_fn=explode)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_record"
