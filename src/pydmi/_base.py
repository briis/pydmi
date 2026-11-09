"""Shared, I/O-free logic for the sync and async clients: URL building,
caching and fair-use bookkeeping."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import const as c
from .exceptions import DMIInvalidLocationError, DMIRateLimitError, DMIRequestError
from .models import Forecast


def parse_retry_after(value: str | None) -> float:
    """Seconds to wait from a Retry-After header (falls back to a safe default)."""
    if value:
        try:
            return max(1.0, float(value))
        except ValueError:
            pass
    return c.DEFAULT_RETRY_AFTER.total_seconds()


class _ForecastClientBase:
    """Holds configuration, cache and back-off state."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        collection: str = c.DEFAULT_COLLECTION,
        parameters: Iterable[str] = c.DEFAULT_PARAMETERS,
        update_interval: timedelta = c.DEFAULT_UPDATE_INTERVAL,
        timeout: float = c.DEFAULT_TIMEOUT,
        base_url: str = c.FORECAST_BASE_URL,
        user_agent: str | None = None,
    ) -> None:
        lon_min, lat_min, lon_max, lat_max = c.DINI_BBOX
        if collection.startswith("harmonie_dini") and not (
            lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max
        ):
            raise DMIInvalidLocationError(
                f"{latitude}, {longitude} is outside the HARMONIE DINI model area"
            )
        self.latitude = round(latitude, c.COORDINATE_PRECISION)
        self.longitude = round(longitude, c.COORDINATE_PRECISION)
        self.collection = collection
        self.parameters = tuple(dict.fromkeys(parameters))  # dedupe, keep order
        # Never poll more often than MIN_UPDATE_INTERVAL - the model updates every 3 h.
        self.update_interval = max(update_interval, c.MIN_UPDATE_INTERVAL)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or c.USER_AGENT

        self._cache: Forecast | None = None
        self._blocked_until: datetime | None = None

    # -- request ---------------------------------------------------------
    @property
    def url(self) -> str:
        """Fully encoded /position URL (space in POINT() must be %20, not +)."""
        coords = quote(f"POINT({self.longitude} {self.latitude})", safe="()")
        params = quote(",".join(self.parameters), safe=",-")
        return (
            f"{self.base_url}/collections/{quote(self.collection)}/position"
            f"?coords={coords}&crs=crs84&parameter-name={params}"
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/prs.coverage+json, application/json",
            "Accept-Encoding": "gzip",
        }

    # -- cache / fair use -------------------------------------------------
    @property
    def cached(self) -> Forecast | None:
        """Last successfully fetched forecast (may be stale)."""
        return self._cache

    def _cache_valid(self, now: datetime) -> bool:
        return self._cache is not None and now - self._cache.fetched_at < self.update_interval

    def _check_blocked(self, now: datetime) -> Forecast | None:
        """While backing off after a 429: return cache, or raise if there is none."""
        if self._blocked_until and now < self._blocked_until:
            if self._cache is not None:
                return self._cache
            raise DMIRateLimitError((self._blocked_until - now).total_seconds())
        return None

    def _handle_status(self, status: int, retry_after: str | None, body: str) -> None:
        if status == 429:
            wait = parse_retry_after(retry_after)
            self._blocked_until = datetime.now(timezone.utc) + timedelta(seconds=wait)
            raise DMIRateLimitError(wait, header=retry_after, body=body[:300])
        if status == 413:
            raise DMIRequestError(status, "Request too large - ask for fewer parameters")
        if status >= 400:
            raise DMIRequestError(status, body[:300] or "error")

    def _store(self, forecast: Forecast) -> Forecast:
        self._cache = forecast
        self._blocked_until = None
        return forecast
