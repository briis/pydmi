"""Minimal solar position calculation (NOAA approximation), no dependencies.

Accurate to a fraction of a degree - plenty for deciding day vs. night.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def solar_elevation(when: datetime, latitude: float, longitude: float) -> float:
    """Return the sun's elevation above the horizon in degrees."""
    when = when.astimezone(timezone.utc)
    day_of_year = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600

    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_minutes = hour * 60 + eqtime + 4 * longitude
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat = math.radians(latitude)

    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(
        hour_angle
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90 - math.degrees(math.acos(cos_zenith))


def is_daylight(when: datetime, latitude: float, longitude: float) -> bool:
    """True when the sun is above the horizon (incl. refraction)."""
    return solar_elevation(when, latitude, longitude) > -0.833
