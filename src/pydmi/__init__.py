"""pydmi - Python wrapper for DMI's free Open Data APIs.

Forecast EDR API (HARMONIE DINI), Meteorological Observation API (metObs) and
Lightning Data API.

    from pydmi import DMIForecast           # stdlib only, blocking
    from pydmi.client_async import DMIForecastAsync  # needs aiohttp

Data: DMI Open Data, CC BY 4.0 - always show `ATTRIBUTION` with the data.
"""

from .client_sync import DMIForecast, DMILightning, DMIObservations
from .const import ATTRIBUTION, DEFAULT_PARAMETERS, LICENSE_URL, __version__
from .exceptions import (
    DMIConnectionError,
    DMIError,
    DMIInvalidLocationError,
    DMIParseError,
    DMIRateLimitError,
    DMIRequestError,
)
from .lightning import Lightning, Strike
from .models import Forecast, ForecastDay, ForecastHour
from .observations import OBSERVATION_PARAMETERS, Observations, ObservedValue, Station
from .parser import derive_condition, parse_coverage

__all__ = [
    "ATTRIBUTION",
    "DEFAULT_PARAMETERS",
    "LICENSE_URL",
    "OBSERVATION_PARAMETERS",
    "DMIConnectionError",
    "DMIError",
    "DMIForecast",
    "DMILightning",
    "DMIObservations",
    "DMIInvalidLocationError",
    "DMIParseError",
    "DMIRateLimitError",
    "DMIRequestError",
    "Forecast",
    "ForecastDay",
    "ForecastHour",
    "Lightning",
    "Observations",
    "ObservedValue",
    "Station",
    "Strike",
    "__version__",
    "derive_condition",
    "parse_coverage",
]
