"""WP14 config, CLI, and the full-module scope checks.

Tests call ``main`` directly. The production root is monkeypatched to the
temporary directory before that call. Example files and synthetic fixtures
are copied in; real config, raw, and wiki are not.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
SENTINEL = "SENTINEL_CREDENTIAL_9f3a"
FAKE_SECRET = "SUPERSECRET"
OUTSIDE = "OUTSIDE_SECRET_BODY"
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
HELP = (
    "usage: python3 -m second_brain.cli run [--offline | --live] "
    "[--example-config] [--config-dir DIR] [--db PATH] [--now TIMESTAMP] [--root PATH]\n"
    "PYTHONPATH=src SECOND_BRAIN_GROK=tests/fakes/grok "
    "python3 -m second_brain.cli run --offline --config-dir examples/offline "
    "--now 2030-01-15T00:00:00Z --db data/second_brain.sqlite\n"
)
ALIASES = "topic_aliases: {}\nclaim_aliases: {}\n"
PREFERENCES = "合成偏好\n"
BLOG = """\
sources:
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客
    backfill_days: 14
"""
_UPWARD = {
    "second_brain.run": frozenset({"second_brain.cli", "second_brain.config"}),
    "second_brain.config": frozenset({"second_brain.cli", "second_brain.run"}),
    "second_brain.store": frozenset(
        {"second_brain.run", "second_brain.cli", "second_brain.config", "second_brain.http"}
    ),
    "second_brain.http": frozenset(
        {"second_brain.store", "second_brain.run", "second_brain.cli", "second_brain.config"}
    ),
    "second_brain.llm.grok_cli": frozenset(
        {"second_brain.run", "second_brain.cli", "second_brain.config"}
    ),
    "second_brain.classify.buckets": frozenset(
        {"second_brain.llm.grok_cli", "second_brain.run", "second_brain.cli"}
    ),
    "second_brain.review.multi_source": frozenset(
        {"second_brain.llm.grok_cli", "second_brain.wiki.compile"}
    ),
    "second_brain.sources.youtube": frozenset(
        {"second_brain.sources.rss", "second_brain.store", "second_brain.http"}
    ),
    "second_brain.sources.rss": frozenset(
        {"second_brain.store", "second_brain.http", "second_brain.run"}
    ),
    "second_brain.wiki.compile": frozenset(
        {"second_brain.store", "second_brain.run", "second_brain.cli"}
    ),
    "second_brain.sources.ingest": frozenset(
        {"second_brain.run", "second_brain.cli", "second_brain.config"}
    ),
}


def _boundary():
    spec = importlib.util.spec_from_file_location(
        "_wp14_boundary_rules",
        REPO / "tests" / "test_boundaries.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules():
    import second_brain.cli as cli
    import second_brain.config as config

    return cli, config


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


def _copy_tree(name: str, root: Path) -> None:
    shutil.copytree(REPO / "tests" / "fixtures" / name, root / "tests" / "fixtures" / name)


def _release_cwd(path: Path) -> None:
    try:
        cwd = Path.cwd().resolve()
        cwd.relative_to(path.resolve())
    except (OSError, ValueError):
        return
    os.chdir(REPO)


@pytest.fixture
def repo_root() -> Iterator[Path]:
    """Project root on the same filesystem as ``tests/fakes/grok``.

    ``os.link`` into pytest's ``/tmp`` fails when that directory is a
    different device from the checkout. The directory is removed after
    the test. A copy of the fake is still a different file.
    """

    parent = REPO / ".pytest-samefs"
    parent.mkdir(mode=0o700, exist_ok=True)
    fake = REPO / "tests" / "fakes" / "grok"
    try:
        with tempfile.TemporaryDirectory(prefix="root-", dir=parent) as raw:
            path = Path(raw)
            if path.stat().st_dev != fake.stat().st_dev:
                raise AssertionError("project root is not on the fake's filesystem")
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


def _link_fake(root: Path) -> None:
    # A copy is a different file, and the tripwire allows only the real fake inode.
    source = REPO / "tests" / "fakes" / "grok"
    destination = root / "tests" / "fakes" / "grok"
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, destination)
    if destination.is_symlink() or not os.path.samefile(source, destination):
        raise AssertionError("fake hardlink is not the same inode")


def _write_config(root: Path, sources: str, runtime: str, directory: str = "config") -> Path:
    config_dir = root / directory
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "sources.yaml").write_text(sources, encoding="utf-8")
    (config_dir / "topic_aliases.yaml").write_text(ALIASES, encoding="utf-8")
    (config_dir / "runtime.yaml").write_text(runtime, encoding="utf-8")
    (config_dir / "preferences.md").write_text(PREFERENCES, encoding="utf-8")
    return config_dir


def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECOND_BRAIN_GROK", "tests/fakes/grok")


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    cli, _config = _modules()
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _fields(stdout: str) -> dict[str, str]:
    line = stdout.strip("\n")
    assert "\n" not in line and line != ""
    found: dict[str, str] = {}
    for part in line.split(" "):
        key, value = part.split("=", 1)
        found[key] = value
    return found


def _imported(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _is_stdlib(name: str) -> bool:
    top = name.split(".", 1)[0]
    return top in sys.stdlib_module_names or top == "__future__"


def _contains(root: Path, needle: str, *, skip: set[Path]) -> list[str]:
    raw = needle.encode("utf-8")
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.resolve() in skip:
            continue
        if raw in path.read_bytes():
            found.append(str(path))
    return found


def test_config_strict_and_fixture_boundary(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli, config = _modules()
    assert config.PROJECT_ROOT == REPO.resolve()
    monkeypatch.chdir(repo_root)
    assert config.PROJECT_ROOT == REPO.resolve()
    _use_root(monkeypatch, repo_root)
    _copy_tree("rss", repo_root)
    _link_fake(repo_root)
    _offline_env(monkeypatch)
    fixture_root = repo_root / "tests" / "fixtures"

    duplicate = """\
