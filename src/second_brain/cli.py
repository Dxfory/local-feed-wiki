"""Assemble one offline or gated live run.

``python -m second_brain.cli`` and the ``second-brain`` script both call
``main``. The project root is ``--root``, then ``SECOND_BRAIN_ROOT``, then
``config.PROJECT_ROOT``. It does not follow the working directory unless
that override is relative.
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import second_brain.config as config
from second_brain.errors import (
    ConcurrentRun,
    ConfigError,
    FetchError,
    GrokUnavailable,
    PublishError,
    SchemaError,
    StorageError,
    UnsafeUrl,
)
from second_brain.http import HttpClient, StdlibTransport
from second_brain.llm.grok_cli import complete
from second_brain.models import CompletionResult, RunReport, RuntimePolicy, Source, parse_timestamp
from second_brain.run import run_once
from second_brain.store import Store

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOG_WARNING = "日志尚未写入"
_VALUE_FLAGS = {"--config-dir": "config_dir", "--db": "db", "--now": "now", "--root": "root"}
_USAGE = (
    "usage: python3 -m second_brain.cli run [--offline | --live] "
    "[--example-config] [--config-dir DIR] [--db PATH] [--now TIMESTAMP] [--root PATH]"
)
_DEMO = (
    "PYTHONPATH=src SECOND_BRAIN_GROK=tests/fakes/grok "
    "python3 -m second_brain.cli run --offline --config-dir examples/offline "
    "--now 2030-01-15T00:00:00Z --db data/second_brain.sqlite"
)


@dataclass(frozen=True, slots=True)
class _Request:
    offline: bool
    example: bool
    config_dir: str
    db: str
    now: datetime | None
    root: str | None


@dataclass(frozen=True, slots=True)
class _Paths:
    data: Path
    raw: Path
    wiki: Path
    sandbox: Path
    temp: Path


class _Clock:
    """Real UTC, or the instant passed to ``--now``."""

    def __init__(self, frozen: datetime | None) -> None:
        self._frozen = frozen

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. Return 0, 2, or 1.

    No subcommand, and ``--help``, print the usage line and the offline
    demo command, then return 0. An unknown command still returns 1.
    """

    if argv is None:
        argv = sys.argv[1:]
    try:
        if _is_help(argv):
            sys.stdout.write(_USAGE + "\n" + _DEMO + "\n")
            return 0
        return _run(_parse(argv))
    except (
        ConfigError,
        PublishError,
        StorageError,
        SchemaError,
        GrokUnavailable,
        UnsafeUrl,
        FetchError,
    ) as exc:
        _fail(_public_code(exc))
        return 1
    except ConcurrentRun:
        _fail("run_locked")
        return 1
    except OSError:
        _fail("io")
        return 1
    except Exception:
        _fail("run_failed")
        return 1


def _is_help(argv: Sequence[str]) -> bool:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        return False
    items = tuple(argv)
    if any(not isinstance(item, str) for item in items):
        return False
    return not items or "--help" in items


def _parse(argv: Sequence[str]) -> _Request:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise ConfigError("invalid_argv")
    items = tuple(argv)
    if any(not isinstance(item, str) for item in items):
        raise ConfigError("invalid_argv")
    if not items or items[0] != "run":
        raise ConfigError("unknown_command")
    offline_flag: bool | None = None
    example = False
    config_dir = "config"
    db = "data/second_brain.sqlite"
    now_text: str | None = None
    root_text: str | None = None
    config_dir_set = False
    index = 1
    while index < len(items):
        item = items[index]
        if item == "--offline":
            if offline_flag is False:
                raise ConfigError("conflicting_mode")
            offline_flag = True
        elif item == "--live":
            if offline_flag is True:
                raise ConfigError("conflicting_mode")
            offline_flag = False
        elif item == "--example-config":
            example = True
        elif item in _VALUE_FLAGS:
            index += 1
            if index >= len(items):
                raise ConfigError("missing_value")
            value = items[index]
            if value.startswith("-"):
                raise ConfigError("missing_value")
            slot = _VALUE_FLAGS[item]
            if slot == "config_dir":
                config_dir = value
                config_dir_set = True
            elif slot == "db":
                db = value
            elif slot == "root":
                root_text = value
            else:
                now_text = value
        else:
            raise ConfigError("unknown_argument")
        index += 1
    if example and config_dir_set:
        raise ConfigError("conflicting_config")
    offline = True if offline_flag is None else offline_flag
    if now_text is not None and not offline:
        raise ConfigError("now_requires_offline")
    frozen: datetime | None = None
    if now_text is not None:
        try:
            frozen = parse_timestamp(now_text)
        except SchemaError:
            raise ConfigError("invalid_now") from None
    return _Request(
        offline=offline,
        example=example,
        config_dir=config_dir,
        db=db,
        now=frozen,
        root=root_text,
    )


