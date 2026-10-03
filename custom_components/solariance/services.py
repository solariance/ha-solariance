"""Actions: the forecast as response data, and the best time to run a load."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SolarianceConfigEntry
from .forecast import Day, SolarForecast, find_surplus_window

SERVICE_GET_FORECAST = "get_forecast"
SERVICE_FIND_SURPLUS_WINDOW = "find_surplus_window"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DAY = "day"
ATTR_RESOLUTION = "resolution"
ATTR_LOAD_KW = "load_kw"
ATTR_DURATION_HOURS = "duration_hours"
ATTR_BASELINE_KW = "baseline_kw"
ATTR_PRICE = "electricity_price_eur_per_kwh"
ATTR_FEED_IN = "feed_in_tariff_eur_per_kwh"

GET_FORECAST_SCHEMA = vol.Schema({
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_DAY): vol.All(vol.Coerce(int), vol.Range(min=0, max=13)),
    vol.Optional(ATTR_RESOLUTION, default="hour"): vol.In(("hour", "quarter_hour")),
})

FIND_SURPLUS_WINDOW_SCHEMA = vol.Schema({
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_LOAD_KW): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=1000)),
    vol.Optional(ATTR_DURATION_HOURS, default=2.0):
        vol.All(vol.Coerce(float), vol.Range(min=0.25, max=24)),
    vol.Optional(ATTR_BASELINE_KW, default=0.0):
        vol.All(vol.Coerce(float), vol.Range(min=0, max=1000)),
    vol.Optional(ATTR_DAY, default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=13)),
    vol.Optional(ATTR_PRICE): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
    vol.Optional(ATTR_FEED_IN): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
})


def _forecast(hass: HomeAssistant, call: ServiceCall) -> SolarForecast:
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry: SolarianceConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(translation_domain=DOMAIN,
                                     translation_key="entry_not_found",
                                     translation_placeholders={"entry_id": entry_id})
    if entry.state is not ConfigEntryState.LOADED or entry.runtime_data.data is None:
        raise ServiceValidationError(translation_domain=DOMAIN,
                                     translation_key="entry_not_loaded",
                                     translation_placeholders={"name": entry.title})
    return entry.runtime_data.data


def _local(forecast: SolarForecast, moment: datetime) -> str:
    return moment.astimezone(forecast.time_zone).isoformat()


def _series(forecast: SolarForecast, day: Day, resolution: str) -> list[dict[str, Any]]:
    if resolution == "quarter_hour":
        return [{"start": _local(forecast, i.start), "end": _local(forecast, i.end),
                 "power_w": round(i.power_w), "energy_kwh": round(i.energy_kwh, 3)}
                for i in day.intervals]
    hours: dict[datetime, list] = {}
    for i in day.intervals:
        key = i.start.replace(minute=0, second=0, microsecond=0)
        entry = hours.setdefault(key, [i.start, i.end, 0.0, 0.0, 0.0])
        entry[1] = max(entry[1], i.end)
        seconds = (i.end - i.start).total_seconds()
        entry[2] += i.energy_kwh
        entry[3] += i.power_w * seconds
        entry[4] += seconds
    return [{"start": _local(forecast, start), "end": _local(forecast, end),
             "power_w": round(weighted / seconds) if seconds else 0,
             "energy_kwh": round(energy, 3)}
            for start, end, energy, weighted, seconds in hours.values()]


def _summary(forecast: SolarForecast, day: Day, resolution: str) -> dict[str, Any]:
    peak = day.peak
    start, end = day.production_start, day.production_end
    return {
        "date": day.date.isoformat(),
        "energy_kwh": round(day.energy_kwh, 3),
        "peak_power_w": round(peak.power_w) if peak else 0,
        "peak_time": _local(forecast, peak.start) if peak else None,
        "production_start": _local(forecast, start) if start else None,
        "production_end": _local(forecast, end) if end else None,
        "series": _series(forecast, day, resolution),
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""

    async def get_forecast(call: ServiceCall) -> ServiceResponse:
        forecast = _forecast(hass, call)
        now = dt_util.utcnow()
        resolution = call.data[ATTR_RESOLUTION]
        if ATTR_DAY in call.data:
            day = forecast.day(call.data[ATTR_DAY], now)
            days = [day] if day else []
        else:
            today = forecast.local_date(now)
            days = [d for d in forecast.days if d.date >= today]
        return {
            "system_id": forecast.system_id,
            "time_zone": str(forecast.time_zone),
            "days": [_summary(forecast, d, resolution) for d in days],
        }

    async def find_window(call: ServiceCall) -> ServiceResponse:
        forecast = _forecast(hass, call)
        now = dt_util.utcnow()
        offset = call.data[ATTR_DAY]
        day = forecast.day(offset, now)
        if day is None:
            return {"found": False, "reason": "no_forecast_for_day"}
        # Today, only windows that have not started yet: an automation asking
        # at 10:00 cannot use 09:00-11:00.
        not_before = now if offset == 0 else None
        window = find_surplus_window(day, call.data[ATTR_LOAD_KW],
                                     call.data[ATTR_DURATION_HOURS],
                                     call.data[ATTR_BASELINE_KW], not_before)
        if window is None:
            return {"found": False, "reason": "no_window_left"}
        result: dict[str, Any] = {
            "found": True,
            "date": day.date.isoformat(),
            "window_start": _local(forecast, window.start),
            "window_end": _local(forecast, window.end),
            "load_kwh": round(window.load_kwh, 2),
            "pv_covered_kwh": round(window.pv_covered_kwh, 2),
            "grid_kwh": round(window.grid_kwh, 2),
            "pv_coverage_percent": window.pv_coverage_percent,
        }
        if ATTR_PRICE in call.data:
            # Self-consumed solar is worth the grid price it avoids, minus the
            # feed-in revenue given up by not exporting it.
            margin = call.data[ATTR_PRICE] - call.data.get(ATTR_FEED_IN, 0.0)
            result["estimated_saving_eur"] = round(window.pv_covered_kwh * margin, 2)
        return result

    hass.services.async_register(DOMAIN, SERVICE_GET_FORECAST, get_forecast,
                                 schema=GET_FORECAST_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, SERVICE_FIND_SURPLUS_WINDOW, find_window,
                                 schema=FIND_SURPLUS_WINDOW_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)

