"""Set up a Solariance system with a personal API token."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    SolarianceApiClient,
    SolarianceAuthError,
    SolarianceError,
    SolarianceRateLimitError,
    normalise_token,
)
from .const import (
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    SCAN_INTERVALS,
    TOKEN_URL,
)
from .coordinator import SolarianceConfigEntry

TOKEN_SCHEMA = vol.Schema({
    vol.Required(CONF_API_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
})


def _label(system: dict[str, Any]) -> str:
    """'Roof (Berlin, 9.8 kWp)', marked when the plan pauses it."""
    details = [str(system["city"])] if system.get("city") else []
    if system.get("kWp") is not None:
        details.append(f"{float(system['kWp']):g} kWp")
    label = str(system.get("name") or system["system_id"])
    if details:
        label += f" ({', '.join(details)})"
    if system.get("locked_reason"):
        label += " – paused by your plan"
    return label


class SolarianceConfigFlow(ConfigFlow, domain=DOMAIN):
    """One config entry per Solariance system; the token is checked first."""

    VERSION = 1

    def __init__(self) -> None:
        self._token: str | None = None
        self._systems: list[dict[str, Any]] = []

    async def _async_validate(self, token: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """The account's systems, or a form error. system/list proves the token:
        it is the cheapest authenticated call, outside the forecast limit."""
        client = SolarianceApiClient(async_get_clientsession(self.hass), token)
        try:
            systems = await client.async_list_systems()
        except SolarianceAuthError:
            return [], {"base": "invalid_auth"}
        except SolarianceRateLimitError:
            return [], {"base": "rate_limited"}
        except SolarianceError:
            return [], {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001 -- the form must answer, whatever happened
            LOGGER.exception("Unexpected error while checking the Solariance token")
            return [], {"base": "unknown"}
        if not systems:
            return [], {"base": "no_systems"}
        return systems, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            token = normalise_token(user_input[CONF_API_TOKEN])
            systems, errors = await self._async_validate(token)
            if not errors:
                self._token, self._systems = token, systems
                return await self.async_step_system()
        return self.async_show_form(step_id="user", data_schema=TOKEN_SCHEMA, errors=errors,
                                    description_placeholders={"token_url": TOKEN_URL})

    async def async_step_system(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        configured = {entry.unique_id for entry in self._async_current_entries()}
        available = [s for s in self._systems if s["system_id"] not in configured]
        if not available:
            return self.async_abort(reason="already_configured")
        if user_input is None and len(available) == 1:
            user_input = {CONF_SYSTEM_ID: available[0]["system_id"]}
        if user_input is not None:
            system = next(s for s in available if s["system_id"] == user_input[CONF_SYSTEM_ID])
            await self.async_set_unique_id(system["system_id"])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=str(system.get("name") or system["system_id"]),
                data={CONF_API_TOKEN: self._token, CONF_SYSTEM_ID: system["system_id"]},
            )
        options = [SelectOptionDict(value=s["system_id"], label=_label(s)) for s in available]
        return self.async_show_form(
            step_id="system",
            data_schema=vol.Schema({vol.Required(CONF_SYSTEM_ID): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))}),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None
                                        ) -> ConfigFlowResult:
        """A revoked or expired token: ask for a new one for the same system."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            token = normalise_token(user_input[CONF_API_TOKEN])
            systems, errors = await self._async_validate(token)
            if not errors and not any(s["system_id"] == entry.data[CONF_SYSTEM_ID]
                                      for s in systems):
                errors = {"base": "system_not_in_account"}
            if not errors:
                return self.async_update_reload_and_abort(entry,
                                                          data_updates={CONF_API_TOKEN: token})
        return self.async_show_form(step_id="reauth_confirm", data_schema=TOKEN_SCHEMA,
                                    errors=errors,
                                    description_placeholders={"name": entry.title,
                                                              "token_url": TOKEN_URL})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SolarianceConfigEntry) -> SolarianceOptionsFlow:
        return SolarianceOptionsFlow()


class SolarianceOptionsFlow(OptionsFlowWithReload):
    """How often the forecast is fetched. A changed interval takes effect
    through Home Assistant's own reload after the flow; an update listener
    doing the same is deprecated (it breaks in Home Assistant 2026.12) and
    made re-authentication log a warning."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])})
        current = int(self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_SCAN_INTERVAL, default=str(current)):
                SelectSelector(SelectSelectorConfig(
                    options=[str(m) for m in SCAN_INTERVALS],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_SCAN_INTERVAL))}),
        )
