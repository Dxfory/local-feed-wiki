"""Stdlib chat-completions adapter, with no socket opened.

Breaks if a missing credential or base URL still builds a request, if the
request is not the chat-completions JSON this project can forward, if the
HTTP response is not the stdout envelope the completion step parses, or if
``--help`` omits a flag the project passes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_ADAPTER = REPO / "scripts" / "adapters" / "chat_completions.py"
_KEY = "fixture-key-value"
_KEY_NAME = "FIXTURE_ENDPOINT_CREDENTIAL"
_INNER = '{"items":[]}'
_SCHEMA = '{"type":"object","additionalProperties":false}'


def _load():
    spec = importlib.util.spec_from_file_location("chat_completions_adapter", _ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Body:
    def __init__(self, body: bytes, status: int) -> None:
        self._body = body
        self.status = status
        self.closed = False

    def read(self, limit: int = -1) -> bytes:
        if limit is None or limit < 0:
            return self._body
        return self._body[:limit]

    def close(self) -> None:
        self.closed = True


class _Capture:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.calls: list[tuple[object, object]] = []
        self._body = body
        self._status = status

    def __call__(self, request: object, timeout: object) -> _Body:
        self.calls.append((request, timeout))
        return _Body(self._body, self._status)


def _prompt(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.json"
    document = {"type": "acp", "content": [{"type": "text", "text": _INNER}]}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _argv(prompt: Path) -> list[str]:
    from second_brain.llm.grok_cli import compatible_argv

    built = compatible_argv(
        "adapter",
        prompt.parent,
        _SCHEMA,
        prompt,
        help_text=None,
    )
    return built[1:]


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECOND_BRAIN_LLM_BASE", "https://example.com/v1/")
    monkeypatch.setenv("SECOND_BRAIN_LLM_MODEL", "demo-model")
    monkeypatch.setenv("SECOND_BRAIN_LLM_KEY_ENV", _KEY_NAME)
    monkeypatch.setenv(_KEY_NAME, _KEY)


def _http_body() -> bytes:
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": _INNER},
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    return json.dumps(payload).encode("utf-8")


def test_missing_credential_sends_no_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _load()
    prompt = _prompt(tmp_path)
    capture = _Capture(_http_body())
    cases = (
        {},
        {"SECOND_BRAIN_LLM_KEY_ENV": _KEY_NAME},
        {"SECOND_BRAIN_LLM_KEY_ENV": _KEY_NAME, _KEY_NAME: ""},
    )
    for extra in cases:
        monkeypatch.setenv("SECOND_BRAIN_LLM_BASE", "https://example.com/v1")
        monkeypatch.setenv("SECOND_BRAIN_LLM_MODEL", "demo-model")
        monkeypatch.delenv("SECOND_BRAIN_LLM_KEY_ENV", raising=False)
        monkeypatch.delenv(_KEY_NAME, raising=False)
        for name, value in extra.items():
            monkeypatch.setenv(name, value)
        code = adapter.main(_argv(prompt), opener=capture)
        out, err = capsys.readouterr()
        assert code != 0
        assert out == ""
        assert err.strip() == "missing_credential"
        assert _KEY not in out + err
    assert capture.calls == []


def test_missing_base_sends_no_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _load()
    prompt = _prompt(tmp_path)
    capture = _Capture(_http_body())
    monkeypatch.delenv("SECOND_BRAIN_LLM_BASE", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_LLM_MODEL", "demo-model")
    monkeypatch.setenv("SECOND_BRAIN_LLM_KEY_ENV", _KEY_NAME)
    monkeypatch.setenv(_KEY_NAME, _KEY)
    code = adapter.main(_argv(prompt), opener=capture)
    out, err = capsys.readouterr()
    assert code != 0
    assert out == ""
    assert err.strip() == "missing_endpoint"
    assert _KEY not in out + err
    assert capture.calls == []


def test_missing_model_sends_no_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _load()
    prompt = _prompt(tmp_path)
    capture = _Capture(_http_body())
    monkeypatch.setenv("SECOND_BRAIN_LLM_BASE", "https://example.com/v1")
    monkeypatch.delenv("SECOND_BRAIN_LLM_MODEL", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_LLM_KEY_ENV", _KEY_NAME)
    monkeypatch.setenv(_KEY_NAME, _KEY)
    code = adapter.main(_argv(prompt), opener=capture)
    out, err = capsys.readouterr()
    assert code != 0
    assert out == ""
    assert err.strip() == "missing_model"
    assert capture.calls == []


def test_request_uses_named_credential_and_maps_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _load()
    prompt = _prompt(tmp_path)
    capture = _Capture(_http_body())
    _configured(monkeypatch)
    code = adapter.main(_argv(prompt), opener=capture)
    out, err = capsys.readouterr()
    assert code == 0, (code, out, err)
    assert err == ""
    assert _KEY not in out
    assert json.loads(out) == {
        "structuredOutput": {"items": []},
        "stopReason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    assert len(capture.calls) == 1
    request, timeout = capture.calls[0]
    assert isinstance(timeout, int) and 1 <= timeout <= 120
    assert request.full_url == "https://example.com/v1/chat/completions"  # type: ignore[attr-defined]
    assert request.get_method() == "POST"  # type: ignore[attr-defined]
    headers = {name.lower(): value for name, value in request.header_items()}  # type: ignore[attr-defined]
    assert headers["authorization"] == f"Bearer {_KEY}"
    assert headers["content-type"] == "application/json"
    assert _KEY.encode("utf-8") not in request.data  # type: ignore[attr-defined]
    body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
    assert body["model"] == "demo-model"
    assert body["messages"] == [{"role": "user", "content": _INNER}]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "completion",
            "strict": True,
            "schema": {"type": "object", "additionalProperties": False},
        },
    }


def test_redirect_does_not_produce_a_follow_request() -> None:
    import urllib.error

    adapter = _load()
    handler = adapter.RefuseRedirect()
    with pytest.raises(urllib.error.URLError) as caught:
        handler.redirect_request(None, None, 302, "moved", {}, "https://example.com/elsewhere")
    assert caught.value.reason == "redirect_refused"


def test_help_advertises_every_project_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from second_brain.llm.grok_cli import compatible_argv

    adapter = _load()
    code = adapter.main(["--help"])
    out, err = capsys.readouterr()
    assert code == 0
    assert err == ""
    full = compatible_argv(
        "adapter",
        Path("/tmp/empty-sandbox"),
        "{}",
        Path("/tmp/prompt.json"),
        help_text=None,
    )
    flags = [item for item in full if item.startswith("--")]
    assert flags
    for flag in flags:
        assert flag in out
