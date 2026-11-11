"""Frozen records, hashes, and design constants.

Numeric limits are engineering defaults from the v3 plan. They are not
measured API quotas, prices, or a promise that a live account is funded.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from second_brain.errors import (
    ConfigError,
    IdentityCollision,
    InvalidBucket,
    InvalidStance,
    SchemaError,
)

SCHEMA_VERSION = "3"
POLICY_TEMPLATE_VERSION = "classify-v3"
REVIEW_TEMPLATE_VERSION = "review-v3"
PACKAGE_VERSION = "0.3.0"
USER_AGENT = "SecondBrain/0.3 (local; personal)"

PROMPT_TITLE_CHARS = 300
PROMPT_BODY_CHARS = 1500
PROMPT_CHANNEL_CHARS = 300
PROMPT_COMMENT_CHARS = 300
PROMPT_COMMENT_MAX = 5
PROMPT_PREFERENCES_CHARS = 4000
PROMPT_TOTAL_CHARS = 32000
CLASSIFY_BATCH_MAX = 8
KNOWN_CLAIM_KEYS_MAX = 32
CLAIM_KEY_CHARS = 200
CLAIM_SENTENCE_CHARS = 400
CLAIM_QUOTE_CHARS = 300
TOPIC_CHARS = 80
REASON_CHARS = 800
CLAIMS_PER_ITEM_MAX = 5
REVIEW_SENTENCE_CHARS = 300
REVIEW_SENTENCE_MIN = 2
REVIEW_SENTENCE_MAX = 8
REVIEW_TOPIC_BATCH_MAX = 4
REVIEW_ROWS_MAX = 8

HTTP_TIMEOUT_S = 20
HTTP_MAX_BYTES = 2 * 1024 * 1024
HTTP_MAX_REDIRECTS = 3
HTTP_MIN_INTERVAL_S = 1.0
HTTP_CIRCUIT_OPEN_S = 900
HTTP_BACKOFF_CAP_S = 60
HTTP_CIRCUIT_AFTER_FAILURES = 3
BILIBILI_INTERVAL_S = 2.0
BILIBILI_HOSTS = ("api.bilibili.com", "www.bilibili.com", "api.bilibili.tv")
BILIBILI_COMMUNITY_CODES = frozenset({-412, -799, -509, -352, -503})

GROK_TIMEOUT_S = 120
GROK_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
GROK_MODEL = "grok-4.6"
GROK_ARGV_BEFORE_CWD = (
    "--no-auto-update",
    "--verbatim",
    "--no-plan",
    "--no-subagents",
    "--disable-web-search",
    "--max-turns",
    "1",
    "--disallowed-tools",
    "run_terminal_cmd,web_search,web_fetch,search_replace,Agent",
    "--deny",
    "Bash",
    "--deny",
    "Edit",
    "--deny",
    "Write",
    "--deny",
    "WebFetch",
    "--model",
    "grok-4.6",
    "--reasoning-effort",
    "low",
)
GROK_ARGV_DYNAMIC_FLAGS = ("--cwd", "--json-schema", "--prompt-file")

Modality = Literal["text", "podcast", "video"]
Bucket = Literal["must", "recommend", "skip"]
Stance = Literal["for", "against"]
SourceKind = Literal["rss", "youtube_rss", "podcast_rss", "file"]
FeedFormat = Literal["rss", "podcast_rss", "youtube_rss", "video_json", "podcast_json"]
TranscriptStatus = Literal["ready", "pending_transcript", "not_applicable"]
RunStatus = Literal["running", "publishing", "success", "partial", "failed"]
EvidenceKind = Literal["title", "body", "comment"]
AssessmentStatus = Literal[
    "opportunity",
    "insufficient",
    "single_source",
    "disputed",
    "one_sided",
    "conflicted",
]
BillingPath = Literal["test", "session", "api", "unknown"]
BatchStatus = Literal["success", "invalid", "unavailable", "budget_skipped"]
SummaryStatus = Literal[
    "ok",
    "disabled",
    "failed",
    "budget_skipped",
    "single_source",
    "not_requested",
]
ReviewKind = Literal["consensus", "difference", "audience", "limit"]

_SOURCE_KINDS = frozenset({"rss", "youtube_rss", "podcast_rss", "file"})
_FEED_FORMATS = frozenset({"rss", "podcast_rss", "youtube_rss", "video_json", "podcast_json"})
_MODALITIES = frozenset({"text", "podcast", "video"})
_TRANSCRIPTS = frozenset({"ready", "pending_transcript", "not_applicable"})
_RUN_STATUSES = frozenset({"running", "publishing", "success", "partial", "failed"})
_EVIDENCE = frozenset({"title", "body", "comment"})
_ASSESSMENTS = frozenset(
    {"opportunity", "insufficient", "single_source", "disputed", "one_sided", "conflicted"}
)
_BILLING = frozenset({"test", "session", "api", "unknown"})
_BATCH_STATUSES = frozenset({"success", "invalid", "unavailable", "budget_skipped"})
_SUMMARY = frozenset({"ok", "disabled", "failed", "budget_skipped", "single_source", "not_requested"})
_REVIEW_KINDS = frozenset({"consensus", "difference", "audience", "limit"})
_BUCKETS = frozenset({"must", "recommend", "skip"})
_STANCES = frozenset({"for", "against"})
_SECRET_QUERY_KEYS = frozenset(
    {"token", "access_token", "api_key", "key", "password", "signature", "sig"}
)
_TRACKING_KEYS = frozenset({"fbclid", "gclid"})
_SOURCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CANONICAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")
_REDACTED_URL = re.compile(r"^(?:https?|invalid)://[^/?#@\\\s]+/…$")
_IDENTITY_BREAKS = ("\n", "\r", "\x00", "\u2028", "\u2029")


def _set(obj: object, name: str, value: object) -> None:
    object.__setattr__(obj, name, value)


def canonical_json(value: object) -> str:
    """Compact UTF-8 JSON with sorted object keys. Tuples become arrays."""
    try:
        return json.dumps(
            _json_ready(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise SchemaError("invalid_json") from None


def _json_ready(value: object) -> object:
    if value is None or isinstance(value, str):
        if isinstance(value, str) and "\x00" in value:
            raise SchemaError("invalid_json")
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError("invalid_json")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, Mapping):
        ready: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError("invalid_json")
            ready[key] = _json_ready(item)
        return ready
    raise SchemaError("invalid_json")


def _payload_value(value: object) -> object:
    if value is None or isinstance(value, str):
        if isinstance(value, str) and "\x00" in value:
            raise SchemaError("invalid_payload")
        return value
    if isinstance(value, bool) or isinstance(value, float):
        raise SchemaError("invalid_payload")
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [_payload_value(item) for item in value]
    if isinstance(value, dict):
        ready: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or "\x00" in key:
                raise SchemaError("invalid_payload")
            ready[key] = _payload_value(item)
        return ready
    raise SchemaError("invalid_payload")


def single_line(text: str) -> str:
    """Turn title line breaks into spaces. CRLF is one break, not two."""
    if not isinstance(text, str):
        raise SchemaError("invalid_text")
    normalized = text
    for old, new in (("\r\n", "\n"), ("\r", "\n"), ("\u2028", "\n"), ("\u2029", "\n")):
        normalized = normalized.replace(old, new)
    return normalized.replace("\n", " ")


def normalize_key(text: str) -> str:
    """NFKC, strip, casefold, then collapse whitespace. Do not translate."""
    if not isinstance(text, str):
        raise ConfigError("alias_type")
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def _alias_table(aliases: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(aliases, Mapping) or isinstance(aliases, (str, bytes)):
        raise ConfigError("alias_type")
    table: dict[str, str] = {}
    for raw_key, raw_value in aliases.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ConfigError("alias_type")
        key = normalize_key(raw_key)
        value = normalize_key(raw_value)
        if key == "" or value == "":
            raise ConfigError("alias_empty_target")
        previous = table.get(key)
        if previous is not None and previous != value:
            raise ConfigError("alias_conflict")
        table[key] = value
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ConfigError("alias_cycle")
        nxt = table.get(node)
        if nxt is None:
            visited.add(node)
            return
        visiting.add(node)
        walk(nxt)
        visiting.remove(node)
        visited.add(node)

    for node in tuple(table):
        walk(node)
    return table


def _alias_pairs(aliases: Mapping[str, str]) -> list[list[str]]:
    table = _alias_table(aliases)
    return [[key, table[key]] for key in sorted(table)]


def resolve_alias(key: str, aliases: Mapping[str, str]) -> str:
    """Follow a normalized alias map to its end. Cycles are config errors."""
    if not isinstance(key, str):
        raise ConfigError("alias_type")
    table = _alias_table(aliases)
    current = normalize_key(key)
    if current == "":
        raise ConfigError("alias_empty_target")
    seen: set[str] = set()
    while current in table:
        if current in seen:
            raise ConfigError("alias_cycle")
        seen.add(current)
        current = table[current]
    return current


def _register(
    digest: str,
    key: tuple[str, ...],
    registry: MutableMapping[str, tuple[str, ...]] | None,
) -> str:
    if registry is None:
        return digest
    existing = registry.get(digest)
    if existing is None:
        registry[digest] = key
        return digest
    if existing != key:
        raise IdentityCollision()
    return digest


def _reject_identity_part(value: str, *, source: bool) -> None:
    if not isinstance(value, str) or value == "":
        raise SchemaError("invalid_identity")
    if any(char in value for char in _IDENTITY_BREAKS):
        raise SchemaError("invalid_identity")
    if source and _SOURCE_ID.fullmatch(value) is None:
        raise SchemaError("invalid_identity")


def item_id(
    source_id: str,
    guid: str,
    *,
    registry: MutableMapping[str, tuple[str, ...]] | None = None,
) -> str:
    """First 32 hex chars of SHA256(source_id + newline + guid)."""
    _reject_identity_part(source_id, source=True)
    _reject_identity_part(guid, source=False)
    digest = hashlib.sha256(f"{source_id}\n{guid}".encode("utf-8")).hexdigest()[:32]
    return _register(digest, ("item", source_id, guid), registry)


def content_hash(title: str, body_raw: str) -> str:
    """SHA256 of the single-line title, a newline, and the raw body."""
    if not isinstance(title, str) or not isinstance(body_raw, str):
        raise SchemaError("invalid_text")
    if "\x00" in title or "\x00" in body_raw:
        raise SchemaError("invalid_text")
    line = single_line(title)
    return hashlib.sha256(f"{line}\n{body_raw}".encode("utf-8")).hexdigest()


def topic_slug(
    topic: str,
    *,
    registry: MutableMapping[str, tuple[str, ...]] | None = None,
) -> str:
    normalized = normalize_key(topic)
    if normalized == "":
        raise SchemaError("invalid_topic")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return _register(digest, ("topic", normalized), registry)


def claim_slug(
    topic: str,
    claim_key: str,
    *,
    registry: MutableMapping[str, tuple[str, ...]] | None = None,
) -> str:
    """Slug of normalized topic plus claim key, so topics do not share a file."""
    topic_key = normalize_key(topic)
    claim = normalize_key(claim_key)
    if topic_key == "" or claim == "":
        raise SchemaError("invalid_topic")
    digest = hashlib.sha256(canonical_json([topic_key, claim]).encode("utf-8")).hexdigest()[:12]
    return _register(digest, ("claim_slug", topic_key, claim), registry)


def _claim_digest(
    *,
    item_id: str,
    analysis_hash: str,
    topic: str,
    claim_key: str,
    stance: str,
    evidence_kind: str,
    comment_index: int | None,
    evidence_quote: str,
) -> str:
    payload = {
        "item_id": item_id,
        "analysis_hash": analysis_hash,
        "topic": topic,
        "claim_key": claim_key,
        "stance": stance,
        "evidence_kind": evidence_kind,
        "comment_index": comment_index,
        "evidence_quote": evidence_quote,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def parse_timestamp(value: str) -> datetime:
    """Accept aware ISO-8601 and return aware UTC."""
    if not isinstance(value, str) or value == "":
        raise SchemaError("invalid_timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SchemaError("invalid_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchemaError("naive_timestamp")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Format aware UTC as YYYY-MM-DDTHH:MM:SS.ffffffZ."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SchemaError("naive_timestamp")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def require_canonical_timestamp(value: str) -> str:
    if not isinstance(value, str) or _CANONICAL_TIMESTAMP.fullmatch(value) is None:
        raise SchemaError("invalid_timestamp")
    if format_timestamp(parse_timestamp(value)) != value:
        raise SchemaError("invalid_timestamp")
    return value


def _optional_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("invalid_timestamp")
    return require_canonical_timestamp(value)


def _canonical_host(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if host == "" or ".." in host or host.startswith("."):
        raise SchemaError("invalid_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address):
        return f"[{address.compressed}]"
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    try:
        encoded = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise SchemaError("invalid_url") from None
    if len(encoded) > 253 or any(label == "" for label in encoded.split(".")):
        raise SchemaError("invalid_url")
    return encoded


def _canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=False, separator="&")
    kept: list[tuple[str, str]] = []
    for key, item in pairs:
        folded = key.casefold()
        if folded.startswith("utm_") or folded in _TRACKING_KEYS:
            continue
        kept.append((key, item))
    return urlencode(kept, doseq=True, quote_via=quote, safe="")


def canonical_url(url: str) -> str:
    """Normalize http(s) URLs without fetching or merging http into https."""
    if not isinstance(url, str) or url == "":
        raise SchemaError("invalid_url")
    if "\\" in url or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in url):
        raise SchemaError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise SchemaError("invalid_url") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise SchemaError("invalid_url")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SchemaError("invalid_url")
    if not hostname:
        raise SchemaError("invalid_url")
    host = _canonical_host(hostname)
    if port is not None and (port < 1 or port > 65535):
        raise SchemaError("invalid_url")
    default_port = 443 if scheme == "https" else 80
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, _canonical_query(parts.query), ""))


def url_has_secret_query(url: str) -> bool:
    """True when a query key is exactly one of the configured secret names."""
    try:
        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True, separator="&")
    except ValueError:
        return False
    return any(key.casefold() in _SECRET_QUERY_KEYS for key, _item in pairs)


def redact_url(url: str) -> str:
    """Return scheme://host[:port]/… and drop path, query, and userinfo."""
    fallback = "invalid://redacted/\u2026"
    if not isinstance(url, str) or url == "":
        return fallback
    try:
        if "\\" in url or any(ord(char) < 32 or ord(char) == 127 for char in url):
            return fallback
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return fallback
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        return fallback
    try:
        host = _canonical_host(hostname)
    except SchemaError:
        return fallback
    default_port = 443 if scheme == "https" else 80
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    return f"{scheme}://{netloc}/\u2026"


