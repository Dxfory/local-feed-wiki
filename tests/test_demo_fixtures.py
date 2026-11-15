"""English offline demo, minority comparison, and public starter feeds.

These tests stay on disk. They do not call a feed host, a browser, or ffmpeg.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from second_brain.config import load_config
from second_brain.models import url_has_secret_query

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "demo"
FEEDS = REPO / "examples" / "feeds"
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_CJK = re.compile(
    r"[\u1100-\u11ff\u2e80-\u2eff\u2f00-\u2fdf\u3000-\u303f\u3040-\u30ff"
    r"\u3130-\u318f\u31c0-\u31ef\u3200-\u32ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)
_CREDENTIAL_QUERY = frozenset(
    {
        "token",
        "access_token",
        "api_key",
        "apikey",
        "key",
        "password",
        "signature",
        "sig",
        "secret",
        "auth",
        "credential",
        "credentials",
    }
)
_FEED_NAMES = (
    "ai-research.yaml",
    "finance-podcasts.yaml",
    "indie-builders.yaml",
)
_RUNTIME = """\
offline: false
live_authorized: false
billing_path: test
billing_path_verified: false
tool_policy_verified: false
review_llm: false
max_classify_batches: 1
max_review_batches: 1
max_transcript_fetches: 0
"""
_ALIASES = "topic_aliases: {}\nclaim_aliases: {}\n"


def _split_heading(text: str, headings: tuple[str, ...], end: str) -> str:
    for heading in headings:
        if heading in text:
            return text.split(heading, 1)[1].split(end, 1)[0]
    raise AssertionError(headings)


def _has_label(text: str, labels: tuple[str, ...]) -> None:
    assert any(label in text for label in labels), labels


def _fields(stdout: str) -> dict[str, str]:
    line = stdout.strip("\n")
    assert "\n" not in line and line != ""
    found: dict[str, str] = {}
    for part in line.split(" "):
        key, value = part.split("=", 1)
        found[key] = value
    return found


def _stage_demo(root: Path) -> None:
    fake_src = REPO / "tests" / "fakes" / "grok"
    fake_dst = root / "tests" / "fakes" / "grok"
    fake_dst.parent.mkdir(parents=True)
    os.link(fake_src, fake_dst)
    if fake_dst.is_symlink() or not os.path.samefile(fake_src, fake_dst):
        raise AssertionError("demo root must use the same offline binary file")
    shutil.copytree(DEMO, root / "examples" / "demo")
    shutil.copytree(REPO / "tests" / "fixtures" / "demo", root / "tests" / "fixtures" / "demo")


def test_demo_run_publishes_minority_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dropped claim or a one-sided fixture leaves the minority section empty."""

    import second_brain.cli as cli

    _stage_demo(tmp_path)
    monkeypatch.setenv("SECOND_BRAIN_GROK", "tests/fakes/grok")
    monkeypatch.delenv("SECOND_BRAIN_GROK_SCENARIO", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GROK_ARGV_LOG", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_ROOT", raising=False)
    code = cli.main(
        [
            "run",
            "--offline",
            "--config-dir",
            "examples/demo",
            "--now",
            "2030-01-15T00:00:00Z",
            "--db",
            "data/second_brain.sqlite",
            "--root",
            os.fspath(tmp_path),
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0, (code, out, err)
    fields = _fields(out)
    assert fields["status"] == "success"
    assert fields["classified"] == "10"
    page = (tmp_path / "wiki" / fields["html"]).read_text(encoding="utf-8")
    assert "2030-01-15T00:00:00.000000Z" in page
    assert "East dock says the noon gate stays open" in page
    assert "Ledger says the noon gate stays shut" in page
    assert "https://example.com/north-ledger/noon-gate-shut" in page
    minority = _split_heading(
        page,
        ("<h2>非共识</h2>\n", "<h2>Minority view</h2>\n"),
        "<h2>",
    )
    assert "<p>无</p>" not in minority
    assert "<p>None</p>" not in minority
    assert "<ul>" in minority
    _has_label(minority, ("少数候选", "Minority candidate"))
    assert "the noon gate stays open" in minority
    opportunities = list((tmp_path / "wiki").glob("snapshots/*/opportunities/*.md"))
    assert len(opportunities) == 1
    comparison = opportunities[0].read_text(encoding="utf-8")
    _has_label(
        comparison,
        (
            "来源票数：少数 1，多数 3，来源 4，条目 5",
            "Votes: minority 1, majority 3, sources 4, items 5",
        ),
    )
    _has_label(
        comparison,
        ("少数句：The noon gate stays shut.", "Minority sentence: The noon gate stays shut."),
    )
    _has_label(comparison, ("## 少数来源", "## Minority sources"))
    _has_label(comparison, ("## 多数来源", "## Majority sources"))
    assert "north-ledger" in comparison
    assert "harbor-watch" in comparison
    assert "field-notes" in comparison
    assert "quay-gazette" in comparison


def _http_urls(text: str) -> list[str]:
    return [match.rstrip(".,") for match in _URL.findall(text)]


def test_demo_links_stay_on_example_com() -> None:
    """An http link, or any host other than example.com, fails this check."""

    assert DEMO.is_dir()
    texts = [path.read_text(encoding="utf-8") for path in sorted(DEMO.rglob("*")) if path.is_file()]
    combined = "\n".join(texts)
    for match in re.findall(
        r"file:tests/fixtures/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.[A-Za-z0-9]+",
        combined,
    ):
        relative = match.removeprefix("file:tests/fixtures/")
        texts.append((REPO / "tests" / "fixtures" / relative).read_text(encoding="utf-8"))
    urls = [url for text in texts for url in _http_urls(text)]
    assert urls
    for url in urls:
        assert url.startswith("https://example.com/"), url
        assert urlsplit(url).hostname == "example.com"


def test_feed_packs_are_public_https(tmp_path: Path) -> None:
    """A non-https URL or a credential-like query key is rejected."""

    names = sorted(path.name for path in FEEDS.glob("*.yaml"))
    assert names == sorted(_FEED_NAMES)
    for name in _FEED_NAMES:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "sources.yaml").write_text((FEEDS / name).read_text(encoding="utf-8"), encoding="utf-8")
        (directory / "preferences.md").write_text("Starter pack.\n", encoding="utf-8")
        (directory / "topic_aliases.yaml").write_text(_ALIASES, encoding="utf-8")
        (directory / "runtime.yaml").write_text(_RUNTIME, encoding="utf-8")
        loaded = load_config(directory, offline=False)
        assert loaded.sources
        for source in loaded.sources:
            assert source.url.startswith("https://"), source.url
            parts = urlsplit(source.url)
            assert parts.scheme == "https"
            assert parts.username is None and parts.password is None
            assert parts.hostname
            assert not url_has_secret_query(source.url)
            for key, _value in parse_qsl(parts.query, keep_blank_values=True):
                assert key.casefold() not in _CREDENTIAL_QUERY


def test_demo_text_has_no_cjk() -> None:
    """Han, kana, or hangul characters in the English demo fail this check."""

    assert DEMO.is_dir()
    offenders: list[str] = []
    for path in sorted(DEMO.rglob("*")):
        if not path.is_file():
            continue
        if _CJK.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