sources:
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客乙
"""
    _write_config(repo_root, duplicate, _runtime())
    with pytest.raises(config.ConfigError) as duplicate_error:
        config.load_config(repo_root / "config", offline=True)
    assert duplicate_error.value.code == "duplicate_source_id"

    cookie = BLOG.replace("title: 合成博客\n", f"title: 合成博客\n    cookie: {SENTINEL}\n")
    _write_config(repo_root, cookie, _runtime())
    code, out, err = _invoke(["run", "--offline", "--db", "data/cookie.sqlite"], capsys)
    assert code == 1
    assert err.strip() == "unknown_field"
    assert SENTINEL not in out + err

    cycled = "topic_aliases:\n  a: b\n  b: a\nclaim_aliases: {}\n"
    (repo_root / "config" / "topic_aliases.yaml").write_text(cycled, encoding="utf-8")
    _write_config(repo_root, BLOG, _runtime())
    (repo_root / "config" / "topic_aliases.yaml").write_text(cycled, encoding="utf-8")
    with pytest.raises(config.ConfigError) as cycle_error:
        config.load_config(repo_root / "config", offline=True)
    assert cycle_error.value.code == "alias_cycle"

    unknown = BLOG.replace("file_format: rss", "file_format: atom")
    _write_config(repo_root, unknown, _runtime())
    with pytest.raises(config.ConfigError) as format_error:
        config.load_config(repo_root / "config", offline=True)
    assert format_error.value.code == "invalid_file_format"

    secret_url = """\
sources:
  - id: secret-feed
    kind: rss
    url: https://example.com/feed?token=SENTINEL_CREDENTIAL_9f3a
    title: 凭据查询
