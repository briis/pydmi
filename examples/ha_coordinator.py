"""Sketch: using pydmi in a Home Assistant integration.

coordinator.py + the relevant bits of weather.py. Not a complete integration
(config_flow, manifest etc. omitted). manifest.json:
    "requirements": ["pydmi-weather==0.1.1"]
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.weather import (
    Forecast as HAForecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfLength, UnitOfPrecipitationDepth, UnitOfPressure, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from pydmi import ATTRIBUTION, DMIError, DMIRateLimitError, Forecast
from pydmi.client_async import DMIForecastAsync

_LOGGER = logging.getLogger(__name__)

# The model updates every 3 h - 30 min polling is plenty and kind to DMI.
UPDATE_INTERVAL = timedelta(minutes=30)


class DMICoordinator(DataUpdateCoordinator[Forecast]):
    def __init__(self, hass: HomeAssistant, latitude: float, longitude: float) -> None:
        super().__init__(hass, _LOGGER, name="DMI forecast", update_interval=UPDATE_INTERVAL)
        self.api = DMIForecastAsync(
            latitude,
            longitude,
            session=async_get_clientsession(hass),  # never create your own session in HA
            update_interval=UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> Forecast:
        try:
            return await self.api.get_forecast()
        except DMIRateLimitError as err:
            # Newer HA versions accept retry_after on UpdateFailed; drop it on older cores
            raise UpdateFailed(str(err), retry_after=err.retry_after) from err
        except DMIError as err:
            raise UpdateFailed(str(err)) from err


class DMIWeather(SingleCoordinatorWeatherEntity[DMICoordinator]):
    # SingleCoordinatorWeatherEntity pushes new forecasts to subscribers
    # automatically on every coordinator update.

    _attr_attribution = ATTRIBUTION  # required by DMI's CC BY 4.0 licence
    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_native_visibility_unit = UnitOfLength.METERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY
    )

    @property
    def _now(self):
        return self.coordinator.data.current()

    @property
    def condition(self) -> str | None:
        return self._now.condition if self._now else None

    @property
    def native_temperature(self) -> float | None:
        return self._now.temperature_c if self._now else None

    @property
    def native_dew_point(self) -> float | None:
        return self._now.dew_point_c if self._now else None

    @property
    def humidity(self) -> float | None:
        return self._now.humidity_pct if self._now else None

    @property
    def native_pressure(self) -> float | None:
        return self._now.pressure_hpa if self._now else None

    @property
    def native_wind_speed(self) -> float | None:
        return self._now.wind_speed_ms if self._now else None

    @property
    def native_wind_gust_speed(self) -> float | None:
        return self._now.wind_gust_ms if self._now else None

    @property
    def wind_bearing(self) -> float | None:
        return self._now.wind_bearing_deg if self._now else None

    @property
    def cloud_coverage(self) -> float | None:
        return self._now.cloud_cover_pct if self._now else None

    @property
    def native_visibility(self) -> float | None:
        return self._now.visibility_m if self._now else None

    @callback
    def _async_forecast_hourly(self) -> list[HAForecast]:
        return [
            HAForecast(
                datetime=h.datetime.isoformat(),
                condition=h.condition,
                native_temperature=h.temperature_c,
                native_dew_point=h.dew_point_c,
                humidity=h.humidity_pct,
                native_pressure=h.pressure_hpa,
                native_wind_speed=h.wind_speed_ms,
                native_wind_gust_speed=h.wind_gust_ms,
                wind_bearing=h.wind_bearing_deg,
                native_precipitation=h.precipitation_mm,
                cloud_coverage=h.cloud_cover_pct,
                is_daytime=h.is_daylight,
            )
            for h in self.coordinator.data.hourly()
        ]

    @callback
    def _async_forecast_daily(self) -> list[HAForecast]:
        tz = dt_util.get_default_time_zone()
        return [
            HAForecast(
                datetime=dt_util.start_of_local_day(d.date).isoformat(),
                condition=d.condition,
                native_temperature=d.temperature_max_c,
                native_templow=d.temperature_min_c,
                native_precipitation=d.precipitation_mm,
                native_wind_speed=d.wind_speed_max_ms,
                native_wind_gust_speed=d.wind_gust_max_ms,
                wind_bearing=d.wind_bearing_deg,
                humidity=d.humidity_avg_pct,
                cloud_coverage=d.cloud_cover_avg_pct,
            )
            for d in self.coordinator.data.daily(tz)
        ]
