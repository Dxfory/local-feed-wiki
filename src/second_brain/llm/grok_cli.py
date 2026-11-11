"""Spawn the grok CLI and read one JSON envelope.

A live binary is probed with ``grok --help`` before the model argv runs.
The checked-in fake skips that probe. Stdout must be one object. A nonzero
model exit keeps only the first stderr line, after token redaction. The rest
of stderr is discarded. Failures raise GrokUnavailable. The message stays a
safe code.
"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from second_brain.errors import GrokUnavailable, SchemaError
from second_brain.models import (
    GROK_ARGV_BEFORE_CWD,
    GROK_ARGV_DYNAMIC_FLAGS,
    GROK_MODEL,
    GROK_OUTPUT_LIMIT_BYTES,
    GROK_TIMEOUT_S,
    CompletionResult,
    Usage,
    canonical_json,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BILLING = frozenset({"test", "session", "api", "unknown"})
_USAGE_INTS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_PROXY_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})
_ENV_SUFFIXES = ("_API_KEY", "_API_BASE", "_BASE_URL")
_FENCE_OPENERS = frozenset({"```", "```json"})
_OPTIONAL_FLAG = "--no-auto-update"
# grok 1.0.46 documents 1 USD = 10**10 integer ticks on the json envelope.
_USD_TICKS = 10_000_000_000
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LONG_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z0-9][A-Za-z0-9-]*)\b")
# A quoted value runs through its closer, or to the end of the line when the
# closer is missing. An unquoted value stops at whitespace or a delimiter.
_VALUE = r"""(?:"[^"]*(?:"|$)|'[^']*(?:'|$)|[^\s"',;]+)"""
_AUTHORIZATION = re.compile(
    rf"(?i)\bauthorization\b\s*[:=]\s*(?:(?:bearer|basic|digest|negotiate|ntlm)\s+\S+|{_VALUE})"
)
_ASSIGNMENT = re.compile(
    rf"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    rf"credential|key)\b\s*[:=]\s*{_VALUE}"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_PREFIXED = re.compile(
    r"(?i)\b(?:sk|xai|ghp|gho|github_pat|xox[baprs]|ya29|AIza)[-_][A-Za-z0-9_\-]{6,}\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{5,}){1,2}\b")
_HEX = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
_ALLCAPS = re.compile(r"\b[A-Z0-9]{10,}\b")
_MIXED = re.compile(
    r"\b(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}\b"
)
_SECRET_PATTERNS = (
    _AUTHORIZATION,
    _BEARER,
    _PREFIXED,
    _JWT,
    _HEX,
    _ALLCAPS,
    _MIXED,
    _ASSIGNMENT,
)


def complete(
    prompt: str,
    *,
    binary: str,
    model: str = GROK_MODEL,
    schema: dict[str, object],
    cwd: Path,
    temp_dir: Path,
    timeout_s: int = GROK_TIMEOUT_S,
    billing_path: str = "unknown",
) -> CompletionResult:
    """Run one locked grok invocation and return its payload.

    ``cwd`` is the empty ``.grok-sandbox`` working directory. The prompt file
    is written under ``.grok-tmp`` and removed before this function returns.
    ``--json-schema`` receives the schema JSON text. grok 1.0.46 parses that
    argument as JSON, not as a file path. ``billing_path`` is recorded as given.
    """

    prompt_bytes = _prompt_bytes(prompt)
    schema_text = _schema_text(schema)
    _check_model(model)
    _check_timeout(timeout_s)
    _check_billing(billing_path)
    binary_text = _binary(binary)
    sandbox = _sandbox(cwd)
    temp = _temp(temp_dir, sandbox)
    stamp = f"{os.getpid()}-{time.monotonic_ns()}"
    prompt_path = temp / f"prompt-{stamp}.json"
    env = _child_env()
    # Probe in the temp directory so a help process cannot fill the empty sandbox.
    help_text = _help_for(binary_text, temp, env, timeout_s)
    argv = compatible_argv(
        binary_text,
        sandbox,
        schema_text,
        prompt_path,
        help_text=help_text,
    )
    if help_text is not None and missing_cli_flags(help_text, argv):
        raise GrokUnavailable("unsupported_flag")
    created: list[Path] = []
    try:
        try:
            os.chmod(temp, 0o700)
        except OSError:
            raise GrokUnavailable("invalid_temp_dir") from None
        if not _is_empty(sandbox):
            raise GrokUnavailable("sandbox_not_empty")
        try:
            _write_private(prompt_path, prompt_bytes)
            created.append(prompt_path)
        except OSError:
            raise GrokUnavailable("spawn_failed") from None
        if not _is_empty(sandbox):
            raise GrokUnavailable("sandbox_not_empty")
        code, stdout, stderr = _spawn(argv, sandbox, env, timeout_s)
        return _interpret(code, stdout, billing_path, stderr_error_detail(stderr))
    finally:
        for path in created:
            try:
                os.unlink(path)
            except OSError:
                pass