"""
    _write_config(repo_root, secret_url, _runtime())
    with pytest.raises(config.ConfigError) as secret_error:
        config.load_config(repo_root / "config", offline=True)
    assert secret_error.value.code == "secret_query"
    assert SENTINEL not in str(secret_error.value)

    for url, title in (
        ("https://127.0.0.1/feed", "回环"),
        ("https://169.254.169.254/feed", "链路本地"),
        ("https://metadata.google.internal/feed", "元数据"),
        ("https://api.bilibili.com/x/feed", "哔哩哔哩"),
    ):
        text = f"sources:\n  - id: blocked\n    kind: rss\n    url: {url}\n    title: {title}\n"
        _write_config(repo_root, text, _runtime())
        with pytest.raises(config.ConfigError) as blocked:
            config.load_config(repo_root / "config", offline=True)
        assert blocked.value.code in {"non_global_address", "metadata_host", "bilibili_forbidden"}
        assert url not in str(blocked.value)

    outside = repo_root / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(OUTSIDE, encoding="utf-8")
    linked = fixture_root / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(config.ConfigError) as link_error:
        config.read_fixture("file:tests/fixtures/linked/secret.txt", fixture_root=fixture_root)
    assert link_error.value.code == "fixture_escape"
    assert OUTSIDE not in str(link_error.value)
    leak = fixture_root / "rss" / "leak.xml"
    leak.symlink_to(outside / "secret.txt")
    with pytest.raises(config.ConfigError) as file_link:
        config.read_fixture("file:tests/fixtures/rss/leak.xml", fixture_root=fixture_root)
    assert file_link.value.code == "fixture_escape"
    with pytest.raises(config.ConfigError) as parent_error:
        config.read_fixture(
            "file:tests/fixtures/rss/../../outside/secret.txt",
            fixture_root=fixture_root,
        )
    assert parent_error.value.code == "fixture_escape"
    with pytest.raises(config.ConfigError) as host_error:
        config.read_fixture("file://evil/tests/fixtures/rss/feed.xml", fixture_root=fixture_root)
    assert host_error.value.code == "invalid_fixture_url"

    _write_config(repo_root, BLOG, _runtime())
    original = (repo_root / "config" / "sources.yaml").read_bytes()
    code, out, err = _invoke(
        [
            "run",
            "--offline",
            "--now",
            "2030-01-15T00:00:00Z",
            "--db",
            "data/legal.sqlite",
        ],
        capsys,
    )
    assert code == 0, (code, out, err)
    assert (repo_root / "config" / "sources.yaml").read_bytes() == original
    fields = _fields(out)
    assert fields["status"] == "success"
    assert int(fields["classified"]) >= 1
    page = (repo_root / "wiki" / fields["html"]).read_text(encoding="utf-8")
    for label in ("必读", "推荐阅读", "不推荐", "增量摘要"):
        assert label in page
    assert OUTSIDE not in page
    assert "正文甲" not in err


def test_example_cli_runs_offline(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_root(monkeypatch, repo_root)
    _offline_env(monkeypatch)
    for name in ("rss", "podcast", "youtube"):
        _copy_tree(name, repo_root)
    _link_fake(repo_root)
    shutil.copytree(REPO / "examples" / "offline", repo_root / "examples" / "offline")
    originals = {
        path.name: path.read_bytes()
        for path in (REPO / "examples" / "offline").iterdir()
        if path.is_file()
    }
    elsewhere = repo_root / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    code, out, err = _invoke(DEMO, capsys)
    assert code == 0, (code, out, err)
    assert SENTINEL not in out + err
    first = _fields(out)
    assert first["status"] == "success"
    assert int(first["classified"]) >= 1
    page = (repo_root / "wiki" / first["html"]).read_text(encoding="utf-8")
    for label in ("必读", "推荐阅读", "不推荐", "增量摘要", "待转写"):
        assert label in page
    assert "2030-01-15T00:00:00.000000Z" in page
    assert "离线合成结果" in page
    assert "合成文章甲" in page
    assert "https://example.com/posts/alpha" in page
    assert "<script" not in page
    assert "secret()" not in page
    assert (repo_root / "data" / "second_brain.sqlite").is_file()
    assert (repo_root / "data" / "run.lock").is_file()
    assert not (elsewhere / "wiki").exists()
    assert not (elsewhere / "data").exists()

    code, out, err = _invoke(DEMO, capsys)
    assert code == 0, (code, out, err)
    second = _fields(out)
    assert second["status"] == "success"
    assert second["classified"] == "0"
    again = (repo_root / "wiki" / second["html"]).read_text(encoding="utf-8")
    assert "本次没有新的已分档条目" in again
    for label in ("必读", "推荐阅读", "不推荐"):
        assert label in again
    demo_dir = repo_root / "examples" / "offline"
    for name, payload in originals.items():
        assert (demo_dir / name).read_bytes() == payload
    assert not (repo_root / "config").exists()


def test_live_requires_authorization_and_tool_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tripwire,
) -> None:
    start = len(tripwire.events)
    _use_root(monkeypatch, tmp_path)
    monkeypatch.delenv("SECOND_BRAIN_GROK", raising=False)
    network = """\
