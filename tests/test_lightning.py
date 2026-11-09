from datetime import UTC, datetime, timedelta
import json

import pytest

from pydmi import DMILightning, DMIRateLimitError
from pydmi.client_async import DMILightningAsync
from pydmi.lightning import parse_lightning

from .conftest import make_lightning

HOME = (55.7357, 12.1516)


def test_parse_filters_to_circle_and_sorts_newest_first():
    now = datetime.now(UTC)
    lt = parse_lightning(make_lightning(now), *HOME, 50.0, now)
    assert lt.count_1h == 2  # the ~64 km corner strike is dropped
    assert lt.count_10min == 1
    assert lt.latest.distance_km == pytest.approx(30, abs=0.5)
    assert lt.latest.bearing_deg == 0  # due north
    assert lt.latest.current_ka == -12.4
    assert lt.latest.type == 0  # "0.0" from DMI -> 0
    assert lt.nearest.distance_km == pytest.approx(10, abs=0.5)
    assert lt.nearest.bearing_deg == 90  # due east
    json.dumps(lt.to_dict())


def test_no_strikes():
    now = datetime.now(UTC)
    lt = parse_lightning({"features": []}, *HOME, 50.0, now)
    assert lt.count_1h == 0 and lt.latest is None and lt.nearest is None
    assert lt.to_dict()["nearest"] is None


def test_url_box_covers_radius():
    c = DMILightning(*HOME, radius_km=50)
    assert "period=latest-hour" in c.url
    bbox = [float(x) for x in c.url.split("bbox=")[1].split("&")[0].split(",")]
    assert bbox[0] < 12.15 - 0.75 and bbox[2] > 12.15 + 0.75  # ~50 km east/west at 56 N
    assert bbox[1] < 55.74 - 0.44 and bbox[3] > 55.74 + 0.44  # ~50 km north/south
    with pytest.raises(ValueError):
        DMILightning(*HOME, radius_km=0)


def test_sync_client_caches(fake_dmi):
    c = DMILightning(*HOME, base_url=fake_dmi.lightning_url)
    lt = c.get_lightning()
    assert lt.count_1h == 2
    assert c.get_lightning() is lt
    assert len([r for r in fake_dmi.requests if "/lightningdata/" in r[0]]) == 1
    assert c.update_interval == timedelta(minutes=5)


def test_rate_limit(fake_dmi):
    fake_dmi.obs_status["lightning"] = 429
    c = DMILightning(*HOME, base_url=fake_dmi.lightning_url)
    with pytest.raises(DMIRateLimitError):
        c.get_lightning()


async def test_async_client(fake_dmi):
    async with DMILightningAsync(*HOME, base_url=fake_dmi.lightning_url, radius_km=20) as c:
        lt = await c.get_lightning()
    assert lt.count_1h == 1  # only the 10 km strike within 20 km
