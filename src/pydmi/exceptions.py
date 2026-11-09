"""Exceptions for pydmi."""

from __future__ import annotations


class DMIError(Exception):
    """Base class for all pydmi errors."""


class DMIConnectionError(DMIError):
    """Network problem or timeout talking to DMI."""


class DMIRequestError(DMIError):
    """DMI answered with an HTTP error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class DMIRateLimitError(DMIRequestError):
    """DMI's fair use limit was hit (HTTP 429). Back off for `retry_after` seconds."""

    def __init__(
        self, retry_after: float, header: str | None = None, body: str = ""
    ) -> None:
        detail = f"Retry-After header: {header!r}" if header else "no Retry-After header"
        msg = f"Rate limited by DMI, retry after {retry_after:.0f} s ({detail})"
        if body:
            msg += f" - {body}"
        super().__init__(429, msg)
        self.retry_after = retry_after
        self.retry_after_header = header
        self.body = body


class DMIInvalidLocationError(DMIError):
    """Location is outside the model area (or DMI returned no data for it)."""


class DMIParseError(DMIError):
    """The response could not be understood."""