sources:
  - id: public-feed
    kind: rss
    url: https://example.com/feed.xml
    title: 公开源
"""
    _write_config(
        tmp_path,
        network,
        _runtime(
            offline="false",
            live_authorized="true",
            billing_path="session",
            billing_path_verified="true",
            tool_policy_verified="true",
        ),
    )
    code, out, err = _invoke(["run", "--offline", "--db", "data/net.sqlite"], capsys)
    assert code == 1
    assert err.strip() == "offline_network_source"
    assert out == ""
    code, out, err = _invoke(["run", "--db", "data/net-default.sqlite"], capsys)
    assert code == 1
    assert err.strip() == "offline_network_source"
    assert not (tmp_path / "data" / "net.sqlite").exists()

    cases = (
        (_runtime(), "live_not_authorized"),
        (
            _runtime(live_authorized="true", billing_path="session", tool_policy_verified="true"),
            "billing_path_unverified",
        ),
        (
            _runtime(
                live_authorized="true",
                billing_path="session",
                billing_path_verified="true",
            ),
            "tool_policy_unverified",
        ),
        (
            _runtime(live_authorized="true", billing_path="test", billing_path_verified="true", tool_policy_verified="true"),
            "billing_path_unverified",
        ),
        (
            _runtime(
                live_authorized="true",
                billing_path="unknown",
                billing_path_verified="true",
                tool_policy_verified="true",
            ),
            "billing_path_unverified",
        ),
    )
    for runtime, expected in cases:
        _write_config(tmp_path, network, runtime)
        code, out, err = _invoke(["run", "--live", "--db", "data/live.sqlite"], capsys)
        assert (code, err.strip(), out) == (1, expected, "")

    _copy_tree("rss", tmp_path)
    _write_config(tmp_path, BLOG, _runtime())
    monkeypatch.setenv("SECOND_BRAIN_GROK", "/opt/second-brain-tests/grok")
    code, out, err = _invoke(
        ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/real.sqlite"],
        capsys,
    )
    assert code == 1
    assert err.strip() == "offline_grok_rejected"
    assert not (tmp_path / "data" / "real.sqlite").exists()
    monkeypatch.delenv("SECOND_BRAIN_GROK", raising=False)
    code, out, err = _invoke(
        ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/unset.sqlite"],
        capsys,
    )
    assert code == 1
    assert err.strip() == "grok_binary_required"
    assert "/opt/second-brain-tests/grok" not in out + err
    _write_config(
        tmp_path,
        BLOG,
        _runtime(
            live_authorized="true",
            billing_path="session",
            billing_path_verified="true",
            tool_policy_verified="true",
        ),
    )
    code, out, err = _invoke(["run", "--live", "--db", "data/live-unset.sqlite"], capsys)
    assert code == 1
    assert err.strip() == "grok_binary_required"
    assert out == ""
    assert not (tmp_path / "data" / "live-unset.sqlite").exists()
    assert tripwire.events[start:] == []
    assert not (tmp_path / ".grok").exists()


def test_cli_exit_codes(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in ([], ["--help"], ["run", "--help"]):
        code, out, err = _invoke(argv, capsys)
        assert code == 0
        assert err == ""
        assert out == HELP
        assert "正文甲" not in out
        assert SENTINEL not in out
        assert "cookie" not in out
        assert "api_key" not in out
    code, out, err = _invoke(["fetch"], capsys)
    assert code == 1
    assert out == ""
    assert err.strip() == "unknown_command"
    code, out, err = _invoke(["run", "-h"], capsys)
    assert code == 1
    assert out == ""
    assert err.strip() == "unknown_argument"
    code, out, err = _invoke(["run", "--live", "--offline"], capsys)
    assert err.strip() == "conflicting_mode"
    assert code == 1
    code, out, err = _invoke(
        ["run", "--live", "--now", "2030-01-15T00:00:00Z"],
        capsys,
    )
    assert code == 1
    assert err.strip() == "now_requires_offline"

    _use_root(monkeypatch, repo_root)
    _offline_env(monkeypatch)
    _copy_tree("rss", repo_root)
    _link_fake(repo_root)
    code, out, err = _invoke(["run", "--offline", "--db", "data/missing.sqlite"], capsys)
    assert code == 1
    assert err.strip() == "missing_config"
    assert "正文甲" not in out + err

    cookie = BLOG.replace("title: 合成博客\n", f"title: 合成博客\n    cookie: {SENTINEL}\n")
    _write_config(repo_root, cookie, _runtime(), "config-cookie")
    code, out, err = _invoke(
        ["run", "--offline", "--config-dir", "config-cookie", "--db", "data/cookie.sqlite"],
        capsys,
    )
    assert code == 1
    assert err.strip() == "unknown_field"
    assert SENTINEL not in out + err

    _write_config(repo_root, BLOG, _runtime(), "config-ok")
    ok = [
        "run",
        "--offline",
        "--config-dir",
        "config-ok",
        "--now",
        "2030-01-15T00:00:00Z",
        "--db",
        "data/ok.sqlite",
    ]
    code, out, err = _invoke(ok, capsys)
    assert code == 0, (code, out, err)
    assert _fields(out)["status"] == "success"
    assert "正文甲" not in out + err

    partial = """\
