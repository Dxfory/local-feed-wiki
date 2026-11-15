"""Contributor drafts stay countable, labeled, and marked as drafts.

These checks fail when a seed heading is dropped or merged, when a section
loses its acceptance block or good-first-issue label, when a discussion
draft is missing its draft header, or when the starter-issue page no
longer points at the seed file.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "docs" / "ISSUE_SEEDS.md"
ROADMAP = ROOT / "docs" / "discussions" / "roadmap.md"
SHOW_FEEDS = ROOT / "docs" / "discussions" / "show-your-feeds.md"
STARTER = ROOT / "docs" / "GOOD_FIRST_ISSUES.md"
DRAFT_HEADER = (
    "Draft. Issues/posts are created by a maintainer; this file does not create anything."
)


def _titled_sections(text: str) -> list[str]:
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("### ")]
    sections: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        sections.append("\n".join(lines[start:end]))
    return sections


def test_issue_seeds_has_exactly_nine_titles() -> None:
    assert SEEDS.is_file()
    titles = [line for line in SEEDS.read_text(encoding="utf-8").splitlines() if line.startswith("### ")]
    assert len(titles) == 9
    assert len(set(titles)) == 9


def test_each_issue_seed_has_acceptance_and_good_first_issue_label() -> None:
    assert SEEDS.is_file()
    sections = _titled_sections(SEEDS.read_text(encoding="utf-8"))
    assert len(sections) == 9
    for section in sections:
        assert "Acceptance" in section
        assert "Labels: good first issue" in section


def test_issue_seeds_and_discussion_drafts_carry_the_draft_header() -> None:
    for path in (SEEDS, ROADMAP, SHOW_FEEDS):
        assert path.is_file(), path
        assert DRAFT_HEADER in path.read_text(encoding="utf-8").splitlines()


def test_good_first_issues_links_issue_seeds_on_the_first_line() -> None:
    lines = STARTER.read_text(encoding="utf-8").splitlines()
    assert "ISSUE_SEEDS.md" in lines[0]
    assert lines[1] == ""
    assert lines[2] == "# Good first issues"
