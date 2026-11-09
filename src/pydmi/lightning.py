"""Lightning strikes near a location, from DMI's Lightning Data API.

One request every 5 minutes for the last hour of strikes inside a box around
the location; strikes outside the radius are then dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any

from . import const as c
from ._base import _FairUseMixin
from .exceptions import DMIInvalidLocationError, DMIParseError, DMIRateLimitError

_KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True, slots=True)
class Strike:
    """One triangulated lightning observation."""

    observed: datetime
    latitude: float
    longitude: float
    distance_km: float
    bearing_deg: float  # direction FROM the location TO the strike (0 = north)
    current_ka: float | None  # signed peak current
    strokes: int | None
    type: int | None  # DMI's lightning type code, passed through as-is


@dataclass(slots=True)
class Lightning:
    """Strikes within `radius_km` during the last hour, newest first."""

    fetched_at: datetime
    radius_km: float
    strikes: list[Strike] = field(default_factory=list)
    attribution: str = c.ATTRIBUTION

    def _since(self, delta: timedelta) -> list[Strike]:
        cutoff = self.fetched_at - delta
        return [s for s in self.strikes if s.observed >= cutoff]

    @property
    def count_1h(self) -> int:
        return len(self.strikes)

    @property
    def count_10min(self) -> int:
        return len(self._since(timedelta(minutes=10)))

    @property
    def latest(self) -> Strike | None:
        """Most recent strike in the last hour."""
        return self.strikes[0] if self.strikes else None

    @property
    def nearest(self) -> Strike | None:
        """Closest strike in the last hour."""
        return min(self.strikes, key=lambda s: s.distance_km, default=None)

    def to_dict(self) -> dict[str, Any]:
        def strike(s: Strike | None) -> dict[str, Any] | None:
            if s is None:
                return None
            return {
                "observed": s.observed.isoformat(),
                "distance_km": s.distance_km,
                "bearing_deg": s.bearing_deg,
                "current_ka": s.current_ka,
            }

        return {
            "fetched_at": self.fetched_at.isoformat(),
            "radius_km": self.radius_km,
            "count_1h": self.count_1h,
            "count_10min": self.count_10min,
            "latest": strike(self.latest),
            "nearest": strike(self.nearest),
            "attribution": self.attribution,
        }


def _distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Great-circle distance (km) and initial bearing (deg) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    distance = 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    return distance, bearing


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _opt(value: Any, cast: type) -> Any:
    try:
        return None if value is None else cast(value)
    except (TypeError, ValueError):
        return None


def parse_lightning(
    data: dict[str, Any],
    latitude: float,
    longitude: float,
    radius_km: float,
    now: datetime,
) -> Lightning:
    """Strikes within radius_km of the location, newest first."""
    try:
        features = data["features"]
    except (KeyError, TypeError) as err:
        raise DMIParseError(f"Unexpected lightning response: {err}") from err
    strikes: list[Strike] = []
    for feature in features:
        props = feature.get("properties") or {}
        try:
            lon, lat = feature["geometry"]["coordinates"][:2]
            observed = _parse_time(props["observed"])
        except (KeyError, TypeError, ValueError):
            continue
        distance, bearing = _distance_bearing(latitude, longitude, float(lat), float(lon))
        if distance > radius_km:
            continue  # inside the box, outside the circle
        strikes.append(
            Strike(
                observed=observed,
                latitude=float(lat),
                longitude=float(lon),
                distance_km=round(distance, 1),
                bearing_deg=round(bearing),
                current_ka=_opt(props.get("amp"), float),
                strokes=_opt(props.get("strokes"), int),
                type=_opt(_opt(props.get("type"), float), int),  # sometimes "0.0"
            )
        )
    strikes.sort(key=lambda s: s.observed, reverse=True)
    return Lightning(fetched_at=now, radius_km=radius_km, strikes=strikes)


class _LightningClientBase(_FairUseMixin):
    """Configuration, cache and back-off for the lightning clients."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_km: float = c.LIGHTNING_RADIUS_KM,
        update_interval: timedelta = c.LIGHTNING_UPDATE_INTERVAL,
        timeout: float = c.DEFAULT_TIMEOUT,
        base_url: str = c.LIGHTNING_BASE_URL,
        user_agent: str | None = None,
    ) -> None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise DMIInvalidLocationError(f"Invalid coordinates {latitude}, {longitude}")
        if not 0 < radius_km <= c.LIGHTNING_MAX_RADIUS_KM:
            raise ValueError(f"radius_km must be between 0 and {c.LIGHTNING_MAX_RADIUS_KM}")
        self.latitude = round(latitude, c.COORDINATE_PRECISION)
        self.longitude = round(longitude, c.COORDINATE_PRECISION)
        self.radius_km = radius_km
        self.update_interval = max(update_interval, c.LIGHTNING_MIN_UPDATE_INTERVAL)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or c.USER_AGENT
        self._cache: Lightning | None = None
        self._blocked_until: datetime | None = None

    @property
    def cached(self) -> Lightning | None:
        return self._cache

    @property
    def url(self) -> str:
        """Box around the location (slightly larger than the radius), last hour."""
        dlat = self.radius_km / _KM_PER_DEG_LAT
        dlon = dlat / max(0.01, math.cos(math.radians(self.latitude)))
        bbox = (
            f"{self.longitude - dlon:.3f},{self.latitude - dlat:.3f},"
            f"{self.longitude + dlon:.3f},{self.latitude + dlat:.3f}"
        )
        return (
            f"{self.base_url}/collections/observation/items"
            f"?bbox={bbox}&period=latest-hour&limit={c.LIGHTNING_LIMIT}"
        )

    def _cache_valid(self, now: datetime) -> bool:
        return self._cache is not None and now - self._cache.fetched_at < self.update_interval

    def _check_blocked(self, now: datetime) -> Lightning | None:
        if self._blocked_until and now < self._blocked_until:
            if self._cache is not None:
                return self._cache
            raise DMIRateLimitError((self._blocked_until - now).total_seconds())
        return None

    def _parse(self, data: dict[str, Any], now: datetime) -> Lightning:
        return parse_lightning(data, self.latitude, self.longitude, self.radius_km, now)

    def _store(self, lightning: Lightning) -> Lightning:
        self._cache = lightning
        self._blocked_until = None
        return lightning
