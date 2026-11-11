"""One-hop HTTP client and the per-host rate-limit state machine.

HttpClient follows redirects itself. StdlibTransport performs a single GET
and does not follow Location, inherit environment proxies, or keep cookies.
Tests inject a transport, a connector, or a connection factory. The default
factory is what a live process uses; unit tests do not call it.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import ssl
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

from second_brain.errors import FetchError, RateLimited, UnsafeUrl
from second_brain.models import (
    BILIBILI_COMMUNITY_CODES,
    BILIBILI_HOSTS,
    BILIBILI_INTERVAL_S,
    HTTP_BACKOFF_CAP_S,
    HTTP_CIRCUIT_AFTER_FAILURES,
    HTTP_CIRCUIT_OPEN_S,
    HTTP_MAX_BYTES,
    HTTP_MAX_REDIRECTS,
    HTTP_MIN_INTERVAL_S,
    HTTP_TIMEOUT_S,
    USER_AGENT,
    FetchEvent,
    HttpResponse,
    RateState,
    format_timestamp,
    hash_url,
    parse_timestamp,
    redact_url,
    url_has_secret_query,
)
from second_brain.ports import Clock, HttpStateStore, HttpTransport
from second_brain.urlguard import RequestTarget, normalize_hostname, parse_public_url

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_OUTAGE_CODES = frozenset({"timeout", "connection_failed", "dns_error", "transport_failed"})
_TRANSCRIPT_TYPES = frozenset({"text/plain", "text/vtt", "application/x-subrip"})
_TEXT_EXTENSIONS = frozenset({".txt", ".vtt", ".srt"})
_MEDIA_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".avi",
        ".flac",
        ".m3u",
        ".m3u8",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mpga",
        ".ogg",
        ".opus",
        ".ts",
        ".wav",
        ".webm",
        ".wma",
        ".wmv",
    }
)
_BLOCKED_TYPES = frozenset(
    {
        "application/gzip",
        "application/javascript",
        "application/octet-stream",
        "application/pdf",
        "application/xhtml+xml",
        "application/zip",
        "text/html",
        "text/javascript",
    }
)
_BLOCKED_REQUEST_HEADERS = frozenset(
    {
        "accept-encoding",
        "authorization",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "transfer-encoding",
        "user-agent",
    }
)
_FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"
_TEXT_ACCEPT = "text/plain, text/vtt, application/x-subrip"
_RETRY_SECONDS = re.compile(r"\d+(?:\.\d+)?")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")
_CHUNK = 65536


def rate_limit_code(payload: object) -> bool:
    """True when a synthetic JSON object has a top-level community rate-limit code."""
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeError:
            return False
        data = _load_json(text)
    elif isinstance(payload, str):
        data = _load_json(payload)
    elif isinstance(payload, Mapping):
        data = payload
    else:
        return False
    if not isinstance(data, Mapping):
        return False
    code = data.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return False
    return code in BILIBILI_COMMUNITY_CODES


def policy_interval(hostname: str, default: float) -> float:
    """Local minimum gap for one host. Bilibili names keep a two-second floor."""
    baseline = float(default)
    if hostname in BILIBILI_HOSTS:
        return max(baseline, float(BILIBILI_INTERVAL_S))
    return baseline


def _load_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extension(url: str) -> str:
    path = urlsplit(url).path
    slash = path.rfind("/")
    dot = path.rfind(".")
    if dot <= slash:
        return ""
    ext = path[dot:].lower()
    if _EXTENSION.fullmatch(ext) is None:
        return ""
    return ext


def _media_type(value: str | None) -> str | None:
    if value is None:
        return None
    base = value.split(";", 1)[0].strip().lower()
    if base == "":
        return None
    return base


def _is_xml_type(media: str) -> bool:
    return media in {"text/xml", "application/xml"} or media.endswith("+xml")


def _is_blocked_type(media: str | None) -> bool:
    if media is None:
        return False
    if media.startswith(("audio/", "video/", "image/")):
        return True
    return media in _BLOCKED_TYPES


def _declared_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    text = raw.strip()
    if not text.isdigit():
        return None
    return int(text)


def _reject_encoding(headers: Mapping[str, str]) -> None:
    encoding = headers.get("content-encoding", "")
    if encoding.strip() == "":
        encoding = "identity"
    else:
        encoding = encoding.strip().lower()
    if encoding != "identity":
        raise FetchError("bad_content_encoding")
    transfer = headers.get("transfer-encoding", "")
    tokens = {part.strip().lower() for part in transfer.split(",") if part.strip()}
    if tokens & {"gzip", "deflate", "br", "compress"}:
        raise FetchError("bad_content_encoding")


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    lowered: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise FetchError("invalid_header")
        lowered[key.lower()] = value
    return lowered


def _should_read(purpose: str, headers: Mapping[str, str], url: str) -> bool:
    media = _media_type(headers.get("content-type"))
    if _is_blocked_type(media):
        return False
    if purpose == "transcript":
        return _transcript_headers_ok(media, url)
    if media is None:
        return True
    if _is_xml_type(media) or media in {"text/plain", "application/json"} or media.endswith("+json"):
        return True
    return False


def _transcript_headers_ok(media: str | None, url: str) -> bool:
    if media in _TRANSCRIPT_TYPES:
        return True
    return media is None and _extension(url) in _TEXT_EXTENSIONS


def _read_limited(inbound: object, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        want = min(_CHUNK, max_bytes - total + 1)
        if want <= 0:
            raise FetchError("body_too_large")
        part = inbound.read(want)  # type: ignore[attr-defined]
        if not part:
            break
        if not isinstance(part, bytes):
            raise FetchError("invalid_body")
        if len(part) > want:
            part = part[:want]
        total += len(part)
        if total > max_bytes:
            raise FetchError("body_too_large")
        chunks.append(part)
    return b"".join(chunks)


def _has_dtd(body: bytes) -> bool:
    folded = body.lower()
    tokens = (b"<!doctype", b"<!entity", b"<!element", b"<!attlist", b"<!notation")
    return any(token in folded for token in tokens)


def _looks_like_xml(body: bytes) -> bool:
    sample = body.lstrip(b"\xef\xbb\xbf \t\r\n")
    return sample.startswith(b"<")


def _require_transcript_bytes(body: bytes) -> None:
    if b"\x00" in body:
        raise FetchError("bad_content_type")
    try:
        body.decode("utf-8")
    except UnicodeError:
        raise FetchError("bad_content_type") from None


def _merge_headers(headers: Mapping[str, str]) -> dict[str, str]:
    merged = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise FetchError("invalid_header")
        if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
            raise FetchError("invalid_header")
        if key.lower() in _BLOCKED_REQUEST_HEADERS:
            continue
        merged[key] = value
    return merged


def _without_fragment(url: str) -> str:
    parts = urlsplit(url)
    if parts.fragment == "":
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _finish(inbound: object, *, url: str, max_bytes: int, purpose: str) -> HttpResponse:
    status = inbound.status  # type: ignore[attr-defined]
    headers = _lower_headers(inbound.headers)  # type: ignore[attr-defined]
    if status != 200:
        return HttpResponse(status=status, body=b"", headers=headers, url=url)
    _reject_encoding(headers)
    declared = _declared_length(headers)
    if declared is not None and declared > max_bytes:
        raise FetchError("body_too_large")
    if not _should_read(purpose, headers, url):
        raise FetchError("bad_content_type")
    body = _read_limited(inbound, max_bytes)
    if purpose == "feed" and _has_dtd(body):
        raise FetchError("xml_dtd")
    if purpose == "transcript":
        _require_transcript_bytes(body)
    return HttpResponse(status=status, body=body, headers=headers, url=url)


class _HTTPInbound:
    def __init__(self, response: http.client.HTTPResponse) -> None:
        self.status = response.status
        self.headers = {key.lower(): value for key, value in response.headers.items()}
        self._response = response

    def read(self, amount: int) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        self._response.close()


class StdlibTransport:
    """One GET. Redirects, proxies, and cookies stay out of this class."""

    def __init__(self, *, connector: object | None = None, connection_factory: object | None = None) -> None:
        self._connector = connector
        self._connection_factory = connection_factory or self.open_connection

    @staticmethod
    def open_connection(scheme: str, host: str, port: int, timeout_s: int) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(
                host,
                port,
                timeout=timeout_s,
                context=ssl.create_default_context(),
            )
        if scheme == "http":
            return http.client.HTTPConnection(host, port, timeout=timeout_s)
        raise FetchError("unsupported_scheme")

    def request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: int,
        max_bytes: int,
        purpose: str,
    ) -> HttpResponse:
        if purpose not in {"feed", "transcript"}:
            raise FetchError("invalid_purpose")
        if _extension(url) in _MEDIA_EXTENSIONS:
            raise FetchError("media_extension")
        merged = _merge_headers(headers)
        if self._connector is not None:
            inbound = self._connector(url, merged, timeout_s)  # type: ignore[operator]
            try:
                return _finish(inbound, url=url, max_bytes=max_bytes, purpose=purpose)
            finally:
                close = getattr(inbound, "close", None)
                if close is not None:
                    close()
        return self._request_stdlib(url, merged, timeout_s, max_bytes, purpose)

    def _request_stdlib(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_s: int,
        max_bytes: int,
        purpose: str,
    ) -> HttpResponse:
        parts = urlsplit(url)
        if not parts.hostname:
            raise FetchError("invalid_url")
        port = parts.port
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn = self._connection_factory(parts.scheme, parts.hostname, port, timeout_s)  # type: ignore[operator]
        try:
            conn.request("GET", path, headers=dict(headers))
            incoming = conn.getresponse()
            return _finish(_HTTPInbound(incoming), url=url, max_bytes=max_bytes, purpose=purpose)
        except FetchError:
            raise
        except TimeoutError:
            raise FetchError("timeout") from None
        except socket.gaierror:
            raise FetchError("dns_error") from None
        except (OSError, http.client.HTTPException):
            raise FetchError("connection_failed") from None
        finally:
            conn.close()


class HttpClient:
    """Fetch one public URL, follow a short redirect chain, and persist host state."""

    def __init__(
        self,
        transport: HttpTransport,
        clock: Clock,
        state_store: HttpStateStore,
        *,
        min_interval_s: float = HTTP_MIN_INTERVAL_S,
    ) -> None:
        if isinstance(min_interval_s, bool) or not isinstance(min_interval_s, (int, float)):
            raise FetchError("invalid_interval")
        interval = float(min_interval_s)
        if not math.isfinite(interval) or interval < 0:
            raise FetchError("invalid_interval")
        self._transport = transport
        self._clock = clock
        self._state = state_store
        self._min_interval_s = interval

    def fetch(
        self,
        url: str,
        *,
        source_id: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        purpose: str = "feed",
        allowed_hosts: frozenset[str] | None = None,
    ) -> HttpResponse:
        if purpose not in {"feed", "transcript"}:
            raise FetchError("invalid_purpose")
        if source_id is not None and (not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None):
            raise FetchError("invalid_source_id")
        conditional = _conditional_headers(etag, last_modified)
        allowed = None if allowed_hosts is None else _allow_set(allowed_hosts)
        current = _without_fragment(url)
        seen: set[tuple[str, str, str, str]] = set()
        conditional_host: str | None = None
        previous_scheme: str | None = None
        # The constant caps the chain at three requests. The fourth hop is refused.
        for hop in range(1, HTTP_MAX_REDIRECTS + 2):
            if hop > HTTP_MAX_REDIRECTS:
                self._log(current, source_id=source_id, error_code="redirect_limit")
                raise FetchError("redirect_limit")
            target = self._validate(current, allowed=allowed, source_id=source_id)
            identity = (target.scheme, target.rate_host, target.path, target.query)
            if identity in seen:
                self._log(current, source_id=source_id, error_code="redirect_loop", host=target.rate_host)
                raise FetchError("redirect_loop")
            seen.add(identity)
            if previous_scheme == "https" and target.scheme == "http":
                self._log(current, source_id=source_id, error_code="https_downgrade", host=target.rate_host)
                raise FetchError("https_downgrade")
            if conditional_host is not None and conditional_host != target.rate_host:
                headers = {}
            else:
                headers = dict(conditional)
            headers["Accept"] = _FEED_ACCEPT if purpose == "feed" else _TEXT_ACCEPT
            response, sent_at = self._transmit(
                target,
                headers,
                purpose=purpose,
                source_id=source_id,
                url=current,
            )
            previous_scheme = target.scheme
            if response.status in _REDIRECTS:
                self._touch(target.rate_host, sent_at, clear=False)
                location = response.headers.get("location")
                if location is None or location.strip() == "":
                    self._log(
                        current,
                        source_id=source_id,
                        status=response.status,
                        error_code="missing_location",
                        host=target.rate_host,
                    )
                    raise FetchError("missing_location", response.status)
                if any(ord(char) < 32 or ord(char) == 127 for char in location):
                    self._log(
                        current,
                        source_id=source_id,
                        status=response.status,
                        error_code="invalid_location",
                        host=target.rate_host,
                    )
                    raise FetchError("invalid_location", response.status)
                self._log(current, source_id=source_id, status=response.status, host=target.rate_host)
                conditional_host = target.rate_host
                current = _without_fragment(urljoin(current, location.strip()))
                continue
            return self._finish_status(
                response,
                target=target,
                sent_at=sent_at,
                purpose=purpose,
                source_id=source_id,
                url=current,
            )
        raise FetchError("redirect_limit")

    def _validate(
        self,
        url: str,
        *,
        allowed: frozenset[str] | None,
        source_id: str | None,
    ) -> RequestTarget:
        try:
            target = parse_public_url(url)
        except UnsafeUrl as exc:
            self._log(url, source_id=source_id, error_code=exc.code)
            raise
        if url_has_secret_query(url):
            self._log(url, source_id=source_id, error_code="secret_query", host=target.rate_host)
            raise UnsafeUrl("secret_query")
        if target.hostname in BILIBILI_HOSTS:
            self._log(url, source_id=source_id, error_code="bilibili_forbidden", host=target.rate_host)
            raise UnsafeUrl("bilibili_forbidden")
        if allowed is not None and target.hostname not in allowed:
            self._log(url, source_id=source_id, error_code="host_not_allowed", host=target.rate_host)
            raise UnsafeUrl("host_not_allowed")
        if _extension(url) in _MEDIA_EXTENSIONS:
            self._log(url, source_id=source_id, error_code="media_extension", host=target.rate_host)
            raise FetchError("media_extension")
        return target

    def _wait(self, target: RequestTarget) -> datetime:
        state = self._state.get_rate_state(target.rate_host)
        now = self._clock.now()
        if state.blocked_until is not None:
            until = parse_timestamp(state.blocked_until)
            if until > now:
                remaining = (until - now).total_seconds()
                raise RateLimited(target.rate_host, remaining)
        if state.last_request_at is not None:
            interval = policy_interval(target.hostname, self._min_interval_s)
            earliest = parse_timestamp(state.last_request_at) + timedelta(seconds=interval)
            if earliest > now:
                self._clock.sleep((earliest - now).total_seconds())
                now = self._clock.now()
                if now < earliest:
                    raise FetchError("clock_stalled")
        return now

    def _transmit(
        self,
        target: RequestTarget,
        headers: Mapping[str, str],
        *,
        purpose: str,
        source_id: str | None,
        url: str,
    ) -> tuple[HttpResponse, datetime]:
        try:
            sent_at = self._wait(target)
        except RateLimited as exc:
            self._log(
                url,
                source_id=source_id,
                error_code="rate_limited",
                retry_after_s=exc.retry_after_s,
                host=target.rate_host,
            )
            raise
        try:
            response = self._transport.request(
                target.url,
                headers=headers,
                timeout_s=HTTP_TIMEOUT_S,
                max_bytes=HTTP_MAX_BYTES,
                purpose=purpose,
            )
        except RateLimited:
            raise
        except FetchError as exc:
            self._note_transport_error(target.rate_host, sent_at, exc, source_id=source_id, url=url)
            raise
        except Exception:
            delay = self._record_failure(target.rate_host, sent_at, None)
            self._log(
                url,
                source_id=source_id,
                error_code="transport_failed",
                retry_after_s=delay,
                host=target.rate_host,
            )
            raise FetchError("transport_failed") from None
        if not isinstance(response, HttpResponse):
            delay = self._record_failure(target.rate_host, sent_at, None)
            self._log(
                url,
                source_id=source_id,
                error_code="transport_failed",
                retry_after_s=delay,
                host=target.rate_host,
            )
            raise FetchError("transport_failed")
        return response, sent_at

    def _note_transport_error(
        self,
        rate_host: str,
        sent_at: datetime,
        exc: FetchError,
        *,
        source_id: str | None,
        url: str,
    ) -> None:
        if _is_outage(exc):
            delay = self._record_failure(rate_host, sent_at, None)
            self._log(
                url,
                source_id=source_id,
                status=exc.status,
                error_code=exc.code,
                retry_after_s=delay,
                host=rate_host,
            )
            return
        self._touch(rate_host, sent_at, clear=False)
        self._log(url, source_id=source_id, status=exc.status, error_code=exc.code, host=rate_host)

    def _finish_status(
        self,
        response: HttpResponse,
        *,
        target: RequestTarget,
        sent_at: datetime,
        purpose: str,
        source_id: str | None,
        url: str,
    ) -> HttpResponse:
        status = response.status
        if status in {429, 403}:
            delay = self._record_failure(target.rate_host, sent_at, response)
            self._log(
                url,
                source_id=source_id,
                status=status,
                error_code="rate_limited",
                retry_after_s=delay,
                host=target.rate_host,
            )
            raise RateLimited(target.rate_host, delay, status=status)
        if status == 408 or status >= 500:
            delay = self._record_failure(target.rate_host, sent_at, response)
            code = "request_timeout" if status == 408 else "server_error"
            self._log(
                url,
                source_id=source_id,
                status=status,
                error_code=code,
                retry_after_s=delay,
                host=target.rate_host,
            )
            raise FetchError(code, status)
        if 400 <= status < 500:
            self._touch(target.rate_host, sent_at, clear=False)
            self._log(url, source_id=source_id, status=status, error_code="client_error", host=target.rate_host)
            raise FetchError("client_error", status)
        if status == 304:
            self._touch(target.rate_host, sent_at, clear=True)
            self._log(url, source_id=source_id, status=304, host=target.rate_host)
            return _rebuild(response, url)
        if status == 200:
            return self._accept_ok(
                response,
                target=target,
                sent_at=sent_at,
                purpose=purpose,
                source_id=source_id,
                url=url,
            )
        self._touch(target.rate_host, sent_at, clear=False)
        self._log(url, source_id=source_id, status=status, error_code="http_status", host=target.rate_host)
        raise FetchError("http_status", status)

    def _accept_ok(
        self,
        response: HttpResponse,
        *,
        target: RequestTarget,
        sent_at: datetime,
        purpose: str,
        source_id: str | None,
        url: str,
    ) -> HttpResponse:
        try:
            _reject_success_headers(response, purpose=purpose, url=url)
            if purpose == "feed":
                if _has_dtd(response.body):
                    raise FetchError("xml_dtd", 200)
                if rate_limit_code(response.body):
                    delay = self._record_failure(target.rate_host, sent_at, response)
                    self._log(
                        url,
                        source_id=source_id,
                        status=200,
                        error_code="rate_limited",
                        retry_after_s=delay,
                        host=target.rate_host,
                    )
                    raise RateLimited(target.rate_host, delay, status=200)
                if not _feed_body_ok(response):
                    raise FetchError("bad_content_type", 200)
            else:
                _require_transcript_bytes(response.body)
        except RateLimited:
            raise
        except FetchError as exc:
            self._touch(target.rate_host, sent_at, clear=False)
            self._log(
                url,
                source_id=source_id,
                status=exc.status or 200,
                error_code=exc.code,
                host=target.rate_host,
            )
            raise
        self._touch(target.rate_host, sent_at, clear=True)
        self._log(url, source_id=source_id, status=200, host=target.rate_host)
        return _rebuild(response, url)

    def _record_failure(self, rate_host: str, now: datetime, response: HttpResponse | None) -> float:
        state = self._state.get_rate_state(rate_host)
        failures = state.consecutive_failures + 1
        local = min(HTTP_BACKOFF_CAP_S, 2 ** min(failures, 16))
        if failures >= HTTP_CIRCUIT_AFTER_FAILURES:
            local = max(local, HTTP_CIRCUIT_OPEN_S)
        parsed = _retry_after(response, now)
        delay = float(local if parsed is None else max(float(local), parsed))
        self._state.put_rate_state(
            RateState(
                host=rate_host,
                blocked_until=format_timestamp(_later(now, delay)),
                consecutive_failures=failures,
                last_request_at=format_timestamp(now),
            )
        )
        return delay

    def _touch(self, rate_host: str, now: datetime, *, clear: bool) -> None:
        state = self._state.get_rate_state(rate_host)
        self._state.put_rate_state(
            RateState(
                host=rate_host,
                blocked_until=None if clear else state.blocked_until,
                consecutive_failures=0 if clear else state.consecutive_failures,
                last_request_at=format_timestamp(now),
            )
        )

    def _log(
        self,
        url: str,
        *,
        source_id: str | None,
        host: str | None = None,
        status: int | None = None,
        error_code: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        self._state.append_fetch_event(
            FetchEvent(
                source_id=source_id,
                host=host if host is not None else _event_host(url),
                url_redacted=redact_url(url),
                url_hash=hash_url(url),
                status=status,
                at=format_timestamp(self._clock.now()),
                error_code=error_code,
                retry_after_s=retry_after_s,
            )
        )


def _allow_set(allowed_hosts: object) -> frozenset[str]:
    if isinstance(allowed_hosts, (str, bytes)) or not isinstance(allowed_hosts, Collection):
        raise FetchError("invalid_host")
    found: set[str] = set()
    for item in allowed_hosts:
        if not isinstance(item, str):
            continue
        try:
            found.add(normalize_hostname(item))
        except UnsafeUrl:
            continue
    return frozenset(found)


def _conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    etag_value = _optional_header(etag)
    modified = _optional_header(last_modified)
    if etag_value is not None:
        headers["If-None-Match"] = etag_value
    if modified is not None:
        headers["If-Modified-Since"] = modified
    return headers


def _optional_header(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FetchError("invalid_header")
    if value == "":
        return None
    return value


def _reject_success_headers(response: HttpResponse, *, purpose: str, url: str) -> None:
    _reject_encoding(response.headers)
    declared = _declared_length(response.headers)
    if declared is not None and declared > HTTP_MAX_BYTES:
        raise FetchError("body_too_large", response.status)
    media = _media_type(response.headers.get("content-type"))
    if _is_blocked_type(media):
        raise FetchError("bad_content_type", response.status)
    if purpose == "transcript" and not _transcript_headers_ok(media, url):
        raise FetchError("bad_content_type", response.status)


def _feed_body_ok(response: HttpResponse) -> bool:
    media = _media_type(response.headers.get("content-type"))
    textual = media in {"text/plain", "application/json"} or (media is not None and media.endswith("+json"))
    if media is not None and not _is_xml_type(media) and not textual:
        return False
    return _looks_like_xml(response.body)


def _rebuild(response: HttpResponse, url: str) -> HttpResponse:
    return HttpResponse(status=response.status, body=response.body, headers=dict(response.headers), url=url)


def _is_outage(exc: FetchError) -> bool:
    if exc.status is not None and (exc.status == 408 or exc.status >= 500):
        return True
    return exc.code in _OUTAGE_CODES


def _retry_after(response: HttpResponse | None, now: datetime) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    text = raw.strip()
    if _RETRY_SECONDS.fullmatch(text):
        value = float(text)
        if not math.isfinite(value) or value < 0:
            return None
        return value
    return _http_date_delay(text, now)


def _http_date_delay(value: str, now: datetime) -> float | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        return None
    delay = (parsed - now.astimezone(timezone.utc)).total_seconds()
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay


def _later(now: datetime, seconds: float) -> datetime:
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _event_host(url: str) -> str:
    try:
        return parse_public_url(url).rate_host
    except UnsafeUrl:
        pass
    if not isinstance(url, str):
        return "unknown"
    if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in url):
        return "unknown"
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    cleaned = host.lower().rstrip(".")
    if cleaned == "" or any(char in cleaned for char in "/?#@\\"):
        return "unknown"
    if len(cleaned) > 253:
        return "unknown"
    return cleaned
