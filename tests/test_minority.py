"""Source-vote consensus over synthetic claims.

No network, no model calls, and no store. Aliases are applied by the caller
before find_minority, using the frozen key helpers.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from second_brain.consensus.minority import find_minority
from second_brain.errors import ConfigError, InvalidStance, SchemaError
from second_brain.models import (
    Claim,
    ClaimAssessment,
    ConsensusResult,
    KeyPolicy,
    Opportunity,
    canonical_json,
    claim_slug,
    item_id,
    make_claim,
    normalize_key,
    resolve_alias,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "consensus" / "golden_claims.json"
PLAN_TEMPLATE = (
    "本条仅为本库材料分歧。命题：{claim_key}。"
    "请核对所列原文是否支持少数立场；若主体、时期、范围不一致，"
    "或后续同范围的公开证据否定少数句，则本候选失效。"
    "少数句：{minority_sentence}"
)
S10_CASES = (
    "four_for_one_against",
    "two_items",
    "tie_two_two",
    "single_source_five",
    "single_source_conflict",
    "source_spam_four_one",
    "two_keys_same_topic",
    "duplicate_claims",
    "item_conflict",
    "comments_only_opposition",
    "scope_period_split",
    "unknown_scope",
    "alias_merge",
    "shuffle_stable",
)
SHUFFLE_SOURCES = (
    "four_for_one_against",
    "source_spam_four_one",
    "tie_two_two",
)

GOLDEN = json.loads(FIXTURE.read_text(encoding="utf-8"))
_CASE_LIST = GOLDEN["cases"]
if len({case["case_id"] for case in _CASE_LIST}) != len(_CASE_LIST):
    raise RuntimeError("duplicate consensus case_id")
CASES = {case["case_id"]: case for case in _CASE_LIST}
ANALYSIS_HASH = GOLDEN["analysis_hash"]
PROPOSITION_TOPIC = GOLDEN["proposition"]["topic"]
PROPOSITION_KEY = GOLDEN["proposition"]["claim_key"]
FOR_SENTENCE = GOLDEN["proposition"]["for_sentence"]
AGAINST_SENTENCE = GOLDEN["proposition"]["against_sentence"]


def _make(raw: dict) -> Claim:
    return make_claim(
        item_id=item_id(raw["source_id"], raw["guid"]),
        source_id=raw["source_id"],
        analysis_hash=ANALYSIS_HASH,
        topic=raw["topic"],
        claim_key=raw["claim_key"],
        sentence=raw["sentence"],
        stance=raw["stance"],
        evidence_kind=raw["evidence_kind"],
        evidence_quote=raw["evidence_quote"],
        comment_index=raw["comment_index"],
    )


def _with_alias(claim: Claim, topic_aliases: dict, claim_aliases: dict) -> Claim:
    return make_claim(
        item_id=claim.item_id,
        source_id=claim.source_id,
        analysis_hash=claim.analysis_hash,
        topic=resolve_alias(claim.topic, topic_aliases),
        claim_key=resolve_alias(claim.claim_key, claim_aliases),
        sentence=claim.sentence,
        stance=claim.stance,
        evidence_kind=claim.evidence_kind,
        evidence_quote=claim.evidence_quote,
        comment_index=claim.comment_index,
    )


def materialize(case: dict) -> list[Claim]:
    repeats = case.get("repeat", 1)
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise AssertionError(case["case_id"])
    built = [_make(raw) for _ in range(repeats) for raw in case["claims"]]
    aliases = case.get("aliases")
    if aliases is None:
        return built
    topic_aliases = aliases["topic_aliases"]
    claim_aliases = aliases["claim_aliases"]
    KeyPolicy(topic_aliases=topic_aliases, claim_aliases=claim_aliases)
    return [_with_alias(claim, topic_aliases, claim_aliases) for claim in built]


def _pair_ids(pairs: list[dict]) -> tuple[str, ...]:
    return tuple(sorted({item_id(pair["source_id"], pair["guid"]) for pair in pairs}))


def expected_result(case: dict) -> ConsensusResult:
    assessments = tuple(
        ClaimAssessment(
            topic=raw["topic"],
            claim_key=raw["claim_key"],
            status=raw["status"],
            n_items=raw["n_items"],
            n_sources=raw["n_sources"],
            n_for=raw["n_for"],
            n_against=raw["n_against"],
            excluded_source_ids=tuple(raw["excluded_source_ids"]),
        )
        for raw in case["expected_assessments"]
    )
    opportunities = tuple(
        Opportunity(
            topic=raw["topic"],
            claim_key=raw["claim_key"],
            minority_sentence=raw["minority_sentence"],
            minority_stance=raw["minority_stance"],
            minority_item_ids=_pair_ids(raw["minority_items"]),
            majority_item_ids=_pair_ids(raw["majority_items"]),
            minority_source_ids=tuple(raw["minority_source_ids"]),
            majority_source_ids=tuple(raw["majority_source_ids"]),
            majority_count=raw["majority_count"],
            minority_count=raw["minority_count"],
            n_sources=raw["n_sources"],
            falsify_hint=raw["falsify_hint"],
        )
        for raw in case["expected_opportunities"]
    )
    return ConsensusResult(opportunities, assessments)


def _opportunity_key(item: Opportunity) -> tuple:
    return (
        item.topic,
        item.claim_key,
        item.minority_stance,
        item.minority_source_ids,
        item.minority_item_ids,
        item.majority_source_ids,
    )


def _serialize(result: ConsensusResult) -> str:
    payload = {
        "assessments": [
            {
                "claim_key": item.claim_key,
                "excluded_source_ids": list(item.excluded_source_ids),
                "n_against": item.n_against,
                "n_for": item.n_for,
                "n_items": item.n_items,
                "n_sources": item.n_sources,
                "status": item.status,
                "topic": item.topic,
            }
            for item in result.assessments
        ],
        "opportunities": [
            {
                "claim_key": item.claim_key,
                "falsify_hint": item.falsify_hint,
                "majority_count": item.majority_count,
                "majority_item_ids": list(item.majority_item_ids),
                "majority_source_ids": list(item.majority_source_ids),
                "minority_count": item.minority_count,
                "minority_item_ids": list(item.minority_item_ids),
                "minority_sentence": item.minority_sentence,
                "minority_source_ids": list(item.minority_source_ids),
                "minority_stance": item.minority_stance,
                "n_sources": item.n_sources,
                "topic": item.topic,
            }
            for item in result.opportunities
        ],
    }
    return canonical_json(payload)


def _assert_invariants(result: ConsensusResult) -> None:
    assessment_keys = [(item.topic, item.claim_key) for item in result.assessments]
    opportunity_keys = [(item.topic, item.claim_key) for item in result.opportunities]
    assert assessment_keys == sorted(assessment_keys)
    assert opportunity_keys == sorted(opportunity_keys)
    assert len(assessment_keys) == len(set(assessment_keys))
    by_key = {(item.topic, item.claim_key): item for item in result.assessments}
    seen_opportunities = set()
    for opportunity in result.opportunities:
        key = (opportunity.topic, opportunity.claim_key)
        seen_opportunities.add(key)
        assessment = by_key[key]
        assert assessment.status == "opportunity"
        assert 0 < opportunity.minority_count < opportunity.majority_count
        assert opportunity.majority_count + opportunity.minority_count == opportunity.n_sources == assessment.n_sources
        assert min(assessment.n_for, assessment.n_against) == opportunity.minority_count
        assert max(assessment.n_for, assessment.n_against) == opportunity.majority_count
        assert len(opportunity.minority_source_ids) == opportunity.minority_count
        assert len(opportunity.majority_source_ids) == opportunity.majority_count
        assert set(opportunity.minority_source_ids).isdisjoint(opportunity.majority_source_ids)
        assert set(opportunity.minority_item_ids).isdisjoint(opportunity.majority_item_ids)
        assert opportunity.minority_source_ids == tuple(sorted(opportunity.minority_source_ids))
        assert opportunity.majority_source_ids == tuple(sorted(opportunity.majority_source_ids))
        assert opportunity.minority_item_ids == tuple(sorted(opportunity.minority_item_ids))
        assert opportunity.majority_item_ids == tuple(sorted(opportunity.majority_item_ids))
        assert opportunity.falsify_hint == PLAN_TEMPLATE.format(
            claim_key=opportunity.claim_key,
            minority_sentence=opportunity.minority_sentence,
        )
        for banned in ("价格", "交易", "概率", "必然赚钱"):
            assert banned not in opportunity.falsify_hint
    for assessment in result.assessments:
        assert assessment.n_for + assessment.n_against == assessment.n_sources
        assert assessment.n_items >= assessment.n_sources
        assert assessment.excluded_source_ids == tuple(sorted(set(assessment.excluded_source_ids)))
        key = (assessment.topic, assessment.claim_key)
        if assessment.status == "opportunity":
            assert key in seen_opportunities
            assert assessment.n_items >= 3
            assert assessment.n_sources >= 2
            assert assessment.n_for > 0 and assessment.n_against > 0
            assert assessment.n_for != assessment.n_against
        else:
            assert key not in seen_opportunities
        if assessment.status == "conflicted":
            assert assessment.excluded_source_ids
            assert assessment.n_sources == assessment.n_items == assessment.n_for == assessment.n_against == 0
        if assessment.status == "insufficient":
            assert assessment.n_items < 3
            assert assessment.excluded_source_ids == () or assessment.n_sources > 0
        if assessment.status == "single_source":
            assert assessment.n_items >= 3
            assert assessment.n_sources < 2
        if assessment.status == "one_sided":
            assert assessment.n_items >= 3
            assert assessment.n_sources >= 2
            assert assessment.n_for == 0 or assessment.n_against == 0
        if assessment.status == "disputed":
            assert assessment.n_items >= 3
            assert assessment.n_sources >= 2
            assert assessment.n_for == assessment.n_against > 0


def _assert_shuffle_stable() -> None:
    case = CASES["shuffle_stable"]
    assert tuple(case["source_case_ids"]) == SHUFFLE_SOURCES
    assert len(case["seeds"]) == 5
    assert len(set(case["seeds"])) == 5
    for source_id in case["source_case_ids"]:
        claims = materialize(CASES[source_id])
        baseline = find_minority(claims)
        assert baseline == expected_result(CASES[source_id])
        original = [claim.id for claim in claims]
        orders = []
        for seed in case["seeds"]:
            copied = list(claims)
            random.Random(seed).shuffle(copied)
            orders.append([claim.id for claim in copied])
            shuffled = find_minority(copied)
            assert shuffled == baseline
            assert _serialize(shuffled) == _serialize(baseline)
            assert tuple(item.minority_sentence for item in shuffled.opportunities) == tuple(
                item.minority_sentence for item in baseline.opportunities
            )
        assert any(order != original for order in orders)


def _forge(claim: Claim, **overrides: object) -> Claim:
    forged = Claim.__new__(Claim)
    values = {
        "id": claim.id,
        "item_id": claim.item_id,
        "source_id": claim.source_id,
        "analysis_hash": claim.analysis_hash,
        "topic": claim.topic,
        "claim_key": claim.claim_key,
        "sentence": claim.sentence,
        "stance": claim.stance,
        "evidence_kind": claim.evidence_kind,
        "evidence_quote": claim.evidence_quote,
        "comment_index": claim.comment_index,
    }
    values.update(overrides)
    for name, value in values.items():
        object.__setattr__(forged, name, value)
    return forged


def _body(source: str, guid: str, stance: str, **overrides: object) -> Claim:
    sentence = FOR_SENTENCE if stance == "for" else AGAINST_SENTENCE
    values = {
        "item_id": item_id(source, guid),
        "source_id": source,
        "analysis_hash": ANALYSIS_HASH,
        "topic": PROPOSITION_TOPIC,
        "claim_key": PROPOSITION_KEY,
        "sentence": sentence,
        "stance": stance,
        "evidence_kind": "body",
        "evidence_quote": sentence,
    }
    values.update(overrides)
    return make_claim(**values)


def test_golden_cases_cover_the_pure_function_set() -> None:
    assert set(S10_CASES) <= set(CASES)
    assert "canonical_duplicate_integration" not in CASES
    plan = (ROOT / "docs" / "BUILD_PLAN_CN_v3.md").read_text(encoding="utf-8")
    assert PLAN_TEMPLATE in plan
    assert GOLDEN["falsify_template"] == PLAN_TEMPLATE
    assert PROPOSITION_KEY in plan
    assert FOR_SENTENCE in plan
    assert AGAINST_SENTENCE in plan
    text = FIXTURE.read_text(encoding="utf-8")
    assert "库存过高" not in text
    lowered = text.lower()
    for banned in ("http://", "https://", "api_key", "password"):
        assert banned not in lowered
    names = sorted(path.name for path in FIXTURE.parent.iterdir())
    assert names == ["golden_claims.json"]


@pytest.mark.parametrize("case_id", list(CASES))
def test_golden_claims(case_id: str) -> None:
    case = CASES[case_id]
    if case_id == "shuffle_stable":
        _assert_shuffle_stable()
        return
    cycle = case.get("alias_cycle")
    if cycle is not None:
        with pytest.raises(ConfigError) as caught:
            KeyPolicy(topic_aliases=cycle["topic_aliases"])
        assert caught.value.code == "alias_cycle"
    for raw in case["expected_opportunities"]:
        assert raw["falsify_hint"] == PLAN_TEMPLATE.format(
            claim_key=raw["claim_key"],
            minority_sentence=raw["minority_sentence"],
        )
    result = find_minority(materialize(case))
    expected = expected_result(case)
    assert [_opportunity_key(item) for item in result.opportunities] == [
        _opportunity_key(item) for item in expected.opportunities
    ]
    assert result == expected
    _assert_invariants(result)
    if case_id == "alias_merge":
        assert result == find_minority(materialize(CASES["four_for_one_against"]))
    if case_id == "duplicate_claims":
        assert len(materialize(case)) == 3 * len(case["claims"])
        assert result == find_minority(materialize(CASES["four_for_one_against"]))


def test_source_repetition_cannot_create_majority() -> None:
    spam = find_minority(materialize(CASES["source_spam_four_one"]))
    assert spam.opportunities == ()
    assessment = spam.assessments[0]
    assert assessment.status == "disputed"
    assert assessment.n_items == 5
    assert assessment.n_sources == 2
    assert assessment.n_for == 1
    assert assessment.n_against == 1
    assert assessment.excluded_source_ids == ()

    conflict = find_minority(materialize(CASES["single_source_conflict"]))
    assert conflict.opportunities == ()
    assert conflict.assessments[0].status == "conflicted"
    assert conflict.assessments[0].excluded_source_ids == ("source-a",)
    assert conflict.assessments[0].n_items == 0
    assert conflict.assessments[0].n_sources == 0

    partial = find_minority(materialize(CASES["partial_conflict_insufficient"]))
    assert partial.opportunities == ()
    assert partial.assessments[0].status == "insufficient"
    assert partial.assessments[0].excluded_source_ids == ("source-a",)
    assert partial.assessments[0].n_items == 2
    assert partial.assessments[0].n_for == 1
    assert partial.assessments[0].n_against == 1

    comments = materialize(CASES["comments_only_opposition"])
    assert any(claim.evidence_kind == "comment" and claim.stance == "against" for claim in comments)
    scored = find_minority(comments)
    assert scored.opportunities == ()
    assert scored.assessments[0].status == "one_sided"
    assert scored.assessments[0].n_for == 3
    assert scored.assessments[0].n_against == 0
    assert scored.assessments[0].n_sources == 3
    assert "source-z" not in scored.assessments[0].excluded_source_ids
    only_comments = [claim for claim in comments if claim.evidence_kind == "comment"]
    assert only_comments
    assert find_minority(only_comments) == ConsensusResult((), ())

    repeated = find_minority(materialize(CASES["repeated_direction_one_vote"]))
    opportunity = repeated.opportunities[0]
    assert repeated.assessments[0].n_items == 5
    assert repeated.assessments[0].n_sources == 4
    assert opportunity.majority_count == 3
    assert opportunity.minority_count == 1
    assert len(opportunity.majority_source_ids) == 3
    assert len(opportunity.majority_item_ids) == 4
    assert len(opportunity.minority_item_ids) == 1


def test_shuffle_full_result_stable() -> None:
    _assert_shuffle_stable()


def test_slug_uses_topic_and_claim() -> None:
    semiconductors = claim_slug(PROPOSITION_TOPIC, PROPOSITION_KEY)
    inventory = claim_slug("库存", PROPOSITION_KEY)
    assert semiconductors != inventory
    assert len(semiconductors) == 12
    assert semiconductors == claim_slug("  半导体\t", PROPOSITION_KEY)
    assert semiconductors == claim_slug(PROPOSITION_TOPIC, normalize_key(PROPOSITION_KEY))
    later = claim_slug(PROPOSITION_TOPIC, "机构A|需求同比增长|2031年度|业务B")
    assert later != semiconductors

    plain = _body("source-a", "a1", "for")
    folded = _body(
        "source-a",
        "a1",
        "for",
        topic="  半导体\t",
        claim_key=normalize_key(PROPOSITION_KEY),
    )
    assert find_minority([plain]) == find_minority([folded])
    both = find_minority([plain, _body("source-b", "b1", "for", topic="库存")])
    assert [item.topic for item in both.assessments] == sorted((PROPOSITION_TOPIC, "库存"))
    slugs = [claim_slug(item.topic, item.claim_key) for item in both.assessments]
    assert slugs == [claim_slug(topic, PROPOSITION_KEY) for topic in sorted((PROPOSITION_TOPIC, "库存"))]


def test_minority_sentence_uses_lowest_item_and_claim_id() -> None:
    sentences = (AGAINST_SENTENCE, "2030年度业务B需求不会同比增长，另一句")
    against = [
        _body("source-a", "a1", "against", sentence=sentence, evidence_quote=sentence) for sentence in sentences
    ]
    claims = against + [_body(source, guid, "for") for source, guid in (("source-b", "b1"), ("source-c", "c1"), ("source-d", "d1"))]
    forward = find_minority(claims)
    backward = find_minority(list(reversed(claims)))
    winner = min(against, key=lambda claim: (claim.item_id, claim.id))
    loser = max(against, key=lambda claim: (claim.item_id, claim.id))
    assert winner.sentence != loser.sentence
    assert forward == backward
    assert forward.opportunities[0].minority_stance == "against"
    assert forward.opportunities[0].minority_sentence == winner.sentence
    assert len(forward.opportunities[0].minority_item_ids) == 1


def test_case_and_space_variants_collapse() -> None:
    plain = _body("source-a", "a1", "for")
    folded = _body(
        "source-a",
        "a1",
        "for",
        topic="  半导体\t",
        claim_key="机构a|需求同比增长|2030年度|业务b",
    )
    separate_plain = find_minority([plain])
    separate_folded = find_minority([folded])
    assert separate_plain == separate_folded
    assert separate_plain.assessments[0].topic == "半导体"
    assert separate_plain.assessments[0].claim_key == "机构a|需求同比增长|2030年度|业务b"
    merged = find_minority([folded, plain])
    assert len(merged.assessments) == 1
    assert merged.assessments[0].n_items == 1
    assert merged == separate_plain


def test_unknown_must_be_a_whole_segment() -> None:
    keys = (
        "机构A|需求UNKNOWN增长|2030年度|业务B",
        "机构A|unknowns|2030年度|业务B",
        "机构A|需求同比增长|未知|业务B",
    )
    for index, key in enumerate(keys):
        result = find_minority([_body("source-a", f"u{index}", "for", claim_key=key, evidence_quote=key)])
        assert len(result.assessments) == 1
        assert result.assessments[0].status == "insufficient"
        assert result.opportunities == ()
    dropped = find_minority(materialize(CASES["unknown_scope"]))
    assert dropped == ConsensusResult((), ())


def test_title_evidence_votes_with_body() -> None:
    claims = materialize(CASES["four_for_one_against"])
    titled = []
    replaced = False
    for claim in claims:
        if replaced or claim.stance != "for":
            titled.append(claim)
            continue
        titled.append(
            make_claim(
                item_id=claim.item_id,
                source_id=claim.source_id,
                analysis_hash=claim.analysis_hash,
                topic=claim.topic,
                claim_key=claim.claim_key,
                sentence=claim.sentence,
                stance=claim.stance,
                evidence_kind="title",
                evidence_quote=claim.evidence_quote,
            )
        )
        replaced = True
    assert replaced
    assert find_minority(titled) == find_minority(claims)


def test_long_casefold_keeps_a_legal_surface() -> None:
    topic = "ß" * 80
    result = find_minority([_body("source-a", "a1", "for", topic=topic)])
    assert result.assessments[0].topic == topic
    assert result.assessments[0].status == "insufficient"
    wide_key = "|".join(("ß" * 49, "ß" * 49, "ß" * 49, "ß" * 50))
    assert len(wide_key) == 200
    assert len(normalize_key(wide_key)) > 200
    wide = find_minority([_body("source-a", "a1", "for", claim_key=wide_key, evidence_quote=wide_key)])
    assert wide.assessments[0].claim_key == wide_key
    assert wide.opportunities == ()


def test_rejects_invalid_claims() -> None:
    good = materialize(CASES["two_items"])
    with pytest.raises(InvalidStance) as stance:
        find_minority([_forge(good[0], stance="later")])
    assert stance.value.code == "invalid_stance"
    with pytest.raises(SchemaError) as empty_key:
        find_minority([_forge(good[0], claim_key="   ")])
    assert empty_key.value.code == "invalid_claim_key"
    with pytest.raises(SchemaError) as empty_topic:
        find_minority([_forge(good[0], topic="   ")])
    assert empty_topic.value.code == "invalid_topic"
    with pytest.raises(SchemaError) as kind:
        find_minority([{"stance": "for"}])  # type: ignore[list-item]
    assert kind.value.code == "invalid_record"
    with pytest.raises(SchemaError) as sequence:
        find_minority("not-a-sequence")  # type: ignore[arg-type]
    assert sequence.value.code == "invalid_record"
    with pytest.raises(SchemaError) as missing:
        find_minority(None)  # type: ignore[arg-type]
    assert missing.value.code == "invalid_record"
    with pytest.raises(InvalidStance):
        find_minority([good[0], _forge(good[1], stance="sideways")])


def test_empty_input_and_signature() -> None:
    assert find_minority(()) == ConsensusResult((), ())
    assert find_minority([]) == ConsensusResult((), ())
    claims = materialize(CASES["four_for_one_against"])
    order = [claim.id for claim in claims]
    listed = find_minority(claims)
    assert [claim.id for claim in claims] == order
    assert find_minority(tuple(claims)) == listed
    code = find_minority.__code__
    assert list(code.co_varnames[: code.co_argcount]) == ["claims"]
    assert code.co_kwonlyargcount == 0
