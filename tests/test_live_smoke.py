"""Offline proof for scripts/live_smoke.sh.

The script is what a machine with the grok CLI will run. These tests never
start that binary and never open a socket. The process tripwire blocks
subprocess, so the script is started with os.posix_spawn. A sitecustomize
guard in the child rejects connect and getaddrinfo.

Live authorization reads SECOND_BRAIN_SMOKE_RUNTIME when that variable is
set, and otherwise the checkout's config/runtime.yaml. These tests point the
variable at a temporary file so a developer's authorized runtime.yaml is not
an input. The authorization rules themselves stay in the script.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from tests.local_process import run

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "live_smoke.sh"
FAKE = REPO / "tests" / "fakes" / "grok"
SOURCE_FEED = REPO / "tests" / "fixtures" / "rss" / "feed.xml"
LIVE_EXAMPLE = REPO / "config" / "runtime.live-smoke.example.yaml"
_PARTIAL_RUNTIME = """\
offline: false
live_authorized: true
billing_path: session
billing_path_verified: false
tool_policy_verified: true
review_llm: true
max_classify_batches: 1
max_review_batches: 1
max_transcript_fetches: 1
"""


def _guard(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(
        "\n".join(
            (
                "import os",
                "import socket",
                "ready = os.environ.get('SECOND_BRAIN_GUARD_READY', '')",
                "if ready:",
                "    with open(ready, 'a', encoding='utf-8') as handle:",
                "        handle.write('ready\\n')",
                "log = os.environ.get('SECOND_BRAIN_SOCKET_LOG', '')",
                "def _block(kind):",
                "    def blocked(*_args, **_kwargs):",
                "        if log:",
                "            with open(log, 'a', encoding='utf-8') as handle:",
                "                handle.write(kind + '\\n')",
                "        raise RuntimeError('network')",
                "    return blocked",
                "socket.socket.connect = _block('socket.connect')",
                "socket.socket.connect_ex = _block('socket.connect_ex')",
                "socket.create_connection = _block('socket.create_connection')",
                "socket.getaddrinfo = _block('socket.getaddrinfo')",
                "",
            )
        ),
        encoding="utf-8",
    )


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tmp / "guard"),
        "SECOND_BRAIN_GUARD_READY": str(tmp / "guard-ready.txt"),
        "SECOND_BRAIN_SOCKET_LOG": str(tmp / "sockets.txt"),
        "SECOND_BRAIN_SMOKE_MARKER": str(tmp / "marker-ran.txt"),
        # Missing on purpose. Live mode must refuse this path instead of
        # opening the checkout's config/runtime.yaml.
        "SECOND_BRAIN_SMOKE_RUNTIME": str(tmp / "no-such-runtime.yaml"),
    }
    for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(extra)
    return env


def _invoke(tmp: Path, args: list[str], **extra: str) -> tuple[int, str, str]:
    _guard(tmp / "guard")
    return run(["/bin/sh", str(SCRIPT), *args], cwd=tmp, env=_env(tmp, **extra))


def _write_marker(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\nprintf ran >> \"$SECOND_BRAIN_SMOKE_MARKER\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _checkout_runtime_present() -> bool:
    return os.path.lexists(REPO / "config" / "runtime.yaml")


def _authorized_runtime() -> str:
    return LIVE_EXAMPLE.read_text(encoding="utf-8")


def _assert_authorized_live_run(tmp: Path, code: int, out: str, err: str) -> None:
    assert "smoke_root=" in out, (code, out, err)
    root = _root_line(out)
    try:
        assert code == 2, (code, out, err)
        assert "live_not_authorized" not in err.splitlines(), (code, out, err)
        assert "模型不可用，未完成分类" in err.splitlines(), (code, out, err)
        assert "status=partial" in out
        assert "ran" in (tmp / "marker-ran.txt").read_text(encoding="utf-8")
        runtime = (root / "config" / "runtime.yaml").read_text(encoding="utf-8").splitlines()
        for line in (
            "offline: false",
            "live_authorized: true",
            "billing_path: session",
            "billing_path_verified: true",
            "tool_policy_verified: true",
            "max_classify_batches: 1",
            "max_review_batches: 1",
            "max_transcript_fetches: 1",
        ):
            assert line in runtime, runtime
        sockets = tmp / "sockets.txt"
        assert not sockets.exists() or sockets.read_text(encoding="utf-8") == ""
        assert (tmp / "guard-ready.txt").is_file()
        assert root.resolve() != REPO.resolve()
        assert REPO.resolve() not in root.resolve().parents
    finally:
        if root.is_dir():
            shutil.rmtree(root)


def _root_line(stdout: str) -> Path:
    found = [line for line in stdout.splitlines() if line.startswith("smoke_root=")]
    assert len(found) == 1, stdout
    return Path(found[0].split("=", 1)[1])


def test_offline_smoke_uses_fake_temp_dir_and_no_sockets(tmp_path: Path) -> None:
    log = tmp_path / "argv.log"
    code, out, err = _invoke(
        tmp_path,
        [],
        SECOND_BRAIN_GROK="tests/fakes/grok",
        SECOND_BRAIN_GROK_ARGV_LOG=str(log),
    )
    assert code == 0, f"stdout:\n{out}\nstderr:\n{err}"
    assert err == ""
    assert "Traceback" not in out
    root: Path | None = None
    try:
        root = _root_line(out)
        assert root.is_dir()
        assert root.resolve() != REPO.resolve()
        assert REPO.resolve() not in root.resolve().parents
        assert not (REPO / "data" / "smoke.sqlite").exists()
        status = [line for line in out.splitlines() if line.startswith("status=")]
        assert len(status) == 1, out
        assert "updated=" in status[0]
        assert "html=digest/" in status[0]
        assert (root / "data" / "run.lock").is_file()
        assert (root / "data" / "smoke.sqlite").is_file()
        pages = list((root / "wiki" / "digest").glob("*.html"))
        assert len(pages) == 1
        html = pages[0].read_text(encoding="utf-8")
        assert "增量摘要" in html
        assert "<script" not in html.lower()
        assert "SUPERSECRET" not in html
        feed = (root / "tests" / "fixtures" / "rss" / "feed.xml").read_text(encoding="utf-8")
        available = SOURCE_FEED.read_text(encoding="utf-8").count("<item>")
        assert feed.count("<item>") == min(3, available)
        assert feed.count("<item>") <= 3
        sources = (root / "config" / "sources.yaml").read_text(encoding="utf-8")
        assert sources.count("\n  - id:") == 1
        assert "kind: file" in sources
        assert "file_format: rss" in sources
        assert "file:tests/fixtures/rss/feed.xml" in sources
        assert "http://" not in sources and "https://" not in sources
        runtime = (root / "config" / "runtime.yaml").read_text(encoding="utf-8")
        for line in (
            "offline: true",
            "live_authorized: false",
            "billing_path: test",
            "max_classify_batches: 1",
            "max_review_batches: 1",
            "max_transcript_fetches: 1",
        ):
            assert line in runtime.splitlines()
        assert log.is_file()
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
        assert records
        for record in records:
            argv0 = Path(record["argv"][0])
            assert argv0.is_file() and not argv0.is_symlink()
            assert os.path.samefile(argv0, FAKE)
            assert argv0.name == "grok"
        assert (tmp_path / "guard-ready.txt").is_file()
        sockets = tmp_path / "sockets.txt"
        assert not sockets.exists() or sockets.read_text(encoding="utf-8") == ""
        assert not (tmp_path / "marker-ran.txt").exists()
    finally:
        if root is not None and root.is_dir():
            shutil.rmtree(root)


def test_offline_item_cap_limits_the_fixture(tmp_path: Path) -> None:
    code, out, err = _invoke(
        tmp_path,
        ["--offline", "--max-items", "1", "--budget", "2"],
        SECOND_BRAIN_GROK=str(FAKE),
    )
    assert code == 0, f"stdout:\n{out}\nstderr:\n{err}"
    root: Path | None = None
    try:
        root = _root_line(out)
        feed = (root / "tests" / "fixtures" / "rss" / "feed.xml").read_text(encoding="utf-8")
        assert feed.count("<item>") == 1
        runtime = (root / "config" / "runtime.yaml").read_text(encoding="utf-8")
        for name in ("max_classify_batches", "max_review_batches", "max_transcript_fetches"):
            assert f"{name}: 2" in runtime.splitlines()
        assert (tmp_path / "guard-ready.txt").is_file()
        sockets = tmp_path / "sockets.txt"
        assert not sockets.exists() or sockets.read_text(encoding="utf-8") == ""
    finally:
        if root is not None and root.is_dir():
            shutil.rmtree(root)


def test_live_mode_refuses_without_caps_and_does_not_exec(tmp_path: Path) -> None:
    before = _checkout_runtime_present()
    marker = _write_marker(tmp_path / "not-grok")
    denied = tmp_path / "denied-runtime.yaml"
    denied.write_text(_PARTIAL_RUNTIME, encoding="utf-8")
    link = tmp_path / "runtime-link.yaml"
    link.symlink_to(LIVE_EXAMPLE)
    cases = (
        (["--offline"], {"SECOND_BRAIN_GROK": str(marker)}, "offline_grok_rejected"),
        (["--live"], {"SECOND_BRAIN_GROK": str(marker)}, "missing_budget"),
        (["--live", "--budget", "1"], {"SECOND_BRAIN_GROK": str(marker)}, "missing_item_cap"),
        (
            ["--live", "--budget", "1", "--max-items", "1"],
            {"SECOND_BRAIN_GROK": str(FAKE)},
            "offline_grok_rejected",
        ),
        (
            ["--live", "--budget", "1", "--max-items", "1"],
            {"SECOND_BRAIN_GROK": str(marker)},
            "live_not_authorized",
        ),
        (
            ["--live", "--budget", "1", "--max-items", "1"],
            {"SECOND_BRAIN_GROK": str(marker), "SECOND_BRAIN_SMOKE_RUNTIME": str(denied)},
            "live_not_authorized",
        ),
        (
            ["--live", "--budget", "1", "--max-items", "1"],
            {"SECOND_BRAIN_GROK": str(marker), "SECOND_BRAIN_SMOKE_RUNTIME": str(link)},
            "live_not_authorized",
        ),
        (["--live", "--offline"], {}, "conflicting_mode"),
        (["--budget", "9"], {"SECOND_BRAIN_GROK": str(FAKE)}, "budget_cap"),
        (["--max-items", "4"], {"SECOND_BRAIN_GROK": str(FAKE)}, "item_cap"),
        (["--budget"], {"SECOND_BRAIN_GROK": str(FAKE)}, "missing_value"),
        (["--publish"], {}, "unknown_argument"),
    )
    try:
        for index, (args, extra, expected) in enumerate(cases):
            case = tmp_path / "case" / f"{index}-{expected}"
            case.mkdir(parents=True)
            code, out, err = _invoke(case, args, **extra)
            assert code == 1, (args, code, out, err)
            assert err.strip() == expected, (args, err, out)
            assert out.strip() == "", (args, out)
            assert not (case / "marker-ran.txt").exists(), args
            assert not (tmp_path / "marker-ran.txt").exists(), args
            sockets = case / "sockets.txt"
            assert not sockets.exists() or sockets.read_text(encoding="utf-8") == ""
    finally:
        assert _checkout_runtime_present() is before


def test_authorized_runtime_override_still_starts_live_mode(tmp_path: Path) -> None:
    before = _checkout_runtime_present()
    runtime = tmp_path / "authorized-runtime.yaml"
    runtime.write_text(_authorized_runtime(), encoding="utf-8")
    original = runtime.read_bytes()
    marker = _write_marker(tmp_path / "not-grok")
    try:
        code, out, err = _invoke(
            tmp_path,
            ["--live", "--budget", "1", "--max-items", "1"],
            SECOND_BRAIN_GROK=str(marker),
            SECOND_BRAIN_SMOKE_RUNTIME=str(runtime),
        )
        assert runtime.read_bytes() == original
        _assert_authorized_live_run(tmp_path, code, out, err)
    finally:
        assert _checkout_runtime_present() is before


def test_unset_runtime_env_still_reads_repo_runtime_yaml(tmp_path: Path) -> None:
    before = _checkout_runtime_present()
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "scripts" / "live_smoke.sh")
    config = repo / "config"
    config.mkdir()
    shutil.copyfile(REPO / "config" / "preferences.example.md", config / "preferences.example.md")
    shutil.copyfile(
        REPO / "config" / "topic_aliases.example.yaml",
        config / "topic_aliases.example.yaml",
    )
    runtime_text = _authorized_runtime()
    runtime = config / "runtime.yaml"
    runtime.write_text(runtime_text, encoding="utf-8")
    feed = repo / "tests" / "fixtures" / "rss"
    feed.mkdir(parents=True)
    shutil.copyfile(SOURCE_FEED, feed / "feed.xml")
    shutil.copytree(
        REPO / "src",
        repo / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    original = runtime.read_bytes()
    marker = _write_marker(tmp_path / "not-grok")
    work = tmp_path / "work"
    work.mkdir()
    _guard(work / "guard")
    env = _env(work, SECOND_BRAIN_GROK=str(marker))
    env.pop("SECOND_BRAIN_SMOKE_RUNTIME")
    try:
        code, out, err = run(
            [
                "/bin/sh",
                str(repo / "scripts" / "live_smoke.sh"),
                "--live",
                "--budget",
                "1",
                "--max-items",
                "1",
            ],
            cwd=work,
            env=env,
        )
        assert runtime.read_bytes() == original
        _assert_authorized_live_run(work, code, out, err)
    finally:
        assert _checkout_runtime_present() is before
