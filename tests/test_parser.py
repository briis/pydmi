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


def test_current_is_interpolated(coverage):
    fc = parse_coverage(coverage)
    h0, h1 = fc.all_hours[7], fc.all_hours[8]  # 10:00 and 11:00 UTC
    now = h0.datetime.replace(minute=45)
    cur = fc.current(now)
    assert cur.datetime == now
    expected = round(h0.temperature_c + (h1.temperature_c - h0.temperature_c) * 0.75, 1)
    assert cur.temperature_c == expected
    assert cur.precipitation_mm == h0.precipitation_mm
    # opt-out returns the start-of-hour step unchanged
    assert fc.current(now, interpolate=False) is h0


def test_bearing_interpolates_across_north():
    from pydmi.parser import _lerp_bearing

    assert _lerp_bearing(350, 10, 0.5) == 0
    assert _lerp_bearing(10, 350, 0.25) == 5
    assert _lerp_bearing(90, 270, 0.5) in (0, 180)


def test_current_at_exact_hour(coverage):
    fc = parse_coverage(coverage)
    h0 = fc.all_hours[7]
    cur = fc.current(h0.datetime)
    assert cur.temperature_c == h0.temperature_c


def test_condition_matches_rounded_precipitation():
    cov = make_coverage()
    acc = cov["ranges"]["total-precipitation"]["values"]
    # 0.06 mm in the hour starting at step 2 -> shown as 0.1 mm -> must be rainy
    for i in range(3, len(acc)):
        acc[i] = acc[i] + 0.06
    fc = parse_coverage(cov)
    h = fc.all_hours[2]
    assert h.precipitation_mm == 0.1
    assert h.condition == "rainy"
    now = h.datetime.replace(minute=20)
    assert fc.current(now).condition == fc.all_hours[2].condition
