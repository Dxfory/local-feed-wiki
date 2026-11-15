"""Repository markdown: relative links exist, and the history docs stay put."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
_PULL_URL = "/Dxfory/local-feed-wiki/pull/"
_RUN_STATE_DIRS = frozenset(
    {"data", "raw", "wiki", "venv", "node_modules", "build", "dist"}
)


def _tracked_markdown() -> list[Path]:
    paths: list[Path] = []
    for path in ROOT.rglob("*.md"):
        parts = path.relative_to(ROOT).parts
        if any(
            part.startswith(".") or part in _RUN_STATE_DIRS or part.endswith(".egg-info")
            for part in parts
        ):
            continue
        paths.append(path)
    assert paths
    return paths


def _destination(text: str, start: int) -> tuple[str, int]:
    n = len(text)
    i = start
    while i < n and text[i] in " \t":
        i += 1
    if i >= n:
        return "", i
    if text[i] == "<":
        end = text.find(">", i + 1)
        if end == -1:
            return "", n
        return text[i + 1 : end], end + 1
    depth = 0
    buf: list[str] = []
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
            buf.append(ch)
        elif ch in " \t\r\n" and depth == 0:
            break
        else:
            buf.append(ch)
        i += 1
    return "".join(buf), i


def _relative_targets(text: str) -> list[str]:
    targets: list[str] = []
    start = 0
    while True:
        found = text.find("](", start)
        if found == -1:
            break
        dest, end = _destination(text, found + 2)
        start = end if end > found else found + 2
        lowered = dest.lower()
        if lowered.startswith(("http://", "https://", "mailto:")):
            continue
        if dest.startswith("#"):
            continue
        path = unquote(dest.split("#", 1)[0]).strip()
        if path == "":
            continue
        targets.append(path)
    return targets


def _on_disk(markdown: Path, target: str) -> Path:
    if target.startswith("/"):
        return ROOT / target.lstrip("/")
    return markdown.parent / target


def test_relative_markdown_links_in_tracked_files_exist() -> None:
    missing: list[str] = []
    checked = 0
    for markdown in _tracked_markdown():
        text = markdown.read_text(encoding="utf-8")
        for target in _relative_targets(text):
            checked += 1
            candidate = _on_disk(markdown, target)
            if not candidate.exists():
                missing.append(f"{markdown.relative_to(ROOT)} -> {target}")
    assert checked
    assert missing == []


def test_history_decisions_has_no_pull_request_url() -> None:
    path = ROOT / "docs" / "history" / "DECISIONS.md"
    assert path.is_file()
    assert _PULL_URL not in path.read_text(encoding="utf-8")


def test_decision_and_audit_files_moved_under_history() -> None:
    assert not (ROOT / "docs" / "DECISIONS.md").exists()
    assert not (ROOT / "docs" / "PUBLICATION_AUDIT.md").exists()
    assert (ROOT / "docs" / "history" / "DECISIONS.md").is_file()
    assert (ROOT / "docs" / "history" / "PUBLICATION_AUDIT.md").is_file()
