"""HTTP guard, one-hop transport, and per-host circuit tests.

No test opens a socket. Transports and clocks are fakes, except the stdlib
transport tests, which use an injected connection whose socket is already
attached.
"""

from __future__ import annotations

import io
import json
import ssl
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import formatdate

import http.client
import pytest

from second_brain.errors import FetchError, RateLimited, UnsafeUrl
from second_brain.http import HttpClient, StdlibTransport, policy_interval, rate_limit_code
from second_brain.models import (
    HTTP_CIRCUIT_OPEN_S,
    HTTP_MAX_BYTES,
    HTTP_MAX_REDIRECTS,
    HTTP_MIN_INTERVAL_S,
    HTTP_TIMEOUT_S,
    USER_AGENT,
    BILIBILI_COMMUNITY_CODES,
    BILIBILI_HOSTS,
    BILIBILI_INTERVAL_S,
    FetchEvent,
    HttpResponse,
    RateState,
    canonical_url,
    hash_url,
    parse_timestamp,
    redact_url,
)
from second_brain.urlguard import guard_public_url

SENTINEL = "SUPERSECRET"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ELLIPSIS = "\u2026"


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError(seconds)
        self.sleeps.append(float(seconds))
        if len(self.sleeps) > 20:
            raise AssertionError("clock slept too many times")
        self._now += timedelta(seconds=float(seconds))

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class MemoryState:
    def __init__(self) -> None:
        self.rates: dict[str, RateState] = {}
        self.events: list[FetchEvent] = []

    def get_rate_state(self, host: str) -> RateState:
        return self.rates.get(host, RateState(host=host))

    def put_rate_state(self, state: RateState) -> None:
        self.rates[state.host] = state

    def append_fetch_event(self, event: FetchEvent) -> None:
        self.events.append(event)


class ScriptTransport:
    def __init__(self, scripts: list[object]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: int,
        max_bytes: int,
        purpose: str,
    ) -> HttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout_s": timeout_s,
                "max_bytes": max_bytes,
                "purpose": purpose,
            }
        )
        if not self.scripts:
            raise AssertionError(f"unexpected request {url}")
        action = self.scripts.pop(0)
        if isinstance(action, BaseException):
            raise action
        status, response_headers, body = action
        return HttpResponse(status=status, body=body, headers=response_headers, url=url)


class StreamBody:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes, *, explode: bool = False) -> None:
        self.status = status
        self.headers = dict(headers)
        self._body = body
        self._pos = 0
        self.explode = explode
        self.reads = 0
        self.closed = False

    def read(self, amount: int) -> bytes:
        self.reads += 1
        if self.explode:
            raise AssertionError("body was read")
        chunk = self._body[self._pos : self._pos + amount]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class EndlessBody:
    def __init__(self, status: int, headers: Mapping[str, str]) -> None:
        self.status = status
        self.headers = dict(headers)
        self.reads = 0
        self.bytes_out = 0
        self.closed = False

    def read(self, amount: int) -> bytes:
        self.reads += 1
        if self.reads > 80:
            raise AssertionError("stream was not capped")
        self.bytes_out += amount
        return b"x" * amount

    def close(self) -> None:
        self.closed = True


