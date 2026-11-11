"""One run: lock, fetch, classify, publish, and recover.

The injected completion function is the only model entry. HTTP comes from
the injected client. This module does not spawn a model process, read the
environment, or delete the lock file.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from second_brain.classify.buckets import classify_items
from second_brain.consensus.minority import find_minority
from second_brain.errors import (
    BudgetExceeded,
    ConcurrentRun,
    ConfigError,
    FetchError,
    GrokUnavailable,
    PublishError,
    SchemaError,
    StorageError,
    UnsafeUrl,
)
from second_brain.http import HttpClient
from second_brain.models import (
    AppConfig,
    ClassificationResult,
    CompletionResult,
    FeedBatch,
    FetchResult,
    LlmEvent,
    Publication,
    RunReport,
    RuntimePolicy,
    Source,
    Usage,
    format_timestamp,
)
from second_brain.ports import Clock, CompletionFn, FixtureReader
from second_brain.review.multi_source import build_topic_reviews, enrich_topic_reviews
from second_brain.sources.ingest import hydrate_transcripts, persist_feed
from second_brain.sources.rss import parse_local_records, parse_rss
from second_brain.sources.youtube import parse_youtube_feed
from second_brain.store import Store
from second_brain.wiki.compile import WikiPublisher

_PURPOSES = frozenset({"classify", "review"})
_SOURCE_FAILURES = (FetchError, OSError, SchemaError, UnsafeUrl)
_LOG_FAILURES = (OSError, PublishError, StorageError)
_WARN_BUDGET = "分类预算已用完，其余延后"
_WARN_GROK = "模型不可用，未完成分类"
_WARN_INVALID = "分类结果未通过校验"
_WARN_TRANSCRIPT = "文稿获取未完成"
_WARN_REVIEW = "短评不可用"
_WARN_REVIEW_BUDGET = "短评预算已用完"
_WARN_LOG = "日志尚未写入"
_WARN_PUBLISH = "发布未完成"


class _Tally(NamedTuple):
    classified: int
    deferred: int
    failed_items: tuple[str, ...]
    unavailable: bool
    invalid: bool


class BudgetedCompletion:
    """Count classify and review attempts and stop after the model is down.

    The delegate is not called once this run has seen GrokUnavailable, and it
    is not called when the purpose budget is already spent. Those refusals do
    not insert another model event. A started event is written only when a
    call is about to be delegated, then updated with the same call id.
    """

    def __init__(
        self,
        delegate: CompletionFn,
        policy: RuntimePolicy,
        store: Store,
        run_id: str,
        clock: Clock,
    ) -> None:
        if not callable(delegate) or not isinstance(policy, RuntimePolicy) or not isinstance(store, Store):
            raise SchemaError("invalid_record")
        if not isinstance(run_id, str):
            raise SchemaError("invalid_run_id")
        self._delegate = delegate
        self._policy = policy
        self._store = store
        self._run_id = run_id
        self._clock = clock
        self._used = {"classify": 0, "review": 0}
        self._down = False

    def __call__(self, prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
        if purpose not in _PURPOSES:
            raise SchemaError("invalid_purpose")
        if self._down:
            raise GrokUnavailable("grok_unavailable")
        limit = self._policy.max_classify_batches if purpose == "classify" else self._policy.max_review_batches
        if self._used[purpose] >= limit:
            raise BudgetExceeded(purpose)
        self._used[purpose] += 1
        call_id = uuid.uuid4().hex
        at = format_timestamp(self._clock.now())
        billing = self._policy.billing_path
        self._store.append_llm_event(
            LlmEvent(
                call_id=call_id,
                run_id=self._run_id,
                purpose=purpose,
                at=at,
                status="started",
                billing_path=billing,
            )
        )
        try:
            result = self._delegate(prompt, schema=schema, purpose=purpose)
        except GrokUnavailable as exc:
            self._down = True
            self._finish(call_id, purpose, at, "unavailable", exc.code, exc.usage, billing)
            detail = getattr(exc, "error_detail", None)
            if isinstance(detail, str) and detail != "":
                self._store.set_llm_error_detail(call_id, detail)
            raise
        except BudgetExceeded:
            self._finish(call_id, purpose, at, "budget_skipped", "budget_exceeded", None, billing)
            raise
        except SchemaError as exc:
            self._finish(call_id, purpose, at, "invalid", exc.code, None, billing)
            raise
        except ConfigError as exc:
            self._finish(call_id, purpose, at, "invalid", exc.code, None, billing)
            raise
        if not isinstance(result, CompletionResult):
            self._finish(call_id, purpose, at, "invalid", "invalid_payload", None, billing)
            raise SchemaError("invalid_payload")
        self._finish(call_id, purpose, at, "ok", None, result.usage, result.billing_path)
        return result

    def _finish(
        self,
        call_id: str,
        purpose: str,
        at: str,
        status: str,
        error_code: str | None,
        usage: Usage | None,
        billing: str,
    ) -> None:
        self._store.append_llm_event(
            LlmEvent(
                call_id=call_id,
                run_id=self._run_id,
                purpose=purpose,
                at=at,
                status=status,
                billing_path=billing,
                error_code=error_code,
                usage=usage,
            )
        )


def run_once(
    store: Store,
    *,
    config: AppConfig,
    clock: Clock,
    run_id_factory: Callable[[], str],
    complete_fn: CompletionFn,
    wiki_dir: Path,
    raw_dir: Path,
    lock_path: Path,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
) -> RunReport:
    """Run one pass. Hold lock_path until return, and do not delete that file.

    A second caller that cannot lock raises ConcurrentRun before migrate or
    fetch. Publishing rows are finished before any new request. A running row
    with no publication is marked failed and its saved rows stay.
    """
    fd = _acquire(lock_path)
    try:
        return _locked(
            store,
            config=config,
            clock=clock,
            run_id_factory=run_id_factory,
            complete_fn=complete_fn,
            wiki_dir=wiki_dir,
            raw_dir=raw_dir,
            http=http,
            fixture_reader=fixture_reader,
        )
    finally:
        _release(fd)


def _acquire(lock_path: Path) -> int:
    if not isinstance(lock_path, Path) or lock_path.is_symlink():
        raise StorageError("invalid_path")
    parent = lock_path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise StorageError("invalid_path")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if isinstance(exc, BlockingIOError) or exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
            raise ConcurrentRun() from None
        raise StorageError("io") from None
    return fd


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _locked(
    store: Store,
    *,
    config: AppConfig,
    clock: Clock,
    run_id_factory: Callable[[], str],
    complete_fn: CompletionFn,
    wiki_dir: Path,
    raw_dir: Path,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
) -> RunReport:
    _validate(
        store,
        config=config,
        clock=clock,
        run_id_factory=run_id_factory,
        complete_fn=complete_fn,
        wiki_dir=wiki_dir,
        raw_dir=raw_dir,
        http=http,
        fixture_reader=fixture_reader,
    )
    store.migrate()
    publisher = WikiPublisher(wiki_dir)
    recovered_at = format_timestamp(clock.now())
    _recover_publications(store, publisher, recovered_at)
    store.recover_interrupted_runs(now=recovered_at)
    prior_logs_ok = _flush_logs(store, publisher)
    if config.runtime.offline and any(source.kind != "file" for source in config.sources):
        raise ConfigError("offline_network_source")
    now = format_timestamp(clock.now())
    run_id = run_id_factory()
    if not isinstance(run_id, str):
        raise SchemaError("invalid_run_id")
    store.begin_run(run_id, started_at=now)
    try:
        return _execute(
            store,
            config=config,
            clock=clock,
            complete_fn=complete_fn,
            wiki_dir=wiki_dir,
            raw_dir=raw_dir,
            http=http,
            fixture_reader=fixture_reader,
            publisher=publisher,
            run_id=run_id,
            now=now,
            prior_logs_ok=prior_logs_ok,
        )
    except Exception as exc:
        _fail_running(store, run_id, now, _safe_code(exc))
        raise


def _validate(
    store: Store,
    *,
    config: AppConfig,
    clock: Clock,
    run_id_factory: Callable[[], str],
    complete_fn: CompletionFn,
    wiki_dir: Path,
    raw_dir: Path,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
) -> None:
    if not isinstance(store, Store):
        raise SchemaError("invalid_record")
    if not isinstance(config, AppConfig):
        raise ConfigError("invalid_config")
    if not isinstance(wiki_dir, Path) or not isinstance(raw_dir, Path):
        raise StorageError("invalid_path")
    if http is not None and not isinstance(http, HttpClient):
        raise SchemaError("invalid_record")
    if not callable(complete_fn) or not callable(fixture_reader) or not callable(run_id_factory):
        raise SchemaError("invalid_record")
    if not callable(getattr(clock, "now", None)):
        raise SchemaError("invalid_record")


def _recover_publications(store: Store, publisher: WikiPublisher, now: str) -> None:
    for publication in store.unfinished_publications():
        publisher.publish(publication)
        store.finish_publication(publication, finished_at=now)


def _flush_logs(store: Store, publisher: WikiPublisher) -> bool:
    ok = True
    for run_id in store.pending_log_runs():
        if not _append_log(store, publisher, run_id):
            ok = False
    return ok


def _append_log(store: Store, publisher: WikiPublisher, run_id: str) -> bool:
    try:
        publisher.append_events(run_id, store.events_for_run(run_id))
        store.mark_log_written(run_id)
    except _LOG_FAILURES:
        return False
    return True


def _execute(
    store: Store,
    *,
    config: AppConfig,
    clock: Clock,
    complete_fn: CompletionFn,
    wiki_dir: Path,
    raw_dir: Path,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
    publisher: WikiPublisher,
    run_id: str,
    now: str,
    prior_logs_ok: bool,
) -> RunReport:
    inserted, updated, failed_sources = _fetch_sources(
        store,
        config.sources,
        http=http,
        fixture_reader=fixture_reader,
        now=now,
        raw_dir=raw_dir,
    )
    ready, issues = hydrate_transcripts(
        store,
        http=http,
        fixture_reader=fixture_reader,
        sources=config.sources,
        now=now,
        raw_dir=raw_dir,
        limit=config.runtime.max_transcript_fetches,
    )
    updated += ready
    items = store.items_for_classify(now=now)
    store.select_run_items(run_id, items)
    budgeted = BudgetedCompletion(complete_fn, config.runtime, store, run_id, clock)
    result = classify_items(
        items,
        store.assets_for(tuple(item.id for item in items)),
        run_id=run_id,
        now=now,
        preferences=config.preferences,
        keys=config.keys,
        known_claim_keys=store.known_claim_keys(),
        complete_fn=budgeted,
    )
    _save_batches(store, result, run_id)
    tally = _tally(result)
    current = store.get_current_classified(now=now)
    claims = store.get_current_claims(now=now)
    changed = store.get_changed_topics(run_id)
    reviews = build_topic_reviews(current)
    review_fn = budgeted if config.runtime.review_llm else None
    try:
        reviews = enrich_topic_reviews(reviews, changed_topics=changed, complete_fn=review_fn)
    except (BudgetExceeded, ConfigError, GrokUnavailable, SchemaError):
        reviews = build_topic_reviews(current)
    consensus = find_minority(claims)
    pending = store.pending_transcript_items(now=now)
    held = store.held_date_items(now=now)
    unclassified = store.items_for_classify(now=now)
    warnings = _warnings(
        failed_sources=failed_sources,
        tally=tally,
        issues=bool(issues),
        reviews=reviews,
        prior_logs_ok=prior_logs_ok,
    )
    # Review trouble stays on the page. It does not turn a finished archive into partial.
    status = "partial" if failed_sources or tally.deferred or tally.failed_items else "success"
    page_warnings = tuple(warnings)

    def report(final_status: str, html_path: str | None, note: list[str]) -> RunReport:
        return RunReport(
            run_id=run_id,
            status=final_status,
            inserted=inserted,
            updated=updated,
            classified=tally.classified,
            pending=len(pending),
            deferred=tally.deferred,
            failed_source_ids=failed_sources,
            failed_item_ids=tally.failed_items,
            html_path=html_path,
            warnings=tuple(note),
        )

    try:
        publication = publisher.stage(
            run_id=run_id,
            now=now,
            delta=store.get_unpublished_classified(now=now),
            reviews=reviews,
            consensus=consensus,
            pending=pending,
            unclassified=unclassified,
            held_date=held,
            warnings=page_warnings,
        )
    except PublishError:
        store.fail_run(run_id, finished_at=now, code="publish_failed")
        _add(warnings, _WARN_PUBLISH)
        return report("failed", None, warnings)
    store.mark_publishing(
        publication,
        status_after_publish=status,
        summary={"files": _checksums(wiki_dir, publication)},
    )
    try:
        publisher.publish(publication)
        store.finish_publication(publication, finished_at=now)
    except (PublishError, StorageError) as exc:
        _fail_running(store, run_id, now, _safe_code(exc))
        raise
    if not _append_log(store, publisher, run_id):
        _add(warnings, _WARN_LOG)
    return report(status, publication.html_path, warnings)


def _fetch_sources(
    store: Store,
    sources: Sequence[Source],
    *,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
    now: str,
    raw_dir: Path,
) -> tuple[int, int, tuple[str, ...]]:
    store.sync_sources(sources, now=now, raw_dir=raw_dir)
    inserted = 0
    updated = 0
    failed: list[str] = []
    for source in sorted(sources, key=lambda item: item.id):
        try:
            result = _read_source(
                store,
                source,
                http=http,
                fixture_reader=fixture_reader,
                now=now,
                raw_dir=raw_dir,
            )
        except _SOURCE_FAILURES:
            failed.append(source.id)
            continue
        if result is None:
            continue
        inserted += result.inserted
        updated += result.updated
    return inserted, updated, tuple(failed)


def _read_source(
    store: Store,
    source: Source,
    *,
    http: HttpClient | None,
    fixture_reader: FixtureReader,
    now: str,
    raw_dir: Path,
) -> FetchResult | None:
    if source.kind == "file":
        payload = fixture_reader(source.url)
        if not isinstance(payload, bytes):
            raise SchemaError("invalid_body")
        batch = _parse(source, payload, now)
        return persist_feed(store, source, batch, now=now, raw_dir=raw_dir, response=None)
    if http is None:
        raise FetchError("http_required")
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
        return None
    batch = _parse(source, response.body, now)
    return persist_feed(store, source, batch, now=now, raw_dir=raw_dir, response=response)


def _parse(source: Source, payload: bytes, now: str) -> FeedBatch:
    kind = source.file_format if source.kind == "file" else source.kind
    if kind == "youtube_rss":
        return parse_youtube_feed(source, payload, now=now)
    if kind in {"podcast_json", "video_json"}:
        return parse_local_records(source, payload, now=now)
    if kind in {"rss", "podcast_rss"}:
        return parse_rss(source, payload, now=now)
    raise SchemaError("invalid_source")


def _save_batches(store: Store, result: ClassificationResult, run_id: str) -> None:
    classes = {row.item_id: row for row in result.classifications}
    grouped: dict[str, list] = {}
    for claim in result.claims:
        grouped.setdefault(claim.item_id, []).append(claim)
    for outcome in result.batches:
        if outcome.status != "success":
            store.save_batch(outcome, (), (), run_id=run_id)
            continue
        picked = tuple(classes[item_id] for item_id in outcome.item_ids)
        claims = tuple(claim for item_id in outcome.item_ids for claim in grouped.get(item_id, ()))
        store.save_batch(outcome, picked, claims, run_id=run_id)


def _tally(result: ClassificationResult) -> _Tally:
    classified = 0
    deferred = 0
    failed: list[str] = []
    unavailable = False
    invalid = False
    for outcome in result.batches:
        if outcome.status == "success":
            classified += len(outcome.item_ids)
        elif outcome.status == "budget_skipped":
            deferred += len(outcome.item_ids)
        elif outcome.status == "unavailable":
            unavailable = True
            failed.extend(outcome.item_ids)
        elif outcome.status == "invalid":
            invalid = True
            failed.extend(outcome.item_ids)
    return _Tally(classified, deferred, tuple(sorted(set(failed))), unavailable, invalid)


def _warnings(
    *,
    failed_sources: tuple[str, ...],
    tally: _Tally,
    issues: bool,
    reviews: Sequence[object],
    prior_logs_ok: bool,
) -> list[str]:
    notes: list[str] = []
    if failed_sources:
        _add(notes, "来源取数失败：" + ",".join(failed_sources))
    if tally.deferred:
        _add(notes, _WARN_BUDGET)
    if tally.unavailable:
        _add(notes, _WARN_GROK)
    if tally.invalid:
        _add(notes, _WARN_INVALID)
    if issues:
        _add(notes, _WARN_TRANSCRIPT)
    statuses = {getattr(review, "summary_status", "") for review in reviews}
    if "failed" in statuses:
        _add(notes, _WARN_REVIEW)
    if "budget_skipped" in statuses:
        _add(notes, _WARN_REVIEW_BUDGET)
    if not prior_logs_ok:
        _add(notes, _WARN_LOG)
    return notes


def _add(notes: list[str], text: str) -> None:
    if text not in notes and len(text) <= 500:
        notes.append(text)


def _checksums(wiki_dir: Path, publication: Publication) -> list[dict[str, str]]:
    run_id = publication.run_id
    manifest_path = publication.manifest_path
    path = wiki_dir / ".staging" / run_id / manifest_path
    if path.is_symlink() or not path.is_file():
        raise PublishError("missing_snapshot")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise PublishError("invalid_manifest") from None
    files = loaded.get("files") if isinstance(loaded, dict) else None
    if not isinstance(files, list):
        raise PublishError("invalid_manifest")
    checksums: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise PublishError("invalid_manifest")
        relative = item.get("path")
        digest = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise PublishError("invalid_manifest")
        checksums.append({"path": relative, "sha256": digest})
    return checksums


def _fail_running(store: Store, run_id: str, now: str, code: str) -> None:
    """Mark a running row failed. Leave a publishing row in place.

    fail_run keeps a publishing snapshot so the next pass can finish it. A
    second call, after the row is already failed, must not replace the error
    that the caller is about to raise.
    """
    try:
        store.fail_run(run_id, finished_at=now, code=code)
    except StorageError:
        return


def _safe_code(exc: BaseException) -> str:
    value = getattr(exc, "code", None)
    if not isinstance(value, str) or not value or len(value) > 64:
        return "run_failed"
    first = value[0]
    if not first.isascii() or not first.isalpha() or not first.islower():
        return "run_failed"
    for char in value:
        if not char.isascii() or not (char.islower() or char.isdigit() or char == "_"):
            return "run_failed"
    return value
