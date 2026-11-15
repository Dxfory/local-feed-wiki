"""``demo`` copies the English offline demo into a separate root and runs it.

Tests call ``main`` in-process. A second interpreter without ``PYTHONPATH``
cannot import the package until the frozen packaging change ships the files.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_RUNTIME = ("data", "raw", "wiki", ".grok-sandbox", ".grok-tmp")


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    import second_brain.cli as cli

    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _runtime_snapshot(root: Path) -> tuple[tuple[str, bool], ...]:
    return tuple((name, (root / name).exists()) for name in _RUNTIME)


def _tree_bytes(root: Path) -> tuple[tuple[str, bytes], ...]:
    found: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return tuple(found)


def _release_cwd(path: Path) -> None:
    try:
        cwd = Path.cwd().resolve()
        cwd.relative_to(path.resolve())
    except (OSError, ValueError):
        return
    os.chdir(REPO)


def _published(stdout: str) -> tuple[dict[str, str], Path, Path]:
    lines = stdout.splitlines()
    assert len(lines) == 3
    fields: dict[str, str] = {}
    for part in lines[0].split(" "):
        key, value = part.split("=", 1)
        fields[key] = value
    html = Path(lines[1])
    index = Path(lines[2])
    assert html.is_absolute()
    assert index.is_absolute()
    return fields, html, index


@pytest.fixture
def same_fs() -> Iterator[Path]:
    """Directory on the same filesystem as the offline binary, so a hard link works."""

    parent = REPO / ".pytest-samefs"
    parent.mkdir(mode=0o700, exist_ok=True)
    binary = REPO / "tests" / "fakes" / "grok"
    try:
        with tempfile.TemporaryDirectory(prefix="demo-", dir=parent) as raw:
            path = Path(raw)
            if path.stat().st_dev != binary.stat().st_dev:
                raise AssertionError("demo directory is not on the offline binary filesystem")
            try:
                yield path
            finally:
                _release_cwd(path)
    finally:
        try:
            os.chdir(REPO)
        except OSError:
            pass
        try:
            parent.rmdir()
        except OSError:
            pass


@pytest.fixture(autouse=True)
def no_socket() -> Iterator[None]:
    """Count connect, create_connection, and getaddrinfo. The demo must not call them."""

    calls: list[str] = []
    saved_connect = socket.socket.connect
    saved_create = socket.create_connection
    saved_addr = socket.getaddrinfo

    def connect(self, *args, **kwargs):
        calls.append("socket.socket.connect")
        return saved_connect(self, *args, **kwargs)

    def create_connection(*args, **kwargs):
        calls.append("socket.create_connection")
        return saved_create(*args, **kwargs)

    def getaddrinfo(*args, **kwargs):
        calls.append("getaddrinfo")
        return saved_addr(*args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = saved_connect  # type: ignore[method-assign]
        socket.create_connection = saved_create
        socket.getaddrinfo = saved_addr
    assert calls == []


def test_help_mentions_demo_and_keeps_run_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """Dropping ``demo`` from the usage text, or a run flag, fails this check."""

    code, out, err = _invoke(["--help"], capsys)
    assert code == 0
    assert err == ""
    assert "python3 -m second_brain.cli run [--offline | --live]" in out
    assert "[--example-config] [--config-dir DIR] [--db PATH] [--now TIMESTAMP] [--root PATH]" in out
    assert "python3 -m second_brain.cli demo [--out DIR]" in out
    code, out, err = _invoke(["run", "--help"], capsys)
    assert code == 0
    assert err == ""
    assert "python3 -m second_brain.cli demo [--out DIR]" in out


def test_demo_command_requires_out_value(capsys: pytest.CaptureFixture[str]) -> None:
    """``demo --out`` with no directory is a usage error and does not run."""

    code, out, err = _invoke(["demo", "--out"], capsys)
    assert (code, out, err.strip()) == (1, "", "missing_value")


def test_demo_command_rejects_run_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """``demo`` does not accept ``run`` flags such as ``--offline``."""

    code, out, err = _invoke(["demo", "--offline"], capsys)
    assert (code, out, err.strip()) == (1, "", "unknown_argument")


def test_demo_command_rejects_unknown_argument(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An extra positional argument is rejected before any output directory is filled."""

    code, out, err = _invoke(["demo", "--out", os.fspath(tmp_path), "extra"], capsys)
    assert (code, out, err.strip()) == (1, "", "unknown_argument")
    assert not (tmp_path / "wiki").exists()
    assert not (tmp_path / "data").exists()


