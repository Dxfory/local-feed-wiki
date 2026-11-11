"""Video metadata stays text: five comments, nullable counts, no frames."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import get_type_hints

import pytest

from second_brain.errors import FramesNotAllowed, SchemaError
from second_brain.models import (
    PROMPT_CHANNEL_CHARS,
    PROMPT_COMMENT_CHARS,
    PROMPT_TITLE_CHARS,
    VideoMeta,
)
from second_brain.parse.video import parse_video

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "video"
SENTINEL = "zz-do-not-echo-7f3a"
MEDIA_FIELDS = (
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
)
PATH_TITLES = (
    "clip.mp4",
    "CLIP.JPG",
    "shot.png",
    "a.webp",
    ".mp4",
    "./shot.jpg",
    "../shot.png",
    "/tmp/a.mp4",
    "/tmp/my clip.webp",
    "~/a.jpg",
    r"C:\videos\a.mp4",
    r"\\server\share\a.mp4",
    "C:/videos/a.png",
    "C:relative.mp4",
    "file:///tmp/a.mp4",
    "file://localhost/tmp/a.jpg",
    "file:/tmp/a.webp",
    "file:tests/fixtures/video/a.png",
    "  clip.mp4  ",
    '"/tmp/a.mp4"',
    "'./a.png'",
    "`clip.webp`",
    "clip.mp4\n",
)
TEXT_TITLES = (
    "Watch the .mp4 later",
    "notes about clip.mp4 today",
    "my clip.mp4",
    "https://example.com/a.mp4",
    "HTTPS://example.com/a.JPG",
    "http://example.com/a.png",
    "//cdn.example/a.webp",
    "clip.mp4.bak",
    "clip.mp4.",
    '"Watch the .mp4"',
    "file:notes.txt",
)


def load_fixture(name: str) -> dict[str, object]:
    loaded = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(name)
    return loaded


def video(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "合成视频",
        "channel": "合成频道",
        "comments": [],
        "frame_paths": [],
    }
    payload.update(overrides)
    return payload


def assert_safe(exc: BaseException) -> None:
    text = f"{exc} {exc!r}"
    assert SENTINEL not in text
    assert "DO_NOT_READ" not in text
    assert "424242" not in text
    assert "://" not in text
    assert "frame.jpg" not in text


def test_keeps_five_comments() -> None:
    signature = inspect.signature(parse_video)
    assert list(signature.parameters) == ["payload"]
    assert signature.parameters["payload"].default is inspect.Parameter.empty
    assert signature.parameters["payload"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    hints = get_type_hints(parse_video)
    assert hints["return"] is VideoMeta
    assert hints["payload"] == Mapping[str, object]

    raw = load_fixture("keeps_five.json")
    original_comments = list(raw["comments"])  # type: ignore[arg-type]
    meta = parse_video(raw)
    again = parse_video(payload=MappingProxyType(dict(raw)))
    assert isinstance(meta, VideoMeta)
    assert not isinstance(meta, tuple)
    assert meta == again
    assert meta.title == "合成视频"
    assert meta.channel == "合成频道"
    assert meta.like_count == 10
    assert meta.comment_count == 4
    assert meta.view_count == 100
    assert meta.comments == ("第一条", "第二条", "第三条", "第四条", "第五条")
    assert "第六条" not in meta.comments
    assert meta.transcript_status == "not_applicable"
    assert raw["comments"] == original_comments

    missing = load_fixture("missing_counts.json")
    missed = parse_video(missing)
    assert missed.like_count is None
    assert missed.comment_count is None
    assert missed.view_count is None
    assert missed.comment_count != 2
    assert missed.comments == ("只有一条", "第二条")
    assert missed.transcript_status == "not_applicable"

    explicit_null = video(like_count=None, comment_count=None, view_count=None, comments=["甲"])
    nulled = parse_video(explicit_null)
    assert (nulled.like_count, nulled.comment_count, nulled.view_count) == (None, None, None)
    assert nulled.comments == ("甲",)

    zeros = parse_video(video(like_count=0, comment_count=0, view_count=0, comments=["仍在"]))
    assert (zeros.like_count, zeros.comment_count, zeros.view_count) == (0, 0, 0)
    assert zeros.comments == ("仍在",)

    skipped = ["", "  ", "一", "二", "三", "四", "五", "六"]
    kept = parse_video(video(comments=list(skipped)))
    assert kept.comments == ("一", "二", "三", "四", "五")
    assert skipped == ["", "  ", "一", "二", "三", "四", "五", "六"]
    spaced = parse_video(video(comments=["  两边  "]))
    assert spaced.comments == ("  两边  ",)
    duplicates = parse_video(video(comments=["a", "a", "b", "c", "d", "e"]))
    assert duplicates.comments == ("a", "a", "b", "c", "d")

    long_comment = "评" * (PROMPT_COMMENT_CHARS + 1)
    long_title = "题" * (PROMPT_TITLE_CHARS + 1)
    long_channel = "频" * (PROMPT_CHANNEL_CHARS + 1)
    lengthy = parse_video(
        video(title=long_title, channel=long_channel, comments=[long_comment, "尾"])
    )
    assert lengthy.title == long_title
    assert lengthy.channel == long_channel
    assert lengthy.comments == (long_comment, "尾")
    assert len(lengthy.comments[0]) == PROMPT_COMMENT_CHARS + 1

    folded = parse_video(video(title="两行\r\n标题"))
    assert folded.title == "两行 标题"
    channel = parse_video(video(channel="频道\n名"))
    assert channel.channel == "频道\n名"

    omitted = video(comments=["留下"])
    del omitted["frame_paths"]
    assert parse_video(omitted).comments == ("留下",)
    assert parse_video(video(frame_paths=None, comments=())).comments == ()
    assert parse_video(video(frame_paths=(), comments=None)).comments == ()


def _arm(monkeypatch: pytest.MonkeyPatch, bait: Path) -> list[str]:
    opened: list[str] = []
    banned = os.path.normcase(str(bait))
    real_open = open
    real_os_open = os.open

    def matches(file: object) -> bool:
        if isinstance(file, int):
            return False
        try:
            text = os.fspath(file)  # type: ignore[arg-type]
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        if text == "":
            return False
        absolute = text if os.path.isabs(text) else os.path.join(os.getcwd(), text)
        return os.path.normcase(os.path.abspath(absolute)) == banned

    def guard_open(file, *args, **kwargs):
        if matches(file):
            opened.append(os.fspath(file))
            raise AssertionError("opened media")
        return real_open(file, *args, **kwargs)

    def guard_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if matches(path):
            opened.append(os.fspath(path))
            raise AssertionError("opened media")
        if dir_fd is None:
            return real_os_open(path, flags, mode)
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("builtins.open", guard_open)
    monkeypatch.setattr(os, "open", guard_os_open)
    return opened


def test_rejects_frames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = sorted(FIXTURES.iterdir())
    assert files
    secret_marks = ("api_key", "password", "bearer ", "secret_token")
    for path in files:
        assert path.suffix == ".json"
        assert path.name.lower().endswith((".mp4", ".jpg", ".png", ".webp")) is False
        text = path.read_text(encoding="utf-8").lower()
        for mark in secret_marks:
            assert mark not in text

    calls: list[object] = []

    def fail_process(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess")

    monkeypatch.setattr(subprocess, "run", fail_process)
    monkeypatch.setattr(subprocess, "Popen", fail_process)

    bait = tmp_path / "frame.jpg"
    bait.write_bytes(b"DO_NOT_READ")
    before = (
        bait.stat().st_size,
        bait.stat().st_mtime_ns,
        tuple(sorted(path.name for path in tmp_path.iterdir())),
    )
    monkeypatch.chdir(tmp_path)
    opened = _arm(monkeypatch, bait)

    framed = load_fixture("frames.json")
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(framed)
    assert caught.value.code == "frames_not_allowed"
    assert caught.value.args == ("frames_not_allowed",)
    assert str(caught.value) == "frames_not_allowed"
    assert_safe(caught.value)

    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(frame_paths=["frame.jpg"], like_count=-1))
    assert_safe(caught.value)
    smuggled = video(frame_paths=[f"{SENTINEL}.jpg"])
    smuggled["starRating"] = 424242
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(smuggled)
    assert_safe(caught.value)

    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(title="frame.jpg"))
    assert_safe(caught.value)
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(title=str(bait)))
    assert_safe(caught.value)
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(comments=["ok", "frame.jpg"]))
    assert_safe(caught.value)
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(comments=["a", "b", "c", "d", "e", "shot.png"]))
    assert_safe(caught.value)
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(channel="clip.mp4"))
    assert_safe(caught.value)
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(video(frame_paths="frame.jpg"))
    assert_safe(caught.value)

    for field in MEDIA_FIELDS:
        payload = video()
        payload[field] = f"https://cdn.example/{SENTINEL}.jpg"
        with pytest.raises(FramesNotAllowed) as caught:
            parse_video(payload)
        assert caught.value.code == "frames_not_allowed"
        assert_safe(caught.value)
    upper = video()
    upper["Thumbnail"] = f"/tmp/{SENTINEL}.png"
    with pytest.raises(FramesNotAllowed) as caught:
        parse_video(upper)
    assert_safe(caught.value)

    for title in PATH_TITLES:
        with pytest.raises(FramesNotAllowed) as caught:
            parse_video(video(title=title))
        assert caught.value.code == "frames_not_allowed"
        assert_safe(caught.value)

    for title in TEXT_TITLES:
        meta = parse_video(video(title=title, comments=["普通句子提到 .mp4"]))
        assert meta.title == title
        assert meta.comments == ("普通句子提到 .mp4",)
        assert "DO_NOT_READ" not in meta.title

    mentioned = load_fixture("mentions_extension.json")
    sentence = f"目录里有 {bait} 这个 .mp4 名字，句子本身不是路径。"
    mentioned["comments"] = [sentence, "my clip.mp4"]
    spoken = parse_video(mentioned)
    assert spoken.title == "普通句子里提到 .mp4 但不是文件"
    assert spoken.comments[0] == sentence
    assert spoken.comments[1] == "my clip.mp4"
    assert spoken.like_count is None
    assert "DO_NOT_READ" not in "".join(spoken.comments)
    for empty in (None, "", []):
        payload = video()
        payload["thumbnail"] = empty
        with pytest.raises(FramesNotAllowed) as caught:
            parse_video(payload)
        assert_safe(caught.value)

    assert opened == []
    assert calls == []
    after = (
        bait.stat().st_size,
        bait.stat().st_mtime_ns,
        tuple(sorted(path.name for path in tmp_path.iterdir())),
    )
    assert after == before


def test_invalid_metadata_types() -> None:
    derived = parse_video(video(comments=["甲", "乙", "丙"]))
    assert derived.like_count is None
    assert derived.comment_count is None
    assert derived.view_count is None
    assert derived.comments == ("甲", "乙", "丙")
    assert derived.comment_count != 3

    rating = load_fixture("star_rating.json")
    with pytest.raises(SchemaError) as caught:
        parse_video(rating)
    assert caught.value.code == "invalid_field"
    assert str(caught.value) == "invalid_field"
    assert_safe(caught.value)

    for name, bad in (
        ("likeCount", 10),
        ("views", 13931),
        ("guid", "g1"),
        ("url", "https://example.com/watch"),
        ("published_at", "2030-01-01T00:00:00.000000Z"),
        ("description", "正文"),
        ("transcript_status", "ready"),
        ("starRating", 5),
    ):
        payload = video(comments=["甲"])
        payload[name] = bad
        with pytest.raises(SchemaError) as caught:
            parse_video(payload)
        assert caught.value.code == "invalid_field"
        assert str(caught.value) == "invalid_field"
        assert "example.com" not in str(caught.value)

    for name in ("like_count", "comment_count", "view_count"):
        for bad in (-1, True, False, 1.5, "10", float("nan")):
            payload = video(comments=["甲"])
            payload[name] = bad
            with pytest.raises(SchemaError) as caught:
                parse_video(payload)
            assert caught.value.code == "invalid_count"
            assert str(caught.value) == "invalid_count"
            assert caught.value.args == ("invalid_count",)

    comment_cases = (
        ["ok", 1],
        ["ok", True],
        ["ok", None],
        ["ok", {"text": "derived"}],
        ["ok", b"hi"],
        "not-a-list",
        {"text": "derived"},
        ["a", "b", "c", "d", "e", {"text": "sixth"}],
        ["甲\x00"],
    )
    for comments in comment_cases:
        with pytest.raises(SchemaError) as caught:
            parse_video(video(comments=comments))
        assert caught.value.code == "invalid_comments"

    def comments_of():
        # The yield marks a generator. Iteration would raise before any comment is read.
        raise AssertionError("consumed")
        yield "甲"

    with pytest.raises(SchemaError) as caught:
        parse_video(video(comments=comments_of()))
    assert caught.value.code == "invalid_comments"

    for bad_title in (None, 1, True, b"title", "a\x00b"):
        with pytest.raises(SchemaError) as caught:
            parse_video(video(title=bad_title))
        assert caught.value.code == "invalid_title"
    with pytest.raises(SchemaError) as caught:
        parse_video({"channel": "合成频道"})
    assert caught.value.code == "invalid_title"

    for bad_channel in (None, 1, b"channel", "频\x00道"):
        with pytest.raises(SchemaError) as caught:
            parse_video(video(channel=bad_channel))
        assert caught.value.code == "invalid_text"
    with pytest.raises(SchemaError) as caught:
        parse_video({"title": "合成视频"})
    assert caught.value.code == "invalid_text"

    for bad_frames in (1, b"frame.jpg", "", {"path": "a.jpg"}):
        with pytest.raises(SchemaError) as caught:
            parse_video(video(frame_paths=bad_frames))
        assert caught.value.code == "invalid_frame_paths"

    for bad_payload in (None, [], "video", 3, b"{}"):
        with pytest.raises(SchemaError) as caught:
            parse_video(bad_payload)  # type: ignore[arg-type]
        assert caught.value.code == "invalid_payload"

    broken = video()
    broken["\x00"] = "x"
    with pytest.raises(SchemaError) as caught:
        parse_video(broken)
    assert caught.value.code == "invalid_field"