def _prompt_bytes(prompt: object) -> bytes:
    """Encode the ``.json`` body passed to ``--prompt-file``.

    grok 1.0.46 treats a ``.json`` file as ACP content blocks. The original
    prompt is the text of one block, inside ``{"type":"acp","content":[...]}``.
    """

    if not isinstance(prompt, str) or "\x00" in prompt:
        raise GrokUnavailable("invalid_prompt")
    document = {
        "type": "acp",
        "content": [{"type": "text", "text": prompt}],
    }
    try:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        raise GrokUnavailable("invalid_prompt") from None


def _schema_text(schema: object) -> str:
    if not isinstance(schema, dict):
        raise GrokUnavailable("invalid_schema")
    try:
        return canonical_json(schema)
    except SchemaError:
        raise GrokUnavailable("invalid_schema") from None


def _check_model(model: object) -> None:
    if model != GROK_MODEL:
        raise GrokUnavailable("invalid_model")


def _check_timeout(timeout_s: object) -> None:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s < 1:
        raise GrokUnavailable("invalid_timeout")


def _check_billing(billing_path: object) -> None:
    if not isinstance(billing_path, str) or billing_path not in _BILLING:
        raise GrokUnavailable("invalid_billing_path")


def _binary(binary: object) -> str:
    if not isinstance(binary, str) or binary == "":
        raise GrokUnavailable("invalid_binary")
    raw = Path(binary)
    if raw.is_symlink():
        raise GrokUnavailable("invalid_binary")
    absolute = Path(os.path.abspath(raw))
    if absolute.is_symlink() or not absolute.is_file():
        raise GrokUnavailable("invalid_binary")
    return os.fspath(absolute)


def _directory(value: object, code: str) -> Path:
    if isinstance(value, Path):
        raw = value
    elif isinstance(value, str) and value != "":
        raw = Path(value)
    else:
        raise GrokUnavailable(code)
    if raw.is_symlink():
        raise GrokUnavailable(code)
    absolute = Path(os.path.abspath(raw))
    if absolute.is_symlink() or not absolute.is_dir():
        raise GrokUnavailable(code)
    return absolute


def _sandbox(cwd: object) -> Path:
    path = _directory(cwd, "invalid_sandbox")
    if path.name != ".grok-sandbox":
        raise GrokUnavailable("invalid_sandbox")
    root = os.path.abspath(_PROJECT_ROOT)
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(root):
        raise GrokUnavailable("invalid_sandbox")
    if not _is_empty(path):
        raise GrokUnavailable("sandbox_not_empty")
    return path


def _temp(value: object, sandbox: Path) -> Path:
    path = _directory(value, "invalid_temp_dir")
    if path.name != ".grok-tmp":
        raise GrokUnavailable("invalid_temp_dir")
    if path == sandbox or _contains(path, sandbox) or _contains(sandbox, path):
        raise GrokUnavailable("invalid_temp_dir")
    return path


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return child != parent


