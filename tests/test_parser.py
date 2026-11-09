from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pydmi import DMIInvalidLocationError, parse_coverage
from pydmi.sun import solar_elevation

from .conftest import RUN, make_coverage


def test_basic_conversion(coverage):
    fc = parse_coverage(coverage)
    assert fc.model_run == RUN
    assert len(fc.all_hours) == 61
    h0 = fc.all_hours[0]
    assert h0.temperature_c == 7.0  # 280.15 K
    assert h0.dew_point_c == 4.0
    assert h0.pressure_hpa == 1013.2
    assert h0.cloud_cover_pct == 10
    assert h0.wind_bearing_deg == 270
    assert fc.latitude == 55.6496 and fc.longitude == 12.0801


def test_precipitation_is_deaccumulated(coverage):
    hours = parse_coverage(coverage).all_hours
    # acc rises 2 mm at steps 10..13 -> the hours *starting* at 9..12 are wet
    assert [h.precipitation_mm for h in hours[8:14]] == [0.0, 2.0, 2.0, 2.0, 2.0, 0.0]
    assert hours[9].precipitation_type == "rain"
    assert hours[13].precipitation_type is None
    assert hours[-1].precipitation_mm is None  # unknown after the last step


def test_conditions(coverage):
    hours = parse_coverage(coverage).all_hours
    # 03 UTC early November in Denmark: dark and few clouds
    assert hours[0].condition == "clear-night"
    assert hours[9].condition == "rainy"
    assert hours[19].condition == "lightning"
    assert hours[30].condition == "windy-variant"
    assert hours[40].condition == "fog"
    assert hours[7].condition == "cloudy"


def test_hourly_drops_past(coverage):
    fc = parse_coverage(coverage)
    now = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)
    assert fc.hourly(now)[0].datetime == datetime(2026, 11, 9, 10, tzinfo=timezone.utc)
    assert fc.current(now).datetime.hour == 10


def test_daily(coverage):
    fc = parse_coverage(coverage)
    now = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
    days = fc.daily(ZoneInfo("Europe/Copenhagen"), now)
    # run ends 2026-11-11 15 UTC = 16 local -> 11 Nov has 16 h (kept), today partial
    assert [d.date.isoformat() for d in days] == ["2026-11-09", "2026-11-10", "2026-11-11"]
    today = days[0]
    assert today.precipitation_mm == 8.0
    assert today.condition == "lightning"  # severe event anywhere in the day wins
    assert today.wind_bearing_deg == 270
    assert days[1].condition == "cloudy"  # one windy + one foggy hour do not dominate


def test_outside_model_area():
    cov = make_coverage()
    for r in cov["ranges"].values():
        r["values"] = [None] * len(r["values"])
    with pytest.raises(DMIInvalidLocationError):
        parse_coverage(cov)


def test_solar_elevation_roskilde():
    # Midsummer noon in Roskilde ~ 58 deg; midwinter midnight well below horizon
    assert 55 < solar_elevation(datetime(2026, 6, 21, 11, 15, tzinfo=timezone.utc), 55.64, 12.08) < 60
    assert solar_elevation(datetime(2026, 12, 21, 23, tzinfo=timezone.utc), 55.64, 12.08) < -50


def test_to_dict_is_json_safe(coverage):
    import json

    now = datetime(2026, 11, 9, 4, tzinfo=timezone.utc)
    json.dumps(parse_coverage(coverage).to_dict(now))


def test_low_lightning_percent_is_not_thunder(coverage):
    # live DMI data: 0.37 means 0.37 %, not 37 %
    hours = parse_coverage(coverage).all_hours
    assert hours[7].lightning_probability_pct == 0.3
    assert hours[7].condition == "cloudy"
    assert hours[19].lightning_probability_pct == 50.0


def test_radiation_is_instant_flux(coverage):
    from pydmi import const as c

    assert c.P_GLOBAL_RADIATION == "downward-short-wave-radiation-flux-instant"
    hours = parse_coverage(coverage).all_hours
    assert hours[0].global_radiation_wm2 == 0  # 1e-15 rounds to 0
    assert hours[5].global_radiation_wm2 == 54