sources:
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客
  - id: missing-feed
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/missing.xml
    title: 缺失夹具
"""
    _write_config(repo_root, partial, _runtime(), "config-partial")
    code, out, err = _invoke(
        [
            "run",
            "--offline",
            "--config-dir",
            "config-partial",
            "--now",
            "2030-01-15T00:00:00Z",
            "--db",
            "data/partial.sqlite",
        ],
        capsys,
    )
    assert code == 2, (code, out, err)
    fields = _fields(out)
    assert fields["status"] == "partial"
    page = (repo_root / "wiki" / fields["html"]).read_text(encoding="utf-8")
    for label in ("必读", "推荐阅读", "不推荐"):
        assert label in page
    assert "合成文章甲" in page
    raw_text = "\n".join(path.read_text(encoding="utf-8") for path in (repo_root / "raw").rglob("*.md"))
    assert "正文甲" in raw_text
    assert "正文甲" not in out + err
    assert SENTINEL not in out + err

    lock_path = repo_root / "data" / "run.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    os.write(fd, b"")
    os.fsync(fd)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code, out, err = _invoke(
            [
                "run",
                "--offline",
                "--config-dir",
                "config-ok",
                "--now",
                "2030-01-15T00:00:00Z",
                "--db",
                "data/lock.sqlite",
            ],
            capsys,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert code == 1
    assert err.strip() == "run_locked"
    assert "正文甲" not in out + err
    assert lock_path.is_file()

    index = repo_root / "wiki" / "index.md"
    index.unlink()
    index.mkdir()
    code, out, err = _invoke(ok, capsys)
    assert code == 1, (code, out, err)
    assert err.strip().splitlines()[-1] == "invalid_index"
    assert "正文甲" not in out + err
    assert SENTINEL not in out + err


def test_config_and_output_secret_sentinel(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_root(monkeypatch, repo_root)
    _copy_tree("rss", repo_root)
    _link_fake(repo_root)
    _offline_env(monkeypatch)
    log_path = repo_root / "argv.log"
    monkeypatch.setenv("SECOND_BRAIN_GROK_ARGV_LOG", str(log_path))
    poisoned = f"""\