def _is_empty(path: Path) -> bool:
    with os.scandir(path) as entries:
        for _entry in entries:
            return False
    return True


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(
        os.fspath(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _flags_in_help(help_text: str) -> frozenset[str]:
    """Return long options advertised by injected ``grok --help`` text."""

    if not isinstance(help_text, str):
        return frozenset()
    return frozenset(_LONG_FLAG.findall(_ANSI.sub("", help_text)))


def missing_cli_flags(help_text: str, argv: Sequence[str]) -> tuple[str, ...]:
    """Flags in ``argv`` that the injected help text does not advertise."""

    supported = _flags_in_help(help_text)
    missing: list[str] = []
    for item in argv:
        if not isinstance(item, str) or not item.startswith("-"):
            continue
        if item not in supported and item not in missing:
            missing.append(item)
    return tuple(missing)


def compatible_argv(
    binary: str,
    sandbox: Path,
    schema_text: str,
    prompt_path: Path,
    *,
    help_text: str | None,
) -> list[str]:
    """Build the model argv.

    ``schema_text`` is the JSON Schema document. grok 1.0.46 rejects a file
    path in this slot (``invalid JSON`` at column 1). ``help_text is None``
    keeps the frozen prefix, including ``--no-auto-update``. A help text that
    does not list that flag omits it. Every other frozen flag stays. This
    never adds ``--auto-update``.
    """

    static: tuple[str, ...] = GROK_ARGV_BEFORE_CWD
    if help_text is not None and _OPTIONAL_FLAG not in _flags_in_help(help_text):
        static = tuple(item for item in static if item != _OPTIONAL_FLAG)
    flags = GROK_ARGV_DYNAMIC_FLAGS
    return [
        binary,
        *static,
        flags[0],
        os.fspath(sandbox),
        flags[1],
        schema_text,
        flags[2],
        os.fspath(prompt_path),
    ]


def redact_cli_detail(text: str) -> str:
    """Replace tokens, full authorization values, and quoted secrets."""

    if not isinstance(text, str) or text == "":
        return ""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def stderr_error_detail(data: bytes) -> str | None:
    """Redacted first stderr line. Later lines are not returned."""

    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    line = bytes(data).split(b"\n", 1)[0].split(b"\r", 1)[0][:2048]
    cleaned = "".join(
        char
        for char in line.decode("utf-8", errors="replace")
        if ord(char) >= 32 and ord(char) != 127
    ).strip()
    if cleaned == "":
        return None
    redacted = redact_cli_detail(cleaned).strip()
    if len(redacted) > 500:
        redacted = redacted[:500].rstrip()
    if redacted == "":
        return None
    return redacted


def _is_project_fake(binary: str) -> bool:
    fake = _PROJECT_ROOT / "tests" / "fakes" / "grok"
    try:
        if fake.is_symlink() or not fake.is_file():
            return False
        if Path(binary).is_symlink():
            return False
        return os.path.samefile(binary, fake)
    except OSError:
        return False


def _help_body(stdout: bytes, stderr: bytes) -> str:
    text = stdout.decode("utf-8", errors="replace")
    if "--" in text:
        return text
    err = stderr.decode("utf-8", errors="replace")
    if "--" in err:
        return err
    return text


def _read_help(binary: str, cwd: Path, env: dict[str, str], timeout_s: int) -> str:
    try:
        code, stdout, stderr = _spawn(
            [binary, "--help"],
            cwd,
            env,
            timeout_s,
            stderr_mode="full",
        )
    except GrokUnavailable:
        raise GrokUnavailable("help_check_failed") from None
    if code != 0:
        raise GrokUnavailable("help_check_failed")
    return _help_body(stdout, stderr)


def _help_for(binary: str, cwd: Path, env: dict[str, str], timeout_s: int) -> str | None:
    if _is_project_fake(binary):
        return None
    return _read_help(binary, cwd, env, timeout_s)


def _drop_env(key: str) -> bool:
    upper = key.upper()
    if upper in _PROXY_NAMES:
        return True
    return upper.endswith(_ENV_SUFFIXES)


def _child_env() -> dict[str, str]:
    # Drop API keys, proxy variables, and base-URL overrides on every path,
    # including api. An ambient key must not choose a billing path.
    cleaned: dict[str, str] = {}
    for key, value in os.environ.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if _drop_env(key):
            continue
        cleaned[key] = value
    return cleaned


def _spawn(
    argv: list[str],
    sandbox: Path,
    env: dict[str, str],
    timeout_s: int,
    *,
    stderr_mode: str = "line",
) -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.fspath(sandbox),
            env=env,
            shell=False,
            start_new_session=True,
            bufsize=0,
        )
    except OSError:
        raise GrokUnavailable("spawn_failed") from None
    try:
        return _collect(proc, timeout_s, stderr_mode)
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if proc.poll() is None:
            _kill_group(proc)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.kill()
        except OSError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=5)


