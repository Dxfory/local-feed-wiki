"""Source-vote minority consensus.

Each source casts one vote from title and body claims whose scope is known.
A source that argues both sides is excluded, and its items leave the tally.
The result describes sentences already in this library.

Callers apply topic and claim aliases first. Grouping uses
normalize_key(topic) and resolve_alias(normalize_key(claim_key)) with an
empty alias map.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from second_brain.errors import InvalidStance, SchemaError
from second_brain.models import (
    CLAIM_KEY_CHARS,
    TOPIC_CHARS,
    Claim,
    ClaimAssessment,
    ConsensusResult,
    Opportunity,
    normalize_key,
    resolve_alias,
    single_line,
)

_FALSIFY_BEFORE = "本条仅为本库材料分歧。命题："
_FALSIFY_AFTER = (
    "。请核对所列原文是否支持少数立场；若主体、时期、范围不一致，"
    "或后续同范围的公开证据否定少数句，则本候选失效。少数句："
)
_STANCES = frozenset({"for", "against"})
_NO_ALIASES: dict[str, str] = {}


def find_minority(claims: Sequence[Claim]) -> ConsensusResult:
    """Score current claims into opportunities and assessments.

    Counts are source votes after conflicted sources are removed. Item id
    lists keep every remaining evidence item. Output order follows the
    displayed topic and claim key.
    """
    grouped: dict[tuple[str, str], list[Claim]] = {}
    for claim in _prepare(claims):
        topic_key = normalize_key(claim.topic)
        claim_key = resolve_alias(normalize_key(claim.claim_key), _NO_ALIASES)
        grouped.setdefault((topic_key, claim_key), []).append(claim)
    scored = [_score(topic_key, claim_key, group) for (topic_key, claim_key), group in grouped.items()]
    scored.sort(key=lambda pair: (pair[0].topic, pair[0].claim_key))
    opportunities = tuple(opportunity for _assessment, opportunity in scored if opportunity is not None)
    assessments = tuple(assessment for assessment, _opportunity in scored)
    return ConsensusResult(opportunities, assessments)


def _prepare(claims: Sequence[Claim]) -> list[Claim]:
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
        raise SchemaError("invalid_record")
    prepared: list[Claim] = []
    seen: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Claim):
            raise SchemaError("invalid_record")
        if claim.stance not in _STANCES:
            raise InvalidStance()
        if not isinstance(claim.claim_key, str) or claim.claim_key.strip() == "":
            raise SchemaError("invalid_claim_key")
        if not isinstance(claim.topic, str) or claim.topic.strip() == "":
            raise SchemaError("invalid_topic")
        if claim.evidence_kind == "comment" or _unknown_scope(claim.claim_key):
            continue
        if claim.id in seen:
            continue
        seen.add(claim.id)
        prepared.append(claim)
    return prepared


def _unknown_scope(claim_key: str) -> bool:
    return any(normalize_key(part) == "unknown" for part in claim_key.split("|"))


def _score(
    topic_key: str,
    claim_key: str,
    group: Sequence[Claim],
) -> tuple[ClaimAssessment, Opportunity | None]:
    topic = _choose(topic_key, [claim.topic for claim in group], _topic_fits, "invalid_topic")
    key = _choose(claim_key, [claim.claim_key for claim in group], _key_fits, "invalid_claim_key")
    conflicted = _conflicted_sources(group)
    valid = [claim for claim in group if claim.source_id not in conflicted]
    for_claims = [claim for claim in valid if claim.stance == "for"]
    against_claims = [claim for claim in valid if claim.stance == "against"]
    n_for = len({claim.source_id for claim in for_claims})
    n_against = len({claim.source_id for claim in against_claims})
    n_items = len({claim.item_id for claim in valid})
    n_sources = n_for + n_against
    status = _status(
        n_items=n_items,
        n_sources=n_sources,
        n_for=n_for,
        n_against=n_against,
        excluded=bool(conflicted),
    )
    assessment = ClaimAssessment(
        topic=topic,
        claim_key=key,
        status=status,
        n_items=n_items,
        n_sources=n_sources,
        n_for=n_for,
        n_against=n_against,
        excluded_source_ids=tuple(sorted(conflicted)),
    )
    if status != "opportunity":
        return assessment, None
    if n_for < n_against:
        minority_claims, majority_claims = for_claims, against_claims
        minority_stance = "for"
        minority_count, majority_count = n_for, n_against
    else:
        minority_claims, majority_claims = against_claims, for_claims
        minority_stance = "against"
        minority_count, majority_count = n_against, n_for
    sentence = min(minority_claims, key=lambda claim: (claim.item_id, claim.id)).sentence
    opportunity = Opportunity(
        topic=topic,
        claim_key=key,
        minority_sentence=sentence,
        minority_stance=minority_stance,
        minority_item_ids=_item_ids(minority_claims),
        majority_item_ids=_item_ids(majority_claims),
        minority_source_ids=_source_ids(minority_claims),
        majority_source_ids=_source_ids(majority_claims),
        majority_count=majority_count,
        minority_count=minority_count,
        n_sources=n_sources,
        falsify_hint=_FALSIFY_BEFORE + key + _FALSIFY_AFTER + sentence,
    )
    return assessment, opportunity


def _status(*, n_items: int, n_sources: int, n_for: int, n_against: int, excluded: bool) -> str:
    """Conflict, then sample size, then the split between the two sides."""
    if n_sources == 0 and excluded:
        return "conflicted"
    if n_items < 3:
        return "insufficient"
    if n_sources < 2:
        return "single_source"
    if n_for == 0 or n_against == 0:
        return "one_sided"
    if n_for == n_against:
        return "disputed"
    return "opportunity"


def _conflicted_sources(group: Sequence[Claim]) -> frozenset[str]:
    by_item: dict[str, list[Claim]] = {}
    by_source: dict[str, list[Claim]] = {}
    for claim in group:
        by_item.setdefault(claim.item_id, []).append(claim)
        by_source.setdefault(claim.source_id, []).append(claim)
    conflicted: set[str] = set()
    for bucket in by_item.values():
        if _both_stances(bucket):
            conflicted.update(claim.source_id for claim in bucket)
    for source_id, bucket in by_source.items():
        if _both_stances(bucket):
            conflicted.add(source_id)
    return frozenset(conflicted)


def _both_stances(bucket: Sequence[Claim]) -> bool:
    return {claim.stance for claim in bucket} == {"for", "against"}


def _choose(normalized: str, originals: Sequence[str], fits: Callable[[str], bool], code: str) -> str:
    if fits(normalized):
        return normalized
    for original in sorted(set(originals)):
        if fits(original):
            return original
    raise SchemaError(code)


def _topic_fits(text: str) -> bool:
    if text.strip() == "" or len(text) > TOPIC_CHARS or text != single_line(text):
        return False
    return normalize_key(text) != ""


def _key_fits(text: str) -> bool:
    if text.strip() == "" or len(text) > CLAIM_KEY_CHARS or text != single_line(text):
        return False
    parts = text.split("|")
    return len(parts) == 4 and all(part.strip() != "" for part in parts)


def _item_ids(claims: Sequence[Claim]) -> tuple[str, ...]:
    return tuple(sorted({claim.item_id for claim in claims}))


def _source_ids(claims: Sequence[Claim]) -> tuple[str, ...]:
    return tuple(sorted({claim.source_id for claim in claims}))
