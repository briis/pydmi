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


class _FairUseMixin:
    """Request headers and 429/4xx handling shared by all DMI clients.

    Subclasses set: user_agent, timeout, _blocked_until.
    """

    user_agent: str
    timeout: float
    _blocked_until: datetime | None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/prs.coverage+json, application/json",
            "Accept-Encoding": "gzip",
        }

    def _handle_status(self, status: int, retry_after: str | None, body: str) -> None:
        if status == 429:
            wait = parse_retry_after(retry_after)
            self._blocked_until = datetime.now(timezone.utc) + timedelta(seconds=wait)
            raise DMIRateLimitError(wait, header=retry_after, body=body[:300])
        if status == 413:
            raise DMIRequestError(status, "Request too large - ask for fewer parameters")
        if status >= 400:
            raise DMIRequestError(status, body[:300] or "error")


class _ForecastClientBase(_FairUseMixin):
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
        include_precipitation_probability: bool = True,
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
        # One extra request per update to DMI's ensemble for the chance of rain.
        self.include_precipitation_probability = include_precipitation_probability

        self._cache: Forecast | None = None
        self._blocked_until: datetime | None = None
        # Ensemble (chance of precipitation), cached separately from the forecast.
        self._probabilities: dict[datetime, float] | None = None
        self._probabilities_fetched: datetime | None = None
        self._probability_retry_at: datetime | None = None
        self.request_spacing = c.REQUEST_SPACING_S
        self.probability_timeout = max(timeout, c.PROBABILITY_TIMEOUT_S)
        self._probability_failures = 0

    # -- request ---------------------------------------------------------
    def _position_url(self, collection: str, parameters: Iterable[str]) -> str:
        """Fully encoded /position URL (space in POINT() must be %20, not +)."""
        coords = quote(f"POINT({self.longitude} {self.latitude})", safe="()")
        params = quote(",".join(parameters), safe=",-")
        return (
            f"{self.base_url}/collections/{quote(collection)}/position"
            f"?coords={coords}&crs=crs84&parameter-name={params}"
        )

    @property
    def url(self) -> str:
        """URL of the main forecast request."""
        return self._position_url(self.collection, self.parameters)

    @property
    def probability_url(self) -> str:
        """URL of the ensemble (chance of precipitation) request."""
        return self._position_url(
            c.EPS_PERCENTILE_COLLECTION,
            (c.P_EPS_PRECIP_1H.format(p=p) for p in c.EPS_PERCENTILES),
        )

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

    # -- ensemble bookkeeping -------------------------------------------------
    @property
    def probability_pending(self) -> bool:
        """True while the chance of precipitation is wanted but not yet available.

        Callers may poll more often meanwhile; only the ensemble request is retried,
        at most every PROBABILITY_RETRY_INTERVAL.
        """
        return self.include_precipitation_probability and self._probabilities is None

    def _probability_due(self, now: datetime) -> bool:
        if not self.include_precipitation_probability:
            return False
        if self._probabilities is None:
            return self._probability_retry_at is None or now >= self._probability_retry_at
        assert self._probabilities_fetched is not None
        return now - self._probabilities_fetched >= c.PROBABILITY_UPDATE_INTERVAL

    def _probability_ok(self, probabilities: dict[datetime, float], now: datetime) -> None:
        self._probabilities = probabilities
        self._probabilities_fetched = now
        self._probability_retry_at = None
        self._probability_failures = 0

    def _probability_failed(self, now: datetime) -> int:
        """Schedule a retry; returns the number of consecutive failures."""
        self._probability_failures += 1
        retry = now + c.PROBABILITY_RETRY_INTERVAL
        if self._blocked_until and self._blocked_until > retry:
            retry = self._blocked_until
        self._probability_retry_at = retry
        return self._probability_failures

    def _store(self, forecast: Forecast) -> Forecast:
        self._cache = forecast
        self._blocked_until = None
        return forecast
