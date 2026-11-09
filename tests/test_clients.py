from datetime import timedelta

import pytest

from pydmi import DMIForecast, DMIInvalidLocationError, DMIRateLimitError
from pydmi.client_async import DMIForecastAsync


def test_url_encoding():
    client = DMIForecast(55.6412345, 12.0801)
    url = client.url
    assert "coords=POINT(12.08%2055.64)" in url  # rounded, space as %20
    assert "crs=crs84" in url
    assert "parameter-name=temperature-2m,dew-point-temperature-2m" in url


def test_update_interval_is_clamped():
    client = DMIForecast(55.64, 12.08, update_interval=timedelta(seconds=5))
    assert client.update_interval == timedelta(minutes=10)


def test_outside_area_rejected_before_any_request():
    with pytest.raises(DMIInvalidLocationError):
        DMIForecast(-33.9, 18.4)  # Cape Town


def test_sync_fetch_and_cache(fake_dmi):
    client = DMIForecast(55.64, 12.08, base_url=fake_dmi.base_url)
    fc1 = client.get_forecast()
    fc2 = client.get_forecast()
    assert fc1 is fc2
    assert len(fake_dmi.main_requests) == 1  # second call served from cache
    path, headers = fake_dmi.main_requests[0]
    assert path.startswith("/v1/forecastedr/collections/harmonie_dini_sf/position?")
    assert headers["User-Agent"].startswith("pydmi/")
    client.get_forecast(force=True)
    assert len(fake_dmi.main_requests) == 2


def test_sync_rate_limit_backoff(fake_dmi):
    client = DMIForecast(55.64, 12.08, base_url=fake_dmi.base_url)
    first = client.get_forecast()
    fake_dmi.status, fake_dmi.retry_after = 429, "120"
    with pytest.raises(DMIRateLimitError) as err:
        client.get_forecast(force=True)
    assert err.value.retry_after == 120
    # during back-off: cache is returned, DMI is not contacted
    assert client.get_forecast(force=True) is first
    assert len(fake_dmi.main_requests) == 2


async def test_async_fetch_and_cache(fake_dmi):
    async with DMIForecastAsync(55.64, 12.08, base_url=fake_dmi.base_url) as client:
        fc = await client.get_forecast()
        assert fc.model_run is not None
        assert (await client.get_forecast()) is fc
    assert len(fake_dmi.main_requests) == 1


async def test_async_rate_limit_without_cache(fake_dmi):
    fake_dmi.status = 429
    async with DMIForecastAsync(55.64, 12.08, base_url=fake_dmi.base_url) as client:
        with pytest.raises(DMIRateLimitError):
            await client.get_forecast()
        with pytest.raises(DMIRateLimitError):  # still blocked, no new request
            await client.get_forecast()
    assert len(fake_dmi.requests) == 1


def test_sync_includes_probability(fake_dmi):
    client = DMIForecast(55.64, 12.08, base_url=fake_dmi.base_url)
    fc = client.get_forecast()
    assert len(fake_dmi.requests) == 2
    assert "harmonie_dini_eps_percentiles" in fake_dmi.requests[1][0]
    assert any(h.precipitation_probability_pct == 40 for h in fc.all_hours)


def test_probability_failure_keeps_forecast(fake_dmi):
    fake_dmi.eps_status = 500
    client = DMIForecast(55.64, 12.08, base_url=fake_dmi.base_url)
    fc = client.get_forecast()
    assert fc.model_run is not None
    assert all(h.precipitation_probability_pct is None for h in fc.all_hours)


def test_probability_can_be_disabled(fake_dmi):
    client = DMIForecast(
        55.64, 12.08, base_url=fake_dmi.base_url, include_precipitation_probability=False
    )
    client.get_forecast()
    assert len(fake_dmi.requests) == 1


async def test_async_includes_probability(fake_dmi):
    async with DMIForecastAsync(55.64, 12.08, base_url=fake_dmi.base_url) as client:
        fc = await client.get_forecast()
    assert any(h.precipitation_probability_pct == 40 for h in fc.all_hours)
