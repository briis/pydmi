from datetime import UTC, datetime, timedelta

import pytest

from pydmi import DMIObservations, DMIRateLimitError
from pydmi.client_async import DMIObservationsAsync
from pydmi.observations import (
    build_observations,
    parse_observations,
    parse_stations,
    select_stations,
)

from .conftest import make_observations, make_stations

HOME = (55.7357, 12.1516)  # Gundsømagle


def test_parse_stations_keeps_only_current_synop():
    stations = parse_stations(make_stations(), *HOME)
    ids = [s.station_id for s in stations]
    assert ids[:2] == ["06170", "06188"]  # nearest first
    assert "06160" not in ids  # closed
    assert "20001" not in ids  # road station
    assert ids.count("06170") == 1  # historical period dropped
    assert stations[0].latitude == 55.5868  # current position


def test_select_nearest_station_per_measurement():
    chosen = select_stations(parse_stations(make_stations(), *HOME))
    assert chosen["temperature_c"].station_id == "06170"
    assert chosen["global_radiation_wm2"].station_id == "06188"  # Roskilde has none
    assert chosen["sunshine_10min_min"].name == "Sjælsmark"
    assert all(s.station_id != "06019" for s in chosen.values())  # beyond 50 km


def test_newest_value_wins_and_stale_dropped():
    now = datetime.now(UTC)
    chosen = select_stations(parse_stations(make_stations(), *HOME))
    per = {sid: parse_observations(make_observations(sid, now)) for sid in ("06170", "06188")}
    obs = build_observations(chosen, per, now)
    assert obs.temperature_c == 6.6  # newest, not the older 6.1
    assert obs.global_radiation_wm2 == 42.0
    assert obs.precipitation_1h_mm == 0.2
    assert obs.values["temperature_c"].station_name == "Roskilde Lufthavn"
    stale = build_observations(chosen, per, now + timedelta(hours=2))
    assert stale.temperature_c is None


def test_to_dict_has_sources():
    import json

    now = datetime.now(UTC)
    chosen = select_stations(parse_stations(make_stations(), *HOME))
    per = {sid: parse_observations(make_observations(sid, now)) for sid in ("06170", "06188")}
    d = build_observations(chosen, per, now).to_dict()
    json.dumps(d)
    assert d["sources"]["global_radiation_wm2"]["station_name"] == "Sjælsmark"


def _client(fake_dmi, cls=DMIObservations):
    client = cls(*HOME, base_url=fake_dmi.metobs_url)
    client.request_spacing = 0
    return client


def _metobs(fake):
    return [r[0] for r in fake.requests if "/metObs/" in r[0]]


def test_sync_one_request_per_station_and_cache(fake_dmi):
    client = _client(fake_dmi)
    obs = client.get_observations()
    paths = _metobs(fake_dmi)
    assert len(paths) == 3  # station list + 06170 + 06188
    assert "collections/station/items" in paths[0]
    assert {p.split("stationId=")[1].split("&")[0] for p in paths[1:]} == {"06170", "06188"}
    assert obs.temperature_c == 6.6 and obs.global_radiation_wm2 == 42.0
    assert client.get_observations() is obs  # cached for 10 min
    client.get_observations(force=True)  # station list is NOT fetched again
    assert len(_metobs(fake_dmi)) == 5


def test_one_station_failing_keeps_the_other(fake_dmi):
    fake_dmi.obs_status["06188"] = 500
    obs = _client(fake_dmi).get_observations()
    assert obs.temperature_c == 6.6
    assert obs.global_radiation_wm2 is None


def test_rate_limit_raises_and_blocks(fake_dmi):
    fake_dmi.obs_status["06170"] = 429
    client = _client(fake_dmi)
    with pytest.raises(DMIRateLimitError):
        client.get_observations()
    n = len(_metobs(fake_dmi))
    with pytest.raises(DMIRateLimitError):
        client.get_observations()
    assert len(_metobs(fake_dmi)) == n  # backing off, no new requests


async def test_async_observations(fake_dmi):
    client = _client(fake_dmi, DMIObservationsAsync)
    async with client:
        obs = await client.get_observations()
    assert obs.wind_gust_ms == 3.6
    assert client.stations["global_radiation_wm2"].station_id == "06188"


def test_visibility_from_10_minute_mean():
    now = datetime.now(UTC)
    chosen = select_stations(parse_stations(make_stations(), *HOME))
    per = {sid: parse_observations(make_observations(sid, now)) for sid in ("06170", "06188")}
    assert build_observations(chosen, per, now).visibility_m == 22400.0
