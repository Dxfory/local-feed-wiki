"""Which environment variable selects the completion program.

Breaks if the primary variable is ignored, if two different values are
allowed to proceed, if a blank primary hides the alias, or if offline mode
accepts any file other than the checked-in fake.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
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
_BLOG = """\
sources:
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客
    backfill_days: 14
"""
_ALIASES = "topic_aliases: {}\nclaim_aliases: {}\n"
_PREFERENCES = "合成偏好\n"
_OTHER = "/opt/second-brain-tests/completion"


def _runtime(**overrides: str) -> str:
    values = {
        "offline": "true",
        "live_authorized": "false",
        "billing_path": "test",
        "billing_path_verified": "false",
        "tool_policy_verified": "false",
        "review_llm": "true",
        "max_classify_batches": "3",
        "max_review_batches": "1",
        "max_transcript_fetches": "20",
    }
    values.update(overrides)
    order = (
        "offline",
        "live_authorized",
        "billing_path",
        "billing_path_verified",
        "tool_policy_verified",
        "review_llm",
        "max_classify_batches",
        "max_review_batches",
        "max_transcript_fetches",
    )
    return "".join(f"{key}: {values[key]}\n" for key in order)


def _use_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    import second_brain.config as config

    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    monkeypatch.delenv("SECOND_BRAIN_ROOT", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GROK_SCENARIO", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GROK_ARGV_LOG", raising=False)


def _write_config(root: Path, runtime: str | None = None) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "sources.yaml").write_text(_BLOG, encoding="utf-8")
    (config_dir / "topic_aliases.yaml").write_text(_ALIASES, encoding="utf-8")
    (config_dir / "runtime.yaml").write_text(runtime or _runtime(), encoding="utf-8")
    (config_dir / "preferences.md").write_text(_PREFERENCES, encoding="utf-8")


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    import second_brain.cli as cli

    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _release_cwd(path: Path) -> None:
    try:
        cwd = Path.cwd().resolve()
        cwd.relative_to(path.resolve())
    except (OSError, ValueError):
        return
    os.chdir(REPO)


@contextmanager
def _samefs_root() -> Iterator[Path]:
    parent = REPO / ".pytest-samefs"
    parent.mkdir(mode=0o700, exist_ok=True)
    fake = REPO / "tests" / "fakes" / "grok"
    with tempfile.TemporaryDirectory(prefix="llm-cmd-", dir=parent) as raw:
        path = Path(raw)
        if path.stat().st_dev != fake.stat().st_dev:
            raise AssertionError("project root is not on the fake's filesystem")
        try:
            yield path
        finally:
            _release_cwd(path)


def _prepare_demo(root: Path) -> None:
    for name in ("rss", "podcast", "youtube"):
        shutil.copytree(REPO / "tests" / "fixtures" / name, root / "tests" / "fixtures" / name)
    destination = root / "tests" / "fakes" / "grok"
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(REPO / "tests" / "fakes" / "grok", destination)
    shutil.copytree(REPO / "examples" / "offline", root / "examples" / "offline")


def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = {"n": 0}

    def uuid4() -> object:
        counter["n"] += 1
        value = f"equiv{counter['n']:04d}"

        class _Id:
            hex = value

        return _Id()

    monkeypatch.setattr(uuid, "uuid4", uuid4)


def _set_command(
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary: str | None,
    alias: str | None,
) -> None:
    if primary is None:
        monkeypatch.delenv("SECOND_BRAIN_LLM_CMD", raising=False)
    else:
        monkeypatch.setenv("SECOND_BRAIN_LLM_CMD", primary)
    if alias is None:
        monkeypatch.delenv("SECOND_BRAIN_GROK", raising=False)
    else:
        monkeypatch.setenv("SECOND_BRAIN_GROK", alias)


def _demo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    *,
    primary: str | None,
    alias: str | None,
) -> tuple[int, str, str, dict[str, bytes]]:
    _use_root(monkeypatch, root)
    _prepare_demo(root)
    _set_command(monkeypatch, primary=primary, alias=alias)
    _fixed_ids(monkeypatch)
    code, out, err = _invoke(DEMO, capsys)
    files: dict[str, bytes] = {}
    wiki = root / "wiki"
    if wiki.is_dir():
        for path in sorted(wiki.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return code, out, err, files


def _offline_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    primary: str | None,
    alias: str | None,
) -> tuple[int, str, str]:
    _use_root(monkeypatch, tmp_path)
    _write_config(tmp_path)
    _set_command(monkeypatch, primary=primary, alias=alias)
    return _invoke(
        ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/check.sqlite"],
        capsys,
    )


def test_primary_offline_demo_matches_alias(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _samefs_root() as alias_root, _samefs_root() as primary_root:
        alias_code, alias_out, alias_err, alias_files = _demo(
            monkeypatch,
            capsys,
            alias_root,
            primary=None,
            alias="tests/fakes/grok",
        )
        primary_code, primary_out, primary_err, primary_files = _demo(
            monkeypatch,
            capsys,
            primary_root,
            primary="tests/fakes/grok",
            alias=None,
        )
    assert alias_code == 0, (alias_code, alias_out, alias_err)
    assert primary_code == 0, (primary_code, primary_out, primary_err)
    assert "status=success" in alias_out
    assert alias_out.split("classified=", 1)[1].split(" ", 1)[0] != "0"
    assert (primary_code, primary_out, primary_err, primary_files) == (
        alias_code,
        alias_out,
        alias_err,
        alias_files,
    )


def test_conflicting_command_paths_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _offline_reject(
        tmp_path,
        monkeypatch,
        capsys,
        primary="tests/fakes/grok",
        alias=_OTHER,
    )
    assert (code, out, err.strip()) == (1, "", "llm_cmd_conflict")
    assert _OTHER not in out + err
    assert not (tmp_path / "data").exists()


def test_identical_values_reach_the_offline_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _offline_reject(
        tmp_path,
        monkeypatch,
        capsys,
        primary="tests/fakes/grok",
        alias="tests/fakes/grok",
    )
    assert (code, out, err.strip()) == (1, "", "offline_grok_rejected")
    assert not (tmp_path / "data" / "check.sqlite").exists()


def test_blank_primary_falls_back_to_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _offline_reject(
        tmp_path,
        monkeypatch,
        capsys,
        primary="",
        alias=_OTHER,
    )
    assert (code, out, err.strip()) == (1, "", "offline_grok_rejected")


def test_offline_primary_must_be_the_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _offline_reject(
        tmp_path,
        monkeypatch,
        capsys,
        primary=_OTHER,
        alias=None,
    )
    assert (code, out, err.strip()) == (1, "", "offline_grok_rejected")
    assert _OTHER not in out + err
    assert not (tmp_path / "data" / "check.sqlite").exists()


def test_offline_primary_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = tmp_path / "tests" / "fakes" / "alias"
    link.parent.mkdir(parents=True)
    link.symlink_to(REPO / "tests" / "fakes" / "grok")
    code, out, err = _offline_reject(
        tmp_path,
        monkeypatch,
        capsys,
        primary="tests/fakes/alias",
        alias=None,
    )
    assert (code, out, err.strip()) == (1, "", "offline_grok_rejected")


def test_live_gate_is_reported_before_command_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_root(monkeypatch, tmp_path)
    _write_config(tmp_path)
    _set_command(monkeypatch, primary="tests/fakes/grok", alias=_OTHER)
    code, out, err = _invoke(["run", "--live", "--db", "data/live.sqlite"], capsys)
    assert (code, out, err.strip()) == (1, "", "live_not_authorized")
    assert not (tmp_path / "data").exists()


def test_authorized_live_reports_conflict_without_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_root(monkeypatch, tmp_path)
    _write_config(
        tmp_path,
        _runtime(
            offline="false",
            live_authorized="true",
            billing_path="session",
            billing_path_verified="true",
            tool_policy_verified="true",
        ),
    )
    _set_command(monkeypatch, primary="tests/fakes/grok", alias=_OTHER)
    code, out, err = _invoke(["run", "--live", "--db", "data/live.sqlite"], capsys)
    assert (code, out, err.strip()) == (1, "", "llm_cmd_conflict")
    assert _OTHER not in out + err
    assert not (tmp_path / "data").exists()
