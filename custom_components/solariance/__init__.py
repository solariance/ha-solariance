"""The Solariance integration: PV forecasts for your Solariance systems."""
from __future__ import annotations

from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, LOGGER
from .coordinator import SolarianceConfigEntry, SolarianceCoordinator, WaitForSolariance
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions once, for every entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarianceConfigEntry) -> bool:
    """Set up one Solariance system."""
    coordinator = SolarianceCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # A spent hourly limit, no forecast yet or a paused system: retrying
        # setup on Home Assistant's own schedule (5, 10, 20 s ... then every
        # 10 min) would add refused calls to the limit the Solariance app
        # shares, and refused calls count. Set up without data instead; the
        # sensors stay unavailable until the next regular update. Anything
        # else (no network, a server error) keeps the quick setup retry.
        if not isinstance(coordinator.last_exception, WaitForSolariance):
            raise
        LOGGER.warning("%s: %s The sensors stay unavailable until the next update.",
                       entry.title, coordinator.last_exception)
    entry.runtime_data = coordinator

    @callback
    def _recompute(_now: datetime) -> None:
        # "Power now" and "remaining today" move with the clock, not with the
        # forecast: recompute them on the quarter hour from the stored data.
        coordinator.async_update_listeners()

    entry.async_on_unload(
        async_track_time_change(hass, _recompute, minute=(0, 15, 30, 45), second=5))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarianceConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
