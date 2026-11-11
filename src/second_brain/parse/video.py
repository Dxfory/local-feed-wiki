"""Parse one local video payload into VideoMeta.

The input is a closed set of metadata fields. Counts stay missing when they
are missing, comments are audience text, and frame or media paths are refused
without being opened. Prompt truncation stays in prompt_fields.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from second_brain.errors import FramesNotAllowed, SchemaError
from second_brain.models import PROMPT_COMMENT_MAX, VideoMeta, single_line

_FIELDS = frozenset(
    {
        "title",
        "channel",
        "like_count",
        "comment_count",
        "view_count",
        "comments",
        "frame_paths",
    }
)
_MEDIA_FIELDS = frozenset(
    {
        "thumbnail",
        "thumbnail_url",
        "thumbnails",
        "media:content",
        "media:thumbnail",
        "media_content",
        "media_thumbnail",
        "frame",
        "frames",
        "frame_path",
        "video_path",
        "video_file",
        "audio_path",
        "audio_file",
        "image_path",
        "image_file",
        "media_path",
        "media_file",
        "local_path",
        "poster",
        "cover",
        "enclosure",
    }
)
_MEDIA_SUFFIXES = (".mp4", ".jpg", ".png", ".webp")
_BREAKS = ("\n", "\r", "\u2028", "\u2029")
_QUOTES = "\"'`"


def parse_video(payload: Mapping[str, object]) -> VideoMeta:
    """Return one VideoMeta. Never open a path, download media, or spawn a process."""
    data = _mapping(payload)
    # Media refusal wins over an unknown key, so a spare field cannot hide a path.
    _reject_media_fields(data)
    _reject_frame_paths(data.get("frame_paths", ()))
    _reject_embedded_paths(data)
    _reject_unknown(data)
    return VideoMeta(
        title=_title(data),
        channel=_channel(data),
        like_count=_count(data, "like_count"),
        comment_count=_count(data, "comment_count"),
        view_count=_count(data, "view_count"),
        comments=_comments(data.get("comments", ())),
    )


def _mapping(payload: object) -> Mapping[str, object]:
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Mapping):
        raise SchemaError("invalid_payload")
    return payload


def _reject_media_fields(payload: Mapping[str, object]) -> None:
    for key in payload:
        if isinstance(key, str) and key.casefold() in _MEDIA_FIELDS:
            raise FramesNotAllowed()


def _reject_frame_paths(value: object) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip() == "":
            raise SchemaError("invalid_frame_paths")
        raise FramesNotAllowed()
    if not isinstance(value, (list, tuple)):
        raise SchemaError("invalid_frame_paths")
    if len(value) > 0:
        raise FramesNotAllowed()


def _reject_unknown(payload: Mapping[str, object]) -> None:
    for key in payload:
        if not isinstance(key, str) or "\x00" in key or key not in _FIELDS:
            raise SchemaError("invalid_field")


def _reject_embedded_paths(payload: Mapping[str, object]) -> None:
    title = payload.get("title")
    if isinstance(title, str) and "\x00" not in title:
        if _is_local_media_path(title) or _is_local_media_path(single_line(title)):
            raise FramesNotAllowed()
    channel = payload.get("channel")
    if isinstance(channel, str) and _is_local_media_path(channel):
        raise FramesNotAllowed()
    comments = payload.get("comments", ())
    if isinstance(comments, (str, bytes)) or not isinstance(comments, Sequence):
        return
    for item in comments:
        if isinstance(item, str) and item.strip() != "" and _is_local_media_path(item):
            raise FramesNotAllowed()


def _title(payload: Mapping[str, object]) -> str:
    if "title" not in payload:
        raise SchemaError("invalid_title")
    value = payload["title"]
    if not isinstance(value, str) or "\x00" in value:
        raise SchemaError("invalid_title")
    return single_line(value)


def _channel(payload: Mapping[str, object]) -> str:
    if "channel" not in payload:
        raise SchemaError("invalid_text")
    value = payload["channel"]
    if not isinstance(value, str) or "\x00" in value:
        raise SchemaError("invalid_text")
    return value


def _count(payload: Mapping[str, object], name: str) -> int | None:
    if name not in payload:
        return None
    value = payload[name]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaError("invalid_count")
    return value


def _comments(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SchemaError("invalid_comments")
    kept: list[str] = []
    for item in value:
        if not isinstance(item, str) or "\x00" in item:
            raise SchemaError("invalid_comments")
        if item.strip() == "":
            continue
        if _is_local_media_path(item):
            raise FramesNotAllowed()
        if len(kept) < PROMPT_COMMENT_MAX:
            kept.append(item)
    return tuple(kept)


def _unwrap_once(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:
        return text[1:-1].strip()
    return text


def _is_local_media_path(value: str) -> bool:
    """True when the whole string is a local path ending in a frame suffix.

    A sentence that only mentions the suffix is not a path. http(s) and
    protocol-relative URLs are not local paths and are not fetched.
    """
    text = _unwrap_once(value.strip())
    if text == "" or any(mark in text for mark in _BREAKS):
        return False
    lowered = text.casefold()
    if not lowered.endswith(_MEDIA_SUFFIXES):
        return False
    # Leading // is a protocol-relative URL. Backslash UNC paths are still local.
    if lowered.startswith(("http://", "https://", "//")):
        return False
    if lowered.startswith("file:"):
        return True
    if "/" in text or "\\" in text or text.startswith("~"):
        return True
    if len(text) >= 2 and text[0].isascii() and text[0].isalpha() and text[1] == ":":
        return True
    return not any(char.isspace() for char in text)
