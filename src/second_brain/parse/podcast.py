"""Turn a supplied podcast transcript into plain text.

parse_podcast does not read audio_url or transcript_url, and it does not
open files or start a program. Blank text is pending_transcript. Plain text
keeps its characters and collapses whitespace. VTT and SRT drop cue
identifiers, timing lines, and NOTE, STYLE, and REGION blocks, then merge
the remaining cue text in file order. A transcript that claims one of those
formats but does not have that shape is invalid_transcript.
"""

from __future__ import annotations

import re
from html import unescape

from second_brain.errors import SchemaError
from second_brain.models import ParsedPodcast

_FORMATS = {"plain": "plain", "vtt": "vtt", "srt": "srt"}
_CONTROL = frozenset({"note", "style", "region"})
_WHITESPACE = re.compile(r"\s+")
_VTT_SIGNATURE = re.compile(r"^WEBVTT(?:[ \t].*)?$")
# Hours are optional for VTT and required for SRT. The fraction is three
# digits, and only ASCII space or tab may surround the arrow.
_CLOCK_HOURS = r"\d+:\d{2}:\d{2}[.,]\d{3}"
_CLOCK_SHORT = r"\d{2}:\d{2}[.,]\d{3}"
_VTT_TIMING = re.compile(
    rf"(?:{_CLOCK_HOURS}|{_CLOCK_SHORT})[ \t]+-->[ \t]+"
    rf"(?:{_CLOCK_HOURS}|{_CLOCK_SHORT})(?:[ \t].*)?",
    re.ASCII,
)
_SRT_TIMING = re.compile(
    rf"{_CLOCK_HOURS}[ \t]+-->[ \t]+{_CLOCK_HOURS}(?:[ \t].*)?",
    re.ASCII,
)


def parse_podcast(
    title: str,
    transcript: str | None,
    audio_url: str | None,
    transcript_url: str | None = None,
    *,
    transcript_format: str = "plain",
) -> ParsedPodcast:
    """Parse transcript text the caller already holds.

    audio_url and transcript_url are accepted and ignored. None, blank text,
    and a subtitle file with no cue text return an empty pending result.
    plain, vtt, and srt are the accepted format names, compared without case
    after stripping. Other names, including media types, are rejected.
    """
    # The addresses stay in the frozen signature. Reading them is out of scope.
    del audio_url, transcript_url
    _require_title(title)
    kind = _require_format(transcript_format)
    raw = _require_transcript(transcript)
    if raw is None or _collapse(_prepare(raw)) == "":
        return _pending()
    if kind == "plain":
        body = _collapse(_prepare(raw))
    elif kind == "vtt":
        body = _vtt_text(raw)
    else:
        body = _srt_text(raw)
    if body == "":
        return _pending()
    return ParsedPodcast(body_plain=body, transcript_status="ready")


def _require_title(title: object) -> None:
    if not isinstance(title, str) or "\x00" in title:
        raise SchemaError("invalid_title")


def _require_format(value: object) -> str:
    if not isinstance(value, str):
        raise SchemaError("invalid_transcript_format")
    kind = _FORMATS.get(value.strip().casefold())
    if kind is None:
        raise SchemaError("invalid_transcript_format")
    return kind


def _require_transcript(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise SchemaError("invalid_text")
    return value


def _pending() -> ParsedPodcast:
    return ParsedPodcast(body_plain="", transcript_status="pending_transcript")


def _prepare(text: str) -> str:
    without_mark = text.lstrip("\ufeff")
    return (
        without_mark.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _is_control(line: str) -> bool:
    stripped = line.strip()
    if stripped == "":
        return False
    token = stripped.split(None, 1)[0]
    return token.casefold() in _CONTROL


def _cue_text(lines: list[str], timing: re.Pattern[str]) -> str:
    """Spoken lines of one cue. At most one identifier line may precede the clock."""
    found: int | None = None
    for index, line in enumerate(lines[:2]):
        if "-->" not in line:
            continue
        if timing.fullmatch(line.strip()) is None:
            raise SchemaError("invalid_transcript")
        found = index
        break
    if found is None:
        raise SchemaError("invalid_transcript")
    spoken = [line.strip() for line in lines[found + 1 :] if line.strip() != ""]
    return " ".join(spoken)


def _merge(cues: list[str]) -> str:
    parts: list[str] = []
    for cue in cues:
        decoded = unescape(cue)
        # A numeric NUL becomes U+FFFD in the standard unescape. Reject a raw NUL.
        if "\x00" in decoded:
            raise SchemaError("invalid_text")
        if decoded.strip() == "":
            continue
        parts.append(decoded)
    return _collapse(" ".join(parts))


def _vtt_text(text: str) -> str:
    prepared = _prepare(text)
    first = prepared.split("\n", 1)[0]
    if prepared[:1].isspace() or _VTT_SIGNATURE.fullmatch(first) is None:
        raise SchemaError("invalid_transcript")
    blocks = _blocks(prepared)
    if any("-->" in line for line in blocks[0]):
        raise SchemaError("invalid_transcript")
    cues: list[str] = []
    for block in blocks[1:]:
        if _is_control(block[0]):
            continue
        cues.append(_cue_text(block, _VTT_TIMING))
    return _merge(cues)


def _srt_text(text: str) -> str:
    blocks = _blocks(_prepare(text))
    if not blocks:
        raise SchemaError("invalid_transcript")
    return _merge([_cue_text(block, _SRT_TIMING) for block in blocks])
