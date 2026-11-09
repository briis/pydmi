"""Blocking client using only the standard library - for threaded services
such as the WeatherDataLogger pipeline (no asyncio, no extra dependencies)."""

from __future__ import annotations

import gzip
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

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


class _SyncRequester:
    """Blocking GET of a DMI JSON document (stdlib only)."""

    def _request(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            self._handle_status(err.code, err.headers.get("Retry-After"), body)
            raise  # pragma: no cover - _handle_status always raises for >= 400
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as err:
            raise DMIConnectionError(f"Error talking to DMI: {err!r}") from err
        try:
            return json.loads(raw)
        except ValueError as err:
            raise DMIParseError(f"Invalid JSON from DMI: {err}") from err


class DMIForecast(_SyncRequester, _ForecastClientBase):
    """Synchronous DMI forecast client (thread-safe)."""

    def __init__(self, latitude: float, longitude: float, **kwargs: Any) -> None:
        super().__init__(latitude, longitude, **kwargs)
        self._lock = threading.Lock()

    def get_forecast(self, *, force: bool = False) -> Forecast:
        """Return the forecast, from cache if it is younger than `update_interval`."""
        with self._lock:
            now = datetime.now(timezone.utc)
            if (blocked := self._check_blocked(now)) is not None:
                return blocked
            fetched_forecast = False
            if force or not self._cache_valid(now):
                data = self._request(self.url)
                forecast = self._store(parse_coverage(data, collection=self.collection))
                fetched_forecast = True
            else:
                assert self._cache is not None
                forecast = self._cache
            if self._probability_due(now):
                # Optional extra: a failure here never costs the main forecast.
                if fetched_forecast:
                    time.sleep(self.request_spacing)
                try:
                    probs = parse_precipitation_probability(self._request(self.probability_url))
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


class DMIObservations(_SyncRequester, _ObservationClientBase):
    """Synchronous client for current observations near a location (thread-safe)."""

    def __init__(self, latitude: float, longitude: float, **kwargs: Any) -> None:
        super().__init__(latitude, longitude, **kwargs)
        self._lock = threading.Lock()

    def get_observations(self, *, force: bool = False) -> Observations:
        """Latest observations, from cache if younger than `update_interval` (10 min)."""
        with self._lock:
            now = datetime.now(timezone.utc)
            if (blocked := self._check_blocked(now)) is not None:
                return blocked
            if not force and self._cache_valid(now):
                assert self._cache is not None
                return self._cache
            requested = False
            if self._stations_due(now):
                try:
                    self._set_stations(self._request(self.stations_url), now)
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
                    time.sleep(self.request_spacing)
                requested = True
                try:
                    per_station[station_id] = parse_observations(
                        self._request(self.observations_url(station_id))
                    )
                except DMIRateLimitError:
                    raise
                except DMIError as err:
                    last_error = err
                    _LOGGER.warning("DMI observations from station %s unavailable: %s", station_id, err)
            if not per_station and last_error is not None:
                raise last_error
            return self._store(build_observations(self._chosen, per_station, now))
