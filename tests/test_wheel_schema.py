"""The built wheel must ship both schema files beside their modules.

The pytest tripwire blocks subprocess and sockets in this process. This test
starts a Python >=3.13 interpreter with os.posix_spawn, builds the local
wheel with pip --no-index, and installs that wheel into a throwaway venv.
The source tree is only the expected-byte oracle. The paths that are read
are the installed modules' neighbors, the same Path(__file__).with_name
expression the loaders use. A Python >=3.13 with no setuptools skips this
test. CI installs setuptools and runs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.local_process import run

REPO = Path(__file__).resolve().parents[1]
CLASSIFY_SCHEMA = REPO / "src" / "second_brain" / "classify" / "schema.json"
REVIEW_SCHEMA = REPO / "src" / "second_brain" / "review" / "schema.json"
_CHECK = r"""
import hashlib
import json
import sys
from pathlib import Path

import second_brain
import second_brain.classify.buckets as buckets
import second_brain.review.multi_source as review

def beside(module):
    return Path(module.__file__).resolve().with_name("schema.json")

classify_path = beside(buckets)
review_path = beside(review)
classify_bytes = classify_path.read_bytes()
review_bytes = review_path.read_bytes()
loaded = json.loads(classify_bytes.decode("utf-8"))
reviewed = json.loads(review_bytes.decode("utf-8"))
if loaded != buckets.CLASSIFICATION_SCHEMA or reviewed != review.REVIEW_SCHEMA:
    sys.stderr.write("schema_mismatch\n")
    raise SystemExit(1)
sys.stdout.write(json.dumps({
    "package": str(Path(second_brain.__file__).resolve()),
    "classify_module": str(Path(buckets.__file__).resolve()),
    "review_module": str(Path(review.__file__).resolve()),
    "classify_schema": str(classify_path),
    "review_schema": str(review_path),
    "classify_sha256": hashlib.sha256(classify_bytes).hexdigest(),
    "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
}))
"""


def _base_env(tmp: Path, *, pythonpath: str | None) -> dict[str, str]:
    env: dict[str, str] = {"PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1"}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env["TMPDIR"] = str(tmp)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return env


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


def _candidates() -> list[Path]:
    found: list[Path] = []
    if sys.version_info >= (3, 13):
        found.append(Path(sys.executable))
    discovered = shutil.which("python3.13")
    if discovered:
        found.append(Path(discovered))
    home = Path.home()
    found.append(home / ".local" / "bin" / "python3.13")
    root = home / ".local" / "share" / "uv" / "python"
    if root.is_dir():
        found.extend(sorted(root.glob("cpython-3.13*/bin/python3.13")))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        if not path.exists():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _python313(tmp: Path) -> Path:
    checked: list[str] = []
    saw_313 = False
    for path in _candidates():
        code, out, err = run(
            [str(path), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            cwd=tmp,
            env=_base_env(tmp, pythonpath=None),
        )
        checked.append(f"{path} -> {code} {out.strip()} {err.strip()}")
        if code != 0:
            continue
        parts = out.strip().split(".")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        if (int(parts[0]), int(parts[1])) < (3, 13):
            continue
        saw_313 = True
        probe, probe_out, probe_err = run(
            [str(path), "-c", "import setuptools"],
            cwd=tmp,
            env=_base_env(tmp, pythonpath=None),
        )
        if probe != 0:
            checked.append(f"{path} setuptools missing {probe_err.strip() or probe_out.strip()}")
            continue
        return path
    detail = "\n".join(checked)
    if saw_313:
        reason = "Python >=3.13 has no setuptools"
        if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(
                "CI must install setuptools and run "
                "test_installed_wheel_reads_schema_beside_module\n"
                + reason
                + ("\n" + detail if detail else "")
            )
        pytest.skip(reason)
    pytest.fail("no Python >=3.13\n" + detail)


def _assert_ok(code: int, out: str, err: str, step: str) -> None:
    assert code == 0, f"{step} exited {code}\nstdout:\n{out}\nstderr:\n{err}"


def test_installed_wheel_reads_schema_beside_module(tmp_path: Path) -> None:
    interpreter = _python313(tmp_path)
    guard = tmp_path / "guard"
    _guard(guard)
    ready = tmp_path / "guard-ready.txt"
    sockets = tmp_path / "sockets.txt"
    env = _base_env(tmp_path, pythonpath=str(guard))
    env["SECOND_BRAIN_GUARD_READY"] = str(ready)
    env["SECOND_BRAIN_SOCKET_LOG"] = str(sockets)
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    venv = tmp_path / "venv"
    work = tmp_path / "work"
    work.mkdir()
    before_eggs = {path.resolve() for path in REPO.rglob("*.egg-info")}
    before_build = (REPO / "build").exists()
    try:
        code, out, err = run(
            [
                str(interpreter),
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-index",
                "--no-build-isolation",
                "--disable-pip-version-check",
                "-w",
                str(wheel_dir),
                str(REPO),
            ],
            cwd=work,
            env=env,
        )
        _assert_ok(code, out, err, "pip wheel")
        wheels = sorted(wheel_dir.glob("*.whl"))
        assert len(wheels) == 1, wheels
        wheel = wheels[0]
        _assert_requires_python(wheel)
        code, out, err = run(
            [str(interpreter), "-m", "venv", str(venv)],
            cwd=work,
            env=env,
        )
        _assert_ok(code, out, err, "venv")
        venv_python = venv / "bin" / "python"
        assert venv_python.is_file()
        code, out, err = run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                str(wheel),
            ],
            cwd=work,
            env=env,
        )
        _assert_ok(code, out, err, "pip install")
        code, out, err = run(
            [str(venv_python), "-c", _CHECK],
            cwd=work,
            env=env,
        )
        _assert_ok(code, out, err, "import installed package")
    finally:
        for path in REPO.rglob("*.egg-info"):
            if path.resolve() not in before_eggs and path.is_dir():
                shutil.rmtree(path)
        build = REPO / "build"
        if build.exists() and not before_build:
            shutil.rmtree(build)
    assert ready.is_file(), "socket guard was not loaded in the build or install"
    assert not sockets.exists() or sockets.read_text(encoding="utf-8") == ""
    payload = json.loads(out)
    site = (venv / "lib").resolve()
    src = (REPO / "src").resolve()
    for key in ("package", "classify_module", "review_module", "classify_schema", "review_schema"):
        installed = Path(payload[key]).resolve()
        assert site in installed.parents, installed
        assert src not in installed.parents, installed
    assert Path(payload["classify_schema"]).name == "schema.json"
    assert Path(payload["review_schema"]).name == "schema.json"
    assert Path(payload["classify_schema"]).parent == Path(payload["classify_module"]).parent
    assert Path(payload["review_schema"]).parent == Path(payload["review_module"]).parent
    assert payload["classify_sha256"] == hashlib.sha256(CLASSIFY_SCHEMA.read_bytes()).hexdigest()
    assert payload["review_sha256"] == hashlib.sha256(REVIEW_SCHEMA.read_bytes()).hexdigest()


def _assert_requires_python(wheel: Path) -> None:
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        metadata = archive.read(name).decode("utf-8")
    assert "Requires-Python: >=3.13" in metadata