class MarkerIO(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.max_pos = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(-1 if size is None else size)
        self.max_pos = max(self.max_pos, self.tell())
        return chunk

    def readline(self, size: int | None = -1) -> bytes:
        line = super().readline(-1 if size is None else size)
        self.max_pos = max(self.max_pos, self.tell())
        return line


class FakeSock:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.sent = bytearray()
        self.fp: MarkerIO | None = None

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def makefile(self, mode: str = "rb", buffering: int | None = None, **_kwargs: object) -> MarkerIO:
        self.fp = MarkerIO(self.payload)
        return self.fp

    def close(self) -> None:
        return None

    def settimeout(self, value: float | None) -> None:
        return None


def _client(
    transport: object,
    *,
    clock: FakeClock | None = None,
    store: MemoryState | None = None,
    min_interval_s: float = HTTP_MIN_INTERVAL_S,
) -> tuple[HttpClient, FakeClock, MemoryState]:
    clock = clock or FakeClock()
    store = store or MemoryState()
    client = HttpClient(transport, clock, store, min_interval_s=min_interval_s)
    return client, clock, store


def _ok(body: bytes = b"<rss></rss>", content_type: str = "application/xml") -> tuple[int, dict[str, str], bytes]:
    return (200, {"Content-Type": content_type}, body)


def _assert_clean(event: FetchEvent, sentinel: str = SENTINEL) -> None:
    blob = repr(event)
    assert sentinel not in blob
    assert "auth.json" not in blob
    for value in (
        event.source_id,
        event.host,
        event.url_redacted,
        event.url_hash,
        event.error_code,
        event.at,
        event.status,
        event.retry_after_s,
    ):
        assert sentinel not in str(value)


def _raise(code: str, url: str) -> str:
    with pytest.raises(UnsafeUrl) as caught:
        guard_public_url(url)
    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in repr(caught.value)
    assert caught.value.code == code
    return caught.value.code


def test_url_guard_matrix(tripwire) -> None:
    before = len(tripwire.events)
    rejected = (
        ("http://127.0.0.2/a", "non_global_address"),
        ("http://127.0.0.2", "non_global_address"),
        ("http://127.0.0.1./secret", "non_global_address"),
        ("http://169.254.169.253/latest/meta-data", "non_global_address"),
        ("http://169.254.169.254/", "non_global_address"),
        ("http://10.0.0.1/", "non_global_address"),
        ("http://192.168.1.1/", "non_global_address"),
        ("http://172.16.0.1/", "non_global_address"),
        ("http://0.0.0.0/", "non_global_address"),
        ("http://224.0.0.1/", "non_global_address"),
        ("http://100.64.0.1/", "non_global_address"),
        ("http://[::1]/", "non_global_address"),
        ("http://[fd00::1]/", "non_global_address"),
        ("http://[fc00::1]/", "non_global_address"),
        ("http://[fe80::1]/", "non_global_address"),
        ("http://[::]/", "non_global_address"),
        ("http://[ff02::1]/", "non_global_address"),
        ("http://[fec0::1]/", "non_global_address"),
        ("http://[::ffff:127.0.0.1]/", "non_global_address"),
        ("http://[::ffff:7f00:1]/", "non_global_address"),
        ("http://[::ffff:10.0.0.1]/", "non_global_address"),
        ("http://[::ffff:169.254.169.254]/", "non_global_address"),
        ("http://127.1/", "ambiguous_address"),
        ("http://127.1./", "ambiguous_address"),
        ("http://127.0.1/", "ambiguous_address"),
        ("http://0x7f000001/", "ambiguous_address"),
        ("http://2130706433/", "ambiguous_address"),
        ("http://0177.0.0.1/", "ambiguous_address"),
        ("http://127.0.0.01/", "ambiguous_address"),
        ("http://0/", "ambiguous_address"),
        (f"https://user:{SENTINEL}@example.com/a", "userinfo"),
        ("https://user@example.com/a", "userinfo"),
        ("http://example.com\\@127.0.0.1/", "backslash"),
        ("http://example.com/a\\b", "backslash"),
        ("http://localhost/a", "localhost"),
        ("http://LOCALHOST./a", "localhost"),
        ("http://foo.localhost/a", "localhost"),
        ("http://Foo.LocalHost./a", "localhost"),
        ("http://metadata.google.internal/computeMetadata/v1/", "metadata_host"),
        ("http://METADATA.GOOGLE.INTERNAL./", "metadata_host"),
        ("http://foo.metadata.google.internal/", "metadata_host"),
        ("http://example.com:0/", "invalid_port"),
        ("http://example.com:65536/", "invalid_port"),
        ("http://example.com:80abc/", "invalid_port"),
        ("ftp://example.com/a", "unsupported_scheme"),
        ("file:tests/fixtures/rss/feed.xml", "unsupported_scheme"),
        ("http:///a", "invalid_url"),
        ("", "invalid_url"),
        ("http://example.com/a\nb", "control_character"),
        ("http://example.com/a\x00b", "control_character"),
        ("http://example.com/a\x7fb", "control_character"),
    )
    for url, code in rejected:
        assert _raise(code, url) == code

    allowed = (
        "https://10.example.com/feed",
        "https://EXAMPLE.com./a",
        "https://8.8.8.8/dns",
        "https://[2001:4860:4860::8888]/dns",
        "https://[::ffff:8.8.8.8]/dns",
        "http://example.com:443/a",
        "https://example.com:8443/a",
        "https://bücher.example/a",
        "https://127.0.0.1.example.com/a",
        "http://localhost.example.com/a",
        "https://example.com/@user",
        "http://example.com",
    )
    for url in allowed:
        assert guard_public_url(url) is None

    assert canonical_url("http://127.0.0.2/a") == "http://127.0.0.2/a"
    assert canonical_url("http://127.1/") == "http://127.1/"
    assert canonical_url("https://10.example.com/feed") == "https://10.example.com/feed"
    with pytest.raises(UnsafeUrl) as caught:
        guard_public_url(None)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_url"
    assert tripwire.events[before:] == []


def test_redirect_to_private_is_blocked() -> None:
    public = "https://news.example/feed.xml"
    private = f"http://127.0.0.2/secret?token={SENTINEL}"
    transport = ScriptTransport([(302, {"Location": private, "Content-Type": "text/html"}, b"ignored")])
    client, _clock, store = _client(transport)
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch(public, source_id="news", etag='"v1"', last_modified="Thu, 01 Jan 2026 00:00:00 GMT")
    assert caught.value.code == "non_global_address"
    assert [item["url"] for item in transport.calls] == [public]
    assert transport.calls[0]["timeout_s"] == HTTP_TIMEOUT_S
    assert transport.calls[0]["max_bytes"] == HTTP_MAX_BYTES
    assert "non_global_address" in [event.error_code for event in store.events]
    for event in store.events:
        _assert_clean(event)
        assert private not in repr(event)
    assert all("127.0.0.2" not in item["url"] for item in transport.calls)

    mapped = "https://cdn.example/a"
    transport = ScriptTransport([(302, {"Location": "//127.0.0.2/x"}, b"")])
    client, _clock, _store = _client(transport)
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch(mapped)
    assert caught.value.code == "non_global_address"
    assert [item["url"] for item in transport.calls] == [mapped]

    transport = ScriptTransport([(302, {"Location": "http://[::1]/secret"}, b"")])
    client, _clock, _store = _client(transport)
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch("https://cdn.example/start")
    assert caught.value.code == "non_global_address"
    assert len(transport.calls) == 1

    first = "https://a.example/a"
    second = "https://b.example/b"
    transport = ScriptTransport(
        [
            (302, {"Location": second}, b""),
            _ok(),
        ]
    )
    client, clock, _store = _client(transport)
    response = client.fetch(first, etag='"v1"', last_modified="Thu, 01 Jan 2026 00:00:00 GMT")
    assert response.status == 200
    assert [item["url"] for item in transport.calls] == [first, second]
    assert transport.calls[0]["headers"]["If-None-Match"] == '"v1"'
    assert transport.calls[0]["headers"]["If-Modified-Since"] == "Thu, 01 Jan 2026 00:00:00 GMT"
    assert "If-None-Match" not in transport.calls[1]["headers"]
    assert "If-Modified-Since" not in transport.calls[1]["headers"]
    assert "Cookie" not in transport.calls[0]["headers"]
    assert "Authorization" not in transport.calls[0]["headers"]
    assert clock.sleeps == []

    same = "https://a.example/same"
    nxt = "https://a.example/next"
    transport = ScriptTransport([(301, {"Location": nxt}, b""), _ok()])
    client, clock, _store = _client(transport)
    client.fetch(same, etag='"keep"')
    assert [item["url"] for item in transport.calls] == [same, nxt]
    assert transport.calls[0]["headers"]["If-None-Match"] == '"keep"'
    assert transport.calls[1]["headers"]["If-None-Match"] == '"keep"'
    assert clock.sleeps == pytest.approx([HTTP_MIN_INTERVAL_S])

    base = "https://news.example/dir/feed.xml"
    transport = ScriptTransport([(302, {"Location": "../secret.xml"}, b""), _ok()])
    client, _clock, _store = _client(transport)
    client.fetch(base)
    assert transport.calls[1]["url"] == "https://news.example/secret.xml"

    urls = [f"https://hop{i}.example/feed.xml" for i in range(HTTP_MAX_REDIRECTS + 1)]
    scripts = [(302, {"Location": urls[index + 1]}, b"") for index in range(HTTP_MAX_REDIRECTS)]
    transport = ScriptTransport(scripts)
    client, _clock, store = _client(transport)
    with pytest.raises(FetchError) as caught:
        client.fetch(urls[0])
    assert caught.value.code == "redirect_limit"
    assert [item["url"] for item in transport.calls] == urls[:HTTP_MAX_REDIRECTS]
    assert urls[-1] not in [item["url"] for item in transport.calls]
    assert store.events[-1].error_code == "redirect_limit"

    loop_a = "https://loop.example/a"
    loop_b = "https://loop.example/b"
    transport = ScriptTransport([(302, {"Location": loop_b}, b""), (302, {"Location": loop_a}, b"")])
    client, _clock, _store = _client(transport)
    with pytest.raises(FetchError) as caught:
        client.fetch(loop_a)
    assert caught.value.code == "redirect_loop"
    assert [item["url"] for item in transport.calls] == [loop_a, loop_b]

    secure = "https://secure.example/a"
    transport = ScriptTransport([(302, {"Location": "http://secure.example/a"}, b"")])
    client, _clock, _store = _client(transport)
    with pytest.raises(FetchError) as caught:
        client.fetch(secure)
    assert caught.value.code == "https_downgrade"
    assert [item["url"] for item in transport.calls] == [secure]

    transport = ScriptTransport([(302, {"Content-Type": "text/html"}, b"")])
    client, _clock, _store = _client(transport)
    with pytest.raises(FetchError) as caught:
        client.fetch("https://news.example/feed.xml")
    assert caught.value.code == "missing_location"
    assert caught.value.status == 302

    transport = ScriptTransport([(302, {"Location": "https://b.example/x"}, b"")])
    client, _clock, _store = _client(transport)
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch("https://a.example/feed.xml", allowed_hosts=frozenset({"a.example"}))
    assert caught.value.code == "host_not_allowed"
    assert len(transport.calls) == 1

    transport = ScriptTransport([_ok()])
    client, _clock, _store = _client(transport)
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch("https://a.example/feed.xml", allowed_hosts=frozenset({"b.example"}))
    assert caught.value.code == "host_not_allowed"
    assert transport.calls == []

    upgraded = "http://news.example/a"
    transport = ScriptTransport([(302, {"Location": "https://news.example/a"}, b""), _ok()])
    client, _clock, _store = _client(transport)
    assert client.fetch(upgraded).status == 200

    payload = (
        b"HTTP/1.1 302 Found\r\n"
        b"Location: http://127.0.0.1/secret\r\n"
        b"Content-Type: text/html\r\n"
        b"Content-Length: 4\r\n"
        b"\r\n"
        b"BODY"
    )
    sock = FakeSock(payload)

    def factory(scheme: str, host: str, port: int, timeout_s: int) -> http.client.HTTPConnection:
        assert (scheme, host, port, timeout_s) == ("http", "news.example", 80, HTTP_TIMEOUT_S)
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.sock = sock
        return conn

    wire = StdlibTransport(connection_factory=factory)
    wire_response = wire.request(
        "http://news.example/feed.xml?x=1",
        headers={"Cookie": "a=b", "Authorization": f"Bearer {SENTINEL}", "If-None-Match": "abc"},
        timeout_s=HTTP_TIMEOUT_S,
        max_bytes=HTTP_MAX_BYTES,
        purpose="feed",
    )
    sent = bytes(sock.sent)
    assert wire_response.status == 302
    assert wire_response.body == b""
    assert sent.splitlines()[0] == b"GET /feed.xml?x=1 HTTP/1.1"
    assert sent.count(b"GET ") == 1
    assert b"User-Agent: " + USER_AGENT.encode() in sent
    assert b"Accept-Encoding: identity" in sent
    assert b"If-None-Match: abc" in sent
    assert b"Cookie" not in sent
    assert b"Authorization" not in sent
    assert b"Proxy-" not in sent
    assert SENTINEL.encode() not in sent
    assert b"127.0.0.1" not in sent
    assert sock.fp is not None
    assert sock.fp.max_pos == payload.index(b"\r\n\r\n") + 4

    secure_conn = StdlibTransport.open_connection("https", "example.com", 443, HTTP_TIMEOUT_S)
    try:
        assert isinstance(secure_conn, http.client.HTTPSConnection)
        assert secure_conn.timeout == HTTP_TIMEOUT_S
        assert secure_conn._context.check_hostname is True
        assert secure_conn._context.verify_mode == ssl.CERT_REQUIRED
    finally:
        secure_conn.close()


def test_429_opens_circuit() -> None:
    transport = ScriptTransport([(429, {"Retry-After": "7", "Content-Type": "text/plain"}, b"slow")])
    client, clock, store = _client(transport)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://a.example/feed.xml", source_id="alpha")
    assert caught.value.host == "a.example"
    assert caught.value.status == 429
    assert caught.value.retry_after_s == pytest.approx(7)
    assert len(transport.calls) == 1
    state = store.get_rate_state("a.example")
    assert state.consecutive_failures == 1
    assert parse_timestamp(state.blocked_until) - clock.now() == timedelta(seconds=7)
    event = store.events[-1]
    assert event.status == 429
    assert event.error_code == "rate_limited"
    assert event.retry_after_s == pytest.approx(7)

    clock.advance(1)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://A.EXAMPLE.:443/other.xml", source_id="alpha")
    assert caught.value.retry_after_s == pytest.approx(6)
    assert len(transport.calls) == 1
    assert store.get_rate_state("a.example").consecutive_failures == 1

    other = ScriptTransport([_ok()])
    other_client, _clock, other_store = _client(other, clock=clock)
    response = other_client.fetch("https://b.example/feed.xml", source_id="beta")
    assert response.status == 200
    assert len(other.calls) == 1
    assert other_store.get_rate_state("b.example").consecutive_failures == 0
    assert store.get_rate_state("a.example").consecutive_failures == 1

    long_wait = ScriptTransport([(429, {"Retry-After": "5000"}, b"")])
    client, clock, store = _client(long_wait)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://long.example/feed.xml")
    assert caught.value.retry_after_s == pytest.approx(5000)
    assert caught.value.retry_after_s > HTTP_CIRCUIT_OPEN_S
    blocked = parse_timestamp(store.get_rate_state("long.example").blocked_until)
    assert blocked - clock.now() == timedelta(seconds=5000)

    dated = ScriptTransport([])
    client, clock, store = _client(dated)
    stamp = formatdate((clock.now() + timedelta(seconds=7200)).timestamp(), usegmt=True)
    dated.scripts.append((429, {"Retry-After": stamp}, b""))
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://dated.example/feed.xml")
    assert caught.value.retry_after_s == pytest.approx(7200, abs=1.5)
    assert caught.value.retry_after_s > HTTP_CIRCUIT_OPEN_S
    assert store.get_rate_state("dated.example").consecutive_failures == 1

    invalid = ScriptTransport([(429, {"Retry-After": "nope"}, b"")])
    client, clock, store = _client(invalid)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://bad-header.example/feed.xml")
    assert caught.value.retry_after_s == pytest.approx(2)
    past = formatdate((clock.now() - timedelta(days=1)).timestamp(), usegmt=True)
    invalid.scripts.append((429, {"Retry-After": past}, b""))
    clock.advance(2)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://bad-header.example/feed.xml")
    assert caught.value.retry_after_s == pytest.approx(4)

    forbidden = ScriptTransport([(403, {"Content-Type": "text/plain"}, b"no")])
    client, _clock, store = _client(forbidden)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://forbid.example/feed.xml")
    assert caught.value.status == 403
    assert caught.value.retry_after_s == pytest.approx(2)
    assert store.get_rate_state("forbid.example").consecutive_failures == 1
    with pytest.raises(RateLimited):
        client.fetch("https://forbid.example/feed.xml")
    assert len(forbidden.calls) == 1

    missing = ScriptTransport([(404, {"Content-Type": "text/plain"}, b"gone")])
    client, _clock, store = _client(missing)
    with pytest.raises(FetchError) as caught:
        client.fetch("https://missing.example/feed.xml")
    assert caught.value.code == "client_error"
    assert caught.value.status == 404
    state = store.get_rate_state("missing.example")
    assert state.consecutive_failures == 0
    assert state.blocked_until is None


def test_failure_backoff_persists() -> None:
    transport = ScriptTransport(
        [
            (500, {"Content-Type": "text/plain"}, b"down"),
            (500, {"Content-Type": "text/plain"}, b"down"),
            (500, {"Content-Type": "text/plain"}, b"down"),
            _ok(),
            (500, {"Content-Type": "text/plain"}, b"down"),
            (304, {"ETag": '"same"'}, b""),
            _ok(),
        ]
    )
    clock = FakeClock()
    store = MemoryState()
    client, _clock, _store = _client(transport, clock=clock, store=store)
    gaps = (2, 4)
    for attempt, gap in enumerate(gaps, start=1):
        with pytest.raises(FetchError) as caught:
            client.fetch("https://fail.example/feed.xml", source_id="feed")
        assert caught.value.code == "server_error"
        assert caught.value.status == 500
        state = store.get_rate_state("fail.example")
        assert state.consecutive_failures == attempt
        assert parse_timestamp(state.blocked_until) - clock.now() == timedelta(seconds=gap)
        clock.advance(gap)

    with pytest.raises(FetchError):
        client.fetch("https://fail.example/feed.xml", source_id="feed")
    state = store.get_rate_state("fail.example")
    assert state.consecutive_failures == 3
    assert parse_timestamp(state.blocked_until) - clock.now() >= timedelta(seconds=HTTP_CIRCUIT_OPEN_S)
    assert parse_timestamp(state.blocked_until) - clock.now() == timedelta(seconds=HTTP_CIRCUIT_OPEN_S)

    clock.advance(1)
    parked = ScriptTransport([_ok()])
    parked_client, _clock, _store = _client(parked, clock=clock, store=store)
    with pytest.raises(RateLimited) as caught:
        parked_client.fetch("https://fail.example/feed.xml", source_id="feed")
    assert parked.calls == []
    assert store.get_rate_state("fail.example").consecutive_failures == 3
    assert caught.value.retry_after_s == pytest.approx(HTTP_CIRCUIT_OPEN_S - 1)

    clock.advance(HTTP_CIRCUIT_OPEN_S - 1)
    response = client.fetch("https://fail.example/feed.xml", source_id="feed")
    assert response.status == 200
    assert store.get_rate_state("fail.example").consecutive_failures == 0
    assert store.get_rate_state("fail.example").blocked_until is None
    assert parked.calls == []
    assert len(transport.calls) == 4

    with pytest.raises(FetchError):
        client.fetch("https://fail.example/feed.xml", source_id="feed")
    assert store.get_rate_state("fail.example").consecutive_failures == 1
    clock.advance(2)
    response = client.fetch("https://fail.example/feed.xml", source_id="feed", etag='"same"')
    assert response.status == 304
    assert store.get_rate_state("fail.example").consecutive_failures == 0
    assert store.get_rate_state("fail.example").blocked_until is None

    before_sleeps = len(clock.sleeps)
    response = client.fetch("https://fail.example/feed.xml", source_id="feed")
    assert response.status == 200
    assert clock.sleeps[before_sleeps:] == pytest.approx([HTTP_MIN_INTERVAL_S])


def test_limits_and_content_types_before_body() -> None:
    assert HTTP_MAX_BYTES == 2 * 1024 * 1024

    def run(
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
        *,
        purpose: str = "feed",
        explode: bool = False,
        endless: bool = False,
    ) -> tuple[object, StreamBody | EndlessBody]:
        stream: StreamBody | EndlessBody
        if endless:
            stream = EndlessBody(status, headers)
        else:
            stream = StreamBody(status, headers, body, explode=explode)
        calls: list[str] = []

        def connector(target: str, headers_sent: Mapping[str, str], timeout_s: int) -> StreamBody | EndlessBody:
            calls.append(target)
            assert timeout_s == HTTP_TIMEOUT_S
            assert headers_sent["Accept-Encoding"] == "identity"
            assert headers_sent["User-Agent"] == USER_AGENT
            return stream

        client, _clock, _store = _client(StdlibTransport(connector=connector))
        try:
            result = client.fetch(url, purpose=purpose)
        except FetchError:
            raise
        else:
            return result, stream
        finally:
            assert stream.closed is True

    over = {"Content-Type": "application/xml", "Content-Length": str(HTTP_MAX_BYTES + 1)}
    with pytest.raises(FetchError) as caught:
        run("https://cdn.example/feed.xml", 200, over, b"", explode=True)
    assert caught.value.code == "body_too_large"

    gzip = {"Content-Type": "application/xml", "Content-Encoding": "gzip"}
    with pytest.raises(FetchError) as caught:
        run("https://cdn.example/feed.xml", 200, gzip, b"", explode=True)
    assert caught.value.code == "bad_content_encoding"

    br = {"Content-Type": "text/xml", "Content-Encoding": "br"}
    with pytest.raises(FetchError) as caught:
        run("https://cdn.example/feed.xml", 200, br, b"", explode=True)
    assert caught.value.code == "bad_content_encoding"

    for media in ("audio/mpeg", "video/mp4", "image/png", "text/html", "application/pdf", "application/json"):
        headers = {"Content-Type": media}
        with pytest.raises(FetchError) as caught:
            run("https://cdn.example/note.txt", 200, headers, b"", purpose="transcript", explode=True)
        assert caught.value.code == "bad_content_type"

    html = {"Content-Type": "text/html; charset=utf-8"}
    with pytest.raises(FetchError) as caught:
        run("https://cdn.example/feed.xml", 200, html, b"", explode=True)
    assert caught.value.code == "bad_content_type"

    endless_calls: list[EndlessBody] = []

    def connector(url: str, headers_sent: Mapping[str, str], timeout_s: int) -> EndlessBody:
        stream = EndlessBody(200, {"Content-Type": "application/xml"})
        endless_calls.append(stream)
        return stream

    client, _clock, _store = _client(StdlibTransport(connector=connector))
    with pytest.raises(FetchError) as caught:
        client.fetch("https://cdn.example/feed.xml")
    assert caught.value.code == "body_too_large"
    assert endless_calls[0].reads > 0
    assert HTTP_MAX_BYTES < endless_calls[0].bytes_out <= HTTP_MAX_BYTES + 65536

    feed, stream = run(
        "https://cdn.example/feed.xml",
        200,
        {"Content-Type": "application/rss+xml; charset=utf-8"},
        b"<?xml version='1.0'?><rss></rss>",
    )
    assert feed.status == 200
    assert feed.body.startswith(b"<?xml")
    assert stream.reads > 0

    text, stream = run(
        "https://cdn.example/note.txt",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
        b"alpha beta",
        purpose="transcript",
    )
    assert text.body == b"alpha beta"
    assert stream.reads > 0

    vtt, _stream = run(
        "https://cdn.example/captions.vtt",
        200,
        {},
        b"WEBVTT\n\nHello",
        purpose="transcript",
    )
    assert vtt.body.startswith(b"WEBVTT")

    calls: list[str] = []

    def refuse_connector(url: str, headers_sent: Mapping[str, str], timeout_s: int) -> StreamBody:
        calls.append(url)
        return StreamBody(200, {"Content-Type": "text/plain"}, b"nope", explode=True)

    client, _clock, _store = _client(StdlibTransport(connector=refuse_connector))
    with pytest.raises(FetchError) as caught:
        client.fetch("https://cdn.example/episode.MP3", purpose="transcript")
    assert caught.value.code == "media_extension"
    assert calls == []

    with pytest.raises(FetchError) as caught:
        run(
            "https://cdn.example/note",
            200,
            {},
            b"",
            purpose="transcript",
            explode=True,
        )
    assert caught.value.code == "bad_content_type"

    dtd = b"<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'http://127.0.0.1/" + SENTINEL.encode() + b"'>]><rss></rss>"
    with pytest.raises(FetchError) as caught:
        run("https://cdn.example/feed.xml", 200, {"Content-Type": "application/xml"}, dtd)
    assert caught.value.code == "xml_dtd"
    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in repr(caught.value)

    with pytest.raises(FetchError) as caught:
        run(
            "https://cdn.example/note.txt",
            200,
            {"Content-Type": "text/plain"},
            b"hello\x00",
            purpose="transcript",
        )
    assert caught.value.code == "bad_content_type"


def test_bilibili_never_reaches_transport() -> None:
    for host in BILIBILI_HOSTS:
        for url in (
            f"https://{host}/x/web-interface/view",
            f"https://{host.upper()}./x/web-interface/view",
            f"https://{host}:443/x",
            f"http://{host}:8443/x",
        ):
            transport = ScriptTransport([_ok()])
            client, _clock, store = _client(transport)
            with pytest.raises(UnsafeUrl) as caught:
                client.fetch(url, source_id="feed")
            assert caught.value.code == "bilibili_forbidden"
            assert str(caught.value) == "bilibili_forbidden"
            assert transport.calls == []
            assert store.events[-1].error_code == "bilibili_forbidden"
            assert store.events[-1].url_redacted == redact_url(url)
            assert "/x" not in store.events[-1].url_redacted

    assert policy_interval("api.bilibili.com", HTTP_MIN_INTERVAL_S) == BILIBILI_INTERVAL_S
    assert policy_interval("www.bilibili.com", 0.5) == BILIBILI_INTERVAL_S
    assert policy_interval("api.bilibili.tv", 5.0) == 5.0
    assert policy_interval("example.com", HTTP_MIN_INTERVAL_S) == HTTP_MIN_INTERVAL_S

    for code in BILIBILI_COMMUNITY_CODES:
        payload = {"code": code, "message": SENTINEL}
        assert rate_limit_code(payload) is True
        assert rate_limit_code(json.dumps(payload).encode()) is True
        assert rate_limit_code(json.dumps(payload)) is True
    assert rate_limit_code(b'{"code":0}') is False
    assert rate_limit_code(b'{"code":-1}') is False
    assert rate_limit_code(b'{"code":"-412"}') is False
    assert rate_limit_code(b'{"code":true}') is False
    assert rate_limit_code(b'{"code":-412.5}') is False
    assert rate_limit_code(b"[]") is False
    assert rate_limit_code(b"") is False
    assert rate_limit_code(b"\xff") is False
    assert rate_limit_code("not-json") is False
    assert rate_limit_code({"message": "x"}) is False
    assert rate_limit_code({"data": {"code": -412}}) is False
    assert rate_limit_code(b' { "code" : -509 } ') is True

    body = json.dumps({"code": -412, "message": SENTINEL}).encode()
    transport = ScriptTransport([(200, {"Content-Type": "application/json"}, body)])
    clock = FakeClock()
    store = MemoryState()
    client, _clock, _store = _client(transport, clock=clock, store=store)
    with pytest.raises(RateLimited) as caught:
        client.fetch("https://cdn.example/status.json", source_id="feed")
    assert len(transport.calls) == 1
    assert caught.value.retry_after_s == pytest.approx(2)
    assert SENTINEL not in repr(caught.value)
    assert store.get_rate_state("cdn.example").consecutive_failures == 1
    follow = ScriptTransport([_ok()])
    client, _clock, _store = _client(follow, clock=clock, store=store)
    with pytest.raises(RateLimited):
        client.fetch("https://cdn.example/again.json", source_id="feed")
    assert follow.calls == []


def test_http_log_redaction() -> None:
    transport = ScriptTransport([])
    client, _clock, store = _client(transport)
    userinfo = f"https://user:{SENTINEL}@cdn.example/private?token={SENTINEL}"
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch(userinfo, source_id="feed")
    assert caught.value.code == "userinfo"
    assert SENTINEL not in str(caught.value)
    assert SENTINEL not in repr(caught.value)
    assert transport.calls == []
    assert store.events[-1].error_code == "userinfo"
    assert store.events[-1].url_redacted == f"https://cdn.example/{ELLIPSIS}"
    _assert_clean(store.events[-1])

    secret_query = f"https://cdn.example/feed.xml?token={SENTINEL}"
    with pytest.raises(UnsafeUrl) as caught:
        client.fetch(secret_query, source_id="feed")
    assert caught.value.code == "secret_query"
    assert transport.calls == []
    assert store.events[-1].url_redacted == redact_url(secret_query)
    assert store.events[-1].url_hash == hash_url(secret_query)
    _assert_clean(store.events[-1])

    hidden = f"https://cdn.example/{SENTINEL}/feed.xml"
    transport.scripts.append(
        (
            429,
            {"Retry-After": "7", "X-Token": SENTINEL, "Content-Type": "text/plain"},
            f"<rss>{SENTINEL}</rss>".encode(),
        )
    )
    with pytest.raises(RateLimited) as caught:
        client.fetch(hidden, source_id="feed")
    assert caught.value.status == 429
    assert caught.value.retry_after_s == pytest.approx(7)
    assert SENTINEL not in repr(caught.value)
    event = store.events[-1]
    assert event.status == 429
    assert event.error_code == "rate_limited"
    assert event.retry_after_s == pytest.approx(7)
    assert event.url_redacted == f"https://cdn.example/{ELLIPSIS}"
    assert event.url_hash == hash_url(hidden)
    assert event.host == "cdn.example"
    _assert_clean(event)
    assert "X-Token" not in repr(event)
    assert "<rss>" not in repr(event)

    transport.scripts.append(RuntimeError(f"dial {SENTINEL} auth.json"))
    with pytest.raises(FetchError) as caught:
        client.fetch("https://other.example/feed.xml", source_id="feed")
    assert caught.value.code == "transport_failed"
    assert caught.value.__cause__ is None
    assert SENTINEL not in str(caught.value)
    assert "auth.json" not in str(caught.value)
    event = store.events[-1]
    assert event.error_code == "transport_failed"
    assert event.status is None
    _assert_clean(event)

    visible = f"<rss>{SENTINEL}</rss>".encode()
    transport.scripts.append((200, {"Content-Type": "application/xml", "X-Token": SENTINEL}, visible))
    response = client.fetch("https://ok.example/feed.xml", source_id="feed")
    assert response.body == visible
    event = store.events[-1]
    assert event.status == 200
    assert event.error_code is None
    assert event.retry_after_s is None
    _assert_clean(event)
    assert "X-Token" not in repr(event)
