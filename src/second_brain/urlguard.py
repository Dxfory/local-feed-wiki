"""Public URL policy for one request target.

The check does not resolve DNS and does not pin a later address. A name that
looks public can still point at a private network. Canonicalization is a
separate helper and is not a substitute for this policy.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from second_brain.errors import UnsafeUrl

_DECIMAL = re.compile(r"\d+")
_HEX = re.compile(r"0x[0-9a-f]+")


@dataclass(frozen=True, slots=True)
class RequestTarget:
    url: str
    scheme: str
    hostname: str
    port: int | None
    rate_host: str
    path: str
    query: str


def guard_public_url(url: str) -> None:
    """Reject a URL that is not a public http(s) target. Return None."""
    parse_public_url(url)


def parse_public_url(url: str) -> RequestTarget:
    """Parse one public http(s) URL. Raise UnsafeUrl when the policy fails."""
    if not isinstance(url, str) or url == "":
        raise UnsafeUrl("invalid_url")
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in url):
        raise UnsafeUrl("control_character")
    if "\\" in url:
        raise UnsafeUrl("backslash")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise UnsafeUrl("invalid_url") from None
    try:
        port = parts.port
    except ValueError:
        raise UnsafeUrl("invalid_port") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrl("unsupported_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeUrl("userinfo")
    if not parts.hostname:
        raise UnsafeUrl("invalid_url")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeUrl("invalid_port")
    hostname = _normalize_host(parts.hostname)
    default_port = 443 if scheme == "https" else 80
    if port is None or port == default_port:
        rate_host = hostname
    else:
        rate_host = f"{hostname}:{port}"
    return RequestTarget(
        url=url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        rate_host=rate_host,
        path=parts.path,
        query=parts.query,
    )


def normalize_hostname(host: str) -> str:
    """Normalize one hostname the same way request targets do."""
    if not isinstance(host, str):
        raise UnsafeUrl("invalid_host")
    text = host.strip()
    if text == "" or "://" in text or "/" in text or "\\" in text:
        raise UnsafeUrl("invalid_host")
    return _normalize_host(text)


def _normalize_host(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "" or host.startswith(".") or ".." in host:
        raise UnsafeUrl("invalid_host")
    if "%" in host or any(char.isspace() for char in host):
        raise UnsafeUrl("invalid_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        _require_public(address)
        if isinstance(address, ipaddress.IPv6Address):
            return f"[{address.compressed}]"
        return str(address)
    if _ambiguous_ip(host):
        raise UnsafeUrl("ambiguous_address")
    if ":" in host:
        raise UnsafeUrl("invalid_host")
    try:
        encoded = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise UnsafeUrl("invalid_host") from None
    labels = encoded.split(".")
    if len(encoded) > 253 or any(label == "" or len(label) > 63 for label in labels):
        raise UnsafeUrl("invalid_host")
    if encoded == "localhost" or encoded.endswith(".localhost"):
        raise UnsafeUrl("localhost")
    if encoded == "metadata.google.internal" or encoded.endswith(".metadata.google.internal"):
        raise UnsafeUrl("metadata_host")
    return encoded


def _require_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        _require_public(address.ipv4_mapped)
        return
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        raise UnsafeUrl("non_global_address")
    if (
        address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or not address.is_global
    ):
        raise UnsafeUrl("non_global_address")


def _ambiguous_ip(host: str) -> bool:
    if _DECIMAL.fullmatch(host) or _HEX.fullmatch(host):
        return True
    if "." not in host:
        return False
    labels = host.split(".")
    if any(label == "" for label in labels):
        return False

    def component(label: str) -> bool:
        return _DECIMAL.fullmatch(label) is not None or _HEX.fullmatch(label) is not None

    return all(component(label) for label in labels)
