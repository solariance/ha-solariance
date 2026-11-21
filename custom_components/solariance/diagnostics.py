"""Diagnostics download: what a bug report needs, without the token."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_API_TOKEN
from .coordinator import SolarianceConfigEntry

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(hass: HomeAssistant,
                                             entry: SolarianceConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    forecast = coordinator.data
    state = coordinator.forecast_state
    now = dt_util.utcnow()
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception else None,
            "update_interval_s": coordinator.update_interval.total_seconds()
            if coordinator.update_interval else None,
            "regular_interval_s": coordinator.regular_interval.total_seconds(),
        },
        # The production upload (0.3): None while no sensor is chosen.
        "upload": None if coordinator.upload_state is None else coordinator.upload_state.as_dict(),
        # The irradiance upload (0.3): None while no irradiance sensor is chosen.
        "irradiance_upload": None if coordinator.irradiance is None
        else coordinator.irradiance.state.as_dict(),
        # What the last answer said about the forecast (None: it said nothing).
        "forecast_state": {
            "status": state.status,
            "eta_utc": _iso(state.eta),
            "retry_after_s": state.retry_after_s,
            "locked_reason": state.locked_reason,
        },
        "forecast": None if forecast is None else {
            "system_id": forecast.system_id,
            "time_zone": str(forecast.time_zone),
            "forecast_status": forecast.forecast_status,
            "generated_utc": _iso(forecast.generated_utc),
            # The standard forecast beside the values (plans with the AI
            # forecast in the API); None and [] when the API does not say.
            "delivered": forecast.delivered,
            "delivered_layers": list(forecast.delivered_layers),
            "has_standard": forecast.has_standard,
            "today": str(forecast.local_date(now)),
            "days": [
                {"date": str(day.date), "intervals": len(day.intervals),
                 "steps_min": sorted({int((i.end - i.start).total_seconds() // 60)
                                      for i in day.intervals}),
                 "energy_kwh": round(day.energy_kwh, 3),
                 "standard_energy_kwh": None if day.standard_energy_kwh is None
                 else round(day.standard_energy_kwh, 3)}
                for day in forecast.days
            ],
        },
    }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()
