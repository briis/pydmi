"""Constants for pydmi."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

__version__: Final = "0.4.0"

# DMI moved the open data APIs to opendataapi.dmi.dk and dropped API keys.
# The old host (dmigw.govcloud.dk) still requires a key - do not use it.
API_HOST: Final = "https://opendataapi.dmi.dk"
FORECAST_BASE_URL: Final = f"{API_HOST}/v1/forecastedr"
METOBS_BASE_URL: Final = f"{API_HOST}/v2/metObs"

# HARMONIE DINI surface model: ~2 km grid, hourly steps, ~60 h forecast,
# new run every 3 hours (00, 03, 06 ... 21 UTC).
DEFAULT_COLLECTION: Final = "harmonie_dini_sf"

# Rough geographic extent of the DINI model (CRS84, from the collection metadata).
DINI_BBOX: Final = (-43.17, 37.69, 40.07, 69.91)  # lon_min, lat_min, lon_max, lat_max

# Parameters needed to build a normal weather forecast. Keep this list short:
# every extra parameter is extra load on a free service.
P_TEMPERATURE: Final = "temperature-2m"
P_DEW_POINT: Final = "dew-point-temperature-2m"
P_HUMIDITY: Final = "relative-humidity-2m"
P_PRESSURE: Final = "pressure-sealevel"
P_WIND_SPEED: Final = "wind-speed-10m"
P_WIND_DIR: Final = "wind-dir-10m"
P_WIND_GUST: Final = "gust-wind-speed-10m"
P_PRECIP_ACC: Final = "total-precipitation"
P_PRECIP_TYPE: Final = "precipitation-type"
P_CLOUD_COVER: Final = "fraction-of-cloud-cover"
P_VISIBILITY: Final = "visibility"
P_LIGHTNING: Final = "probability-of-lightning"
# NB: "global-radiation-flux" is labelled W m-2 but is ACCUMULATED J/m2 since the run
# started (verified 2026-11-09). Use the instantaneous flux instead - same quantity
# a weather station measures.
P_GLOBAL_RADIATION: Final = "downward-short-wave-radiation-flux-instant"

DEFAULT_PARAMETERS: Final = (
    P_TEMPERATURE,
    P_DEW_POINT,
    P_HUMIDITY,
    P_PRESSURE,
    P_WIND_SPEED,
    P_WIND_DIR,
    P_WIND_GUST,
    P_PRECIP_ACC,
    P_PRECIP_TYPE,
    P_CLOUD_COVER,
    P_VISIBILITY,
    P_LIGHTNING,
    P_GLOBAL_RADIATION,
)

# --- Ensemble (chance of precipitation) ----------------------------------
# DMI's ensemble percentiles give, per hour, the spread of precipitation over
# the ensemble members. The chance of >= POP_THRESHOLD_MM is read off them.
EPS_PERCENTILE_COLLECTION: Final = "harmonie_dini_eps_percentiles"
EPS_PERCENTILES: Final = (0, 10, 25, 50, 75, 90, 100)
# Value at time t = precipitation during the hour ENDING at t (first step is null).
P_EPS_PRECIP_1H: Final = "total-precipitation-1h-{p}percentile"
# "Measurable precipitation" - deliberately lower than PRECIP_THRESHOLD_MM used
# for the weather icon: a 30 % chance of rain with a cloud icon is normal.
POP_THRESHOLD_MM: Final = 0.1
POP_ROUND_TO: Final = 10  # percentiles only give coarse resolution
# The ensemble changes every few hours: fetch it at most this often and reuse it
# for every forecast update in between.
PROBABILITY_UPDATE_INTERVAL: Final = timedelta(minutes=60)
# After a failed ensemble request, retry only that request after this delay.
PROBABILITY_RETRY_INTERVAL: Final = timedelta(minutes=5)
# Pause between the forecast request and the ensemble request. DMI answers
# back-to-back requests with 429 "Server is busy".
REQUEST_SPACING_S: Final = 2.0

# DMI precipitation-type index -> name
PRECIPITATION_TYPES: Final = {
    0: "drizzle",
    1: "rain",
    2: "sleet",
    3: "snow",
    4: "freezing_drizzle",
    5: "freezing_rain",
    6: "graupel",
    7: "hail",
}

# --- Fair use -------------------------------------------------------------
# DMI's fair use limit is 500 requests / 5 s across ALL users of the free API.
# A forecast model only changes every 3 hours, so polling often is pointless.
DEFAULT_UPDATE_INTERVAL: Final = timedelta(minutes=30)
MIN_UPDATE_INTERVAL: Final = timedelta(minutes=10)
DEFAULT_RETRY_AFTER: Final = timedelta(seconds=60)  # used when 429 has no Retry-After
DEFAULT_TIMEOUT: Final = 30  # seconds

# Round coordinates before querying. 2 decimals ~ 1 km, finer than the 2 km grid,
# so nothing is lost - and identical requests are easier for DMI to cache.
COORDINATE_PRECISION: Final = 2

USER_AGENT: Final = f"pydmi/{__version__} (+https://github.com/briis/pydmi)"

# --- Licence --------------------------------------------------------------
# DMI Open Data is CC BY 4.0: you MUST credit DMI wherever the data is shown.
ATTRIBUTION: Final = "Data from DMI (Danmarks Meteorologiske Institut), CC BY 4.0"
LICENSE_URL: Final = "https://www.dmi.dk/friedata/dokumentation/terms-of-use"

# --- Condition thresholds (Home Assistant condition strings) --------------
# mm/h needed for a rain/snow condition. 0.3 matches dmi.dk, which shows a
# cloud (not rain) for hours with only a trace of precipitation.
PRECIP_THRESHOLD_MM: Final = 0.3
POURING_THRESHOLD_MM: Final = 4.0  # mm/h considered "pouring"
# NB: DMI metadata says "0..1", but live values are in PERCENT (stable air gives
# ~0.2-0.4, i.e. under half a percent). Verified 2026-11-09 against CAPE.
LIGHTNING_THRESHOLD_PCT: Final = 30.0
FOG_VISIBILITY_M: Final = 1000.0
WINDY_THRESHOLD_MS: Final = 13.9  # Beaufort 7
CLOUD_CLEAR_PCT: Final = 20.0
CLOUD_PARTLY_PCT: Final = 70.0

COND_CLEAR_NIGHT: Final = "clear-night"
COND_CLOUDY: Final = "cloudy"
COND_FOG: Final = "fog"
COND_HAIL: Final = "hail"
COND_LIGHTNING: Final = "lightning"
COND_LIGHTNING_RAINY: Final = "lightning-rainy"
COND_PARTLYCLOUDY: Final = "partlycloudy"
COND_POURING: Final = "pouring"
COND_RAINY: Final = "rainy"
COND_SNOWY: Final = "snowy"
COND_SNOWY_RAINY: Final = "snowy-rainy"
COND_SUNNY: Final = "sunny"
COND_WINDY: Final = "windy"
COND_WINDY_VARIANT: Final = "windy-variant"

# --- Observations (metObs) ------------------------------------------------
# Stations report every 10 minutes.
OBS_UPDATE_INTERVAL: Final = timedelta(minutes=10)
OBS_MIN_UPDATE_INTERVAL: Final = timedelta(minutes=5)
# The station list rarely changes - fetch it once a day.
OBS_STATIONS_UPDATE_INTERVAL: Final = timedelta(hours=24)
# Only full weather stations. Road-weather and rain-only stations are left out
# so e.g. road-surface-influenced temperatures do not slip in.
OBS_STATION_TYPES: Final = frozenset({"Synop"})
# Search area around the location for stations (degrees), and the farthest
# station we are willing to use for a measurement.
OBS_SEARCH_DLAT: Final = 0.6
OBS_SEARCH_DLON: Final = 1.0
OBS_MAX_DISTANCE_KM: Final = 50.0
# A value older than this counts as missing (station down, sensor fault).
OBS_MAX_AGE: Final = timedelta(hours=1)
