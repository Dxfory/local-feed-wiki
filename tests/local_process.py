"""Start a local program without the pytest subprocess tripwire.

``tests/conftest.py`` replaces ``subprocess.*`` and only lets the real
``tests/fakes/grok`` file through. A wheel build and the smoke script are
local programs, so these tests use ``os.posix_spawn``. The child is a new
interpreter: the parent's tripwire is not copied. Callers block sockets in
the child and do not pass a network index.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
    """Run ``argv`` in ``cwd``. Return the exit code, stdout, and stderr."""

    if not argv or any(not isinstance(item, str) or item == "" for item in argv):
        raise AssertionError("empty argv")
    if not isinstance(cwd, Path) or not cwd.is_dir():
        raise AssertionError("cwd")
    folder = Path(tempfile.mkdtemp(prefix="sb-spawn-"))
    stdout_path = folder / "stdout.txt"
    stderr_path = folder / "stderr.txt"
    driver = folder / "driver.py"
    driver.write_text(
        "import os, sys\n"
        "os.chdir(sys.argv[1])\n"
        "os.execv(sys.argv[2], sys.argv[2:])\n",
        encoding="utf-8",
    )
    spawn_env = {
        key: value
        for key, value in env.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    pid = os.posix_spawn(
        sys.executable,
        [sys.executable, os.fspath(driver), os.fspath(cwd), *argv],
        spawn_env,
        file_actions=[
            (os.POSIX_SPAWN_OPEN, 0, "/dev/null", os.O_RDONLY, 0),
            (
                os.POSIX_SPAWN_OPEN,
                1,
                os.fspath(stdout_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o644,
            ),
            (
                os.POSIX_SPAWN_OPEN,
                2,
                os.fspath(stderr_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o644,
            ),
        ],
    )
    _pid, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    out = stdout_path.read_text(encoding="utf-8", errors="replace")
    err = stderr_path.read_text(encoding="utf-8", errors="replace")
    return code, out, err