def _run(request: _Request) -> int:
    root = _root(request.root)
    loaded = config.load_config(
        _under(root, request.config_dir),
        offline=request.offline,
        example=request.example,
    )
    if loaded.runtime.offline and any(source.kind != "file" for source in loaded.sources):
        raise ConfigError("offline_network_source")
    binary = _select_binary(loaded.runtime, root)
    paths = _prepare(root)
    if any(paths.sandbox.iterdir()):
        raise ConfigError("sandbox_not_empty")
    db = _under(root, request.db)
    _ensure_parent(db)
    if db.is_symlink():
        raise ConfigError("symlink_escape")
    lock = paths.data / "run.lock"
    if lock.is_symlink():
        raise ConfigError("symlink_escape")
    clock = _Clock(request.now)
    store = Store.connect(os.fspath(db))
    try:
        http = _http(loaded.sources, clock, store)
        report = run_once(
            store,
            config=loaded,
            clock=clock,
            run_id_factory=_run_id,
            complete_fn=_completion(binary, loaded.runtime, paths.sandbox, paths.temp),
            wiki_dir=paths.wiki,
            raw_dir=paths.raw,
            lock_path=lock,
            http=http,
            fixture_reader=_fixture_reader(root),
        )
    finally:
        store.close()
    _write_report(report)
    return _exit_code(report)


def _root(override: str | None) -> Path:
    if override is not None:
        return _checked_root(override)
    env = os.environ.get("SECOND_BRAIN_ROOT")
    if isinstance(env, str) and env != "":
        return _checked_root(env)
    return _checked_root_path(config.PROJECT_ROOT)


def _checked_root(raw: str) -> Path:
    if "\x00" in raw or raw != raw.strip() or "\\" in raw:
        raise ConfigError("invalid_root")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.absolute()
    except OSError:
        raise ConfigError("invalid_root") from None
    return _checked_root_path(path)


