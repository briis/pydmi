"""pydmi - Python wrapper for DMI's free Open Data APIs.

Currently: Forecast EDR API (HARMONIE DINI). Planned: meteorological observations.

    from pydmi import DMIForecast           # stdlib only, blocking
    from pydmi.client_async import DMIForecastAsync  # needs aiohttp

Data: DMI Open Data, CC BY 4.0 - always show `ATTRIBUTION` with the data.
"""

from .client_sync import DMIForecast
from .const import ATTRIBUTION, DEFAULT_PARAMETERS, LICENSE_URL, __version__
from .exceptions import (
    DMIConnectionError,
    DMIError,
    DMIInvalidLocationError,
    DMIParseError,
    DMIRateLimitError,
    DMIRequestError,
)
from .models import Forecast, ForecastDay, ForecastHour
from .parser import derive_condition, parse_coverage

__all__ = [
    "ATTRIBUTION",
    "DEFAULT_PARAMETERS",
    "LICENSE_URL",
    "DMIConnectionError",
    "DMIError",
    "DMIForecast",
    "DMIInvalidLocationError",
    "DMIParseError",
    "DMIRateLimitError",
    "DMIRequestError",
    "Forecast",
    "ForecastDay",
    "ForecastHour",
    "__version__",
    "derive_condition",
    "parse_coverage",
]
