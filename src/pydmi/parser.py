"""Pure functions: CoverageJSON -> models. No I/O here, so it is easy to test."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timezone, tzinfo
from typing import Any

from . import const as c
from .exceptions import DMIInvalidLocationError, DMIParseError
from .models import Forecast, ForecastDay, ForecastHour
from .sun import is_daylight

KELVIN = 273.15


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _k_to_c(value: float | None) -> float | None:
    return None if value is None else value - KELVIN


def _scale(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def derive_condition(
    *,
    precipitation_mm: float | None,
    precipitation_type: str | None,
    cloud_cover_pct: float | None,
    visibility_m: float | None,
    lightning_probability_pct: float | None,
    wind_speed_ms: float | None,
    daylight: bool,
) -> str | None:
    """Map model output to a Home Assistant weather condition string.

    DMI's EDR API has no "weather symbol", so we derive one. Order matters:
    thunder > precipitation > fog > wind > clouds.
    """
    raining = precipitation_mm is not None and precipitation_mm >= c.PRECIP_THRESHOLD_MM

    if (
        lightning_probability_pct is not None
        and lightning_probability_pct >= c.LIGHTNING_THRESHOLD_PCT
    ):
        return c.COND_LIGHTNING_RAINY if raining else c.COND_LIGHTNING

    if raining:
        if precipitation_type == "snow":
            return c.COND_SNOWY
        if precipitation_type == "sleet":
            return c.COND_SNOWY_RAINY
        if precipitation_type in ("hail", "graupel"):
            return c.COND_HAIL
        if precipitation_mm is not None and precipitation_mm >= c.POURING_THRESHOLD_MM:
            return c.COND_POURING
        return c.COND_RAINY

    if visibility_m is not None and visibility_m < c.FOG_VISIBILITY_M:
        return c.COND_FOG

    if wind_speed_ms is not None and wind_speed_ms >= c.WINDY_THRESHOLD_MS:
        if cloud_cover_pct is not None and cloud_cover_pct >= c.CLOUD_PARTLY_PCT:
            return c.COND_WINDY_VARIANT
        return c.COND_WINDY

    if cloud_cover_pct is None:
        return None
    if cloud_cover_pct < c.CLOUD_CLEAR_PCT:
        return c.COND_SUNNY if daylight else c.COND_CLEAR_NIGHT
    if cloud_cover_pct < c.CLOUD_PARTLY_PCT:
        return c.COND_PARTLYCLOUDY
    return c.COND_CLOUDY


def _series(ranges: dict[str, Any], name: str, length: int) -> list[float | None]:
    """Flatten a [t, y, x] NdArray for a single position into a list of length t."""
    block = ranges.get(name)
    if not block:
        return [None] * length
    values = block.get("values") or []
    if len(values) != length:
        raise DMIParseError(f"{name}: expected {length} values, got {len(values)}")
    return [None if v is None or (isinstance(v, float) and math.isnan(v)) else v for v in values]


def parse_coverage(
    data: dict[str, Any],
    *,
    collection: str = c.DEFAULT_COLLECTION,
    fetched_at: datetime | None = None,
) -> Forecast:
    """Parse a CoverageJSON /position response into a Forecast."""
    try:
        axes = data["domain"]["axes"]
        times = [_parse_time(t) for t in axes["t"]["values"]]
        lon = float(axes["x"]["values"][0])
        lat = float(axes["y"]["values"][0])
        ranges = data["ranges"]
    except (KeyError, IndexError, TypeError, ValueError) as err:
        raise DMIParseError(f"Unexpected CoverageJSON structure: {err}") from err

    n = len(times)
    get = lambda name: _series(ranges, name, n)  # noqa: E731

    temp = get(c.P_TEMPERATURE)
    dew = get(c.P_DEW_POINT)
    rh = get(c.P_HUMIDITY)
    pres = get(c.P_PRESSURE)
    ws = get(c.P_WIND_SPEED)
    wd = get(c.P_WIND_DIR)
    gust = get(c.P_WIND_GUST)
    acc = get(c.P_PRECIP_ACC)
    ptype = get(c.P_PRECIP_TYPE)
    cloud = get(c.P_CLOUD_COVER)
    vis = get(c.P_VISIBILITY)
    light = get(c.P_LIGHTNING)
    rad = get(c.P_GLOBAL_RADIATION)

    if all(v is None for v in temp) and all(v is None for v in ws):
        raise DMIInvalidLocationError(f"No data for {lat}, {lon} - outside the model area?")

    hours: list[ForecastHour] = []
    for i, when in enumerate(times):
        # total-precipitation is accumulated since the start of the run (kg/m2 == mm).
        # Amount for the coming hour = acc[i+1] - acc[i]; last step is unknown.
        precip: float | None = None
        if i + 1 < n and acc[i] is not None and acc[i + 1] is not None:
            # Round once, here, so the displayed amount, the condition and the
            # interpolated "now" all use the same number (0.06 -> 0.1 -> rainy).
            precip = round(max(0.0, acc[i + 1] - acc[i]), 1)

        p_type = None
        if precip is not None and precip >= c.PRECIP_THRESHOLD_MM and ptype[i] is not None:
            p_type = c.PRECIPITATION_TYPES.get(int(ptype[i]))

        cloud_pct = _scale(cloud[i], 100)
        daylight = is_daylight(when, lat, lon)

        hours.append(
            ForecastHour(
                datetime=when,
                condition=derive_condition(
                    precipitation_mm=precip,
                    precipitation_type=p_type,
                    cloud_cover_pct=cloud_pct,
                    visibility_m=vis[i],
                    lightning_probability_pct=light[i],
                    wind_speed_ms=ws[i],
                    daylight=daylight,
                ),
                temperature_c=_round(_k_to_c(temp[i])),
                dew_point_c=_round(_k_to_c(dew[i])),
                humidity_pct=_round(rh[i], 0),
                pressure_hpa=_round(_scale(pres[i], 0.01)),
                wind_speed_ms=_round(ws[i]),
                wind_bearing_deg=_round(wd[i], 0),
                wind_gust_ms=_round(gust[i]),
                precipitation_mm=precip,
                precipitation_type=p_type,
                cloud_cover_pct=_round(cloud_pct, 0),
                visibility_m=_round(vis[i], 0),
                lightning_probability_pct=_round(light[i], 1),  # already percent
                global_radiation_wm2=_round(max(0.0, rad[i]), 0) if rad[i] is not None else None,
                is_daylight=daylight,
            )
        )

    return Forecast(
        latitude=round(lat, 4),
        longitude=round(lon, 4),
        collection=collection,
        model_run=times[0] if times else None,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        all_hours=hours,
    )


# --- interpolation ---------------------------------------------------------


def _lerp(a: float | None, b: float | None, f: float, digits: int = 1) -> float | None:
    if a is None or b is None:
        return a if f < 0.5 else b
    return round(a + (b - a) * f, digits)


def _lerp_bearing(
    a: float | None, b: float | None, f: float
) -> float | None:
    """Interpolate directions along the shortest arc (350 -> 10 passes 0)."""
    if a is None or b is None:
        return a if f < 0.5 else b
    diff = (b - a + 180) % 360 - 180
    return round((a + diff * f) % 360)


def interpolate_hour(
    h0: ForecastHour, h1: ForecastHour, when: datetime, latitude: float, longitude: float
) -> ForecastHour:
    """Conditions at `when`, between hourly steps h0 and h1."""
    span = (h1.datetime - h0.datetime).total_seconds()
    f = 0.0 if span <= 0 else min(1.0, max(0.0, (when - h0.datetime).total_seconds() / span))

    cloud = _lerp(h0.cloud_cover_pct, h1.cloud_cover_pct, f, 0)
    vis = _lerp(h0.visibility_m, h1.visibility_m, f, 0)
    wind = _lerp(h0.wind_speed_ms, h1.wind_speed_ms, f)
    lightning = _lerp(h0.lightning_probability_pct, h1.lightning_probability_pct, f)
    daylight = is_daylight(when, latitude, longitude)

    return ForecastHour(
        datetime=when,
        condition=derive_condition(
            precipitation_mm=h0.precipitation_mm,  # amount for the current hour
            precipitation_type=h0.precipitation_type,
            cloud_cover_pct=cloud,
            visibility_m=vis,
            lightning_probability_pct=lightning,
            wind_speed_ms=wind,
            daylight=daylight,
        ),
        temperature_c=_lerp(h0.temperature_c, h1.temperature_c, f),
        dew_point_c=_lerp(h0.dew_point_c, h1.dew_point_c, f),
        humidity_pct=_lerp(h0.humidity_pct, h1.humidity_pct, f, 0),
        pressure_hpa=_lerp(h0.pressure_hpa, h1.pressure_hpa, f),
        wind_speed_ms=wind,
        wind_bearing_deg=_lerp_bearing(h0.wind_bearing_deg, h1.wind_bearing_deg, f),
        wind_gust_ms=_lerp(h0.wind_gust_ms, h1.wind_gust_ms, f),
        precipitation_mm=h0.precipitation_mm,
        precipitation_type=h0.precipitation_type,
        cloud_cover_pct=cloud,
        visibility_m=vis,
        lightning_probability_pct=lightning,
        global_radiation_wm2=_lerp(h0.global_radiation_wm2, h1.global_radiation_wm2, f, 0),
        is_daylight=daylight,
    )


# --- daily aggregation ----------------------------------------------------

# Higher number wins when picking the condition for a day.
_SEVERITY = {
    c.COND_LIGHTNING_RAINY: 10,
    c.COND_LIGHTNING: 9,
    c.COND_HAIL: 8,
    c.COND_POURING: 7,
    c.COND_SNOWY: 6,
    c.COND_SNOWY_RAINY: 5,
    c.COND_RAINY: 4,
}


def _mean_bearing(bearings: Sequence[float], speeds: Sequence[float]) -> float | None:
    """Speed-weighted vector mean of wind directions."""
    if not bearings:
        return None
    x = sum(s * math.sin(math.radians(b)) for b, s in zip(bearings, speeds))
    y = sum(s * math.cos(math.radians(b)) for b, s in zip(bearings, speeds))
    if x == 0 and y == 0:
        return None
    return round(math.degrees(math.atan2(x, y)) % 360)


def _avg(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 0) if vals else None


def _day_condition(hours: Sequence[ForecastHour], precip_total: float | None) -> str | None:
    conditions = [h.condition for h in hours if h.condition]
    if not conditions:
        return None
    # Thunder or hail anywhere in the day is worth showing.
    severe = [x for x in conditions if _SEVERITY.get(x, 0) >= 8]
    if severe:
        return max(severe, key=_SEVERITY.__getitem__)
    # A wet day (>= 1 mm) shows its dominant precipitation condition.
    if precip_total is not None and precip_total >= 1.0:
        wet = [x for x in conditions if x in _SEVERITY]
        if wet:
            return Counter(wet).most_common(1)[0][0]
    # Otherwise: the most common daytime condition, day-variant.
    day = [h.condition for h in hours if h.condition and h.is_daylight] or conditions
    cond = Counter(day).most_common(1)[0][0]
    return c.COND_SUNNY if cond == c.COND_CLEAR_NIGHT else cond


def aggregate_daily(
    hours: Sequence[ForecastHour], tz: tzinfo, *, min_hours: int = 12
) -> list[ForecastDay]:
    """Group hourly steps into local calendar days."""
    buckets: dict[date, list[ForecastHour]] = defaultdict(list)
    for h in hours:
        buckets[h.datetime.astimezone(tz).date()].append(h)

    days: list[ForecastDay] = []
    for idx, day in enumerate(sorted(buckets)):
        hs = buckets[day]
        if idx > 0 and len(hs) < min_hours:
            continue
        temps = [h.temperature_c for h in hs if h.temperature_c is not None]
        precs = [h.precipitation_mm for h in hs if h.precipitation_mm is not None]
        winds = [h.wind_speed_ms for h in hs if h.wind_speed_ms is not None]
        gusts = [h.wind_gust_ms for h in hs if h.wind_gust_ms is not None]
        pairs = [
            (h.wind_bearing_deg, h.wind_speed_ms)
            for h in hs
            if h.wind_bearing_deg is not None and h.wind_speed_ms is not None
        ]
        precip_total = round(sum(precs), 1) if precs else None
        days.append(
            ForecastDay(
                date=day,
                condition=_day_condition(hs, precip_total),
                temperature_max_c=max(temps) if temps else None,
                temperature_min_c=min(temps) if temps else None,
                precipitation_mm=precip_total,
                wind_speed_max_ms=max(winds) if winds else None,
                wind_gust_max_ms=max(gusts) if gusts else None,
                wind_bearing_deg=_mean_bearing([p[0] for p in pairs], [p[1] for p in pairs]),
                humidity_avg_pct=_avg([h.humidity_pct for h in hs]),
                cloud_cover_avg_pct=_avg([h.cloud_cover_pct for h in hs]),
                hours_count=len(hs),
            )
        )
    return days
