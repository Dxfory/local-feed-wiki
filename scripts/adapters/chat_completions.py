#!/usr/bin/env python3
"""Forward one prompt to a chat-completions-compatible HTTP endpoint.

SECOND_BRAIN_LLM_BASE is the endpoint root. SECOND_BRAIN_LLM_MODEL is the
model name sent in the JSON body. SECOND_BRAIN_LLM_KEY_ENV names another
environment variable that holds the credential. This program reads that
credential only to build a request header. It does not write the credential
to disk and does not print it. With no base URL, model name, or credential,
it exits non-zero and does not send a request.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

_TIMEOUT_S = 60
_LIMIT = 2 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SWITCHES = (
    "--no-auto-update",
    "--verbatim",
    "--no-plan",
    "--no-subagents",
    "--disable-web-search",
)


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx response is a failure. The credential is not sent to a second URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("redirect_refused")


def main(argv: Sequence[str] | None = None, *, opener: Callable[..., object] | None = None) -> int:
    """Speak the completion-binary flags and write one JSON envelope to stdout."""

    if argv is None:
        argv = sys.argv[1:]
    if isinstance(argv, (str, bytes)) or any(not isinstance(item, str) for item in argv):
        return _fail("invalid_invocation")
    parser = _parser()
    if len(argv) >= 1 and argv[0] == "--help":
        sys.stdout.write(parser.format_help())
        return 0
    try:
        args = parser.parse_args(list(argv))
    except argparse.ArgumentError:
        return _fail("invalid_invocation")
    endpoint = _endpoint(_text_env("SECOND_BRAIN_LLM_BASE"))
    if endpoint is None:
        return _fail("missing_endpoint")
    model = _text_env("SECOND_BRAIN_LLM_MODEL")
    if model is None or model != model.strip():
        return _fail("missing_model")
    credential = _credential()
    if credential is None:
        return _fail("missing_credential")
    if not isinstance(args.prompt_file, str) or args.prompt_file == "":
        return _fail("missing_prompt")
    prompt = _prompt_text(args.prompt_file)
    if prompt is None:
        return _fail("invalid_prompt")
    schema = _schema_object(args.json_schema)
    if schema is None:
        return _fail("invalid_schema")
    request = _request(endpoint, model, credential, prompt, schema)
    try:
        response = _transmit(request, opener)
    except urllib.error.URLError:
        return _fail("endpoint_unavailable")
    return _emit(response)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-completions",
        description="Forward one prompt to a chat-completions-compatible HTTP endpoint.",
        allow_abbrev=False,
        exit_on_error=False,
    )
    for flag in _SWITCHES:
        parser.add_argument(flag, action="store_true")
    parser.add_argument("--max-turns")
    parser.add_argument("--disallowed-tools")
    parser.add_argument("--deny", action="append")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--cwd")
    parser.add_argument("--json-schema")
    parser.add_argument("--prompt-file")
    return parser


def _text_env(name: str) -> str | None:
    value = os.environ.get(name)
    if not isinstance(value, str) or value == "":
        return None
    if any(char in value for char in "\r\n\x00"):
        return None
    return value


def _credential() -> str | None:
    name = _text_env("SECOND_BRAIN_LLM_KEY_ENV")
    if name is None or _NAME.fullmatch(name) is None:
        return None
    value = os.environ.get(name)
    if not isinstance(value, str) or value == "":
        return None
    if any(char in value for char in "\r\n\x00"):
        return None
    return value


def _endpoint(base: str | None) -> str | None:
    if base is None or base != base.strip():
        return None
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in {"http", "https"} or parts.hostname is None:
        return None
    if parts.username is not None or parts.password is not None or parts.fragment:
        return None
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    suffix = "/chat/completions"
    if not path.endswith(suffix):
        path = path + suffix
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _prompt_text(path: str) -> str | None:
    if "\x00" in path or os.path.islink(path):
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_LIMIT + 1)
    except OSError:
        return None
    if len(raw) > _LIMIT:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("type") != "acp":
        return None
    content = value.get("content")
    if not isinstance(content, list) or not content:
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    joined = "".join(parts)
    if joined == "":
        return None
    return joined


def _schema_object(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str) or raw == "":
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _request(
    endpoint: str,
    model: str,
    credential: str,
    prompt: str,
    schema: dict[str, object],
) -> urllib.request.Request:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "completion",
                "strict": True,
                "schema": schema,
            },
        },
    }
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + credential,
        },
        method="POST",
    )


def _transmit(request: urllib.request.Request, opener: Callable[..., object] | None) -> object:
    if opener is not None:
        return opener(request, _TIMEOUT_S)
    built = urllib.request.build_opener(RefuseRedirect)
    return built.open(request, timeout=_TIMEOUT_S)


def _emit(response: object) -> int:
    try:
        status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else None
        read = getattr(response, "read", None)
        if status != 200 or not callable(read):
            return _fail("endpoint_unavailable")
        raw = read(_LIMIT + 1)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > _LIMIT:
        return _fail("invalid_response")
    try:
        parsed = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _fail("invalid_response")
    payload = _payload(parsed)
    if payload is None:
        return _fail("invalid_response")
    usage = _usage(parsed)
    if usage is None:
        return _fail("invalid_response")
    envelope: dict[str, object] = {
        "structuredOutput": payload,
        "stopReason": "end_turn",
    }
    if usage:
        envelope["usage"] = usage
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


def _payload(parsed: object) -> dict[str, object] | None:
    if not isinstance(parsed, dict):
        return None
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    finish = choice.get("finish_reason", "stop")
    if finish != "stop":
        return None
    message = choice.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or content == "":
        return None
    try:
        value = json.loads(_unwrap_fence(content))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _unwrap_fence(text: str) -> str:
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return text
    opener = lines[0].strip()
    if opener not in {"```", "```json"}:
        return text
    if any("```" in line for line in lines[1:-1]):
        return text
    return "\n".join(lines[1:-1])


def _usage(parsed: dict[str, object]) -> dict[str, int] | None:
    raw = parsed.get("usage")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    mapped: dict[str, int] = {}
    pairs = (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"))
    for source, dest in pairs:
        if source not in raw or raw[source] is None:
            continue
        number = raw[source]
        if type(number) is not int or isinstance(number, bool) or number < 0:
            return None
        mapped[dest] = number
    return mapped


def _fail(code: str) -> int:
    sys.stderr.write(code + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
