"""Outdoor temperature from Open-Meteo, for auto mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.const import CONF_LATITUDE, CONF_LOCATION, CONF_LONGITUDE
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER, WEATHER_INTERVAL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 20


@dataclass(frozen=True)
class OutdoorTemperature:
    """The outdoor temperature now and on average over the last 24 hours."""

    current: float
    mean_24h: float


def weather_location(hass: HomeAssistant, entry: ConfigEntry) -> tuple[float, float]:
    """Return the location to read the outdoor temperature for.

    Args:
    ----
        hass: The HomeAssistant instance.
        entry: The config entry.

    Returns:
    -------
        Latitude and longitude: the location in the options, or Home Assistant's.

    """
    location = entry.options.get(CONF_LOCATION) or {}
    return (
        location.get(CONF_LATITUDE, hass.config.latitude),
        location.get(CONF_LONGITUDE, hass.config.longitude),
    )


def parse_forecast(data: Any, now: datetime) -> OutdoorTemperature:
    """Read the temperatures from an Open-Meteo reply.

    Args:
    ----
        data: The decoded JSON reply.
        now: The current time.

    Returns:
    -------
        The outdoor temperature.

    Raises:
    ------
        ValueError: The reply lacks the temperatures.

    """
    try:
        current = float(data["current"]["temperature_2m"])
        hourly = data["hourly"]
        past = [
            float(value)
            for time, value in zip(
                hourly["time"], hourly["temperature_2m"], strict=True
            )
            if value is not None
            and timedelta(0)
            <= now - datetime.fromisoformat(time).replace(tzinfo=UTC)
            < timedelta(hours=24)
        ]
    except (KeyError, TypeError, ValueError) as err:
        msg = f"unexpected reply: {err!r}"
        raise ValueError(msg) from err
    if not past:
        msg = "no temperatures for the last 24 hours"
        raise ValueError(msg)
    return OutdoorTemperature(current, sum(past) / len(past))


class OutdoorCoordinator(DataUpdateCoordinator[OutdoorTemperature]):
    """Read the outdoor temperature every WEATHER_INTERVAL."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator.

        Args:
        ----
            hass: The HomeAssistant instance.
            entry: The config entry.

        """
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_outdoor_temperature",
            update_interval=WEATHER_INTERVAL,
        )
        self.latitude, self.longitude = weather_location(hass, entry)

    @property
    def mean_24h(self) -> float | None:
        """The 24-hour average outdoor temperature, None when unknown."""
        return self.data.mean_24h if self.data and self.last_update_success else None

    async def _async_update_data(self) -> OutdoorTemperature:
        """Read the temperatures.

        Returns
        -------
            The outdoor temperature.

        Raises
        ------
            UpdateFailed: Open-Meteo could not be read.

        """
        params = {
            # Two decimals is about a kilometre: precise enough for the weather.
            "latitude": f"{self.latitude:.2f}",
            "longitude": f"{self.longitude:.2f}",
            "current": "temperature_2m",
            "hourly": "temperature_2m",
            "past_days": "1",
            "forecast_days": "1",
            "timezone": "UTC",
        }
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await async_get_clientsession(self.hass).get(
                    OPEN_METEO_URL, params=params, raise_for_status=True
                )
                data = await response.json()
            return parse_forecast(data, dt_util.utcnow())
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="weather_failed",
                translation_placeholders={"error": str(err)},
            ) from err
