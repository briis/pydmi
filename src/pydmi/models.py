"""Data models for pydmi.

All field names carry a unit suffix so they can be published to MQTT as-is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from .const import ATTRIBUTION, LICENSE_URL


@dataclass(frozen=True, slots=True)
class ForecastHour:
    """One hourly forecast step.

    `datetime` is the start of the hour (UTC). Instantaneous values (temperature,
    wind, ...) are valid at that time; `precipitation_mm` is the amount expected
    during the following hour (same convention as Home Assistant / met.no).
    """

    datetime: datetime
    condition: str | None
    temperature_c: float | None
    dew_point_c: float | None
    humidity_pct: float | None
    pressure_hpa: float | None
    wind_speed_ms: float | None
    wind_bearing_deg: float | None
    wind_gust_ms: float | None
    precipitation_mm: float | None
    precipitation_type: str | None
    cloud_cover_pct: float | None
    visibility_m: float | None
    lightning_probability_pct: float | None
    global_radiation_wm2: float | None
    is_daylight: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict (datetime as ISO 8601)."""
        data = asdict(self)
        data["datetime"] = self.datetime.isoformat()
        return data


@dataclass(frozen=True, slots=True)
class ForecastDay:
    """Daily aggregate in a given time zone."""

    date: date
    condition: str | None
    temperature_max_c: float | None
    temperature_min_c: float | None
    precipitation_mm: float | None
    wind_speed_max_ms: float | None
    wind_gust_max_ms: float | None
    wind_bearing_deg: float | None
    humidity_avg_pct: float | None
    cloud_cover_avg_pct: float | None
    hours_count: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat()
        return data


@dataclass(slots=True)
class Forecast:
    """A complete forecast for one location from one model run."""

    latitude: float  # grid point actually used by DMI
    longitude: float
    collection: str
    model_run: datetime | None  # first step of the run (UTC)
    fetched_at: datetime
    all_hours: list[ForecastHour] = field(default_factory=list)
    attribution: str = ATTRIBUTION
    license_url: str = LICENSE_URL

    def hourly(self, now: datetime | None = None) -> list[ForecastHour]:
        """Hours from the current hour onwards (past steps are dropped)."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = now.replace(minute=0, second=0, microsecond=0)
        return [h for h in self.all_hours if h.datetime >= start]

    def current(
        self, now: datetime | None = None, *, interpolate: bool = True
    ) -> ForecastHour | None:
        """Conditions right now.

        With `interpolate=True` (default) instantaneous values (temperature,
        pressure, wind, ...) are interpolated linearly between the two hourly
        steps around `now`, and `datetime` is `now`. Precipitation is the amount
        for the current hour. With `interpolate=False` the step for the start of
        the current hour is returned unchanged.
        """
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        hours = self.hourly(now)
        if not hours:
            return None
        if not interpolate or len(hours) < 2 or hours[0].datetime > now:
            return hours[0]
        from .parser import interpolate_hour  # local import avoids a cycle

        return interpolate_hour(hours[0], hours[1], now, self.latitude, self.longitude)

    def daily(
        self,
        tz: tzinfo | None = None,
        now: datetime | None = None,
        min_hours: int = 12,
    ) -> list[ForecastDay]:
        """Daily aggregates in time zone `tz` (default Europe/Copenhagen).

        Today is always included (rest of the day). Later days with fewer than
        `min_hours` hourly steps (the tail end of the ~60 h model run) are
        dropped so a partial day does not show a misleading max/min.
        """
        from .parser import aggregate_daily  # local import avoids a cycle

        if tz is None:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo("Europe/Copenhagen")
        return aggregate_daily(self.hourly(now), tz, min_hours=min_hours)

    @property
    def age(self) -> timedelta:
        return datetime.now(timezone.utc) - self.fetched_at

    def to_dict(self, now: datetime | None = None, tz: tzinfo | None = None) -> dict[str, Any]:
        """JSON-friendly representation (e.g. for an MQTT payload)."""
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "collection": self.collection,
            "model_run": self.model_run.isoformat() if self.model_run else None,
            "fetched_at": self.fetched_at.isoformat(),
            "attribution": self.attribution,
            "license_url": self.license_url,
            "hourly": [h.to_dict() for h in self.hourly(now)],
            "daily": [d.to_dict() for d in self.daily(tz, now)],
        }
