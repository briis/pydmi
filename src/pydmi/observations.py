"""Current observations from DMI's Meteorological Observation API (metObs).

For each measurement we pick the nearest active Synop station that measures it
(e.g. temperature from Roskilde Lufthavn, radiation from Sjælsmark), then fetch
the latest hour from each of those stations - one request per station.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any
from urllib.parse import quote

from . import const as c
from ._base import _FairUseMixin
from .exceptions import DMIInvalidLocationError, DMIParseError, DMIRateLimitError

# Our field name -> DMI parameterId. All already in the units we use.
OBSERVATION_PARAMETERS: dict[str, str] = {
    "temperature_c": "temp_dry",
    "dew_point_c": "temp_dew",
    "humidity_pct": "humidity",
    "pressure_hpa": "pressure_at_sea",
    "wind_speed_ms": "wind_speed",  # 10-minute mean
    "wind_bearing_deg": "wind_dir",
    "wind_gust_ms": "wind_max",  # highest 3-second gust in the last 10 minutes
    "precipitation_10min_mm": "precip_past10min",
    "precipitation_1h_mm": "precip_past1h",
    "cloud_cover_pct": "cloud_cover",
    # "visibility" is listed by stations but not reported every 10 minutes;
    # the 10-minute mean is (verified against Roskilde Lufthavn, 2026-11-09).
    "visibility_m": "visib_mean_last10min",
    "global_radiation_wm2": "radia_glob",
    "sunshine_10min_min": "sun_last10min_glob",
}


@dataclass(frozen=True, slots=True)
class Station:
    """An active DMI weather station."""

    station_id: str
    name: str
    latitude: float
    longitude: float
    parameters: frozenset[str]
    distance_km: float = 0.0


@dataclass(frozen=True, slots=True)
class ObservedValue:
    """One measurement, with where and when it was observed."""

    value: float
    observed: datetime
    station_id: str
    station_name: str
    distance_km: float


@dataclass(slots=True)
class Observations:
    """Latest observations around a location. Missing or stale values are None."""

    fetched_at: datetime
    values: dict[str, ObservedValue] = field(default_factory=dict)
    attribution: str = c.ATTRIBUTION

    def get(self, name: str) -> float | None:
        item = self.values.get(name)
        return item.value if item else None

    def __getattr__(self, name: str) -> float | None:
        # obs.temperature_c etc. -> value or None
        if name in OBSERVATION_PARAMETERS:
            return self.get(name)
        raise AttributeError(name)

    @property
    def stations(self) -> dict[str, str]:
        """station_id -> name of the stations actually used."""
        return {v.station_id: v.station_name for v in self.values.values()}

    @property
    def observed(self) -> datetime | None:
        """Newest observation time among the values."""
        return max((v.observed for v in self.values.values()), default=None)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly: values with unit suffix plus per-value source details."""
        data: dict[str, Any] = {name: self.get(name) for name in OBSERVATION_PARAMETERS}
        data["fetched_at"] = self.fetched_at.isoformat()
        data["observed_at"] = self.observed.isoformat() if self.observed else None
        data["attribution"] = self.attribution
        data["sources"] = {
            name: {**asdict(v), "observed": v.observed.isoformat()} for name, v in self.values.items()
        }
        return data


