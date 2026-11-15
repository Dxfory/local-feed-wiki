"""Changelog headings stay tied to the packaged version.

The released heading after Unreleased must match ``project.version`` in
``pyproject.toml``. The matching notes file must name that version, and
every ``## [x]`` heading must have a link reference definition.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)
_LINK = re.compile(r"^\[([^\]]+)\]: (\S+)\s*$", re.MULTILINE)


def _version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert isinstance(version, str) and version != ""
    return version


def _changelog() -> str:
    path = ROOT / "CHANGELOG.md"
    assert path.is_file()
    return path.read_text(encoding="utf-8")


def test_top_version_heading_matches_pyproject() -> None:
    headings = _HEADING.findall(_changelog())
    assert "Unreleased" in headings
    top = headings[headings.index("Unreleased") + 1]
    assert top == _version()


def test_release_notes_name_the_same_version() -> None:
    version = _version()
    path = ROOT / "docs" / "releases" / f"v{version}.md"
    assert path.is_file()
    assert version in path.read_text(encoding="utf-8")


def test_every_heading_has_a_link_reference() -> None:
    text = _changelog()
    headings = _HEADING.findall(text)
    links = dict(_LINK.findall(text))
    assert headings
    version = _version()
    assert links["Unreleased"] == (
        f"https://github.com/Dxfory/local-feed-wiki/compare/v{version}...HEAD"
    )
    assert links[version] == (
        f"https://github.com/Dxfory/local-feed-wiki/releases/tag/v{version}"
    )
    missing = [name for name in headings if name not in links]
    assert missing == []