def _checked_root_path(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConfigError("invalid_root")
    return root


def _under(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or relative == "" or "\x00" in relative or "\\" in relative:
        raise ConfigError("invalid_path")
    if relative.startswith("/"):
        raise ConfigError("invalid_path")
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ConfigError("invalid_path")
    path = root.joinpath(*parts)
    if not _inside(root, path):
        raise ConfigError("invalid_path")
    return path


def _inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare(root: Path) -> _Paths:
    return _Paths(
        data=_ensure_dir(root / "data", private=False),
        raw=_ensure_dir(root / "raw", private=False),
        wiki=_ensure_dir(root / "wiki", private=False),
        sandbox=_ensure_dir(root / ".grok-sandbox", private=True),
        temp=_ensure_dir(root / ".grok-tmp", private=True),
    )


def _ensure_dir(path: Path, *, private: bool) -> Path:
    if path.is_symlink():
        raise ConfigError("symlink_escape")
    if path.exists() and not path.is_dir():
        raise ConfigError("invalid_path")
    if not path.exists():
        path.mkdir(mode=0o700 if private else 0o755)
    if path.is_symlink() or not path.is_dir():
        raise ConfigError("invalid_path")
    if private:
        os.chmod(path, 0o700)
    return path


def _ensure_parent(path: Path) -> None:
    parent = path.parent
    if parent.is_symlink():
        raise ConfigError("symlink_escape")
    if parent.exists() and not parent.is_dir():
        raise ConfigError("invalid_path")
    if not parent.exists():
        parent.mkdir(parents=True)


def _select_binary(policy: RuntimePolicy, root: Path) -> str:
    raw = _grok_env()
    if policy.offline:
        if raw is None:
            raise ConfigError("grok_binary_required")
        return _offline_binary(_clean_binary(raw, "offline_grok_rejected"), root)
    _require_live(policy)
    if raw is not None:
        raw = _clean_binary(raw, "invalid_grok_binary")
    return _live_binary(raw, root)


def _grok_env() -> str | None:
    value = os.environ.get("SECOND_BRAIN_GROK")
    if not isinstance(value, str) or value == "":
        return None
    return value


def _clean_binary(raw: str, code: str) -> str:
    if "\x00" in raw or raw != raw.strip():
        raise ConfigError(code)
    return raw


def _require_live(policy: RuntimePolicy) -> None:
    if not policy.live_authorized:
        raise ConfigError("live_not_authorized")
    if policy.billing_path not in {"session", "api"} or not policy.billing_path_verified:
        raise ConfigError("billing_path_unverified")
    if not policy.tool_policy_verified:
        raise ConfigError("tool_policy_unverified")


def _binary_path(raw: str, root: Path) -> Path:
    if raw.startswith("~") or "\\" in raw:
        raise ConfigError("invalid_grok_binary")
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    parts = candidate.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ConfigError("invalid_grok_binary")
    return root.joinpath(*parts)


def _offline_binary(raw: str, root: Path) -> str:
    path = _binary_path(raw, root)
    fake = root / "tests" / "fakes" / "grok"
    if path.is_symlink() or fake.is_symlink():
        raise ConfigError("offline_grok_rejected")
    try:
        same = path.is_file() and fake.is_file() and os.path.samefile(path, fake)
    except OSError:
        same = False
    if not same:
        raise ConfigError("offline_grok_rejected")
    return os.fspath(path.resolve())


def _live_binary(raw: str | None, root: Path) -> str:
    if raw is None:
        raise ConfigError("grok_binary_required")
    path = _binary_path(raw, root)
    if path.is_symlink() or not path.is_file():
        raise ConfigError("invalid_grok_binary")
    return os.fspath(path.resolve())


def _http(sources: tuple[Source, ...], clock: _Clock, store: Store) -> HttpClient | None:
    if all(source.kind == "file" for source in sources):
        return None
    return HttpClient(StdlibTransport(), clock, store)


def _completion(binary: str, policy: RuntimePolicy, sandbox: Path, temp: Path):
    def complete_once(prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
        if purpose not in {"classify", "review"}:
            raise SchemaError("invalid_purpose")
        return complete(
            prompt,
            binary=binary,
            schema=schema,
            cwd=sandbox,
            temp_dir=temp,
            billing_path=policy.billing_path,
        )

    return complete_once


def _fixture_reader(root: Path):
    fixture_root = root / "tests" / "fixtures"

    def read(relative_uri: str) -> bytes:
        return config.read_fixture(relative_uri, fixture_root=fixture_root)

    return read


def _run_id() -> str:
    return uuid.uuid4().hex


def _write_report(report: RunReport) -> None:
    html = report.html_path if report.html_path is not None else "-"
    sys.stdout.write(
        f"status={report.status} classified={report.classified} "
        f"inserted={report.inserted} updated={report.updated} "
        f"pending={report.pending} deferred={report.deferred} html={html}\n"
    )
    for warning in report.warnings:
        sys.stderr.write(warning + "\n")


def _exit_code(report: RunReport) -> int:
    if report.status == "failed" or _LOG_WARNING in report.warnings:
        return 1
    if report.status == "partial":
        return 2
    if report.status == "success":
        return 0
    return 1


def _public_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return "run_failed"


def _fail(code: str) -> None:
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        code = "run_failed"
    sys.stderr.write(code + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
