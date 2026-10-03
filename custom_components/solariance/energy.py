"""Energy dashboard: the forecast curve on the Energy tab."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(hass: HomeAssistant,
                                   config_entry_id: str) -> dict[str, dict[str, float]] | None:
    """Wh per hour for the Energy dashboard's solar forecast."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    forecast = entry.runtime_data.data
    if forecast is None:
        return None
    return {"wh_hours": {hour.isoformat(): round(wh, 1)
                         for hour, wh in forecast.wh_hours().items()}}
