"""Protocols shared by later work packages.

Concrete parsers, the store, the HTTP client, and the publisher stay in the
modules that own them. This file does not provide placeholder implementations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Protocol, runtime_checkable

from second_brain.models import CompletionResult, FetchEvent, HttpResponse, RateState


@runtime_checkable
class Clock(Protocol):
    """Injected time source. now() is aware UTC. Tests must not sleep for real."""

    def now(self) -> datetime:
        """Return the current aware UTC instant."""

    def monotonic(self) -> float:
        """Return a monotonic clock reading in seconds."""

    def sleep(self, seconds: float) -> None:
        """Pause until the injected clock says the interval has elapsed."""


@runtime_checkable
class HttpTransport(Protocol):
    """One HTTP hop. The transport must not follow redirects."""

    def request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_s: int,
        max_bytes: int,
        purpose: str,
    ) -> HttpResponse:
        """Perform one GET and stop before reading a body that is too large or the wrong type."""


@runtime_checkable
class HttpStateStore(Protocol):
    """Persistent per-host rate state. Required. There is no optional store."""

    def get_rate_state(self, host: str) -> RateState:
        """Return the saved state for a host, or a fresh idle state."""

    def put_rate_state(self, state: RateState) -> None:
        """Replace the saved state for state.host."""

    def append_fetch_event(self, event: FetchEvent) -> None:
        """Append one redacted fetch event."""


@runtime_checkable
class CompletionFn(Protocol):
    """Model call used by classification and review. purpose is classify or review."""

    def __call__(self, prompt: str, *, schema: dict, purpose: str) -> CompletionResult:
        """Return one completion envelope. Do not spawn a process from unrelated modules."""


@runtime_checkable
class FixtureReader(Protocol):
    """Read one synthetic fixture. The implementation must reject paths that escape the fixture root."""

    def __call__(self, relative_uri: str) -> bytes:
        """Return the fixture bytes for a file:tests/fixtures/... URI."""