def test_demo_command_rejects_non_empty_out(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory that already has an entry is refused and left unchanged."""

    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("keep\n", encoding="utf-8")
    code, stdout, err = _invoke(["demo", "--out", os.fspath(out)], capsys)
    assert (code, stdout, err.strip()) == (1, "", "demo_out_not_empty")
    assert (out / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (out / "wiki").exists()
    assert not (out / "data").exists()
    assert not (out / "raw").exists()


def test_demo_command_rejects_file_out(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file at ``--out`` is not a directory the demo can fill."""

    target = tmp_path / "file"
    target.write_text("x\n", encoding="utf-8")
    code, out, err = _invoke(["demo", "--out", os.fspath(target)], capsys)
    assert (code, out, err.strip()) == (1, "", "invalid_path")
    assert target.read_text(encoding="utf-8") == "x\n"


def test_demo_command_rejects_symlinked_out(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A symlink ``--out`` is refused, including when its target is an empty directory."""

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    code, out, err = _invoke(["demo", "--out", os.fspath(link)], capsys)
    assert (code, out, err.strip()) == (1, "", "symlink_escape")
    assert list(target.iterdir()) == []


def test_demo_command_rejects_symlinked_ancestor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A symlink on the way to ``--out`` is refused and the real parent stays empty."""

    parent = tmp_path / "parent"
    parent.mkdir()
    via = tmp_path / "via"
    via.symlink_to(parent, target_is_directory=True)
    code, out, err = _invoke(["demo", "--out", os.fspath(via / "child")], capsys)
    assert (code, out, err.strip()) == (1, "", "symlink_escape")
    assert list(parent.iterdir()) == []


def test_demo_command_refuses_checkout_root(capsys: pytest.CaptureFixture[str]) -> None:
    """The checkout itself is not an output root, so its runtime directories stay put."""

    before = _runtime_snapshot(REPO)
    code, out, err = _invoke(["demo", "--out", os.fspath(REPO)], capsys)
    assert (code, out, err.strip()) == (1, "", "demo_out_protected")
    assert _runtime_snapshot(REPO) == before


def test_demo_command_refuses_checkout_runtime_dirs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``data``, ``raw``, and ``wiki`` under the checkout are not output roots."""

    monkeypatch.chdir(REPO)
    before = _runtime_snapshot(REPO)
    for name in ("data", "raw", "wiki"):
        code, out, err = _invoke(["demo", "--out", name], capsys)
        assert (code, out, err.strip()) == (1, "", "demo_out_protected")
        code, out, err = _invoke(["demo", "--out", f"{name}/nested-demo"], capsys)
        assert (code, out, err.strip()) == (1, "", "demo_out_protected")
    assert _runtime_snapshot(REPO) == before


def test_demo_command_publishes_into_empty_out(
    same_fs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``demo --out`` on an empty directory publishes the digest there and leaves the checkout alone."""

    out = same_fs / "out"
    out.mkdir()
    work = same_fs / "work"
    work.mkdir()
    decoy = same_fs / "decoy"
    decoy.mkdir()
    monkeypatch.chdir(work)
    import second_brain.config as config

    monkeypatch.setattr(config, "PROJECT_ROOT", decoy)
    monkeypatch.setenv("SECOND_BRAIN_ROOT", os.fspath(decoy))
    monkeypatch.setenv("SECOND_BRAIN_GROK", "/opt/second-brain-tests/missing")
    monkeypatch.setenv("SECOND_BRAIN_GROK_SCENARIO", "error")
    monkeypatch.setenv("SECOND_BRAIN_GROK_ARGV_LOG", os.fspath(same_fs / "argv.log"))
    before_runtime = _runtime_snapshot(REPO)
    before_examples = _tree_bytes(REPO / "examples" / "demo")
    before_fixtures = _tree_bytes(REPO / "tests" / "fixtures" / "demo")

    code, stdout, err = _invoke(["demo", "--out", os.fspath(out)], capsys)

    assert code == 0, (code, stdout, err)
    assert err == ""
    fields, html, index = _published(stdout)
    assert fields["status"] == "success"
    assert fields["classified"] == "10"
    assert set(fields) == {
        "status",
        "classified",
        "inserted",
        "updated",
        "pending",
        "deferred",
        "html",
    }
    assert not Path(fields["html"]).is_absolute()
    assert html == (out / "wiki" / fields["html"]).resolve()
    assert html.is_file()
    assert index == (out / "wiki" / "index.md").resolve()
    assert index.is_file()
    page = html.read_text(encoding="utf-8")
    assert "East dock says the noon gate stays open" in page
    assert "https://example.com/north-ledger/noon-gate-shut" in page
    assert "2030-01-15T00:00:00.000000Z" in index.read_text(encoding="utf-8")
    placed = out / "tests" / "fakes" / "grok"
    assert placed.is_file()
    assert not placed.is_symlink()
    assert os.path.samefile(placed, REPO / "tests" / "fakes" / "grok")
    assert (out / "examples" / "demo" / "sources.yaml").read_bytes() == (
        REPO / "examples" / "demo" / "sources.yaml"
    ).read_bytes()
    assert not (work / "data").exists()
    assert not (work / "raw").exists()
    assert not (work / "wiki").exists()
    assert not (decoy / "data").exists()
    assert not (decoy / "raw").exists()
    assert not (decoy / "wiki").exists()
    assert _runtime_snapshot(REPO) == before_runtime
    assert _tree_bytes(REPO / "examples" / "demo") == before_examples
    assert _tree_bytes(REPO / "tests" / "fixtures" / "demo") == before_fixtures
    assert os.environ["SECOND_BRAIN_GROK"] == "/opt/second-brain-tests/missing"
    assert os.environ["SECOND_BRAIN_GROK_SCENARIO"] == "error"
    assert os.environ["SECOND_BRAIN_GROK_ARGV_LOG"] == os.fspath(same_fs / "argv.log")
    assert not (same_fs / "argv.log").exists()


def test_demo_command_default_out_prints_absolute_paths(
    same_fs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no ``--out``, the digest is created in a new temporary directory."""

    cwd = same_fs / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    # tempfile caches its directory after the first lookup, so point that
    # cache at this same-filesystem directory before the command runs.
    monkeypatch.setattr(tempfile, "tempdir", os.fspath(same_fs.resolve()))
    before = _runtime_snapshot(REPO)
    root: Path | None = None
    try:
        code, stdout, err = _invoke(["demo"], capsys)
        assert code == 0, (code, stdout, err)
        assert err == ""
        fields, html, index = _published(stdout)
        root = index.parent.parent
        assert root.name.startswith("second-brain-demo-")
        assert root.parent == same_fs.resolve()
        assert root != REPO
        assert fields["status"] == "success"
        assert fields["classified"] == "10"
        assert html.is_file()
        assert index.is_file()
        assert html == (root / "wiki" / fields["html"]).resolve()
        assert index == (root / "wiki" / "index.md").resolve()
        assert _runtime_snapshot(REPO) == before
        assert not (cwd / "wiki").exists()
        assert not (cwd / "data").exists()
    finally:
        if root is not None and root.is_dir() and not root.is_symlink():
            shutil.rmtree(root)
