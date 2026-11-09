"""Async client (aiohttp) - for Home Assistant and other asyncio code."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Self

try:
    import aiohttp
    from yarl import URL
except ImportError as err:  # pragma: no cover
    raise ImportError(
        "The async client needs aiohttp: pip install 'pydmi-weather[async]'"
    ) from err

from . import const as c
from ._base import _ForecastClientBase
from .exceptions import DMIConnectionError, DMIError, DMIParseError, DMIRateLimitError
from .models import Forecast
from .observations import (
    Observations,
    _ObservationClientBase,
    build_observations,
    parse_observations,
)
from .parser import (
    apply_precipitation_probability,
    parse_coverage,
    parse_precipitation_probability,
)

_LOGGER = logging.getLogger(__name__)


class _AsyncRequester:
    """aiohttp GET of a DMI JSON document, with an injected or owned session."""

    _session: aiohttp.ClientSession | None
    _own_session: bool

    def _init_session(self, session: aiohttp.ClientSession | None) -> None:
        self._session = session
        self._own_session = session is None
        self._lock = asyncio.Lock()

    async def _request(self, url: str) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(
                URL(url, encoded=True),
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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


class DMIForecastAsync(_AsyncRequester, _ForecastClientBase):
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
        self._init_session(session)

    async def get_forecast(self, *, force: bool = False) -> Forecast:
        """Return the forecast, from cache if it is younger than `update_interval`.

        `force=True` skips the cache (but not a 429 back-off) - use sparingly.
        """
        async with self._lock:  # concurrent callers share one request
            now = datetime.now(timezone.utc)
            if (blocked := self._check_blocked(now)) is not None:
                return blocked
            fetched_forecast = False
            if force or not self._cache_valid(now):
                data = await self._request(self.url)
                forecast = self._store(parse_coverage(data, collection=self.collection))
                fetched_forecast = True
            else:
                assert self._cache is not None
                forecast = self._cache
            if self._probability_due(now):
                # Optional extra: a failure here never costs the main forecast.
                if fetched_forecast:
                    await asyncio.sleep(self.request_spacing)
                try:
                    probs = parse_precipitation_probability(await self._request(self.probability_url))
                    self._probability_ok(probs, now)
                except DMIError as err:
                    self._probability_failed(now)
                    _LOGGER.warning(
                        "DMI chance of precipitation unavailable, retrying in %s: %s",
                        c.PROBABILITY_RETRY_INTERVAL,
                        err,
                    )
            if self._probabilities is not None:
                apply_precipitation_probability(forecast, self._probabilities)
            return forecast


class DMIObservationsAsync(_AsyncRequester, _ObservationClientBase):
    """Async client for current observations near a location.

    Pass Home Assistant's shared session; without one the client owns a session.
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
        self._init_session(session)

    async def get_observations(self, *, force: bool = False) -> Observations:
        """Latest observations, from cache if younger than `update_interval` (10 min)."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            if (blocked := self._check_blocked(now)) is not None:
                return blocked
            if not force and self._cache_valid(now):
                assert self._cache is not None
                return self._cache
            requested = False
            if self._stations_due(now):
                try:
                    self._set_stations(await self._request(self.stations_url), now)
                    requested = True
                except DMIRateLimitError:
                    raise
                except DMIError as err:
                    if not self._chosen:
                        raise
                    _LOGGER.warning("DMI station list unavailable, keeping the old one: %s", err)
            per_station: dict[str, dict] = {}
            last_error: DMIError | None = None
            for station_id in self._station_ids():
                if requested:
                    await asyncio.sleep(self.request_spacing)
                requested = True
                try:
                    per_station[station_id] = parse_observations(
                        await self._request(self.observations_url(station_id))
                    )
                except DMIRateLimitError:
                    raise
                except DMIError as err:
                    last_error = err
                    _LOGGER.warning("DMI observations from station %s unavailable: %s", station_id, err)
            if not per_station and last_error is not None:
                raise last_error
            return self._store(build_observations(self._chosen, per_station, now))
