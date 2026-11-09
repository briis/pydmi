#!/usr/bin/env python3
"""DMI forecast -> MQTT publisher for WeatherDataLogger.

Same pattern as tempest_datalogger.py: single file, INI config via configparser,
paho-mqtt with loop_start(), no asyncio. Only extra dependency: pydmi
(itself stdlib-only).

Topics (retained):
  weatherdatalogger/dmi-forecast/current   current hour
  weatherdatalogger/dmi-forecast/hourly    list of hours
  weatherdatalogger/dmi-forecast/daily     list of days
  weatherdatalogger/dmi-forecast/status    model run, attribution, availability

Remember to add the new topics to CONTEXT.md and AGENT.md.
"""

from __future__ import annotations

import configparser
import json
import logging
import signal
import sys
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

from pydmi import DMIError, DMIForecast, DMIRateLimitError

LOG = logging.getLogger("dmi_forecast")

DEFAULT_CONFIG = """
[dmi]
latitude = 55.64
longitude = 12.08
update_interval_minutes = 30
timezone = Europe/Copenhagen

[mqtt]
host = localhost
port = 1883
username =
password =
topic_prefix = weatherdatalogger/dmi-forecast
"""


def main(config_path: str = "config.ini") -> None:
    cfg = configparser.ConfigParser()
    cfg.read_string(DEFAULT_CONFIG)
    cfg.read(config_path)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    interval = timedelta(minutes=cfg.getint("dmi", "update_interval_minutes"))
    tz = ZoneInfo(cfg.get("dmi", "timezone"))
    prefix = cfg.get("mqtt", "topic_prefix").rstrip("/")

    dmi = DMIForecast(
        cfg.getfloat("dmi", "latitude"),
        cfg.getfloat("dmi", "longitude"),
        update_interval=interval,
    )

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dmi-forecast")
    if cfg.get("mqtt", "username"):
        client.username_pw_set(cfg.get("mqtt", "username"), cfg.get("mqtt", "password"))
    client.will_set(f"{prefix}/availability", "offline", retain=True)
    client.connect(cfg.get("mqtt", "host"), cfg.getint("mqtt", "port"))
    client.loop_start()
    client.publish(f"{prefix}/availability", "online", retain=True)

    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_run = None
    while running:
        sleep_s = interval.total_seconds()
        try:
            fc = dmi.get_forecast()
            data = fc.to_dict(tz=tz)
            current = fc.current()
            client.publish(
                f"{prefix}/current", json.dumps(current.to_dict() if current else None), retain=True
            )
            client.publish(f"{prefix}/hourly", json.dumps(data["hourly"]), retain=True)
            client.publish(f"{prefix}/daily", json.dumps(data["daily"]), retain=True)
            client.publish(
                f"{prefix}/status",
                json.dumps(
                    {
                        "model_run": data["model_run"],
                        "fetched_at": data["fetched_at"],
                        "grid_latitude": fc.latitude,
                        "grid_longitude": fc.longitude,
                        "attribution": fc.attribution,
                        "license_url": fc.license_url,
                    }
                ),
                retain=True,
            )
            if fc.model_run != last_run:
                LOG.info("Published DMI model run %s", fc.model_run)
                last_run = fc.model_run
        except DMIRateLimitError as err:
            LOG.warning("DMI rate limit - backing off %.0f s", err.retry_after)
            sleep_s = max(sleep_s, err.retry_after)
        except DMIError as err:
            LOG.error("DMI forecast update failed: %s", err)
            sleep_s = 300  # retry sooner than a full interval, but not hammering

        # sleep in small chunks so SIGTERM is handled promptly
        end = time.monotonic() + sleep_s
        while running and time.monotonic() < end:
            time.sleep(1)

    client.publish(f"{prefix}/availability", "offline", retain=True)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config.ini")
