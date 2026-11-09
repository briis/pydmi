# pydmi-weather

Install as `pydmi-weather`, import as `pydmi`.

Python wrapper for the **free** Open Data APIs from DMI (Danmarks Meteorologiske Institut).

| API | Status |
|---|---|
| [Forecast Data EDR API](https://www.dmi.dk/friedata/dokumentation/forecast-data-edr-api) | ✅ `DMIForecast` / `DMIForecastAsync` |
| [Meteorological Observations API](https://www.dmi.dk/friedata/dokumentation/meteorological-observation-api) | ✅ `DMIObservations` / `DMIObservationsAsync` |

- HARMONIE DINI model: ~2 km grid, hourly steps, ~60 h ahead, new run every 3 hours
- **Sync client**: standard library only, thread-safe. Use it in services like WeatherDataLogger.
- **Async client**: aiohttp, accepts an injected session. Use it in Home Assistant.
- Output in normal units (°C, hPa, m/s, mm, %) with unit-suffixed field names
- Derived Home Assistant condition strings, day/night aware
- Daily aggregates in your local time zone

No API key is needed. DMI removed authentication from `opendataapi.dmi.dk`.

## Install

```bash
pip install pydmi-weather    # sync client, no dependencies
pip install "pydmi-weather[async]"   # + aiohttp for the async client
```

## Usage

```python
from pydmi import DMIForecast, ATTRIBUTION

dmi = DMIForecast(55.64, 12.08)          # lat, lon
fc = dmi.get_forecast()                  # cached for update_interval (30 min)

print(fc.model_run, ATTRIBUTION)
now = fc.current()
print(now.temperature_c, now.condition, now.wind_speed_ms)

for h in fc.hourly()[:6]:
    print(h.datetime, h.temperature_c, h.precipitation_mm, h.condition)

for d in fc.daily():                     # Europe/Copenhagen by default
    print(d.date, d.temperature_min_c, d.temperature_max_c, d.precipitation_mm, d.condition)

payload = fc.to_dict()                   # JSON-ready, e.g. for MQTT
```

Async:

```python
from pydmi.client_async import DMIForecastAsync

async with DMIForecastAsync(55.64, 12.08, session=session) as dmi:
    fc = await dmi.get_forecast()
```

See `examples/` for an MQTT publisher (WeatherDataLogger style) and a Home Assistant
coordinator and weather entity.

### Observations

```python
from pydmi import DMIObservations

obs = DMIObservations(55.74, 12.15).get_observations()   # cached for 10 min
print(obs.temperature_c, obs.wind_speed_ms, obs.global_radiation_wm2)
print(obs.values["temperature_c"].station_name, obs.values["temperature_c"].observed)
payload = obs.to_dict()                                   # values + where/when each came from
```

For each measurement, the nearest active **Synop** station that measures it is used (within 50 km).
That can be different stations for different values: near Roskilde, temperature and wind come from
Roskilde Lufthavn, solar radiation from Sjælsmark. The station list is fetched once a day, then one
request per station every 10 minutes, 2 seconds apart. A value older than one hour counts as missing.
If one station fails, values from the others are still returned.

Fields: `temperature_c`, `dew_point_c`, `humidity_pct`, `pressure_hpa` (sea level), `wind_speed_ms`
(10-min mean), `wind_bearing_deg`, `wind_gust_ms` (highest gust last 10 min), `precipitation_10min_mm`,
`precipitation_1h_mm`, `cloud_cover_pct`, `visibility_m`, `global_radiation_wm2`, `sunshine_10min_min`.
The async client is `pydmi.client_async.DMIObservationsAsync`.

## Respecting the free service

DMI Open Data is free to use, and the library is built to keep it that way:

| What | How |
|---|---|
| Fair use limit (500 req / 5 s, shared by everyone) | Responses are cached. `update_interval` defaults to 30 min and cannot go below 10 min. The model only changes every 3 h anyway. |
| HTTP 429 | The client honours `Retry-After`. While it waits it returns the cached forecast and does not contact DMI. |
| Minimal payload | One `/position` request, 13 parameters, CoverageJSON (DMI says it is >10× smaller than GeoJSON), gzip |
| Cacheable requests | Coordinates are rounded to 2 decimals (~1 km, finer than the 2 km grid) |
| Identification | `User-Agent: pydmi/<version>` |
| Concurrency | A lock makes concurrent callers share one request |

### Licence: CC BY 4.0, attribution required

DMI's data is licensed under [CC BY 4.0](https://www.dmi.dk/friedata/dokumentation/terms-of-use).
Wherever the data is shown, you must credit DMI. Use `pydmi.ATTRIBUTION`
(`fc.attribution`). In Home Assistant, set it as `_attr_attribution`.

## Data notes

- **Precipitation**: DMI's `total-precipitation` is accumulated from the start of the run.
  The library converts it to the amount per hour. `precipitation_mm` on an hour is the
  amount expected *during that hour* (the Home Assistant / met.no convention). The last
  step is `None`.
- **Condition**: the EDR API has no weather symbol, so one is derived. Priority order:
  thunder (lightning probability ≥ 30 %; note DMI returns this in percent, although its metadata says 0..1), precipitation (≥ 0.3 mm/h, with rain, snow,
  sleet or hail taken from `precipitation-type`), fog (visibility < 1 km), wind
  (≥ 13.9 m/s), then cloud cover. Night uses `clear-night`. All thresholds are in `const.py`.
- **Daily**: grouped by local date. Today covers the rest of the day. Later days with
  fewer than 12 hours (the end of the run) are dropped.
- **Model run**: `fc.model_run` is the first time step of the run (UTC).
- **Chance of precipitation**: `precipitation_probability_pct` (0–100, in steps of 10) is the chance of
  at least 0.1 mm during the hour. It comes from DMI's ensemble (`harmonie_dini_eps_percentiles`): the
  spread of 1-hour precipitation over the ensemble members is read as a distribution, and the chance of
  exceeding 0.1 mm is interpolated from it. The daily value is the highest hourly chance that day (an
  approximation). The ensemble is fetched at most once an hour (it changes every few hours) and reused for
  every forecast update in between, 2 seconds after the forecast request. If it fails, the forecast is
  still returned with `None` here, and only the ensemble is retried after 5 minutes
  (`client.probability_pending` is `True` meanwhile). Turn it off with `include_precipitation_probability=False`.
- **Current conditions**: `fc.current()` interpolates instantaneous values (temperature,
  pressure, humidity, wind, cloud cover, radiation) linearly between the two hourly steps
  around now, so values change smoothly through the hour. Wind direction takes the shortest
  way around the compass. Precipitation is the amount for the current hour. Use
  `fc.current(interpolate=False)` to get the start-of-hour step unchanged.

## Development

```bash
pip install -e ".[test]"
pytest
```

The tests use a local mock server, so DMI is never contacted.
