"""Safe exception types for the second brain.

Messages are stable codes. They do not carry article text, stderr, credentials,
or URL queries. Callers that need structured data read attributes.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from second_brain.models import Usage

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UNSAFE_CODE = "unsafe_error_code"


def _code(code: str) -> str:
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return _UNSAFE_CODE


class ConfigError(ValueError):
    """Startup configuration is unusable. No request has been sent."""

    def __init__(self, code: str) -> None:
        self.code = _code(code)
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"ConfigError({self.code!r})"


class UnsafeUrl(ValueError):
    """A source or transcript URL failed the public-URL policy."""

    def __init__(self, code: str) -> None:
        self.code = _code(code)
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"UnsafeUrl({self.code!r})"


class FramesNotAllowed(ValueError):
    """Video input asked the program to read frames or media files."""

    code = "frames_not_allowed"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "FramesNotAllowed('frames_not_allowed')"


class SchemaError(ValueError):
    """One record, or one classification batch, failed structural checks."""

    def __init__(self, code: str) -> None:
        self.code = _code(code)
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"SchemaError({self.code!r})"


class InvalidBucket(SchemaError):
    def __init__(self) -> None:
        super().__init__("invalid_bucket")


class InvalidStance(SchemaError):
    def __init__(self) -> None:
        super().__init__("invalid_stance")


class IdentityCollision(RuntimeError):
    """A short digest already belongs to a different key. Do not overwrite."""

    code = "identity_collision"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "IdentityCollision('identity_collision')"


class FetchError(RuntimeError):
    """The current source failed. Other sources may continue."""

    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = _code(code)
        if isinstance(status, bool) or not isinstance(status, int):
            status = None
        self.status = status
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"FetchError({self.code!r})"


class RateLimited(FetchError):
    """Skip this host for the rest of the run. Do not retry immediately."""

    def __init__(self, host: str, retry_after_s: float, *, status: int | None = 429) -> None:
        if (
            not isinstance(host, str)
            or host == ""
            or any(ord(char) < 32 or ord(char) == 127 for char in host)
        ):
            raise ValueError("rate_limited")
        if isinstance(retry_after_s, bool) or not isinstance(retry_after_s, (int, float)):
            raise ValueError("rate_limited")
        seconds = float(retry_after_s)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("rate_limited")
        self.host = host
        self.retry_after_s = seconds
        super().__init__("rate_limited", status)


class GrokUnavailable(RuntimeError):
    """Stop later model calls in this run. Keep the unfinished queue."""

    def __init__(self, code: str, usage: Usage | None = None) -> None:
        self.code = _code(code)
        self.usage = usage if _looks_like_usage(usage) else None
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"GrokUnavailable({self.code!r})"


class BudgetExceeded(RuntimeError):
    """Do not spawn a process. Remaining items stay deferred."""

    def __init__(self, purpose: str) -> None:
        self.purpose = purpose if purpose in {"classify", "review"} else "unknown"
        super().__init__(self.purpose)

    def __repr__(self) -> str:
        return f"BudgetExceeded({self.purpose!r})"


class StorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = _code(code)
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"StorageError({self.code!r})"


class PublishError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = _code(code)
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"PublishError({self.code!r})"


class ConcurrentRun(RuntimeError):
    """Another run holds the lock. Exit before fetching."""

    code = "run_locked"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "ConcurrentRun('run_locked')"


def _looks_like_usage(value: object) -> bool:
    if value is None:
        return False
    required = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_cost_usd",
    )
    return all(hasattr(value, name) for name in required)