def _collect(
    proc: subprocess.Popen[bytes],
    timeout_s: int,
    stderr_mode: str,
) -> tuple[int, bytes, bytes]:
    stdout = proc.stdout
    stderr = proc.stderr
    if stdout is None or stderr is None:
        raise GrokUnavailable("spawn_failed")
    stdout_fd = stdout.fileno()
    stderr_fd = stderr.fileno()
    os.set_blocking(stdout_fd, False)
    os.set_blocking(stderr_fd, False)
    buffers = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    open_fds = [stdout_fd, stderr_fd]
    deadline = time.monotonic() + timeout_s
    reason: str | None = None
    while open_fds and reason is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "timeout"
            break
        try:
            readable, _, _ = select.select(open_fds, [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            continue
        for fd in readable:
            if reason is not None:
                break
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except InterruptedError:
                continue
            except OSError:
                if fd in open_fds:
                    open_fds.remove(fd)
                continue
            if chunk == b"":
                if fd in open_fds:
                    open_fds.remove(fd)
                continue
            bucket = buffers[fd]
            if len(bucket) + len(chunk) > GROK_OUTPUT_LIMIT_BYTES:
                reason = "output_limit"
                break
            bucket.extend(chunk)
    if reason is not None:
        _kill_group(proc)
        raise GrokUnavailable(reason)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise GrokUnavailable("spawn_failed") from None
    # Line mode returns only the first stderr line. The remainder is discarded
    # here and is not logged. Full mode is only for the no-quota help probe.
    if stderr_mode == "line":
        stderr_bytes = bytes(buffers[stderr_fd]).split(b"\n", 1)[0].split(b"\r", 1)[0]
    else:
        stderr_bytes = bytes(buffers[stderr_fd])
    buffers[stderr_fd].clear()
    return code, bytes(buffers[stdout_fd]), stderr_bytes


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate")
        obj[key] = value
    return obj


def _reject_constant(_name: str) -> object:
    raise ValueError("constant")


def _loads(text: str) -> object:
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        raise GrokUnavailable("invalid_json") from None


def _unwrap_fence(text: str) -> str:
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    if lines[0].strip() not in _FENCE_OPENERS:
        return text
    if lines[-1].strip() != "```":
        return text
    if any("```" in line for line in lines[1:-1]):
        return text
    return "\n".join(lines[1:-1])


def _ticks_to_usd(ticks: int) -> str:
    """Exact decimal for ``total_cost_usd_ticks``. Trailing zeros are dropped."""

    whole, frac = divmod(ticks, _USD_TICKS)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:010d}".rstrip("0")


def _envelope_cost(envelope: dict[str, object]) -> str | None:
    """Cost from the json envelope, outside ``usage``.

    grok 1.0.46 puts a float ``total_cost_usd`` beside integer
    ``total_cost_usd_ticks``. Only the ticks are exact. A float alone stays
    unknown rather than being stored as a rounded string.
    """

    ticks = envelope.get("total_cost_usd_ticks")
    if type(ticks) is int and ticks >= 0:
        return _ticks_to_usd(ticks)
    return None


def _usage_from(envelope: dict[str, object]) -> tuple[Usage | None, bool]:
    if "usage" not in envelope or envelope["usage"] is None:
        try:
            return Usage(total_cost_usd=_envelope_cost(envelope)), False
        except SchemaError:
            return Usage(), True
    raw = envelope["usage"]
    if not isinstance(raw, dict):
        return None, True
    ints: dict[str, int | None] = {}
    invalid = False
    for name in _USAGE_INTS:
        if name not in raw or raw[name] is None:
            ints[name] = None
            continue
        number = raw[name]
        if type(number) is int and number >= 0:
            ints[name] = number
            continue
        invalid = True
        ints[name] = None
    cost: str | None
    if "total_cost_usd" not in raw:
        cost = _envelope_cost(envelope)
    elif raw["total_cost_usd"] is None:
        cost = None
    elif isinstance(raw["total_cost_usd"], str):
        cost = raw["total_cost_usd"]
    else:
        invalid = True
        cost = None
    try:
        usage = Usage(
            input_tokens=ints["input_tokens"],
            output_tokens=ints["output_tokens"],
            reasoning_tokens=ints["reasoning_tokens"],
            cache_read_input_tokens=ints["cache_read_input_tokens"],
            cache_creation_input_tokens=ints["cache_creation_input_tokens"],
            total_cost_usd=cost,
        )
    except SchemaError:
        invalid = True
        usage = Usage(
            input_tokens=ints["input_tokens"],
            output_tokens=ints["output_tokens"],
            reasoning_tokens=ints["reasoning_tokens"],
            cache_read_input_tokens=ints["cache_read_input_tokens"],
            cache_creation_input_tokens=ints["cache_creation_input_tokens"],
            total_cost_usd=None,
        )
    return usage, invalid


def _stop_reason(envelope: dict[str, object], usage: Usage | None) -> str | None:
    if "stopReason" not in envelope or envelope["stopReason"] is None:
        return None
    if envelope["stopReason"] == "end_turn":
        return "end_turn"
    raise GrokUnavailable("unexpected_stop_reason", usage)


def _payload_from(envelope: dict[str, object], usage: Usage | None) -> dict[str, object]:
    # grok 1.0.46 names the schema object structuredOutput. The snake_case
    # field is accepted only when the camelCase field is absent. Null and ""
    # are absent. Any other non-object is an error, with no text fallback.
    if "structuredOutput" in envelope or "structured_output" in envelope:
        raw = (
            envelope["structuredOutput"]
            if "structuredOutput" in envelope
            else envelope["structured_output"]
        )
        if raw not in (None, ""):
            if not isinstance(raw, dict):
                raise GrokUnavailable("invalid_structured_output", usage)
            return raw
    text = envelope.get("text")
    if not isinstance(text, str):
        raise GrokUnavailable("invalid_envelope", usage)
    try:
        parsed = _loads(_unwrap_fence(text))
    except GrokUnavailable as exc:
        raise GrokUnavailable(exc.code, usage) from None
    if not isinstance(parsed, dict):
        raise GrokUnavailable("invalid_payload", usage)
    return parsed


def _interpret(
    code: int,
    stdout: bytes,
    billing_path: str,
    detail: str | None = None,
) -> CompletionResult:
    def fail(name: str, usage: Usage | None = None) -> NoReturn:
        exc = GrokUnavailable(name, usage)
        if code != 0 and isinstance(detail, str) and detail != "":
            exc.error_detail = detail
        raise exc

    try:
        text = stdout.decode("utf-8")
    except UnicodeError:
        fail("nonzero_exit" if code else "invalid_json")
    try:
        value = _loads(text)
    except GrokUnavailable:
        if code:
            fail("nonzero_exit")
        raise
    if not isinstance(value, dict):
        fail("nonzero_exit" if code else "invalid_envelope")
    usage, invalid = _usage_from(value)
    if value.get("type") == "error":
        fail("cli_error", usage)
    if code != 0:
        fail("nonzero_exit", usage)
    if invalid or not isinstance(usage, Usage):
        fail("invalid_usage", usage)
    stop_reason = _stop_reason(value, usage)
    payload = _payload_from(value, usage)
    try:
        return CompletionResult(
            payload=payload,
            usage=usage,
            stop_reason=stop_reason,
            billing_path=billing_path,  # type: ignore[arg-type]
        )
    except SchemaError:
        fail("invalid_payload", usage)
