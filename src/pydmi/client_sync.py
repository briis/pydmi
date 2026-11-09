"""Blocking client using only the standard library - for threaded services
such as the WeatherDataLogger pipeline (no asyncio, no extra dependencies)."""

from __future__ import annotations

import gzip
import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ._base import _ForecastClientBase
from .exceptions import DMIConnectionError, DMIParseError
from .models import Forecast
from .parser import parse_coverage


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
            data = self._request()
            return self._store(parse_coverage(data, collection=self.collection))

    def _request(self) -> dict[str, Any]:
        req = urllib.request.Request(self.url, headers=self.headers)
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