# --- pure functions ------------------------------------------------------------


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371.0 * math.hypot(x, y)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_stations(data: dict[str, Any], latitude: float, longitude: float) -> list[Station]:
    """Currently valid Synop stations from a metObs station response, nearest first.

    The station list also holds a station's historical periods (closed stations,
    old positions). Only entries with validTo = null describe the station today.
    """
    try:
        features = data["features"]
    except (KeyError, TypeError) as err:
        raise DMIParseError(f"Unexpected station response: {err}") from err
    stations: dict[str, Station] = {}
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("validTo") is not None or props.get("status") != "Active":
            continue
        if props.get("type") not in c.OBS_STATION_TYPES:
            continue
        try:
            lon, lat = feature["geometry"]["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            continue
        station = Station(
            station_id=str(props["stationId"]),
            name=str(props.get("name") or props["stationId"]),
            latitude=float(lat),
            longitude=float(lon),
            parameters=frozenset(props.get("parameterId") or []),
            distance_km=round(_distance_km(latitude, longitude, float(lat), float(lon)), 1),
        )
        stations[station.station_id] = station
    return sorted(stations.values(), key=lambda s: s.distance_km)


def select_stations(
    stations: list[Station], max_distance_km: float = c.OBS_MAX_DISTANCE_KM
) -> dict[str, Station]:
    """Our field name -> nearest station (within max distance) that measures it."""
    chosen: dict[str, Station] = {}
    for name, param in OBSERVATION_PARAMETERS.items():
        for station in stations:  # nearest first
            if station.distance_km > max_distance_km:
                break
            if param in station.parameters:
                chosen[name] = station
                break
    return chosen


def parse_observations(data: dict[str, Any]) -> dict[str, tuple[datetime, float]]:
    """parameterId -> (observed, value) for the newest value of each parameter."""
    try:
        features = data["features"]
    except (KeyError, TypeError) as err:
        raise DMIParseError(f"Unexpected observation response: {err}") from err
    newest: dict[str, tuple[datetime, float]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        param, value, observed = props.get("parameterId"), props.get("value"), props.get("observed")
        if param is None or value is None or observed is None:
            continue
        when = _parse_time(observed)
        if param not in newest or when > newest[param][0]:
            newest[param] = (when, float(value))
    return newest


def build_observations(
    chosen: dict[str, Station],
    per_station: dict[str, dict[str, tuple[datetime, float]]],
    now: datetime,
    max_age: timedelta = c.OBS_MAX_AGE,
) -> Observations:
    """Pick each field's value from its station; drop values older than max_age."""
    result = Observations(fetched_at=now)
    for name, station in chosen.items():
        item = per_station.get(station.station_id, {}).get(OBSERVATION_PARAMETERS[name])
        if item is None:
            continue
        observed, value = item
        if now - observed > max_age:
            continue  # station down or parameter not reported recently
        result.values[name] = ObservedValue(
            value=round(value, 2),
            observed=observed,
            station_id=station.station_id,
            station_name=station.name,
            distance_km=station.distance_km,
        )
    return result


# --- client base (no I/O) ------------------------------------------------------


class _ObservationClientBase(_FairUseMixin):
    """Configuration, caches and back-off for the observation clients."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        *,
        update_interval: timedelta = c.OBS_UPDATE_INTERVAL,
        timeout: float = c.DEFAULT_TIMEOUT,
        base_url: str = c.METOBS_BASE_URL,
        user_agent: str | None = None,
        max_distance_km: float = c.OBS_MAX_DISTANCE_KM,
    ) -> None:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise DMIInvalidLocationError(f"Invalid coordinates {latitude}, {longitude}")
        self.latitude = round(latitude, c.COORDINATE_PRECISION)
        self.longitude = round(longitude, c.COORDINATE_PRECISION)
        # Stations report every 10 minutes - polling faster gains nothing.
        self.update_interval = max(update_interval, c.OBS_MIN_UPDATE_INTERVAL)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or c.USER_AGENT
        self.max_distance_km = max_distance_km
        self.request_spacing = c.REQUEST_SPACING_S

        self._cache: Observations | None = None
        self._blocked_until: datetime | None = None
        self._stations: list[Station] | None = None
        self._stations_fetched: datetime | None = None
        self._chosen: dict[str, Station] = {}

    @property
    def cached(self) -> Observations | None:
        return self._cache

    @property
    def stations(self) -> dict[str, Station]:
        """Field name -> station currently used for it."""
        return dict(self._chosen)

    @property
    def stations_url(self) -> str:
        dlat, dlon = c.OBS_SEARCH_DLAT, c.OBS_SEARCH_DLON
        bbox = (
            f"{self.longitude - dlon:.2f},{self.latitude - dlat:.2f},"
            f"{self.longitude + dlon:.2f},{self.latitude + dlat:.2f}"
        )
        return f"{self.base_url}/collections/station/items?bbox={bbox}&status=Active&limit=10000"

    def observations_url(self, station_id: str) -> str:
        return (
            f"{self.base_url}/collections/observation/items"
            f"?stationId={quote(station_id)}&period=latest-hour&limit=1000"
        )

    def _cache_valid(self, now: datetime) -> bool:
        return self._cache is not None and now - self._cache.fetched_at < self.update_interval

    def _stations_due(self, now: datetime) -> bool:
        return (
            self._stations is None
            or self._stations_fetched is None
            or now - self._stations_fetched >= c.OBS_STATIONS_UPDATE_INTERVAL
        )

    def _check_blocked(self, now: datetime) -> Observations | None:
        if self._blocked_until and now < self._blocked_until:
            if self._cache is not None:
                return self._cache
            raise DMIRateLimitError((self._blocked_until - now).total_seconds())
        return None

    def _set_stations(self, data: dict[str, Any], now: datetime) -> None:
        stations = parse_stations(data, self.latitude, self.longitude)
        chosen = select_stations(stations, self.max_distance_km)
        if not chosen:
            raise DMIInvalidLocationError(
                f"No DMI weather station within {self.max_distance_km:.0f} km of "
                f"{self.latitude}, {self.longitude}"
            )
        self._stations, self._chosen, self._stations_fetched = stations, chosen, now

    def _station_ids(self) -> list[str]:
        """Stations to query, nearest first, each once."""
        seen: dict[str, float] = {s.station_id: s.distance_km for s in self._chosen.values()}
        return sorted(seen, key=seen.__getitem__)

    def _store(self, observations: Observations) -> Observations:
        self._cache = observations
        self._blocked_until = None
        return observations
