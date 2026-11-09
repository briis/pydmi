"""Test fixtures: a synthetic CoverageJSON response shaped exactly like DMI's
/position output, and a tiny local HTTP server to stand in for DMI."""

from __future__ import annotations

import gzip
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pydmi import const as c

RUN = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
STEPS = 61  # 0..60 h


def make_coverage(run: datetime = RUN, steps: int = STEPS) -> dict:
    times = [(run + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z") for i in range(steps)]

    def series(fn):
        return {"type": "NdArray", "dataType": "float", "axisNames": ["t", "y", "x"],
                "shape": [steps, 1, 1], "values": [fn(i) for i in range(steps)]}

    acc = [0.0]
    for i in range(1, steps):
        # 2 mm/h between hours 10 and 13, otherwise dry
        acc.append(acc[-1] + (2.0 if 10 <= i <= 13 else 0.0))

    values = {
        c.P_TEMPERATURE: lambda i: 280.15 + (i % 24) * 0.2,  # 7..11.6 C
        c.P_DEW_POINT: lambda i: 277.15,
        c.P_HUMIDITY: lambda i: 85.0,
        c.P_PRESSURE: lambda i: 101325.0,
        c.P_WIND_SPEED: lambda i: 15.0 if i == 30 else 5.0,
        c.P_WIND_DIR: lambda i: 270.0,
        c.P_WIND_GUST: lambda i: 9.0,
        c.P_PRECIP_ACC: lambda i: acc[i],
        c.P_PRECIP_TYPE: lambda i: 1.0 if 9 <= i <= 12 else None,
        c.P_CLOUD_COVER: lambda i: 0.1 if i < 6 else 0.9,
        c.P_VISIBILITY: lambda i: 500.0 if i == 40 else 20000.0,
        c.P_LIGHTNING: lambda i: 50.0 if i == 19 else 0.3,
        c.P_GLOBAL_RADIATION: lambda i: 1e-15 if i < 4 else 53.65625,
    }
    return {
        "type": "Coverage",
        "domain": {
            "type": "Domain", "domainType": "Grid",
            "axes": {
                "t": {"values": times},
                "x": {"values": [12.080110763797222]},
                "y": {"values": [55.64957279677013]},
            },
        },
        "parameters": {k: {"type": "Parameter"} for k in values},
        "ranges": {k: series(fn) for k, fn in values.items()},
    }


@pytest.fixture
def coverage() -> dict:
    return make_coverage()


def make_eps(run: datetime = RUN, steps: int = 55) -> dict:
    """Ensemble percentiles of 1 h precipitation, shaped like DMI's response.

    Value at step i covers the hour ending at run + i h; step 0 is null.
    From step 10 on: p50 = 0.05, p75 = 0.15 -> chance of >= 0.1 mm = 40 %.
    """
    times = [(run + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z") for i in range(steps)]
    def pct(p):
        dry = {0: 0, 10: 0, 25: 0, 50: 0, 75: 0, 90: 0.02, 100: 0.05}[p]
        wet = {0: 0, 10: 0, 25: 0.01, 50: 0.05, 75: 0.15, 90: 0.4, 100: 1.2}[p]
        return [None] + [wet if i >= 10 else dry for i in range(1, steps)]
    return {
        "type": "Coverage",
        "domain": {"axes": {"t": {"values": times}, "x": {"values": [12.08]}, "y": {"values": [55.65]}}},
        "ranges": {
            f"total-precipitation-1h-{p}percentile": {"values": pct(p), "shape": [steps, 1, 1]}
            for p in (0, 10, 25, 50, 75, 90, 100)
        },
    }


@pytest.fixture
def eps() -> dict:
    return make_eps()


def _station(sid, name, lon, lat, params, *, type_="Synop", valid_to=None, status="Active"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "stationId": sid, "name": name, "type": type_, "status": status,
            "validFrom": "2019-01-01T00:00:00Z", "validTo": valid_to, "parameterId": params,
        },
    }


FULL = ["temp_dry", "temp_dew", "humidity", "pressure_at_sea", "wind_speed", "wind_dir",
        "wind_max", "precip_past10min", "precip_past1h", "cloud_cover", "visibility",
        "visib_mean_last10min"]


def make_stations() -> dict:
    """Around Gundsømagle: Roskilde Lufthavn (no radiation) and Sjælsmark (radiation),
    plus a closed period, a road station and a far-away station that must be ignored."""
    return {"type": "FeatureCollection", "features": [
        _station("06170", "Roskilde Lufthavn", 12.1366, 55.5867, FULL,
                 valid_to="2025-10-29T10:31:40Z"),  # old period of the same station
        _station("06170", "Roskilde Lufthavn", 12.1363, 55.5868, FULL),
        _station("06188", "Sjælsmark", 12.4121, 55.8764,
                 [*FULL, "radia_glob", "sun_last10min_glob"]),
        _station("06160", "Flyvestation Værløse", 12.3289, 55.7668, FULL,
                 valid_to="2006-08-18T00:00:00Z"),  # closed
        _station("20001", "Road sensor", 12.15, 55.73, ["temp_dry"], type_="GIWS"),  # nearest, wrong type
        _station("06019", "Far away", 9.0, 57.0, FULL),
    ]}


def make_observations(station_id: str, now: datetime) -> dict:
    """Latest hour for a station: two 10-minute timestamps, newest wins."""
    newest = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 10 + 2)
    older = newest - timedelta(minutes=10)
    values = {
        "06170": {"temp_dry": 6.6, "temp_dew": 3.3, "humidity": 79.2, "pressure_at_sea": 1021.1,
                  "wind_speed": 2.57, "wind_dir": 122.0, "wind_max": 3.6, "precip_past10min": 0.0,
                  "cloud_cover": 100.0, "visib_mean_last10min": 22400.0},
        "06188": {"temp_dry": 6.1, "radia_glob": 42.0, "sun_last10min_glob": 0.0},
    }[station_id]
    feats = []
    for when, delta in ((older, -0.5), (newest, 0.0)):
        for param, val in values.items():
            feats.append({"type": "Feature", "properties": {
                "stationId": station_id, "parameterId": param, "value": val + delta,
                "observed": when.strftime("%Y-%m-%dT%H:%M:%SZ")}})
    if station_id == "06170":  # hourly parameter only at the full hour
        feats.append({"type": "Feature", "properties": {
            "stationId": "06170", "parameterId": "precip_past1h", "value": 0.2,
            "observed": newest.replace(minute=0).strftime("%Y-%m-%dT%H:%M:%SZ")}})
    return {"type": "FeatureCollection", "features": feats}


class FakeDMI:
    def __init__(self) -> None:
        self.status = 200
        self.retry_after: str | None = None
        self.body = json.dumps(make_coverage()).encode()
        self.eps_status = 200
        self.obs_status: dict[str, int] = {}
        self.eps_body = json.dumps(make_eps()).encode()
        self.requests: list[tuple[str, dict]] = []

    @property
    def main_requests(self) -> list[tuple[str, dict]]:
        """Requests to the main forecast (excluding the ensemble request)."""
        return [r for r in self.requests if "harmonie_dini_eps" not in r[0]]


@pytest.fixture
def fake_dmi():
    state = FakeDMI()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state.requests.append((self.path, dict(self.headers)))
            is_eps = "harmonie_dini_eps_percentiles" in self.path
            status = state.eps_status if is_eps else state.status
            if "/metObs/" in self.path:
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                sid = q.get("stationId", [""])[0]
                status = state.obs_status.get(sid or "stations", 200)
                body_obj = (
                    make_observations(sid, datetime.now(timezone.utc)) if sid else make_stations()
                )
            if status != 200:
                self.send_response(status)
                if state.retry_after:
                    self.send_header("Retry-After", state.retry_after)
                self.end_headers()
                self.wfile.write(b"nope")
                return
            if "/metObs/" in self.path:
                body = gzip.compress(json.dumps(body_obj).encode())
            else:
                body = gzip.compress(state.eps_body if is_eps else state.body)
            self.send_response(200)
            self.send_header("Content-Type", "application/prs.coverage+json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}/v1/forecastedr"
    state.metobs_url = f"http://127.0.0.1:{server.server_port}/v2/metObs"
    yield state
    server.shutdown()


@pytest.fixture(autouse=True)
def no_request_spacing(monkeypatch):
    """Skip the 2 s pause between forecast and ensemble requests in tests."""
    monkeypatch.setattr(c, "REQUEST_SPACING_S", 0.0)
