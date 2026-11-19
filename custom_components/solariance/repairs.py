"""Repairs (0.3): the irradiance sensor id that Solariance refuses (409
sensor_changed: it already holds values for that id with another position).

The fix starts a new series: the same sensor and position under a new id (the
next generation), the entry reloads and the upload goes on. The values stored
under the old id stay with it.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .const import CONF_IRRADIANCE_SENSOR_ID
from .irradiance import rotated_options


class IrradianceSensorChangedFlow(RepairsFlow):
    """Confirm, then give the sensor a new id."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None
                                 ) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id or "")
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is not None:
            self.hass.config_entries.async_update_entry(entry,
                                                        options=rotated_options(entry.options))
            # The integration has no update listener (deprecated): reload here.
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}),
            description_placeholders={
                "title": entry.title,
                "sensor_id": str(entry.options.get(CONF_IRRADIANCE_SENSOR_ID))})


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str,
                                data: dict[str, str | int | float | None] | None) -> RepairsFlow:
    """The fix of an irradiance_sensor_changed issue."""
    entry_id = (data or {}).get("entry_id")
    return IrradianceSensorChangedFlow(str(entry_id) if entry_id else None)
