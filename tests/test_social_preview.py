"""Social preview PNG and the proposed repository metadata file.

The PNG contract is the bytes on disk: signature, IHDR size, and file size.
The metadata contract is the proposal a repository owner would copy into
settings: a short description and at most twenty topic slugs.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PNG_PATH = REPO / "docs" / "assets" / "social-preview.png"
METADATA_PATH = REPO / "docs" / "REPO_METADATA.md"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
ONE_MIB = 1024 * 1024

DESCRIPTION = (
    "Local-first LLM wiki for your RSS, podcast & YouTube feeds - "
    "immutable raw evidence, regenerable digest, and a page for where sources disagree."
)
TOPICS = (
    "rss",
    "podcast",
    "youtube",
    "llm",
    "second-brain",
    "knowledge-base",
    "personal-knowledge-management",
    "local-first",
    "offline-first",
    "self-hosted",
    "karpathy",
    "llm-wiki",
    "feed-reader",
    "digest",
    "sqlite",
    "python",
    "cli",
    "wiki",
    "atom",
)


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    assert start is not None, f"missing heading {heading}"
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body).strip()


def _description(text: str) -> str:
    body = _section(text, "## Description")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    assert paragraphs, "description section is empty"
    return paragraphs[0].replace("\n", " ")


def _topics(text: str) -> list[str]:
    body = _section(text, "## Topics")
    return [
        line.strip()[2:].strip()
        for line in body.splitlines()
        if line.strip().startswith("- ")
    ]


def test_social_preview_png_signature_dimensions_and_size() -> None:
    assert PNG_PATH.is_file()
    data = PNG_PATH.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    assert len(data) <= ONE_MIB
    length, chunk_type = struct.unpack(">I4s", data[8:16])
    assert chunk_type == b"IHDR"
    assert length == 13
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1280, 640)


def _metadata_text() -> str:
    assert METADATA_PATH.is_file()
    return METADATA_PATH.read_text(encoding="utf-8")


def test_repo_metadata_description_is_within_350_characters() -> None:
    assert len(DESCRIPTION) <= 350
    assert _description(_metadata_text()) == DESCRIPTION


def test_repo_metadata_lists_at_most_twenty_topic_slugs() -> None:
    topics = _topics(_metadata_text())
    assert topics == list(TOPICS)
    assert len(topics) <= 20
    for topic in topics:
        assert TOPIC_RE.fullmatch(topic), topic


def test_repo_metadata_records_homepage_preview_and_approval() -> None:
    text = _metadata_text()
    assert _section(text, "## Homepage") == "to be set when the sample page is published"
    assert _section(text, "## Social preview") == "docs/assets/social-preview.png"
    assert (
        "applied in repository settings only after owner approval" in text
    )
    assert "This file does not change any setting." in text
    assert "held until the backend work merges" in text
