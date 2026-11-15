"""GitHub Pages workflow contract and the offline sample site.

The suite cannot run GitHub Actions. It parses the workflow and executes the
sample-index check in this process. The assembler is a local program, started
with os.posix_spawn because the process tripwire blocks subprocess.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from tests.local_process import run

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "pages.yml"
SCRIPT = REPO / "scripts" / "build_sample_site.sh"
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_SECRET_WORDS = ("secrets", "github_token", "password", "api_key", "authorization")
_FOREIGN_LINKS = (
    "https://not-example.com/nope",
    "https://www.example.com/nope",
    "https://example.com.evil.test/nope",
    "https://example.com@not-example.com/nope",
)


def _text(path: Path) -> str:
    assert path.is_file(), path
    return path.read_text(encoding="utf-8")


def _workflow() -> dict[str, object]:
    text = _text(WORKFLOW)
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    # PyYAML 1.1 treats an unquoted on key as boolean true.
    if True in loaded and "on" not in loaded:
        loaded["on"] = loaded.pop(True)
    return loaded


def _job(name: str) -> dict[str, object]:
    loaded = _workflow()
    jobs = loaded["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[name]
    assert isinstance(job, dict)
    return job


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _heredoc_body(script: str, invocation: str) -> str:
    marker = invocation + " <<'PY'\n"
    start = script.find(marker)
    assert start >= 0, invocation
    start += len(marker)
    end = script.find("\nPY\n", start)
    assert end > start
    return script[start:end]


def _check_body() -> str:
    workflow_run = ""
    for step in _steps(_job("build")):
        if step.get("name") == "Check sample page links":
            run_text = step.get("run")
            assert isinstance(run_text, str)
            workflow_run = run_text
            break
    assert workflow_run, "check step missing"
    from_workflow = _heredoc_body(workflow_run, "python - _site/index.html")
    from_script = _heredoc_body(_text(SCRIPT), 'python3 - "$DEST/index.html"')
    assert from_workflow == from_script
    return from_workflow


def _exec_check(code: str, index: Path) -> None:
    previous = sys.argv
    sys.argv = ["check", os.fspath(index)]
    try:
        exec(compile(code, "check_sample_index.py", "exec"), {"__name__": "__check__"})
    finally:
        sys.argv = previous


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


def _hosts(text: str) -> set[str | None]:
    return {urlsplit(match.rstrip(".,")).hostname for match in _URL.findall(text)}


def test_pages_workflow_defers_enabling_pages() -> None:
    """Deploy stays on manual dispatch until an owner enables Pages."""

    text = _text(WORKFLOW)
    assert text.startswith("#")
    head, _sep, _rest = text.partition("\nname:")
    lowered = head.lower()
    assert "not enabled" in lowered
    assert "workflow_dispatch" in lowered
    assert "main" in lowered


def test_pages_workflow_triggers_are_pull_request_push_and_dispatch() -> None:
    """A missing trigger would skip the build on pull requests or on push."""

    triggers = _workflow()["on"]
    if isinstance(triggers, dict):
        names = set(triggers)
    else:
        assert isinstance(triggers, list)
        names = set(triggers)
    assert names == {"pull_request", "push", "workflow_dispatch"}


def test_build_job_permissions_are_contents_read_only() -> None:
    """Build must not receive write, pages, or id-token permission."""

    loaded = _workflow()
    assert loaded["permissions"] == {"contents": "read"}
    build = _job("build")
    assert build["permissions"] == {"contents": "read"}
    assert "if" not in build


def test_deploy_job_permissions_are_pages_and_id_token_only() -> None:
    """Any extra deploy permission, including contents, widens the token."""

    deploy = _job("deploy")
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["needs"] == "build"
    assert deploy["environment"] == "github-pages"


def test_deploy_job_runs_only_on_workflow_dispatch() -> None:
    """Push and pull_request must build without deploying."""

    deploy = _job("deploy")
    assert deploy["if"] == "github.event_name == 'workflow_dispatch'"
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"build", "deploy"}


def test_pages_workflow_does_not_reference_secrets() -> None:
    """A secrets context or a token env binding would ship credentials."""

    text = _text(WORKFLOW).lower()
    for word in _SECRET_WORDS:
        assert word not in text
    for job in (_job("build"), _job("deploy")):
        assert "secrets" not in job
        for step in _steps(job):
            assert "secrets" not in step
            assert "env" not in step or "secrets" not in step["env"]


def test_pages_workflow_pins_actions_and_runtime_deps() -> None:
    """Floating actions or a test dependency would change the sample build."""

    build_steps = _steps(_job("build"))
    deploy_steps = _steps(_job("deploy"))
    uses = [step["uses"] for step in build_steps if "uses" in step]
    assert uses == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/upload-pages-artifact@v3",
    ]
    assert [step["uses"] for step in deploy_steps if "uses" in step] == [
        "actions/deploy-pages@v4",
    ]
    setup = next(step for step in build_steps if step.get("uses") == "actions/setup-python@v5")
    with_setup = setup["with"]
    assert isinstance(with_setup, dict)
    assert with_setup["python-version"] == "3.13"
    upload = next(step for step in build_steps if step.get("uses") == "actions/upload-pages-artifact@v3")
    with_upload = upload["with"]
    assert isinstance(with_upload, dict)
    assert with_upload["path"] == "_site"
    install = next(step for step in build_steps if "pip install" in str(step.get("run", "")))
    install_run = install["run"]
    assert isinstance(install_run, str)
    pins = sorted(re.findall(r"\b[A-Za-z0-9_.-]+==[0-9.]+", install_run))
    assert pins == ["PyYAML==6.0.3", "feedparser==6.0.14"]
    assert "pytest" not in install_run
    assert "setuptools" not in install_run
    names = [step.get("name") for step in build_steps]
    assert names.index("Assemble the sample site") < names.index("Check sample page links")
    assert names.index("Check sample page links") < names.index("Upload Pages artifact")
    assemble = next(step for step in build_steps if step.get("name") == "Assemble the sample site")
    assert assemble["run"] == "scripts/build_sample_site.sh _site"
    assert _job("build")["runs-on"] == "ubuntu-latest"
    assert _job("deploy")["runs-on"] == "ubuntu-latest"
    assert len(deploy_steps) == 1
    assert deploy_steps[0]["uses"] == "actions/deploy-pages@v4"


def test_index_check_accepts_only_example_com_links(tmp_path: Path) -> None:
    """A foreign host, including a lookalike suffix, must fail the check."""

    code = _check_body()
    good = tmp_path / "good.html"
    good.write_text(
        "<!DOCTYPE html>\n"
        "<p>See example.com in prose.</p>\n"
        '<a href="https://example.com/north-ledger/noon-gate-shut">north</a>\n'
        '<a href="http://example.com/harbor-watch">harbor</a>\n',
        encoding="utf-8",
    )
    _exec_check(code, good)
    missing = tmp_path / "missing.html"
    with pytest.raises(SystemExit) as missing_exit:
        _exec_check(code, missing)
    assert missing_exit.value.code not in (0, None)
    for href in _FOREIGN_LINKS:
        page = tmp_path / "bad.html"
        page.write_text(
            '<a href="https://example.com/kept">kept</a>\n' f'<a href="{href}">nope</a>\n',
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as bad_exit:
            _exec_check(code, page)
        assert bad_exit.value.code not in (0, None)


def test_build_sample_site_writes_digest_index_and_wiki_pages(tmp_path: Path) -> None:
    """The assembler must publish the demo digest and the snapshot pages offline."""

    script = _text(SCRIPT)
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail\n" in script
    assert os.access(SCRIPT, os.X_OK)
    dest = tmp_path / "sample-site"
    _guard(tmp_path / "guard")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(tmp_path / "guard"),
        "SECOND_BRAIN_GUARD_READY": str(tmp_path / "guard-ready.txt"),
        "SECOND_BRAIN_SOCKET_LOG": str(tmp_path / "sockets.txt"),
    }
    for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    wiki_before = (REPO / "wiki").exists()
    data_before = (REPO / "data").exists()
    raw_before = (REPO / "raw").exists()
    code, out, err = run([os.fspath(SCRIPT), os.fspath(dest)], cwd=tmp_path, env=env)
    assert code == 0, f"stdout:\n{out}\nstderr:\n{err}"
    assert (REPO / "wiki").exists() == wiki_before
    assert (REPO / "data").exists() == data_before
    assert (REPO / "raw").exists() == raw_before
    index = dest / "index.html"
    assert index.is_file()
    assert not index.is_symlink()
    text = index.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>\n")
    assert "https://example.com/north-ledger/noon-gate-shut" in text
    assert "East dock says the noon gate stays open" in text
    assert "Ledger says the noon gate stays shut" in text
    assert _hosts(text) == {"example.com"}
    digests = list(dest.glob("digest/*.html"))
    assert len(digests) == 1
    assert digests[0].read_bytes() == index.read_bytes()
    opportunities = list(dest.glob("snapshots/*/opportunities/*.md"))
    assert len(opportunities) == 1
    comparison = opportunities[0].read_text(encoding="utf-8")
    assert "north-ledger" in comparison
    assert "harbor-watch" in comparison
    assert list(dest.glob("snapshots/*/topics/*.md"))
    index_md = (dest / "index.md").read_text(encoding="utf-8")
    assert "digest/" in index_md
    assert ".html" in index_md
    copied = [path for path in dest.rglob("*") if path.is_file()]
    assert copied
    for path in copied:
        assert not path.is_symlink()
        if path.suffix in {".html", ".md"}:
            assert _hosts(path.read_text(encoding="utf-8")) <= {"example.com"}
    assert not list(dest.rglob("*.sqlite"))
    assert not list(dest.rglob("*.json"))
    ready = tmp_path / "guard-ready.txt"
    assert ready.is_file()
    assert "ready" in ready.read_text(encoding="utf-8")
    sockets = tmp_path / "sockets.txt"
    assert not sockets.exists() or sockets.read_text(encoding="utf-8") == ""
    _exec_check(_check_body(), index)


def _assembler_body() -> str:
    return _heredoc_body(_text(SCRIPT), "python3 -")


def _minority_gate_source() -> str:
    body = _assembler_body()
    start = body.find("page = digest.read_text")
    end = body.find("opportunities = sorted(")
    assert start >= 0 and end > start
    gate = body[start:end]
    prefix = 'page = digest.read_text(encoding="utf-8")\n'
    assert gate.startswith(prefix)
    return gate[len(prefix) :]


def _gate_exit(page: str) -> object:
    source = _minority_gate_source()
    try:
        exec(compile(source, "minority_gate.py", "exec"), {"page": page, "sys": sys})
    except SystemExit as caught:
        return caught.code
    return None


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (
            "<h2>Minority view</h2>\n<ul><li>North gate: Minority candidate</li></ul>\n<h2>Notes</h2>\n",
            None,
        ),
        (
            "<h2>非共识</h2>\n<ul><li>北门：少数候选</li></ul>\n<h2>说明</h2>\n",
            None,
        ),
        (
            "<h2>Minority view</h2>\n<p>无</p>\n<p>Minority candidate</p>\n<h2>Notes</h2>\n",
            None,
        ),
        (
            "<h2>非共识</h2>\n<p>None</p>\n<p>少数候选</p>\n<h2>说明</h2>\n",
            None,
        ),
        (
            "<h2>Minority view</h2>\n<p>Minority candidate</p>\n<h2>非共识</h2>\n<p>无</p>\n",
            None,
        ),
        (
            "<h2>Minority view</h2>\n<p>None</p>\n<h2>Notes</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>Minority view</h2>\n<p>None</p>\n<p>Minority candidate</p>\n<h2>Notes</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>Minority view</h2>\n<ul><li>North gate</li></ul>\n<h2>Notes</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>Minority view</h2>\n<p>少数候选</p>\n<h2>Notes</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>非共识</h2>\n<p>无</p>\n<h2>说明</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>非共识</h2>\n<p>无</p>\n<p>少数候选</p>\n<h2>说明</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>非共识</h2>\n<ul><li>北门</li></ul>\n<h2>说明</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>非共识</h2>\n<p>Minority candidate</p>\n<h2>说明</h2>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>非共识</h2>\n<p>无</p>\n<h2>Minority view</h2>\n<p>Minority candidate</p>\n",
            "digest minority section is empty",
        ),
        (
            "<h2>Notes</h2>\n<p>Minority candidate</p>\n<p>少数候选</p>\n",
            "digest has no minority section",
        ),
        (
            "<h2>Minority view</h2>\n<p>Minority candidate</p>\n",
            "digest minority section is incomplete",
        ),
        (
            "<h2>非共识</h2>\n<p>少数候选</p>\n",
            "digest minority section is incomplete",
        ),
    ],
)
def test_minority_section_gate_accepts_either_language(page: str, expected: str | None) -> None:
    """The sample gate must take one heading language and keep the same three failures."""

    assert _gate_exit(page) == expected
