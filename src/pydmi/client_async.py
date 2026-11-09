"""Async client (aiohttp) - for Home Assistant and other asyncio code."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

try:
    import aiohttp
    from yarl import URL
except ImportError as err:  # pragma: no cover
    raise ImportError(
        "The async client needs aiohttp: pip install 'pydmi-weather[async]'"
    ) from err

from ._base import _ForecastClientBase
from .exceptions import DMIConnectionError, DMIParseError
from .models import Forecast
from .parser import parse_coverage


class DMIForecastAsync(_ForecastClientBase):
    """Async DMI forecast client.

    Pass Home Assistant's shared session (`async_get_clientsession(hass)`);
    if no session is given the client creates and owns one.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        session: aiohttp.ClientSession | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(latitude, longitude, **kwargs)
        self._session = session
        self._own_session = session is None
        self._lock = asyncio.Lock()

    async def get_forecast(self, *, force: bool = False) -> Forecast:
        """Return the forecast, from cache if it is younger than `update_interval`.

        `force=True` skips the cache (but not a 429 back-off) - use sparingly.
        """
        async with self._lock:  # concurrent callers share one request
            now = datetime.now(timezone.utc)
            if (blocked := self._check_blocked(now)) is not None:
                return blocked
            if not force and self._cache_valid(now):
                assert self._cache is not None
                return self._cache
            data = await self._request()
            return self._store(parse_coverage(data, collection=self.collection))

    async def _request(self) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                URL(self.url, encoded=True),
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status >= 400:
                    self._handle_status(
                        resp.status, resp.headers.get("Retry-After"), await resp.text()
                    )
                try:
                    return await resp.json(content_type=None)
                except ValueError as err:
                    raise DMIParseError(f"Invalid JSON from DMI: {err}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise DMIConnectionError(f"Error talking to DMI: {err!r}") from err

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> DMIForecastAsync:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
