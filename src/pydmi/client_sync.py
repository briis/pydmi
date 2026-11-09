"""Blocking client using only the standard library - for threaded services
such as the WeatherDataLogger pipeline (no asyncio, no extra dependencies)."""

from __future__ import annotations

import gzip
import json
import logging
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ._base import _ForecastClientBase
from .exceptions import DMIConnectionError, DMIError, DMIParseError
from .models import Forecast
from .parser import (
    apply_precipitation_probability,
    parse_coverage,
    parse_precipitation_probability,
)

_LOGGER = logging.getLogger(__name__)


class DMIForecast(_ForecastClientBase):
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
            if not force and self._cache_valid(now):
                assert self._cache is not None
                return self._cache
            forecast = self._store(parse_coverage(self._request(self.url), collection=self.collection))
            if self.include_precipitation_probability:
                # Optional extra: a failure here never costs the main forecast.
                try:
                    probs = parse_precipitation_probability(self._request(self.probability_url))
                    apply_precipitation_probability(forecast, probs)
                except DMIError as err:
                    _LOGGER.warning("DMI chance of precipitation unavailable: %s", err)
            return forecast

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
