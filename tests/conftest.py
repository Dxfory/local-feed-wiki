"""Process-wide tripwire for offline tests.

Blocks socket, DNS, urllib, and subprocess entry points before they leave
this process. Loopback is not a bypass. The only subprocess that may run is
the non-symlink path tests/fakes/grok, and only when that file exists.
"""

from __future__ import annotations

import builtins
import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_FAKE_GROK = ROOT / "tests" / "fakes" / "grok"
_ORIGINAL_OPEN = builtins.open
_ORIGINAL_OS_OPEN = os.open
_INSTALLED = False


class TripwireState:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.accepted = 0

    def record(self, kind: str) -> None:
        self.events.append(kind)

    def accept(self, count: int = 1) -> None:
        self.accepted += count


STATE = TripwireState()


class TripwireError(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"network tripwire: {kind}")


def _path_text(value: object) -> str:
    if isinstance(value, int):
        return ""
    try:
        return os.fspath(value)  # type: ignore[arg-type]
    except TypeError:
        return ""


def _is_secret_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    base = lowered.rsplit("/", 1)[-1]
    if lowered.endswith(".grok/auth.json"):
        return True
    if base in {"box-secrets.json", "gateway.json"}:
        return True
    return base.startswith("chrome-cookie")


def _guard_open(file, *args, **kwargs):
    text = _path_text(file)
    if text and _is_secret_path(text):
        STATE.record("open-secret")
        raise TripwireError("open-secret")
    return _ORIGINAL_OPEN(file, *args, **kwargs)


def _guard_os_open(path, flags, mode=0o777, *, dir_fd=None):
    text = _path_text(path)
    if text and _is_secret_path(text):
        STATE.record("open-secret")
        raise TripwireError("open-secret")
    if dir_fd is None:
        return _ORIGINAL_OS_OPEN(path, flags, mode)
    return _ORIGINAL_OS_OPEN(path, flags, mode, dir_fd=dir_fd)


def _block_network(kind: str):
    def blocked(*_args, **_kwargs):
        STATE.record(kind)
        raise TripwireError(kind)

    blocked.__tripwire__ = True  # type: ignore[attr-defined]
    blocked.__name__ = kind
    return blocked


def _argv0(args, kwargs) -> object:
    command = args[0] if args else kwargs.get("args")
    if isinstance(command, (str, bytes)):
        return command
    if isinstance(command, (list, tuple)) and command:
        return command[0]
    return None


def _fake_grok_allowed(args, kwargs) -> bool:
    if kwargs.get("shell"):
        return False
    argv0 = _argv0(args, kwargs)
    if not isinstance(argv0, str) or "/" not in argv0.replace("\\", "/"):
        return False
    try:
        if not _FAKE_GROK.is_file() or _FAKE_GROK.is_symlink():
            return False
        candidate = Path(argv0)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.is_symlink() or not candidate.is_file():
            return False
        return os.path.samefile(candidate, _FAKE_GROK)
    except OSError:
        return False


def _block_subprocess(kind: str, original):
    def blocked(*args, **kwargs):
        if _fake_grok_allowed(args, kwargs):
            return original(*args, **kwargs)
        STATE.record(kind)
        raise TripwireError(kind)

    blocked.__tripwire__ = True  # type: ignore[attr-defined]
    blocked.__name__ = kind
    return blocked


def _assign(obj, name: str, replacement) -> None:
    current = getattr(obj, name)
    if getattr(current, "__tripwire__", False):
        return
    setattr(obj, name, replacement)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _guard_open.__tripwire__ = True  # type: ignore[attr-defined]
    _guard_os_open.__tripwire__ = True  # type: ignore[attr-defined]
    builtins.open = _guard_open
    os.open = _guard_os_open
    _assign(socket.socket, "connect", _block_network("socket.connect"))
    _assign(socket.socket, "connect_ex", _block_network("socket.connect_ex"))
    _assign(socket, "create_connection", _block_network("socket.create_connection"))
    _assign(socket, "getaddrinfo", _block_network("socket.getaddrinfo"))
    _assign(urllib.request, "urlopen", _block_network("urllib.request.urlopen"))
    _assign(
        urllib.request.OpenerDirector,
        "open",
        _block_network("urllib.request.OpenerDirector.open"),
    )
    _assign(subprocess, "Popen", _block_subprocess("subprocess.Popen", subprocess.Popen))
    _assign(subprocess, "run", _block_subprocess("subprocess.run", subprocess.run))
    _assign(subprocess, "call", _block_subprocess("subprocess.call", subprocess.call))
    _assign(subprocess, "check_call", _block_subprocess("subprocess.check_call", subprocess.check_call))
    _assign(
        subprocess,
        "check_output",
        _block_subprocess("subprocess.check_output", subprocess.check_output),
    )
    _assign(os, "system", _block_network("os.system"))
    _assign(os, "popen", _block_network("os.popen"))
    _INSTALLED = True


def pytest_configure(config) -> None:
    install()


def pytest_collection_finish(session) -> None:
    if STATE.events:
        raise pytest.UsageError("import triggered tripwire: " + ",".join(STATE.events))


@pytest.fixture(autouse=True)
def tripwire():
    start = len(STATE.events)
    accepted = STATE.accepted
    yield STATE
    happened = STATE.events[start:]
    newly_accepted = STATE.accepted - accepted
    if len(happened) != newly_accepted:
        pytest.fail(f"unexpected tripwire events {happened!r}; accepted {newly_accepted}")