def hash_url(url: str) -> str:
    if not isinstance(url, str):
        raise SchemaError("invalid_url")
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _fixture_url(url: str) -> bool:
    prefix = "file:tests/fixtures/"
    if not url.startswith(prefix):
        return False
    relative = url[len(prefix) :]
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    return all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)


def _host_name(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("invalid_transcript_host")
    host = value.strip().lower().rstrip(".")
    if host == "" or any(char in host for char in "/@:\\") or ".." in host:
        raise ConfigError("invalid_transcript_host")
    try:
        encoded = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ConfigError("invalid_transcript_host") from None
    if _HOST.fullmatch(encoded) is None or any(label == "" for label in encoded.split(".")):
        raise ConfigError("invalid_transcript_host")
    return encoded


def _plain_text(value: object, code: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise SchemaError(code)
    return value


def _choice(value: object, allowed: frozenset[str], code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise SchemaError(code)
    return value


def _bucket(value: object) -> str:
    if not isinstance(value, str) or value not in _BUCKETS:
        raise InvalidBucket()
    return value


def _stance(value: object) -> str:
    if not isinstance(value, str) or value not in _STANCES:
        raise InvalidStance()
    return value


def _count(value: object, *, allow_none: bool) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaError("invalid_count")
    return value


def _budget(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError("invalid_budget")
    return value


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError("invalid_bool")
    return value


def _hex(value: object, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SchemaError(code)
    return value


def _optional_hex(value: object, pattern: re.Pattern[str], code: str) -> str | None:
    if value is None:
        return None
    return _hex(value, pattern, code)


def _id(value: object, code: str = "invalid_run_id") -> str:
    if not isinstance(value, str) or _SOURCE_ID.fullmatch(value) is None:
        raise SchemaError(code)
    return value


def _optional_id(value: object, code: str = "invalid_run_id") -> str | None:
    if value is None:
        return None
    return _id(value, code)


def _token(value: object, code: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise SchemaError(code)
    return value


def _optional_token(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _token(value, code)


def _str_tuple(value: object, code: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SchemaError(code)
    items = tuple(value)
    if any(not isinstance(item, str) or "\x00" in item for item in items):
        raise SchemaError(code)
    return items


def _id_tuple(value: object, pattern: re.Pattern[str], code: str) -> tuple[str, ...]:
    items = _str_tuple(value, code)
    if any(pattern.fullmatch(item) is None for item in items):
        raise SchemaError(code)
    return items


def _bounded(value: object, limit: int, code: str, *, single: bool = False) -> str:
    text = _plain_text(value, code)
    if text.strip() == "" or len(text) > limit:
        raise SchemaError(code)
    if single and text != single_line(text):
        raise SchemaError(code)
    return text


def _topic(value: object) -> str:
    text = _bounded(value, TOPIC_CHARS, "invalid_topic", single=True)
    if normalize_key(text) == "":
        raise SchemaError("invalid_topic")
    return text


def _claim_key(value: object) -> str:
    text = _bounded(value, CLAIM_KEY_CHARS, "invalid_claim_key", single=True)
    parts = text.split("|")
    if len(parts) != 4 or any(part.strip() == "" for part in parts):
        raise SchemaError("invalid_claim_key")
    return text


def _header_value(value: object) -> str | None:
    if value is None:
        return None
    text = _plain_text(value, "invalid_header")
    if text == "" or len(text) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise SchemaError("invalid_header")
    return text


def _reference_url(value: object, *, allow_fixture: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise SchemaError("invalid_url")
    if allow_fixture and value.startswith("file:"):
        if not _fixture_url(value):
            raise SchemaError("invalid_fixture_url")
        return value
    try:
        normalized = canonical_url(value)
    except SchemaError:
        raise SchemaError("invalid_url") from None
    if normalized != value:
        raise SchemaError("noncanonical_url")
    return value


def _mime(value: object) -> str | None:
    if value is None:
        return None
    text = _plain_text(value, "invalid_media_type")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}(/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,127})?", text) is None:
        raise SchemaError("invalid_media_type")
    return text


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    kind: SourceKind
    url: str
    title: str
    backfill_days: int = 14
    file_format: FeedFormat | None = None
    transcript_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            _set(self, "id", _id(self.id, "invalid_source_id"))
            kind = _choice(self.kind, _SOURCE_KINDS, "invalid_source_kind")
            _set(self, "kind", kind)
            _set(self, "title", _bounded(self.title, PROMPT_TITLE_CHARS, "invalid_title", single=True))
            _set(self, "backfill_days", _budget(self.backfill_days))
            if kind == "file":
                if self.file_format is None:
                    raise ConfigError("file_format_required")
                _set(self, "file_format", _choice(self.file_format, _FEED_FORMATS, "invalid_file_format"))
                if not isinstance(self.url, str) or not _fixture_url(self.url):
                    raise ConfigError("invalid_fixture_url")
            else:
                if self.file_format is not None:
                    raise ConfigError("file_format_forbidden")
                if not isinstance(self.url, str):
                    raise ConfigError("invalid_url")
                normalized = canonical_url(self.url)
                if normalized != self.url:
                    raise ConfigError("noncanonical_url")
                if url_has_secret_query(self.url):
                    raise ConfigError("secret_query")
            hosts = _str_tuple(self.transcript_hosts, "invalid_transcript_host")
            _set(self, "transcript_hosts", tuple(_host_name(host) for host in hosts))
        except SchemaError as exc:
            raise ConfigError(exc.code) from None


@dataclass(frozen=True, slots=True)
class SourceState:
    source_id: str
    initial_since_at: str
    etag: str | None = None
    last_modified: str | None = None
    last_fetched_at: str | None = None
    last_http_status: int | None = None

    def __post_init__(self) -> None:
        _set(self, "source_id", _id(self.source_id, "invalid_source_id"))
        _set(self, "initial_since_at", require_canonical_timestamp(self.initial_since_at))
        _set(self, "etag", _header_value(self.etag))
        _set(self, "last_modified", _header_value(self.last_modified))
        _set(self, "last_fetched_at", _optional_timestamp(self.last_fetched_at))
        status = _count(self.last_http_status, allow_none=True)
        if status is not None and not 100 <= status <= 599:
            raise SchemaError("invalid_status")
        _set(self, "last_http_status", status)


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    source_id: str
    guid: str
    url: str
    canonical_url: str
    title: str
    published_at: str | None
    fetched_at: str
    content_hash: str
    analysis_hash: str
    modality: Modality
    body_raw: str
    body_plain: str
    duplicate_of: str | None = None

    def __post_init__(self) -> None:
        _set(self, "source_id", _id(self.source_id, "invalid_source_id"))
        _set(self, "guid", _plain_text(self.guid, "invalid_identity"))
        if self.id != item_id(self.source_id, self.guid):
            raise SchemaError("item_id_mismatch")
        title = _plain_text(self.title, "invalid_title")
        if title != single_line(title):
            raise SchemaError("invalid_title")
        _set(self, "title", title)
        _set(self, "body_raw", _plain_text(self.body_raw, "invalid_text"))
        _set(self, "body_plain", _plain_text(self.body_plain, "invalid_text"))
        _set(self, "content_hash", _hex(self.content_hash, _HEX64, "invalid_hash"))
        if self.content_hash != content_hash(self.title, self.body_raw):
            raise SchemaError("content_hash_mismatch")
        if self.analysis_hash != "":
            _set(self, "analysis_hash", _hex(self.analysis_hash, _HEX64, "invalid_hash"))
        elif not isinstance(self.analysis_hash, str):
            raise SchemaError("invalid_hash")
        if not isinstance(self.url, str) or not isinstance(self.canonical_url, str):
            raise SchemaError("invalid_url")
        if canonical_url(self.url) != self.canonical_url:
            raise SchemaError("canonical_url_mismatch")
        _set(self, "published_at", _optional_timestamp(self.published_at))
        _set(self, "fetched_at", require_canonical_timestamp(self.fetched_at))
        _set(self, "modality", _choice(self.modality, _MODALITIES, "invalid_modality"))
        duplicate = _optional_hex(self.duplicate_of, _HEX32, "invalid_hash")
        if duplicate is not None and duplicate == self.id:
            raise SchemaError("self_duplicate")
        _set(self, "duplicate_of", duplicate)


@dataclass(frozen=True, slots=True)
class Asset:
    item_id: str
    transcript_status: TranscriptStatus
    channel: str = ""
    audio_url: str | None = None
    transcript_url: str | None = None
    transcript_type: str | None = None
    transcript_etag: str | None = None
    transcript_last_modified: str | None = None
    transcript_last_attempt_at: str | None = None
    like_count: int | None = None
    comment_count: int | None = None
    view_count: int | None = None
    top_comments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "item_id", _hex(self.item_id, _HEX32, "invalid_hash"))
        _set(self, "transcript_status", _choice(self.transcript_status, _TRANSCRIPTS, "invalid_transcript_status"))
        _set(self, "channel", _plain_text(self.channel, "invalid_text"))
        _set(self, "audio_url", _reference_url(self.audio_url, allow_fixture=False))
        _set(self, "transcript_url", _reference_url(self.transcript_url, allow_fixture=True))
        _set(self, "transcript_type", _mime(self.transcript_type))
        _set(self, "transcript_etag", _header_value(self.transcript_etag))
        _set(self, "transcript_last_modified", _header_value(self.transcript_last_modified))
        _set(self, "transcript_last_attempt_at", _optional_timestamp(self.transcript_last_attempt_at))
        _set(self, "like_count", _count(self.like_count, allow_none=True))
        _set(self, "comment_count", _count(self.comment_count, allow_none=True))
        _set(self, "view_count", _count(self.view_count, allow_none=True))
        _set(self, "top_comments", _str_tuple(self.top_comments, "invalid_comments"))


def build_item(
    *,
    source_id: str,
    guid: str,
    url: str,
    title: str,
    fetched_at: str,
    modality: Modality,
    body_raw: str,
    body_plain: str,
    published_at: str | None = None,
    duplicate_of: str | None = None,
    registry: MutableMapping[str, tuple[str, ...]] | None = None,
) -> Item:
    """Build an item with a derived id and content hash. analysis_hash stays empty."""
    stored_title = single_line(title)
    return Item(
        id=item_id(source_id, guid, registry=registry),
        source_id=source_id,
        guid=guid,
        url=url,
        canonical_url=canonical_url(url),
        title=stored_title,
        published_at=published_at,
        fetched_at=fetched_at,
        content_hash=content_hash(stored_title, body_raw),
        analysis_hash="",
        modality=modality,
        body_raw=body_raw,
        body_plain=body_plain,
        duplicate_of=duplicate_of,
    )


def prompt_fields(item: Item, asset: Asset) -> dict[str, object]:
    """Truncation used by prompts, analysis_hash, and the raw archive audience."""
    if not isinstance(item, Item) or not isinstance(asset, Asset):
        raise SchemaError("invalid_record")
    if asset.item_id != item.id:
        raise SchemaError("asset_item_mismatch")
    title, title_cut = _cut(item.title, PROMPT_TITLE_CHARS)
    body, body_cut = _cut(item.body_plain, PROMPT_BODY_CHARS)
    channel, channel_cut = _cut(asset.channel, PROMPT_CHANNEL_CHARS)
    audience: list[str] = []
    flags: list[bool] = []
    for comment in asset.top_comments[:PROMPT_COMMENT_MAX]:
        text, cut = _cut(comment, PROMPT_COMMENT_CHARS)
        audience.append(text)
        flags.append(cut)
    return {
        "id": item.id,
        "source_id": item.source_id,
        "title": title,
        "modality": item.modality,
        "body": body,
        "channel": channel,
        "audience": audience,
        "truncated": {
            "title": title_cut,
            "body": body_cut,
            "channel": channel_cut,
            "audience": flags,
        },
    }


def analysis_hash(item: Item, asset: Asset) -> str:
    """Hash the classification input. Counts and fetched_at are not included."""
    fields = prompt_fields(item, asset)
    payload = {
        "content_hash": content_hash(item.title, item.body_raw),
        "body_plain": item.body_plain,
        "modality": item.modality,
        "channel": asset.channel,
        "transcript_status": asset.transcript_status,
        "transcript_url": asset.transcript_url,
        "transcript_type": asset.transcript_type,
        "top_comments": fields["audience"],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def finalize_item(item: Item, asset: Asset) -> Item:
    """Replace an empty or stale analysis_hash with the derived digest."""
    digest = analysis_hash(item, asset)
    if item.analysis_hash == digest:
        return item
    return replace(item, analysis_hash=digest)


@dataclass(frozen=True, slots=True)
class IngestRecord:
    item: Item
    asset: Asset

    def __post_init__(self) -> None:
        if not isinstance(self.item, Item) or not isinstance(self.asset, Asset):
            raise SchemaError("invalid_record")
        if self.asset.item_id != self.item.id:
            raise SchemaError("asset_item_mismatch")
        if self.item.analysis_hash == "" or self.item.analysis_hash != analysis_hash(self.item, self.asset):
            raise SchemaError("analysis_hash_mismatch")


def make_ingest_record(item: Item, asset: Asset) -> IngestRecord:
    return IngestRecord(finalize_item(item, asset), asset)


@dataclass(frozen=True, slots=True)
class EntryIssue:
    entry_key_hash: str
    code: str

    def __post_init__(self) -> None:
        _set(self, "entry_key_hash", _hex(self.entry_key_hash, _HEX64, "invalid_hash"))
        _set(self, "code", _token(self.code, "invalid_error_code"))


@dataclass(frozen=True, slots=True)
class FeedBatch:
    records: tuple[IngestRecord, ...]
    issues: tuple[EntryIssue, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "records", _typed_tuple(self.records, IngestRecord, "invalid_record"))
        _set(self, "issues", _typed_tuple(self.issues, EntryIssue, "invalid_entry"))


@dataclass(frozen=True, slots=True)
class FetchResult:
    inserted: int
    updated: int
    unchanged: int
    skipped_old: int = 0
    skipped_dup: int = 0
    held_date: int = 0
    invalid: int = 0

    def __post_init__(self) -> None:
        for name in ("inserted", "updated", "unchanged", "skipped_old", "skipped_dup", "held_date", "invalid"):
            _set(self, name, _count(getattr(self, name), allow_none=False))


@dataclass(frozen=True, slots=True)
class ParsedPodcast:
    body_plain: str
    transcript_status: TranscriptStatus

    def __post_init__(self) -> None:
        _set(self, "body_plain", _plain_text(self.body_plain, "invalid_text"))
        status = _choice(self.transcript_status, _TRANSCRIPTS, "invalid_transcript_status")
        _set(self, "transcript_status", status)
        if status == "ready" and self.body_plain.strip() == "":
            raise SchemaError("invalid_transcript_status")


@dataclass(frozen=True, slots=True)
class VideoMeta:
    title: str
    channel: str
    like_count: int | None
    comment_count: int | None
    view_count: int | None
    comments: tuple[str, ...]
    transcript_status: Literal["not_applicable"] = "not_applicable"

    def __post_init__(self) -> None:
        title = _plain_text(self.title, "invalid_title")
        if title != single_line(title):
            raise SchemaError("invalid_title")
        _set(self, "title", title)
        _set(self, "channel", _plain_text(self.channel, "invalid_text"))
        _set(self, "like_count", _count(self.like_count, allow_none=True))
        _set(self, "comment_count", _count(self.comment_count, allow_none=True))
        _set(self, "view_count", _count(self.view_count, allow_none=True))
        _set(self, "comments", _str_tuple(self.comments, "invalid_comments"))
        if self.transcript_status != "not_applicable":
            raise SchemaError("invalid_transcript_status")


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    total_cost_usd: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            _set(self, name, _count(getattr(self, name), allow_none=True))
        cost = self.total_cost_usd
        if cost is not None and (not isinstance(cost, str) or _DECIMAL.fullmatch(cost) is None):
            raise SchemaError("invalid_cost")


@dataclass(frozen=True, slots=True)
class CompletionResult:
    payload: dict[str, object]
    usage: Usage
    stop_reason: str | None
    billing_path: BillingPath

    def __post_init__(self) -> None:
        if not isinstance(self.payload, dict):
            raise SchemaError("invalid_payload")
        _set(self, "payload", _payload_value(self.payload))
        if not isinstance(self.usage, Usage):
            raise SchemaError("invalid_usage")
        if self.stop_reason is not None:
            _set(self, "stop_reason", _plain_text(self.stop_reason, "invalid_stop_reason"))
        _set(self, "billing_path", _choice(self.billing_path, _BILLING, "invalid_billing_path"))


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    batch_id: str
    item_ids: tuple[str, ...]
    status: BatchStatus
    error_code: str | None = None

    def __post_init__(self) -> None:
        _set(self, "batch_id", _id(self.batch_id, "invalid_run_id"))
        _set(self, "item_ids", _id_tuple(self.item_ids, _HEX32, "invalid_hash"))
        status = _choice(self.status, _BATCH_STATUSES, "invalid_batch_status")
        _set(self, "status", status)
        if status == "success":
            if self.error_code is not None:
                raise SchemaError("invalid_error_code")
        else:
            _set(self, "error_code", _token(self.error_code, "invalid_error_code"))


@dataclass(frozen=True, slots=True)
class Classification:
    item_id: str
    run_id: str
    topic: str
    bucket: Bucket
    reason: str
    analysis_hash: str
    policy_hash: str
    classified_at: str
    published_run_id: str | None = None

    def __post_init__(self) -> None:
        _set(self, "item_id", _hex(self.item_id, _HEX32, "invalid_hash"))
        _set(self, "run_id", _id(self.run_id))
        _set(self, "topic", _topic(self.topic))
        _set(self, "bucket", _bucket(self.bucket))
        _set(self, "reason", _bounded(self.reason, REASON_CHARS, "invalid_reason"))
        _set(self, "analysis_hash", _hex(self.analysis_hash, _HEX64, "invalid_hash"))
        _set(self, "policy_hash", _hex(self.policy_hash, _HEX64, "invalid_hash"))
        _set(self, "classified_at", require_canonical_timestamp(self.classified_at))
        _set(self, "published_run_id", _optional_id(self.published_run_id))


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    item_id: str
    source_id: str
    analysis_hash: str
    topic: str
    claim_key: str
    sentence: str
    stance: Stance
    evidence_kind: EvidenceKind
    evidence_quote: str
    comment_index: int | None = None

    def __post_init__(self) -> None:
        _set(self, "item_id", _hex(self.item_id, _HEX32, "invalid_hash"))
        _set(self, "source_id", _id(self.source_id, "invalid_source_id"))
        _set(self, "analysis_hash", _hex(self.analysis_hash, _HEX64, "invalid_hash"))
        _set(self, "topic", _topic(self.topic))
        _set(self, "claim_key", _claim_key(self.claim_key))
        _set(self, "sentence", _bounded(self.sentence, CLAIM_SENTENCE_CHARS, "invalid_sentence"))
        _set(self, "stance", _stance(self.stance))
        kind = _choice(self.evidence_kind, _EVIDENCE, "invalid_evidence")
        _set(self, "evidence_kind", kind)
        _set(self, "evidence_quote", _bounded(self.evidence_quote, CLAIM_QUOTE_CHARS, "invalid_quote"))
        if kind == "comment":
            index = _count(self.comment_index, allow_none=False)
        else:
            if self.comment_index is not None:
                raise SchemaError("invalid_comment_index")
            index = None
        _set(self, "comment_index", index)
        digest = _claim_digest(
            item_id=self.item_id,
            analysis_hash=self.analysis_hash,
            topic=self.topic,
            claim_key=self.claim_key,
            stance=self.stance,
            evidence_kind=self.evidence_kind,
            comment_index=self.comment_index,
            evidence_quote=self.evidence_quote,
        )
        if self.id != digest:
            raise SchemaError("claim_id_mismatch")


def claim_id(
    claim: Claim,
    *,
    registry: MutableMapping[str, tuple[str, ...]] | None = None,
) -> str:
    """First 32 hex chars of the claim evidence digest. The claim's own id is not an input."""
    if not isinstance(claim, Claim):
        raise SchemaError("invalid_record")
    key = (
        "claim",
        claim.item_id,
        claim.analysis_hash,
        claim.topic,
        claim.claim_key,
        claim.stance,
        claim.evidence_kind,
        "" if claim.comment_index is None else str(claim.comment_index),
        claim.evidence_quote,
    )
    return _register(claim.id, key, registry)


def _typed_tuple(value: object, kind: type, code: str) -> tuple:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SchemaError(code)
    items = tuple(value)
    if any(not isinstance(item, kind) for item in items):
        raise SchemaError(code)
    return items


def make_claim(
    *,
    item_id: str,
    source_id: str,
    analysis_hash: str,
    topic: str,
    claim_key: str,
    sentence: str,
    stance: Stance,
    evidence_kind: EvidenceKind,
    evidence_quote: str,
    comment_index: int | None = None,
    registry: MutableMapping[str, tuple[str, ...]] | None = None,
) -> Claim:
    digest = _claim_digest(
        item_id=item_id,
        analysis_hash=analysis_hash,
        topic=topic,
        claim_key=claim_key,
        stance=stance,
        evidence_kind=evidence_kind,
        comment_index=comment_index,
        evidence_quote=evidence_quote,
    )
    claim = Claim(
        id=digest,
        item_id=item_id,
        source_id=source_id,
        analysis_hash=analysis_hash,
        topic=topic,
        claim_key=claim_key,
        sentence=sentence,
        stance=stance,
        evidence_kind=evidence_kind,
        evidence_quote=evidence_quote,
        comment_index=comment_index,
    )
    if registry is not None:
        claim_id(claim, registry=registry)
    return claim


@dataclass(frozen=True, slots=True)
class ClassifiedItem:
    id: str
    source_id: str
    topic: str
    url: str
    title: str
    bucket: Bucket
    reason: str
    published_at: str | None
    analysis_hash: str
    policy_hash: str
    comments: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "id", _hex(self.id, _HEX32, "invalid_hash"))
        _set(self, "source_id", _id(self.source_id, "invalid_source_id"))
        _set(self, "topic", _topic(self.topic))
        if not isinstance(self.url, str) or canonical_url(self.url) != self.url:
            raise SchemaError("canonical_url_mismatch")
        title = _plain_text(self.title, "invalid_title")
        if title != single_line(title):
            raise SchemaError("invalid_title")
        _set(self, "title", title)
        _set(self, "bucket", _bucket(self.bucket))
        _set(self, "reason", _bounded(self.reason, REASON_CHARS, "invalid_reason"))
        _set(self, "published_at", _optional_timestamp(self.published_at))
        _set(self, "analysis_hash", _hex(self.analysis_hash, _HEX64, "invalid_hash"))
        _set(self, "policy_hash", _hex(self.policy_hash, _HEX64, "invalid_hash"))
        comments = _str_tuple(self.comments, "invalid_comments")
        if len(comments) > PROMPT_COMMENT_MAX or any(len(comment) > PROMPT_COMMENT_CHARS for comment in comments):
            raise SchemaError("invalid_comments")
        _set(self, "comments", comments)
        claims = _typed_tuple(self.claims, Claim, "invalid_record")
        if len(claims) > CLAIMS_PER_ITEM_MAX:
            raise SchemaError("invalid_record")
        for claim in claims:
            if claim.item_id != self.id or claim.source_id != self.source_id or claim.analysis_hash != self.analysis_hash:
                raise SchemaError("claim_id_mismatch")
        _set(self, "claims", claims)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classifications: tuple[Classification, ...]
    claims: tuple[Claim, ...]
    batches: tuple[BatchOutcome, ...]

    def __post_init__(self) -> None:
        _set(self, "classifications", _typed_tuple(self.classifications, Classification, "invalid_record"))
        _set(self, "claims", _typed_tuple(self.claims, Claim, "invalid_record"))
        _set(self, "batches", _typed_tuple(self.batches, BatchOutcome, "invalid_record"))


@dataclass(frozen=True, slots=True)
class Opportunity:
    topic: str
    claim_key: str
    minority_sentence: str
    minority_stance: Stance
    minority_item_ids: tuple[str, ...]
    majority_item_ids: tuple[str, ...]
    minority_source_ids: tuple[str, ...]
    majority_source_ids: tuple[str, ...]
    majority_count: int
    minority_count: int
    n_sources: int
    falsify_hint: str

    def __post_init__(self) -> None:
        _set(self, "topic", _topic(self.topic))
        _set(self, "claim_key", _claim_key(self.claim_key))
        _set(self, "minority_sentence", _bounded(self.minority_sentence, CLAIM_SENTENCE_CHARS, "invalid_sentence"))
        _set(self, "minority_stance", _stance(self.minority_stance))
        _set(self, "minority_item_ids", _id_tuple(self.minority_item_ids, _HEX32, "invalid_hash"))
        _set(self, "majority_item_ids", _id_tuple(self.majority_item_ids, _HEX32, "invalid_hash"))
        _set(self, "minority_source_ids", _id_tuple(self.minority_source_ids, _SOURCE_ID, "invalid_source_id"))
        _set(self, "majority_source_ids", _id_tuple(self.majority_source_ids, _SOURCE_ID, "invalid_source_id"))
        _set(self, "majority_count", _count(self.majority_count, allow_none=False))
        _set(self, "minority_count", _count(self.minority_count, allow_none=False))
        _set(self, "n_sources", _count(self.n_sources, allow_none=False))
        _set(self, "falsify_hint", _bounded(self.falsify_hint, 4000, "invalid_text"))


@dataclass(frozen=True, slots=True)
class ClaimAssessment:
    topic: str
    claim_key: str
    status: AssessmentStatus
    n_items: int
    n_sources: int
    n_for: int
    n_against: int
    excluded_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _set(self, "topic", _topic(self.topic))
        _set(self, "claim_key", _claim_key(self.claim_key))
        _set(self, "status", _choice(self.status, _ASSESSMENTS, "invalid_assessment"))
        for name in ("n_items", "n_sources", "n_for", "n_against"):
            _set(self, name, _count(getattr(self, name), allow_none=False))
        _set(self, "excluded_source_ids", _id_tuple(self.excluded_source_ids, _SOURCE_ID, "invalid_source_id"))


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    opportunities: tuple[Opportunity, ...]
    assessments: tuple[ClaimAssessment, ...]

    def __post_init__(self) -> None:
        _set(self, "opportunities", _typed_tuple(self.opportunities, Opportunity, "invalid_record"))
        _set(self, "assessments", _typed_tuple(self.assessments, ClaimAssessment, "invalid_record"))


@dataclass(frozen=True, slots=True)
class ReviewSentence:
    kind: ReviewKind
    text: str
    item_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _set(self, "kind", _choice(self.kind, _REVIEW_KINDS, "invalid_review"))
        _set(self, "text", _bounded(self.text, REVIEW_SENTENCE_CHARS, "invalid_sentence", single=True))
        _set(self, "item_ids", _id_tuple(self.item_ids, _HEX32, "invalid_hash"))


@dataclass(frozen=True, slots=True)
class TopicReview:
    topic: str
    rows: tuple[ClassifiedItem, ...]
    sentences: tuple[ReviewSentence, ...]
    summary_status: SummaryStatus

    def __post_init__(self) -> None:
        _set(self, "topic", _topic(self.topic))
        _set(self, "rows", _typed_tuple(self.rows, ClassifiedItem, "invalid_record"))
        _set(self, "sentences", _typed_tuple(self.sentences, ReviewSentence, "invalid_record"))
        _set(self, "summary_status", _choice(self.summary_status, _SUMMARY, "invalid_summary_status"))


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]
    url: str

    def __post_init__(self) -> None:
        status = _count(self.status, allow_none=False)
        if status is None or not 100 <= status <= 599:
            raise SchemaError("invalid_status")
        _set(self, "status", status)
        if not isinstance(self.body, bytes):
            raise SchemaError("invalid_body")
        if not isinstance(self.headers, Mapping):
            raise SchemaError("invalid_header")
        lowered: dict[str, str] = {}
        for key, item in self.headers.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise SchemaError("invalid_header")
            if key == "" or any(ord(char) < 32 or ord(char) == 127 for char in key + item):
                raise SchemaError("invalid_header")
            folded = key.lower()
            if folded in lowered and lowered[folded] != item:
                raise SchemaError("header_conflict")
            lowered[folded] = item
        _set(self, "headers", MappingProxyType(lowered))
        _set(self, "url", _plain_text(self.url, "invalid_url"))


@dataclass(frozen=True, slots=True)
class RateState:
    host: str
    blocked_until: str | None = None
    consecutive_failures: int = 0
    last_request_at: str | None = None

    def __post_init__(self) -> None:
        host = _plain_text(self.host, "invalid_header")
        if host == "" or any(char in host for char in "/?#@"):
            raise SchemaError("invalid_header")
        _set(self, "host", host)
        _set(self, "blocked_until", _optional_timestamp(self.blocked_until))
        _set(self, "consecutive_failures", _count(self.consecutive_failures, allow_none=False))
        _set(self, "last_request_at", _optional_timestamp(self.last_request_at))


@dataclass(frozen=True, slots=True)
class FetchEvent:
    source_id: str | None
    host: str
    url_redacted: str
    url_hash: str
    status: int | None
    at: str
    error_code: str | None = None
    retry_after_s: float | None = None

    def __post_init__(self) -> None:
        _set(self, "source_id", _optional_id(self.source_id, "invalid_source_id"))
        host = _plain_text(self.host, "invalid_header")
        if host == "" or any(char in host for char in "/?#@"):
            raise SchemaError("invalid_header")
        _set(self, "host", host)
        if not isinstance(self.url_redacted, str) or _REDACTED_URL.fullmatch(self.url_redacted) is None:
            raise SchemaError("invalid_redacted_url")
        _set(self, "url_hash", _hex(self.url_hash, _HEX64, "invalid_hash"))
        status = _count(self.status, allow_none=True)
        if status is not None and not 100 <= status <= 599:
            raise SchemaError("invalid_status")
        _set(self, "status", status)
        _set(self, "at", require_canonical_timestamp(self.at))
        _set(self, "error_code", _optional_token(self.error_code, "invalid_error_code"))
        retry = self.retry_after_s
        if retry is not None:
            if isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(float(retry)) or float(retry) < 0:
                raise SchemaError("invalid_count")
            retry = float(retry)
        _set(self, "retry_after_s", retry)


@dataclass(frozen=True, slots=True)
class LlmEvent:
    call_id: str
    run_id: str
    purpose: str
    at: str
    status: str
    billing_path: str
    error_code: str | None = None
    usage: Usage | None = None

    def __post_init__(self) -> None:
        _set(self, "call_id", _id(self.call_id))
        _set(self, "run_id", _id(self.run_id))
        if self.purpose not in {"classify", "review"}:
            raise SchemaError("invalid_purpose")
        _set(self, "at", require_canonical_timestamp(self.at))
        _set(self, "status", _token(self.status, "invalid_error_code"))
        _set(self, "billing_path", _choice(self.billing_path, _BILLING, "invalid_billing_path"))
        _set(self, "error_code", _optional_token(self.error_code, "invalid_error_code"))
        if self.usage is not None and not isinstance(self.usage, Usage):
            raise SchemaError("invalid_usage")


@dataclass(frozen=True, slots=True)
class Publication:
    run_id: str
    html_path: str
    manifest_path: str
    classified_refs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _set(self, "run_id", _id(self.run_id))
        _set(self, "html_path", _relative_wiki_path(self.html_path))
        _set(self, "manifest_path", _relative_wiki_path(self.manifest_path))
        if isinstance(self.classified_refs, str) or not isinstance(self.classified_refs, Sequence):
            raise SchemaError("invalid_record")
        refs: list[tuple[str, str]] = []
        for ref in self.classified_refs:
            if not isinstance(ref, tuple) or len(ref) != 2:
                raise SchemaError("invalid_record")
            item, digest = ref
            refs.append((_hex(item, _HEX32, "invalid_hash"), _hex(digest, _HEX64, "invalid_hash")))
        _set(self, "classified_refs", tuple(refs))


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: str
    status: RunStatus
    inserted: int
    updated: int
    classified: int
    pending: int
    deferred: int
    failed_source_ids: tuple[str, ...]
    failed_item_ids: tuple[str, ...]
    html_path: str | None
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _set(self, "run_id", _id(self.run_id))
        _set(self, "status", _choice(self.status, _RUN_STATUSES, "invalid_run_status"))
        for name in ("inserted", "updated", "classified", "pending", "deferred"):
            _set(self, name, _count(getattr(self, name), allow_none=False))
        _set(self, "failed_source_ids", _id_tuple(self.failed_source_ids, _SOURCE_ID, "invalid_source_id"))
        _set(self, "failed_item_ids", _id_tuple(self.failed_item_ids, _HEX32, "invalid_hash"))
        if self.html_path is not None:
            _set(self, "html_path", _relative_wiki_path(self.html_path))
        warnings = _str_tuple(self.warnings, "invalid_text")
        if any(len(warning) > 500 or any(ord(char) < 32 for char in warning) for warning in warnings):
            raise SchemaError("invalid_text")
        _set(self, "warnings", warnings)


def _relative_wiki_path(value: object) -> str:
    text = _plain_text(value, "invalid_path")
    if text.startswith("/") or "\\" in text or ".." in text.split("/"):
        raise SchemaError("invalid_path")
    parts = text.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SchemaError("invalid_path")
    return text


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    offline: bool = True
    live_authorized: bool = False
    billing_path: BillingPath = "test"
    billing_path_verified: bool = False
    tool_policy_verified: bool = False
    review_llm: bool = True
    max_classify_batches: int = 3
    max_review_batches: int = 1
    max_transcript_fetches: int = 20

    def __post_init__(self) -> None:
        try:
            _set(self, "offline", _flag(self.offline))
            _set(self, "live_authorized", _flag(self.live_authorized))
            _set(self, "billing_path", _choice(self.billing_path, _BILLING, "invalid_billing_path"))
            _set(self, "billing_path_verified", _flag(self.billing_path_verified))
            _set(self, "tool_policy_verified", _flag(self.tool_policy_verified))
            _set(self, "review_llm", _flag(self.review_llm))
            _set(self, "max_classify_batches", _budget(self.max_classify_batches))
            _set(self, "max_review_batches", _budget(self.max_review_batches))
            _set(self, "max_transcript_fetches", _budget(self.max_transcript_fetches))
        except SchemaError as exc:
            raise ConfigError(exc.code) from None


@dataclass(frozen=True, slots=True)
class KeyPolicy:
    topic_aliases: Mapping[str, str] = field(default_factory=dict)
    claim_aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _alias_table(self.topic_aliases)
        _alias_table(self.claim_aliases)
        _set(self, "topic_aliases", MappingProxyType(dict(self.topic_aliases)))
        _set(self, "claim_aliases", MappingProxyType(dict(self.claim_aliases)))


@dataclass(frozen=True, slots=True)
class AppConfig:
    sources: tuple[Source, ...]
    preferences: str
    keys: KeyPolicy
    runtime: RuntimePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.preferences, str) or "\x00" in self.preferences:
            raise ConfigError("invalid_preferences")
        if len(self.preferences) > PROMPT_PREFERENCES_CHARS:
            raise ConfigError("preferences_too_long")
        if not isinstance(self.keys, KeyPolicy) or not isinstance(self.runtime, RuntimePolicy):
            raise ConfigError("invalid_config")
        try:
            sources = _typed_tuple(self.sources, Source, "invalid_config")
        except SchemaError as exc:
            raise ConfigError(exc.code) from None
        seen: set[str] = set()
        for source in sources:
            if source.id in seen:
                raise ConfigError("duplicate_source_id")
            seen.add(source.id)
        _set(self, "sources", sources)


def policy_hash(preferences: str, keys: KeyPolicy, schema: Mapping[str, object]) -> str:
    """SHA256 of preferences, normalized aliases, classification schema, and classify-v3."""
    if not isinstance(preferences, str) or "\x00" in preferences:
        raise ConfigError("invalid_preferences")
    if len(preferences) > PROMPT_PREFERENCES_CHARS:
        raise ConfigError("preferences_too_long")
    if not isinstance(keys, KeyPolicy):
        raise ConfigError("invalid_config")
    payload = {
        "template": POLICY_TEMPLATE_VERSION,
        "preferences": preferences,
        "topic_aliases": _alias_pairs(keys.topic_aliases),
        "claim_aliases": _alias_pairs(keys.claim_aliases),
        "schema": schema,
    }
    try:
        encoded = canonical_json(payload)
    except SchemaError:
        raise ConfigError("invalid_schema") from None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