sources:
  - id: example-blog
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 合成博客
    cookie: {SENTINEL}
    api_key: {SENTINEL}
    headers: {SENTINEL}
"""
    _write_config(repo_root, poisoned, _runtime())
    runtime = _runtime() + f"api_key: {SENTINEL}\n"
    (repo_root / "config" / "runtime.yaml").write_text(runtime, encoding="utf-8")
    code, out, err = _invoke(
        ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/secret.sqlite"],
        capsys,
    )
    assert code == 1
    assert err.strip() == "unknown_field"
    assert SENTINEL not in out + err
    assert not log_path.exists()
    assert not (repo_root / "wiki").exists()
    assert not (repo_root / ".grok-tmp").exists()
    assert not (repo_root / "data" / "secret.sqlite").exists()

    _write_config(repo_root, BLOG, _runtime())
    monkeypatch.setenv("SECOND_BRAIN_GROK_SCENARIO", "error")
    code, out, err = _invoke(
        ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/error.sqlite"],
        capsys,
    )
    assert code == 2, (code, out, err)
    assert FAKE_SECRET not in out + err
    assert SENTINEL not in out + err
    skip = {(repo_root / "tests" / "fakes" / "grok").resolve()}
    assert _contains(repo_root / "wiki", FAKE_SECRET, skip=skip) == []
    assert _contains(repo_root / "raw", FAKE_SECRET, skip=skip) == []
    assert _contains(repo_root / "data", FAKE_SECRET, skip=skip) == []
    assert _contains(repo_root / ".grok-tmp", FAKE_SECRET, skip=skip) == []
    assert _contains(repo_root / ".grok-sandbox", FAKE_SECRET, skip=skip) == []
    if log_path.exists():
        logged = log_path.read_text(encoding="utf-8")
        assert FAKE_SECRET not in logged
        assert SENTINEL not in logged
    page = next((repo_root / "wiki").rglob("*.html"))
    assert SENTINEL not in page.read_text(encoding="utf-8")
    assert FAKE_SECRET not in page.read_text(encoding="utf-8")


def test_import_layers() -> None:
    boundary = _boundary()
    present = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")}
    known = {relative for relative, _module in boundary.MODULES}
    assert sorted(known - present) == []
    assert sorted(present - known) == []
    subprocess_modules: list[str] = []
    for relative, module in boundary.MODULES:
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
        imported = _imported(tree)
        for name in imported:
            top = name.split(".", 1)[0]
            if top == "subprocess" or name.startswith("subprocess."):
                subprocess_modules.append(module)
            assert top not in boundary.BANNED_STDLIB
            assert top not in boundary.BANNED_PACKAGES
            if name.startswith("second_brain"):
                continue
            if _is_stdlib(name):
                continue
            assert top in boundary.THIRD_PARTY.get(module, frozenset()), f"{module} imports {name}"
        banned = _UPWARD.get(module, frozenset())
        assert imported.isdisjoint(banned), f"{module} imports upward {imported & banned}"
    assert subprocess_modules == ["second_brain.llm.grok_cli"]
    boundary.test_import_layers()


def test_public_example_is_offline_rejected_and_budgets_cover_many_sources(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_root(monkeypatch, repo_root)
    config_dir = repo_root / "config"
    config_dir.mkdir()
    names = (
        "sources.example.yaml",
        "preferences.example.md",
        "topic_aliases.example.yaml",
        "runtime.example.yaml",
    )
    for name in names:
        shutil.copyfile(REPO / "config" / name, config_dir / name)
    sentinel = f"REAL_CONFIG_SENTINEL {SENTINEL}\n".encode("utf-8")
    real_names = ("sources.yaml", "preferences.md", "topic_aliases.yaml", "runtime.yaml")
    for name in real_names:
        (config_dir / name).write_bytes(sentinel)
    code, out, err = _invoke(
        ["run", "--offline", "--example-config", "--db", "data/public.sqlite"],
        capsys,
    )
    assert code == 1
    assert err.strip() == "offline_network_source"
    assert out == ""
    assert not (repo_root / "data").exists()
    for name in real_names:
        assert (config_dir / name).read_bytes() == sentinel
    import second_brain.config as config

    loaded = config.load_config(config_dir, offline=False, example=True)
    assert loaded.runtime.max_classify_batches == 21
    assert loaded.runtime.max_review_batches == 8
    assert loaded.runtime.offline is False
    assert [source.kind for source in loaded.sources] == [
        "rss",
        "rss",
        "podcast_rss",
        "youtube_rss",
    ]
    assert all(source.url.startswith("https://") for source in loaded.sources)
    assert all(source.file_format is None for source in loaded.sources)


def test_root_flag_beats_env_and_symlink_root_is_rejected(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    decoy = repo_root.parent / "decoy"
    decoy.mkdir()
    link = repo_root.parent / "linked-root"
    link.symlink_to(repo_root, target_is_directory=True)
    try:
        _use_root(monkeypatch, decoy)
        _offline_env(monkeypatch)
        _copy_tree("rss", repo_root)
        _link_fake(repo_root)
        _write_config(repo_root, BLOG, _runtime())
        monkeypatch.chdir(repo_root.parent)

        monkeypatch.setenv("SECOND_BRAIN_ROOT", str(link))
        code, out, err = _invoke(
            ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/sym.sqlite"],
            capsys,
        )
        assert code == 1
        assert out == ""
        assert err.strip() == "invalid_root"
        assert not (repo_root / "data").exists()
        assert not (decoy / "data").exists()

        monkeypatch.setenv("SECOND_BRAIN_ROOT", str(repo_root))
        code, out, err = _invoke(
            ["run", "--offline", "--now", "2030-01-15T00:00:00Z", "--db", "data/env.sqlite"],
            capsys,
        )
        assert code == 0, (code, out, err)
        assert (repo_root / "data" / "env.sqlite").is_file()
        assert not (decoy / "data").exists()

        monkeypatch.setenv("SECOND_BRAIN_ROOT", str(decoy))
        code, out, err = _invoke(
            [
                "run",
                "--offline",
                "--root",
                repo_root.name,
                "--now",
                "2030-01-15T00:00:00Z",
                "--db",
                "data/flag.sqlite",
            ],
            capsys,
        )
        assert code == 0, (code, out, err)
        assert (repo_root / "data" / "flag.sqlite").is_file()
        assert not (decoy / "data").exists()
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()
        if decoy.exists():
            shutil.rmtree(decoy)


def test_scope_contract() -> None:
    boundary = _boundary()
    missing = [relative for relative, _module in boundary.MODULES if not (SRC / relative).is_file()]
    assert missing == []
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    meta = project["project"]
    assert meta["dependencies"] == ["feedparser==6.0.14", "PyYAML==6.0.3"]
    assert meta["optional-dependencies"]["dev"] == ["pytest==9.2.0"]
    assert meta["scripts"] == {"second-brain": "second_brain.cli:main"}
    assert "license" not in meta
    subprocess_modules = []
    for relative, module in boundary.MODULES:
        tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
        imported = _imported(tree)
        if any(name == "subprocess" or name.startswith("subprocess.") for name in imported):
            subprocess_modules.append(module)
    assert subprocess_modules == ["second_brain.llm.grok_cli"]
    cli_text = (SRC / "second_brain" / "cli.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in cli_text
    assert "SystemExit(main())" in cli_text
    boundary.test_scope_contract()
    import second_brain.config as config

    assert config.PROJECT_ROOT == REPO.resolve()
