"""Polls one system's forecast."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SolarianceApiClient,
    SolarianceAuthError,
    SolarianceError,
    SolarianceRateLimitError,
)
from .const import (
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
)
from .forecast import ForecastFormatError, SolarForecast, parse_power_forecast

type SolarianceConfigEntry = ConfigEntry[SolarianceCoordinator]


class SolarianceCoordinator(DataUpdateCoordinator[SolarForecast]):
    """One config entry = one Solariance system."""

    config_entry: SolarianceConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SolarianceConfigEntry) -> None:
        minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_SYSTEM_ID]}",
            update_interval=timedelta(minutes=minutes),
        )
        self.system_id: str = entry.data[CONF_SYSTEM_ID]
        self.client = SolarianceApiClient(async_get_clientsession(hass),
                                          entry.data[CONF_API_TOKEN])

    async def _async_update_data(self) -> SolarForecast:
        try:
            data = await self.client.async_get_power_forecast(self.system_id)
        except SolarianceAuthError as err:
            # Revoked or expired token: Home Assistant asks for a new one.
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN,
                                        translation_key="auth_failed") from err
        except SolarianceRateLimitError as err:
            # The account's hourly limit is shared with the Solariance app;
            # wait as long as the API asks instead of adding to it.
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="rate_limited",
                               retry_after=err.retry_after) from err
        except SolarianceError as err:
            raise UpdateFailed(translation_domain=DOMAIN,
                               translation_key="cannot_connect") from err
        if data is None:
            raise UpdateFailed(translation_domain=DOMAIN, translation_key="no_forecast_yet")
        try:
            return parse_power_forecast(data)
        except ForecastFormatError as err:
            raise UpdateFailed(translation_domain=DOMAIN,
                               translation_key="unreadable_forecast") from err
