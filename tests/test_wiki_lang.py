"""English and Chinese labels for one offline demo run.

``--lang zh`` must match the pages captured before label selection existed.
``--lang en`` is the default. Fixture titles, reasons, topics, and short
summaries may still contain Han characters; labels may not.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

import second_brain.cli as cli
import second_brain.config as config

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "fixtures" / "lang_zh"
RUN_ID = "0123456789abcdef0123456789abcdef"
DEMO = [
    "run",
    "--offline",
    "--config-dir",
    "examples/offline",
    "--now",
    "2030-01-15T00:00:00Z",
    "--db",
    "data/second_brain.sqlite",
]
# Sentences the offline stand-in writes into topics, reasons, and summaries.
_STAND_IN = (
    "离线合成结果只复述所列材料。",
    "所选材料未提供可确认的分歧。",
    "观众文字仅作离线占位。",
    "离线合成结果",
    "离线",
)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_SKIP = {"log.md"}


def _release_cwd(path: Path) -> None:
    try:
        cwd = Path.cwd().resolve()
        cwd.relative_to(path.resolve())
    except (OSError, ValueError):
        return
    os.chdir(REPO)


@pytest.fixture
def demo_root() -> Path:
    parent = REPO / ".pytest-samefs"
    parent.mkdir(mode=0o700, exist_ok=True)
    fake = REPO / "tests" / "fakes" / "grok"
    try:
        with tempfile.TemporaryDirectory(prefix="lang-", dir=parent) as raw:
            root = Path(raw)
            if root.stat().st_dev != fake.stat().st_dev:
                raise AssertionError("project root is not on the fake's filesystem")
            for name in ("rss", "podcast", "youtube"):
                shutil.copytree(REPO / "tests" / "fixtures" / name, root / "tests" / "fixtures" / name)
            shutil.copytree(REPO / "examples" / "offline", root / "examples" / "offline")
            destination = root / "tests" / "fakes" / "grok"
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(fake, destination)
            try:
                yield root
            finally:
                _release_cwd(root)
    finally:
        try:
            os.chdir(REPO)
        except OSError:
            pass
        try:
            parent.rmdir()
        except OSError:
            pass


def _pages(wiki: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(wiki.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(wiki).as_posix()
        if relative in _SKIP or relative.startswith(".staging/"):
            continue
        found[relative] = path.read_bytes()
    return found


def _golden() -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(GOLDEN.rglob("*")):
        if path.is_file():
            found[path.relative_to(GOLDEN).as_posix()] = path.read_bytes()
    return found


def _content_strings() -> tuple[str, ...]:
    found: set[str] = set(_STAND_IN)
    feed = (REPO / "tests" / "fixtures" / "rss" / "feed.xml").read_text(encoding="utf-8")
    found.update(re.findall(r"<title>([^<]*)</title>", feed))
    episodes = json.loads(
        (REPO / "tests" / "fixtures" / "podcast" / "episodes.json").read_text(encoding="utf-8")
    )
    for item in episodes["items"]:
        title = item.get("title")
        transcript = item.get("transcript")
        if isinstance(title, str):
            found.add(title)
        if isinstance(transcript, str):
            found.add(transcript)
    channel = (REPO / "tests" / "fixtures" / "youtube" / "feed.xml").read_text(encoding="utf-8")
    found.update(re.findall(r"<title>([^<]*)</title>", channel))
    return tuple(sorted((item for item in found if item), key=len, reverse=True))


def _strip_content(text: str) -> str:
    for item in _content_strings():
        text = text.replace(item, "")
    return text


def _run(
    root: Path,
    extra: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str, Path]:
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.delenv("SECOND_BRAIN_ROOT", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GROK_SCENARIO", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_GROK", "tests/fakes/grok")
    monkeypatch.setattr(cli, "_run_id", lambda: RUN_ID)
    code = cli.main([*DEMO, "--root", str(root), *extra])
    out, err = capsys.readouterr()
    return code, out, err, root / "wiki"


def test_lang_zh_matches_chinese_golden(
    demo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err, wiki = _run(demo_root, ["--lang", "zh"], monkeypatch, capsys)
    assert code == 0, (code, out, err)
    assert _pages(wiki) == _golden()


def test_lang_en_labels_leave_only_fixture_cjk(
    demo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err, wiki = _run(demo_root, ["--lang", "en"], monkeypatch, capsys)
    assert code == 0, (code, out, err)
    pages = _pages(wiki)
    html = pages[f"digest/{RUN_ID}.html"].decode("utf-8")
    index = pages["index.md"].decode("utf-8")
    assert '<html lang="en">' in html
    for label in ("Must read", "Recommended", "Skip", "Minority view", "Waiting on transcript"):
        assert label in html
    assert _CJK.search(_strip_content(html)) is None
    assert _CJK.search(_strip_content(index)) is None
    for relative, payload in pages.items():
        if relative.endswith(".html") or relative.endswith(".md"):
            assert _CJK.search(_strip_content(payload.decode("utf-8"))) is None, relative


def test_default_lang_matches_en(
    demo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err, wiki = _run(demo_root, [], monkeypatch, capsys)
    assert code == 0, (code, out, err)
    default_pages = _pages(wiki)
    fresh = Path(tempfile.mkdtemp(prefix="lang-en-", dir=demo_root.parent))
    try:
        for name in ("rss", "podcast", "youtube"):
            shutil.copytree(REPO / "tests" / "fixtures" / name, fresh / "tests" / "fixtures" / name)
        shutil.copytree(REPO / "examples" / "offline", fresh / "examples" / "offline")
        destination = fresh / "tests" / "fakes" / "grok"
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(REPO / "tests" / "fakes" / "grok", destination)
        again, again_out, again_err, again_wiki = _run(fresh, ["--lang", "en"], monkeypatch, capsys)
        assert again == 0, (again, again_out, again_err)
        assert _pages(again_wiki) == default_pages
    finally:
        shutil.rmtree(fresh)


def test_lang_xx_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "--lang", "xx"])
    out, err = capsys.readouterr()
    assert code == 1
    assert out == ""
    assert err.strip() == "invalid_lang"
