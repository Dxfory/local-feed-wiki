"""Local wiki pages and the only writer of wiki files.

stage renders a snapshot under wiki/.staging and does not move index.md.
publish renames the snapshot directory, then the digest, and only then
replaces index.md. append_events adds one complete log block and does not
rewrite older bytes.

Pages escape untrusted text. The only HTML links are canonical public
http(s) URLs. No script, image, font, or stylesheet is emitted.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from second_brain.errors import IdentityCollision, PublishError, SchemaError, UnsafeUrl
from second_brain.models import (
    ClassifiedItem,
    ConsensusResult,
    FetchEvent,
    Item,
    LlmEvent,
    Publication,
    TopicReview,
    canonical_json,
    canonical_url,
    claim_slug,
    normalize_key,
    require_canonical_timestamp,
    topic_slug,
)
from second_brain.urlguard import guard_public_url

LIBRARY = "本库当前已收录材料"
EMPTY_DELTA = "本次没有新的已分档条目"
FOOTNOTE = (
    "非共识页只对照库内句子的分布。它不是市场结论，也不是买卖、仓位或目标价。"
    "多数不等于事实，少数不等于机会。相关转载与多账号可能仍不独立。"
    "无法用短评覆盖具体事实的证据链。"
)
BUCKET_LABEL = {"must": "必读", "recommend": "推荐阅读", "skip": "不推荐"}
ASSESSMENT_LABEL = {
    "insufficient": "样本不足",
    "disputed": "争议（未写机会）",
    "single_source": "单源，未写机会",
    "one_sided": "未出现对立立场",
    "conflicted": "来源自身冲突，已排除",
    "opportunity": "少数候选",
}
SUMMARY_LABEL = {
    "ok": "短评",
    "disabled": "短评已关闭",
    "failed": "短评不可用",
    "budget_skipped": "预算延后",
    "single_source": "单源材料汇总",
    "not_requested": "本次未请求短评",
}
KIND_LABEL = {"consensus": "共同点", "difference": "差异", "audience": "观众", "limit": "限度"}
STANCE_LABEL = {"for": "支持", "against": "反对"}
EVIDENCE_LABEL = {"title": "标题摘句", "body": "正文摘句", "comment": "观众摘句"}
_RUN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_SLUG = re.compile(r"^[0-9a-f]{12}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MD = str.maketrans(
    {
        "\\": "\\\\",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
        "{": "\\{",
        "}": "\\}",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        "#": "\\#",
        "!": "\\!",
    }
)
_BLOCK = re.compile(
    rb"<!-- BEGIN run_id=([A-Za-z0-9_-]{1,64}) sha256=([0-9a-f]{64}) -->\n"
    rb"(.*?)\n<!-- END run_id=\1 sha256=\2 -->\n",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    run_id: str
    files: tuple[tuple[str, bytes], ...]
    manifest_path: str
    manifest: bytes
    publication: Publication


@dataclass(frozen=True, slots=True)
class _TopicBucket:
    topic: str
    review: TopicReview | None
    assessments: tuple[object, ...]
    opportunities: tuple[object, ...]


class _Slugs:
    """Short file names for one snapshot. Different full keys cannot share one."""

    def __init__(self) -> None:
        self._topics: dict[str, tuple[str, ...]] = {}
        self._claims: dict[str, tuple[str, ...]] = {}
        self._topic_registry: dict[str, tuple[str, ...]] = {}
        self._claim_registry: dict[str, tuple[str, ...]] = {}

    def topic(self, topic: str) -> str:
        key = ("topic", normalize_key(topic))
        return self._take(self._topics, topic_slug(topic, registry=self._topic_registry), key)

    def claim(self, topic: str, claim_key: str) -> str:
        key = ("claim", normalize_key(topic), normalize_key(claim_key))
        slug = claim_slug(topic, claim_key, registry=self._claim_registry)
        return self._take(self._claims, slug, key)

    def _take(self, table: dict[str, tuple[str, ...]], slug: str, key: tuple[str, ...]) -> str:
        if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
            raise SchemaError("invalid_path")
        previous = table.get(slug)
        if previous is not None and previous != key:
            raise IdentityCollision()
        table[slug] = key
        return slug


class WikiPublisher:
    """Stage, publish, and append the wiki for one run. This is the only wiki writer."""

    def __init__(self, wiki_dir: Path) -> None:
        if not isinstance(wiki_dir, Path):
            raise PublishError("invalid_path")
        self.wiki_dir = wiki_dir

    def stage(
        self,
        *,
        run_id: str,
        now: str,
        delta: Sequence[ClassifiedItem],
        reviews: Sequence[TopicReview],
        consensus: ConsensusResult,
        pending: Sequence[Item],
        unclassified: Sequence[Item],
        held_date: Sequence[Item],
        warnings: Sequence[str],
    ) -> Publication:
        """Write a complete staged snapshot. Do not move index.md or final files."""
        snapshot = _render(
            run_id=_run_id(run_id),
            now=require_canonical_timestamp(now),
            delta=_rows(delta),
            reviews=_reviews(reviews),
            consensus=_consensus(consensus),
            pending=_items(pending),
            unclassified=_items(unclassified),
            held_date=_items(held_date),
            warnings=_warnings(warnings),
        )
        self._store_staging(self._root(), snapshot)
        return snapshot.publication

    def publish(self, publication: Publication) -> None:
        """Move a staged snapshot into place, then the digest, then the index."""
        if not isinstance(publication, Publication):
            raise SchemaError("invalid_record")
        root = self._root()
        run_id = publication.run_id
        if publication.html_path != f"digest/{run_id}.html":
            raise PublishError("publication_mismatch")
        if publication.manifest_path != f"snapshots/{run_id}/manifest.json":
            raise PublishError("publication_mismatch")
        final_manifest = _join(root, publication.manifest_path)
        if final_manifest.is_file() and not final_manifest.is_symlink():
            manifest = _load_manifest(final_manifest, publication)
            self._finish_existing(root, run_id, manifest)
            return
        staged_root = _join(root, f".staging/{run_id}")
        staged_manifest = _join(staged_root, publication.manifest_path)
        if not staged_manifest.is_file() or staged_manifest.is_symlink():
            raise PublishError("missing_snapshot")
        manifest = _load_manifest(staged_manifest, publication)
        _verify(staged_root, manifest)
        self._move_snapshot(root, run_id)
        self._move_digest(root, run_id, manifest)
        _verify(root, manifest)
        self._ensure_index(root, manifest)

    def append_events(self, run_id: str, events: Sequence[LlmEvent | FetchEvent]) -> None:
        """Append one complete log block. A matching block is left untouched."""
        run_id = _run_id(run_id)
        body = _log_body(run_id, events)
        digest = hashlib.sha256(body).hexdigest()
        block = _log_block(run_id, digest, body)
        root = self._root()
        path = root / "log.md"
        if path.is_symlink():
            raise PublishError("symlink_escape")
        existing = b""
        if path.exists():
            if not path.is_file():
                raise PublishError("invalid_log")
            existing = _read(path)
        if _has_block(existing, run_id, digest):
            return
        prefix = b"" if existing == b"" or existing.endswith(b"\n") else b"\n"
        _append(path, prefix + block)

    def _root(self) -> Path:
        root = self.wiki_dir
        if root.is_symlink():
            raise PublishError("symlink_escape")
        if not root.exists():
            parent = root.parent
            if parent.is_symlink() or not parent.is_dir():
                raise PublishError("not_a_directory")
            root.mkdir()
        if root.is_symlink() or not root.is_dir():
            raise PublishError("not_a_directory")
        return root

    def _store_staging(self, root: Path, snapshot: _Snapshot) -> None:
        rendered = dict(snapshot.files)
        rendered[snapshot.manifest_path] = snapshot.manifest
        all_present = True
        for relative, payload in rendered.items():
            dest = _join(root, relative)
            if dest.is_symlink():
                raise PublishError("symlink_escape")
            if not dest.exists():
                all_present = False
                continue
            if not dest.is_file() or _read(dest) != payload:
                raise PublishError("hash_mismatch")
        if all_present:
            return
        self._write_staging(root, snapshot.run_id, rendered)

    def _write_staging(self, root: Path, run_id: str, rendered: dict[str, bytes]) -> None:
        staging_root = root / ".staging"
        if staging_root.is_symlink():
            raise PublishError("symlink_escape")
        if not staging_root.exists():
            staging_root.mkdir()
        if not staging_root.is_dir():
            raise PublishError("not_a_directory")
        partial = staging_root / f".partial-{run_id}"
        final = staging_root / run_id
        if partial.exists() or partial.is_symlink():
            _remove_tree(root, partial)
        partial.mkdir()
        try:
            for relative, payload in rendered.items():
                dest = _join(partial, relative)
                _assert_inside(partial, dest)
                if not dest.parent.is_dir():
                    dest.parent.mkdir(parents=True)
                _write_new(dest, payload)
            _fsync_dir(partial)
        except Exception:
            _remove_tree(root, partial)
            raise
        if final.exists() or final.is_symlink():
            _remove_tree(root, final)
        os.rename(partial, final)
        _fsync_dir(staging_root)

    def _finish_existing(self, root: Path, run_id: str, manifest: dict[str, object]) -> None:
        missing: list[dict[str, str]] = []
        for entry in _file_entries(manifest):
            path = _join(root, entry["path"])
            if path.is_symlink():
                raise PublishError("symlink_escape")
            if not path.exists():
                missing.append(entry)
                continue
            if not path.is_file() or _sha_file(path) != entry["sha256"]:
                raise PublishError("hash_mismatch")
        digest = f"digest/{run_id}.html"
        if any(entry["path"] != digest for entry in missing):
            raise PublishError("missing_snapshot")
        if missing:
            self._move_digest(root, run_id, manifest)
            _verify(root, manifest)
        self._ensure_index(root, manifest)

    def _move_snapshot(self, root: Path, run_id: str) -> None:
        src = _join(root, f".staging/{run_id}/snapshots/{run_id}")
        dst = _join(root, f"snapshots/{run_id}")
        if dst.exists() or dst.is_symlink():
            raise PublishError("hash_mismatch")
        if src.is_symlink() or not src.is_dir():
            raise PublishError("missing_snapshot")
        parent = dst.parent
        if parent.is_symlink():
            raise PublishError("symlink_escape")
        if not parent.is_dir():
            parent.mkdir(parents=True)
        try:
            os.rename(src, dst)
            _fsync_dir(parent)
        except OSError:
            raise PublishError("publish_io") from None

    def _move_digest(self, root: Path, run_id: str, manifest: dict[str, object]) -> None:
        relative = f"digest/{run_id}.html"
        expected = _hash_for(manifest, relative)
        src = _join(root, f".staging/{run_id}/{relative}")
        dst = _join(root, relative)
        if dst.is_symlink():
            raise PublishError("symlink_escape")
        if dst.exists():
            if dst.is_file() and _sha_file(dst) == expected:
                return
            raise PublishError("hash_mismatch")
        if src.is_symlink() or not src.is_file():
            raise PublishError("missing_digest")
        if _sha_file(src) != expected:
            raise PublishError("hash_mismatch")
        parent = dst.parent
        if parent.is_symlink():
            raise PublishError("symlink_escape")
        if not parent.is_dir():
            parent.mkdir(parents=True)
        try:
            os.rename(src, dst)
            _fsync_dir(parent)
        except OSError:
            raise PublishError("publish_io") from None

    def _ensure_index(self, root: Path, manifest: dict[str, object]) -> None:
        payload = _index_text(root, manifest).encode("utf-8")
        dest = root / "index.md"
        if dest.is_symlink():
            raise PublishError("symlink_escape")
        if dest.exists() and not dest.is_file():
            raise PublishError("invalid_index")
        if dest.is_file() and _read(dest) == payload:
            return
        _replace_file(root, dest, payload)


def _render(
    *,
    run_id: str,
    now: str,
    delta: tuple[ClassifiedItem, ...],
    reviews: tuple[TopicReview, ...],
    consensus: ConsensusResult,
    pending: tuple[Item, ...],
    unclassified: tuple[Item, ...],
    held_date: tuple[Item, ...],
    warnings: tuple[str, ...],
) -> _Snapshot:
    _unique(delta)
    _unique(pending)
    _unique(unclassified)
    _unique(held_date)
    items_by_id = _index_items(delta, reviews)
    slugs = _Slugs()
    pages = _topic_pages(reviews, consensus)
    files: dict[str, bytes] = {}
    topic_meta: list[dict[str, str]] = []
    for key in sorted(pages):
        page = pages[key]
        slug = slugs.topic(page.topic)
        relative = f"snapshots/{run_id}/topics/{slug}.md"
        files[relative] = _topic_markdown(page)
        topic_meta.append({"slug": slug, "topic": page.topic})
    opportunity_meta: list[dict[str, str]] = []
    assessments = _assessment_index(consensus)
    for opportunity in consensus.opportunities:
        slug = slugs.claim(opportunity.topic, opportunity.claim_key)
        relative = f"snapshots/{run_id}/opportunities/{slug}.md"
        key = (normalize_key(opportunity.topic), normalize_key(opportunity.claim_key))
        files[relative] = _opportunity_markdown(opportunity, assessments.get(key), items_by_id)
        opportunity_meta.append(
            {"claim_key": opportunity.claim_key, "slug": slug, "topic": opportunity.topic}
        )
    files[f"digest/{run_id}.html"] = _digest(
        run_id=run_id,
        now=now,
        delta=delta,
        reviews=reviews,
        consensus=consensus,
        pending=pending,
        unclassified=unclassified,
        held_date=held_date,
        warnings=warnings,
    )
    refs = tuple(sorted((item.id, item.analysis_hash) for item in delta))
    moments = [item.published_at for item in delta]
    for review in reviews:
        moments.extend(row.published_at for row in review.rows)
    start, end = _bounds(moments)
    status = "部分完成" if unclassified else "已发布"
    manifest_path = f"snapshots/{run_id}/manifest.json"
    manifest = _manifest_bytes(
        run_id=run_id,
        now=now,
        refs=refs,
        files=files,
        status=status,
        warnings=warnings,
        topics=tuple(sorted(topic_meta, key=lambda item: (item["topic"], item["slug"]))),
        opportunities=tuple(
            sorted(
                opportunity_meta,
                key=lambda item: (item["topic"], item["claim_key"], item["slug"]),
            )
        ),
        published_from=start,
        published_to=end,
    )
    publication = Publication(
        run_id=run_id,
        html_path=f"digest/{run_id}.html",
        manifest_path=manifest_path,
        classified_refs=refs,
    )
    return _Snapshot(run_id, tuple(sorted(files.items())), manifest_path, manifest, publication)


def _digest(
    *,
    run_id: str,
    now: str,
    delta: tuple[ClassifiedItem, ...],
    reviews: tuple[TopicReview, ...],
    consensus: ConsensusResult,
    pending: tuple[Item, ...],
    unclassified: tuple[Item, ...],
    held_date: tuple[Item, ...],
    warnings: tuple[str, ...],
) -> bytes:
    moments = [item.published_at for item in delta]
    for review in reviews:
        moments.extend(row.published_at for row in review.rows)
    grouped = {bucket: [] for bucket in ("must", "recommend", "skip")}
    for item in sorted(delta, key=lambda row: (row.source_id, row.id)):
        grouped[item.bucket].append(item)
    parts = [
        "<!DOCTYPE html>\n",
        '<html lang="zh-CN">\n',
        "<head>\n",
        '<meta charset="utf-8">\n',
        f"<title>增量摘要 {_h(run_id)}</title>\n",
        "</head>\n",
        "<body>\n",
        "<h1>增量摘要</h1>\n",
        f"<p>{_h(LIBRARY)}</p>\n",
        f"<p>时间：{_h(now)}</p>\n",
        f"<p>时间范围：{_h(_range_text(moments))}</p>\n",
    ]
    for bucket in ("must", "recommend", "skip"):
        parts.append(f"<h2>{BUCKET_LABEL[bucket]}</h2>\n")
        items = grouped[bucket]
        if not delta:
            parts.append(f"<p>{EMPTY_DELTA}</p>\n")
        elif not items:
            parts.append("<p>本节没有新的条目</p>\n")
        else:
            parts.extend(_article_html(item) for item in items)
    parts.append("<h2>待转写</h2>\n")
    parts.extend(_queue_html(item, confirmed=True) for item in _by_source(pending))
    if not pending:
        parts.append("<p>无</p>\n")
    parts.append("<h2>日期待确认</h2>\n")
    parts.extend(_queue_html(item, confirmed=False) for item in _by_source(held_date))
    if not held_date:
        parts.append("<p>无</p>\n")
    parts.append("<h2>待分类/本次未完成</h2>\n")
    parts.extend(_queue_html(item, confirmed=True) for item in _by_source(unclassified))
    if not unclassified:
        parts.append("<p>无</p>\n")
    parts.append("<h2>非共识</h2>\n")
    if consensus.assessments:
        parts.append("<ul>\n")
        ordered = sorted(
            consensus.assessments,
            key=lambda row: (normalize_key(row.topic), normalize_key(row.claim_key)),
        )
        for assessment in ordered:
            label = ASSESSMENT_LABEL[assessment.status]
            topic = _h(assessment.topic)
            claim = _h(assessment.claim_key)
            parts.append(f"<li>{topic} {claim}：{_h(label)}</li>\n")
        parts.append("</ul>\n")
    else:
        parts.append("<p>无</p>\n")
    parts.append("<h2>说明</h2>\n")
    notes = [f"<li>{_h(warning)}</li>\n" for warning in warnings]
    for review in sorted(reviews, key=lambda row: normalize_key(row.topic)):
        if review.summary_status == "ok":
            continue
        label = SUMMARY_LABEL[review.summary_status]
        notes.append(f"<li>{_h(review.topic)}：{_h(label)}</li>\n")
    if notes:
        parts.append("<ul>\n")
        parts.extend(notes)
        parts.append("</ul>\n")
    else:
        parts.append("<p>没有额外警告</p>\n")
    parts.append("<h2>脚注</h2>\n")
    parts.append(f"<p>{_h(FOOTNOTE)}</p>\n")
    parts.append("</body>\n</html>\n")
    return "".join(parts).encode("utf-8")


def _article_html(item: ClassifiedItem) -> str:
    comments = _comment_html(item.comments)
    return (
        "<article>\n"
        f"<h3>{_h(item.title)}</h3>\n"
        f"<p>来源：{_h(item.source_id)}</p>\n"
        f"<p>主题：{_h(item.topic)}</p>\n"
        f"<p>分档：{_h(BUCKET_LABEL[item.bucket])}</p>\n"
        f"<p>发布时间：{_h(_when(item.published_at))}</p>\n"
        f"<p>理由：{_h(item.reason)}</p>\n"
        f"{comments}"
        f"{_link_html(item.url)}"
        "</article>\n"
    )


def _comment_html(comments: tuple[str, ...]) -> str:
    if not comments:
        return "<p>观众摘句：未提供评论文字</p>\n"
    items = "".join(f"<li>{_h(comment)}</li>\n" for comment in comments)
    return f"<p>观众摘句</p>\n<ul>\n{items}</ul>\n"


def _queue_html(item: Item, *, confirmed: bool) -> str:
    when = _when(item.published_at) if confirmed else "日期待确认"
    return (
        "<article>\n"
        f"<h3>{_h(item.title)}</h3>\n"
        f"<p>来源：{_h(item.source_id)}</p>\n"
        f"<p>发布时间：{_h(when)}</p>\n"
        f"{_link_html(item.url)}"
        "</article>\n"
    )


def _topic_markdown(page: _TopicBucket) -> bytes:
    review = page.review
    moments = [] if review is None else [row.published_at for row in review.rows]
    lines = [
        f"# {_md(page.topic)}",
        "",
        f"范围：{LIBRARY}",
        f"时间范围：{_range_text(moments)}",
        "",
    ]
    rows = ()
    if review is not None:
        rows = tuple(sorted(review.rows, key=lambda row: (row.source_id, row.id)))
    if review is not None and rows:
        sources = {row.source_id for row in rows}
        lines.append(f"## {'多源对照' if len(sources) >= 2 else '单源材料汇总'}")
        lines.append("")
        lines.extend(
            [
                "| 标题 | 链接 | 来源 | 发布时间 | 分档 | 理由 | 证据 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for row in rows:
            evidence = _evidence_cell(row)
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md(row.title, pipe=True),
                        _md_link(row.title, row.url, pipe=True),
                        _md(row.source_id, pipe=True),
                        _md(_when(row.published_at), pipe=True),
                        BUCKET_LABEL[row.bucket],
                        _md(row.reason, pipe=True),
                        evidence,
                    )
                )
                + " |"
            )
        lines.append("")
        lines.append("## 观众摘句")
        lines.append("")
        for row in rows:
            if row.comments:
                for comment in row.comments:
                    lines.append(f"- {_md(row.title)}：{_md(comment)}")
            else:
                lines.append(f"- {_md(row.title)}：未提供评论文字")
        lines.append("")
        lines.append("## 证据")
        lines.append("")
        wrote = False
        for row in rows:
            for claim in row.claims:
                wrote = True
                kind = EVIDENCE_LABEL[claim.evidence_kind]
                lines.append(f"- {_md(row.title)} {kind}：{_md(claim.evidence_quote)}")
        if not wrote:
            lines.append("未提供正文摘句")
        lines.append("")
        lines.append("## 短评")
        lines.append("")
        lines.append(SUMMARY_LABEL[review.summary_status])
        if review.sentences:
            lines.append("仅基于本次所列选中材料")
            for sentence in review.sentences:
                lines.append(f"- {KIND_LABEL[sentence.kind]}：{_md(sentence.text)}")
        lines.append("")
    lines.append("## 非共识")
    lines.append("")
    if not page.assessments:
        lines.append("无")
    for assessment in page.assessments:
        label = ASSESSMENT_LABEL[assessment.status]
        lines.append(
            f"- {_md(assessment.claim_key)}：{label}。"
            f"来源票：支持 {assessment.n_for}，反对 {assessment.n_against}，"
            f"条目 {assessment.n_items}，来源 {assessment.n_sources}。"
        )
        if assessment.excluded_source_ids:
            names = "、".join(_md(source) for source in assessment.excluded_source_ids)
            lines.append(f"排除来源：{names}")
    lines.extend(["", "## 脚注", "", FOOTNOTE, ""])
    return _text(lines)


def _opportunity_markdown(
    opportunity: object,
    assessment: object | None,
    items_by_id: dict[str, ClassifiedItem],
) -> bytes:
    n_items = assessment.n_items if assessment is not None else len(
        set(opportunity.minority_item_ids) | set(opportunity.majority_item_ids)
    )
    involved = []
    for item_id in (*opportunity.minority_item_ids, *opportunity.majority_item_ids):
        row = items_by_id.get(item_id)
        involved.append(None if row is None else row.published_at)
    lines = [
        "# 少数候选",
        "",
        f"范围：{LIBRARY}",
        f"时间范围：{_range_text(involved)}",
        "",
        f"命题：{_md(opportunity.claim_key)}",
        f"主题：{_md(opportunity.topic)}",
        f"少数立场：{STANCE_LABEL[opportunity.minority_stance]}",
        f"少数句：{_md(opportunity.minority_sentence)}",
        (
            f"来源票数：少数 {opportunity.minority_count}，多数 {opportunity.majority_count}，"
            f"来源 {opportunity.n_sources}，条目 {n_items}"
        ),
        "",
        "## 少数来源",
        "",
        *_side_lines(opportunity.minority_item_ids, items_by_id),
        "",
        "## 多数来源",
        "",
        *_side_lines(opportunity.majority_item_ids, items_by_id),
        "",
        "## 排除来源",
        "",
    ]
    excluded = () if assessment is None else assessment.excluded_source_ids
    if excluded:
        lines.extend(f"- {_md(source)}" for source in excluded)
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 可证伪说明",
            "",
            _md(opportunity.falsify_hint),
            "",
            "## 脚注",
            "",
            FOOTNOTE,
            "",
        ]
    )
    return _text(lines)


def _side_lines(item_ids: tuple[str, ...], items_by_id: dict[str, ClassifiedItem]) -> list[str]:
    if not item_ids:
        return ["- 无"]
    lines = []
    ordered = sorted(
        item_ids,
        key=lambda item_id: (
            items_by_id[item_id].source_id if item_id in items_by_id else "",
            item_id,
        ),
    )
    for item_id in ordered:
        row = items_by_id.get(item_id)
        if row is None:
            lines.append(f"- {_md(item_id)}")
            continue
        lines.append(f"- {_md(row.source_id)} {_md_link(row.title, row.url, pipe=False)}")
    return lines


def _topic_pages(
    reviews: tuple[TopicReview, ...],
    consensus: ConsensusResult,
) -> dict[str, _TopicBucket]:
    pages: dict[str, dict[str, object]] = {}

    def bucket(topic: str) -> dict[str, object]:
        key = normalize_key(topic)
        found = pages.get(key)
        if found is None:
            found = {"topic": topic, "review": None, "assessments": [], "opportunities": []}
            pages[key] = found
        return found

    seen: set[str] = set()
    for review in reviews:
        key = normalize_key(review.topic)
        if key in seen:
            raise SchemaError("invalid_topic")
        seen.add(key)
        if any(normalize_key(row.topic) != key for row in review.rows):
            raise SchemaError("invalid_topic")
        slot = bucket(review.topic)
        slot["topic"] = review.topic
        slot["review"] = review
    for assessment in consensus.assessments:
        bucket(assessment.topic)["assessments"].append(assessment)
    for opportunity in consensus.opportunities:
        bucket(opportunity.topic)["opportunities"].append(opportunity)
    rendered: dict[str, _TopicBucket] = {}
    for key, slot in pages.items():
        assessments = tuple(
            sorted(slot["assessments"], key=lambda row: normalize_key(row.claim_key))
        )
        opportunities = tuple(
            sorted(slot["opportunities"], key=lambda row: normalize_key(row.claim_key))
        )
        rendered[key] = _TopicBucket(slot["topic"], slot["review"], assessments, opportunities)
    return rendered


def _assessment_index(consensus: ConsensusResult) -> dict[tuple[str, str], object]:
    found: dict[tuple[str, str], object] = {}
    for assessment in consensus.assessments:
        key = (normalize_key(assessment.topic), normalize_key(assessment.claim_key))
        if key in found:
            raise SchemaError("invalid_record")
        found[key] = assessment
    seen: set[tuple[str, str]] = set()
    for opportunity in consensus.opportunities:
        key = (normalize_key(opportunity.topic), normalize_key(opportunity.claim_key))
        if key in seen:
            raise SchemaError("invalid_record")
        seen.add(key)
    return found


def _index_items(
    delta: tuple[ClassifiedItem, ...],
    reviews: tuple[TopicReview, ...],
) -> dict[str, ClassifiedItem]:
    found: dict[str, ClassifiedItem] = {}
    seen: set[str] = set()
    for review in reviews:
        for row in review.rows:
            if row.id in seen:
                raise SchemaError("invalid_record")
            seen.add(row.id)
            found[row.id] = row
    for row in delta:
        current = found.get(row.id)
        if current is None:
            found[row.id] = row
            continue
        same = (
            current.analysis_hash == row.analysis_hash
            and current.title == row.title
            and current.url == row.url
        )
        if not same:
            raise SchemaError("invalid_record")
    return found


def _manifest_bytes(
    *,
    run_id: str,
    now: str,
    refs: tuple[tuple[str, str], ...],
    files: dict[str, bytes],
    status: str,
    warnings: tuple[str, ...],
    topics: tuple[dict[str, str], ...],
    opportunities: tuple[dict[str, str], ...],
    published_from: str | None,
    published_to: str | None,
) -> bytes:
    payload = {
        "classified_refs": [list(ref) for ref in refs],
        "files": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(files.items())
        ],
        "now": now,
        "opportunities": [dict(item) for item in opportunities],
        "run_id": run_id,
        "scope": {"label": LIBRARY, "published_from": published_from, "published_to": published_to},
        "status": status,
        "topics": [dict(item) for item in topics],
        "warnings": list(warnings),
    }
    return (canonical_json(payload) + "\n").encode("utf-8")


def _load_manifest(path: Path, publication: Publication) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PublishError("invalid_manifest")
    try:
        loaded = json.loads(_read(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise PublishError("invalid_manifest") from None
    if not isinstance(loaded, dict):
        raise PublishError("invalid_manifest")
    expected = {
        "classified_refs",
        "files",
        "now",
        "opportunities",
        "run_id",
        "scope",
        "status",
        "topics",
        "warnings",
    }
    if set(loaded) != expected or loaded.get("run_id") != publication.run_id:
        raise PublishError("invalid_manifest")
    if loaded.get("status") not in {"已发布", "部分完成"}:
        raise PublishError("invalid_manifest")
    if not isinstance(loaded.get("now"), str) or _TIME.fullmatch(loaded["now"]) is None:
        raise PublishError("invalid_manifest")
    refs = loaded.get("classified_refs")
    if not isinstance(refs, list):
        raise PublishError("invalid_manifest")
    try:
        parsed = tuple((item, digest) for item, digest in (tuple(ref) for ref in refs))
    except (TypeError, ValueError):
        raise PublishError("invalid_manifest") from None
    if parsed != publication.classified_refs:
        raise PublishError("publication_mismatch")
    scope = loaded.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"label", "published_from", "published_to"}:
        raise PublishError("invalid_manifest")
    if scope.get("label") != LIBRARY:
        raise PublishError("invalid_manifest")
    for name in ("published_from", "published_to"):
        value = scope.get(name)
        if value is not None and (not isinstance(value, str) or _TIME.fullmatch(value) is None):
            raise PublishError("invalid_manifest")
    warnings = loaded.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise PublishError("invalid_manifest")
    _checked_names(loaded.get("topics"), ("slug", "topic"))
    _checked_names(loaded.get("opportunities"), ("claim_key", "slug", "topic"))
    files = _file_entries(loaded)
    digest = f"digest/{publication.run_id}.html"
    if sum(1 for entry in files if entry["path"] == digest) != 1:
        raise PublishError("invalid_manifest")
    topic_slugs = {item["slug"] for item in loaded["topics"]}
    opportunity_slugs = {item["slug"] for item in loaded["opportunities"]}
    file_topics = set()
    file_opportunities = set()
    for entry in files:
        path = entry["path"]
        if not _allowed_file(publication.run_id, path):
            raise PublishError("invalid_manifest")
        if "/topics/" in path:
            file_topics.add(path.rsplit("/", 1)[1][:-3])
        elif "/opportunities/" in path:
            file_opportunities.add(path.rsplit("/", 1)[1][:-3])
    if file_topics != topic_slugs or file_opportunities != opportunity_slugs:
        raise PublishError("invalid_manifest")
    return loaded


def _checked_names(value: object, keys: tuple[str, ...]) -> None:
    if not isinstance(value, list):
        raise PublishError("invalid_manifest")
    for item in value:
        if not isinstance(item, dict) or set(item) != set(keys):
            raise PublishError("invalid_manifest")
        for key in keys:
            text = item.get(key)
            if not isinstance(text, str) or text == "" or any(ord(char) < 32 for char in text):
                raise PublishError("invalid_manifest")
        if _SLUG.fullmatch(item["slug"]) is None:
            raise PublishError("invalid_manifest")


def _file_entries(manifest: dict[str, object]) -> list[dict[str, str]]:
    value = manifest.get("files")
    if not isinstance(value, list) or not value:
        raise PublishError("invalid_manifest")
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise PublishError("invalid_manifest")
        path = item.get("path")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or _SHA.fullmatch(digest) is None
        ):
            raise PublishError("invalid_manifest")
        entries.append({"path": path, "sha256": digest})
    return entries


def _allowed_file(run_id: str, path: str) -> bool:
    if path == f"digest/{run_id}.html":
        return True
    for folder in ("topics", "opportunities"):
        prefix = f"snapshots/{run_id}/{folder}/"
        name = path[len(prefix) : -3]
        if path.startswith(prefix) and path.endswith(".md") and _SLUG.fullmatch(name):
            return True
    return False


def _verify(base: Path, manifest: dict[str, object]) -> None:
    for entry in _file_entries(manifest):
        path = _join(base, entry["path"])
        _assert_inside(base, path)
        if path.is_symlink() or not path.is_file():
            raise PublishError("missing_snapshot")
        if _sha_file(path) != entry["sha256"]:
            raise PublishError("hash_mismatch")


def _hash_for(manifest: dict[str, object], relative: str) -> str:
    for entry in _file_entries(manifest):
        if entry["path"] == relative:
            return entry["sha256"]
    raise PublishError("invalid_manifest")


def _index_text(root: Path, manifest: dict[str, object]) -> str:
    history: list[tuple[str, str]] = []
    index = root / "index.md"
    if index.exists():
        if index.is_symlink() or not index.is_file():
            raise PublishError("invalid_index")
        _run, _moment, _status, parsed = _parse_index(_read(index).decode("utf-8"))
        history.extend(parsed)
    current = (str(manifest["now"]), str(manifest["run_id"]))
    if current not in history:
        history.append(current)
    return _render_index(
        run_id=str(manifest["run_id"]),
        now=str(manifest["now"]),
        status=str(manifest["status"]),
        history=tuple(sorted(set(history))),
    )


def _render_index(
    *,
    run_id: str,
    now: str,
    status: str,
    history: tuple[tuple[str, str], ...],
) -> str:
    lines = [
        "# 索引",
        "",
        f"当前run_id: {run_id}",
        f"当前时间: {now}",
        f"当前状态: {status}",
        f"当前快照: snapshots/{run_id}/",
        f"当前摘要: digest/{run_id}.html",
        "",
        "## 历史",
        "",
    ]
    lines.extend(f"历史: {moment} {run}" for moment, run in history)
    lines.append("")
    return "\n".join(lines)


def _parse_index(text: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    lines = text.splitlines()
    if len(lines) < 11 or lines[0] != "# 索引" or lines[1] != "":
        raise PublishError("invalid_index")
    run_id = _index_field(lines[2], "当前run_id")
    now = _index_field(lines[3], "当前时间")
    status = _index_field(lines[4], "当前状态")
    if lines[5] != f"当前快照: snapshots/{run_id}/" or lines[6] != f"当前摘要: digest/{run_id}.html":
        raise PublishError("invalid_index")
    if lines[7] != "" or lines[8] != "## 历史" or lines[9] != "":
        raise PublishError("invalid_index")
    known_status = status in {"已发布", "部分完成"}
    if _RUN.fullmatch(run_id) is None or _TIME.fullmatch(now) is None or not known_status:
        raise PublishError("invalid_index")
    history: list[tuple[str, str]] = []
    for line in lines[10:]:
        if not line.startswith("历史: "):
            raise PublishError("invalid_index")
        moment, separator, run = line[len("历史: ") :].partition(" ")
        if separator != " " or _TIME.fullmatch(moment) is None or _RUN.fullmatch(run) is None:
            raise PublishError("invalid_index")
        history.append((moment, run))
    if (now, run_id) not in history:
        raise PublishError("invalid_index")
    return run_id, now, status, tuple(history)


def _index_field(line: str, name: str) -> str:
    prefix = f"{name}: "
    if not line.startswith(prefix):
        raise PublishError("invalid_index")
    return line[len(prefix) :]


def _log_body(run_id: str, events: Sequence[LlmEvent | FetchEvent]) -> bytes:
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise SchemaError("invalid_record")
    rendered: list[dict[str, object]] = []
    for event in events:
        if isinstance(event, LlmEvent):
            if event.run_id != run_id:
                raise SchemaError("invalid_run_id")
            rendered.append(_llm_payload(event))
        elif isinstance(event, FetchEvent):
            rendered.append(_fetch_payload(event))
        else:
            raise SchemaError("invalid_record")
    body = (canonical_json({"events": rendered, "run_id": run_id}) + "\n").encode("utf-8")
    if b"<!--" in body:
        raise PublishError("invalid_log")
    return body


def _llm_payload(event: LlmEvent) -> dict[str, object]:
    usage = event.usage
    cost = None if usage is None else usage.total_cost_usd
    return {
        "at": event.at,
        "billing_path": event.billing_path,
        "cache_creation_input_tokens": None if usage is None else usage.cache_creation_input_tokens,
        "cache_read_input_tokens": None if usage is None else usage.cache_read_input_tokens,
        "call_id": event.call_id,
        "error_code": event.error_code,
        "input_tokens": None if usage is None else usage.input_tokens,
        "kind": "llm",
        "output_tokens": None if usage is None else usage.output_tokens,
        "purpose": event.purpose,
        "reasoning_tokens": None if usage is None else usage.reasoning_tokens,
        "status": event.status,
        "total_cost_usd": "UNKNOWN" if cost is None else cost,
    }


def _fetch_payload(event: FetchEvent) -> dict[str, object]:
    return {
        "at": event.at,
        "error_code": event.error_code,
        "host": event.host,
        "kind": "fetch",
        "retry_after_s": event.retry_after_s,
        "source_id": event.source_id,
        "status": event.status,
        "url_hash": event.url_hash,
        "url_redacted": event.url_redacted,
    }


def _log_block(run_id: str, digest: str, body: bytes) -> bytes:
    begin = f"<!-- BEGIN run_id={run_id} sha256={digest} -->\n".encode("utf-8")
    end = f"<!-- END run_id={run_id} sha256={digest} -->\n".encode("utf-8")
    return begin + body + end


def _has_block(existing: bytes, run_id: str, digest: str) -> bool:
    for match in _BLOCK.finditer(existing):
        found_run = match.group(1).decode("ascii")
        found_hash = match.group(2).decode("ascii")
        content = match.group(3) + b"\n"
        if hashlib.sha256(content).hexdigest() != found_hash:
            continue
        if found_run == run_id and found_hash == digest:
            return True
    return False


def _rows(value: object) -> tuple[ClassifiedItem, ...]:
    return _typed(value, ClassifiedItem, "invalid_record")


def _reviews(value: object) -> tuple[TopicReview, ...]:
    return _typed(value, TopicReview, "invalid_record")


def _items(value: object) -> tuple[Item, ...]:
    return _typed(value, Item, "invalid_record")


def _consensus(value: object) -> ConsensusResult:
    if not isinstance(value, ConsensusResult):
        raise SchemaError("invalid_record")
    return value


def _warnings(value: object) -> tuple[str, ...]:
    items = _typed(value, str, "invalid_text")
    for item in items:
        if len(item) > 500 or any(ord(char) < 32 for char in item):
            raise SchemaError("invalid_text")
    return items


def _typed(value: object, kind: type, code: str) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SchemaError(code)
    items = tuple(value)
    if any(not isinstance(item, kind) for item in items):
        raise SchemaError(code)
    return items


def _unique(items: Sequence[ClassifiedItem] | Sequence[Item]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise SchemaError("invalid_record")
        seen.add(item.id)


def _by_source(items: Sequence[Item]) -> tuple[Item, ...]:
    return tuple(sorted(items, key=lambda item: (item.source_id, item.id)))


def _bounds(values: Sequence[str | None]) -> tuple[str | None, str | None]:
    known = sorted(value for value in values if value is not None)
    if not known:
        return None, None
    return known[0], known[-1]


def _range_text(values: Sequence[str | None]) -> str:
    known = sorted(value for value in values if value is not None)
    unknown = any(value is None for value in values)
    if not known:
        return "发布时间未知"
    text = f"{known[0]} 至 {known[-1]}"
    if unknown:
        text += "，另有发布时间未知的条目"
    return text


def _when(value: str | None) -> str:
    return "发布时间未知" if value is None else value


def _evidence_cell(row: ClassifiedItem) -> str:
    kinds = {claim.evidence_kind for claim in row.claims}
    if "comment" in kinds and ({"title", "body"} & kinds or row.comments):
        return "正文摘句；观众摘句"
    if "comment" in kinds or row.comments:
        return "观众摘句"
    if kinds:
        return "正文摘句"
    return "未提供评论文字"


def _h(text: str) -> str:
    return html.escape(_flat(text), quote=True)


def _md(text: str, *, pipe: bool = False) -> str:
    safe = html.escape(_flat(text), quote=False).translate(_MD)
    if pipe:
        safe = safe.replace("|", "\\|")
    return safe


def _md_link(text: str, url: str, *, pipe: bool) -> str:
    shown = _md(text, pipe=pipe)
    if _href_ok(url):
        return f"[{shown}]({url})"
    return shown


def _link_html(url: str) -> str:
    shown = _h(url)
    if _href_ok(url):
        return f'<p>来源链接：<a href="{shown}">{shown}</a></p>\n'
    return f"<p>来源链接：{shown}</p>\n"


def _href_ok(url: str) -> bool:
    if any(char in url for char in " \t\r\n\"'<>\\()"):
        return False
    try:
        if canonical_url(url) != url:
            return False
        guard_public_url(url)
    except (SchemaError, UnsafeUrl):
        return False
    return True


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _text(lines: Sequence[str]) -> bytes:
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _run_id(value: object) -> str:
    if not isinstance(value, str) or _RUN.fullmatch(value) is None:
        raise SchemaError("invalid_run_id")
    return value


def _join(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or relative.startswith(("/", "\\")) or "\\" in relative:
        raise PublishError("escape")
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PublishError("escape")
    return root.joinpath(*parts)


def _assert_inside(root: Path, path: Path) -> None:
    if path.is_symlink():
        raise PublishError("symlink_escape")
    root_real = root.resolve(strict=True)
    probe = path
    while True:
        if probe.is_symlink():
            raise PublishError("symlink_escape")
        if probe.exists():
            real = probe.resolve(strict=True)
            if real != root_real and not real.is_relative_to(root_real):
                raise PublishError("symlink_escape")
            return
        if probe == probe.parent:
            raise PublishError("escape")
        probe = probe.parent


def _remove_tree(root: Path, target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink() or not target.is_dir():
        raise PublishError("symlink_escape")
    _assert_inside(root, target)
    for current, dirnames, filenames in os.walk(target, topdown=False, followlinks=False):
        directory = Path(current)
        if directory.is_symlink():
            raise PublishError("symlink_escape")
        for name in filenames:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise PublishError("symlink_escape")
            _assert_inside(root, path)
            path.unlink()
        for name in list(dirnames):
            path = directory / name
            if path.is_symlink() or not path.is_dir():
                raise PublishError("symlink_escape")
            _assert_inside(root, path)
            path.rmdir()
    target.rmdir()


def _read(path: Path) -> bytes:
    if path.is_symlink():
        raise PublishError("symlink_escape")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise PublishError("publish_io") from None
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(_read(path)).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise PublishError("hash_mismatch")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError:
        raise PublishError("publish_io") from None
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    except OSError:
        raise PublishError("publish_io") from None
    finally:
        os.close(fd)


def _append(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError:
        raise PublishError("publish_io") from None
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    except OSError:
        raise PublishError("publish_io") from None
    finally:
        os.close(fd)


def _replace_file(root: Path, dest: Path, payload: bytes) -> None:
    try:
        fd, name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=root)
    except OSError:
        raise PublishError("publish_io") from None
    tmp = Path(name)
    replaced = False
    try:
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dest)
        replaced = True
        _fsync_dir(root)
    except OSError:
        raise PublishError("publish_io") from None
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        raise PublishError("publish_io") from None
    try:
        os.fsync(fd)
    except OSError:
        raise PublishError("publish_io") from None
    finally:
        os.close(fd)
