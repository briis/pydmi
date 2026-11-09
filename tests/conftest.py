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


class FakeDMI:
    def __init__(self) -> None:
        self.status = 200
        self.retry_after: str | None = None
        self.body = json.dumps(make_coverage()).encode()
        self.requests: list[tuple[str, dict]] = []


@pytest.fixture
def fake_dmi():
    state = FakeDMI()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state.requests.append((self.path, dict(self.headers)))
            if state.status != 200:
                self.send_response(state.status)
                if state.retry_after:
                    self.send_header("Retry-After", state.retry_after)
                self.end_headers()
                self.wfile.write(b"nope")
                return
            body = gzip.compress(state.body)
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
    yield state
    server.shutdown()
