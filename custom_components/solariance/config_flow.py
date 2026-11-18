"""Set up a Solariance system with a personal API token."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    SolarianceRefusedError,
    normalise_token,
)
from .const import (
    CONF_API_TOKEN,
    CONF_IRRADIANCE_AZIMUTH,
    CONF_IRRADIANCE_PLANE,
    CONF_IRRADIANCE_SENSOR,
    CONF_IRRADIANCE_TILT,
    CONF_IRRADIANCE_TYPE,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    CONF_UPLOAD_SENSOR,
    DEFAULT_IRRADIANCE_AZIMUTH,
    DEFAULT_IRRADIANCE_TILT,
    DEFAULT_IRRADIANCE_TYPE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    IRRADIANCE_PLANES,
    IRRADIANCE_TYPES,
    LOGGER,
    PLANE_HORIZONTAL,
    PLANE_MODULE,
    SCAN_INTERVALS,
    TOKEN_URL,
)
from .coordinator import SolarianceConfigEntry
from .irradiance import entity_problem, irradiance_options, roof_face

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
        except SolarianceRefusedError:
            # A token this account cannot use here: for the form, a token
            # that does not work.
            return [], {"base": "invalid_auth"}
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
    """How often the forecast is fetched, (0.3) the energy sensor whose
    production is uploaded every hour and (0.3) the irradiance sensor whose
    5-minute means are uploaded every 10 minutes -- neither by default. A
    change takes effect through Home Assistant's own reload after the flow;
    an update listener doing the same is deprecated (it breaks in Home
    Assistant 2026.12) and made re-authentication log a warning.

    The irradiance sensor takes up to two more steps: its plane and type,
    then, for a sensor in the module plane, its tilt and azimuth (prefilled
    from the options, else from the system's largest roof face, else 30° and
    180°)."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._irradiance: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current_options = self.config_entry.options
        if user_input is not None:
            self._options = {CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            if user_input.get(CONF_UPLOAD_SENSOR):
                self._options[CONF_UPLOAD_SENSOR] = user_input[CONF_UPLOAD_SENSOR]
            irradiance = user_input.get(CONF_IRRADIANCE_SENSOR)
            if not irradiance:
                return self.async_create_entry(data=self._options)
            if problem := entity_problem(self.hass, irradiance):
                errors[CONF_IRRADIANCE_SENSOR] = problem
            else:
                self._irradiance = {CONF_IRRADIANCE_SENSOR: irradiance}
                return await self.async_step_irradiance()
        current = int(current_options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        sensor = current_options.get(CONF_UPLOAD_SENSOR)
        irradiance = (user_input or {}).get(CONF_IRRADIANCE_SENSOR,
                                            current_options.get(CONF_IRRADIANCE_SENSOR))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=str(current)):
                    SelectSelector(SelectSelectorConfig(
                        options=[str(m) for m in SCAN_INTERVALS],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_SCAN_INTERVAL)),
                vol.Optional(CONF_UPLOAD_SENSOR, description={"suggested_value": sensor}):
                    EntitySelector(EntitySelectorConfig(domain="sensor",
                                                        device_class=SensorDeviceClass.ENERGY)),
                # Irradiance only: an illuminance sensor (lux) is not offered.
                vol.Optional(CONF_IRRADIANCE_SENSOR, description={"suggested_value": irradiance}):
                    EntitySelector(EntitySelectorConfig(domain="sensor",
                                                        device_class=SensorDeviceClass.IRRADIANCE)),
            }),
            errors=errors,
        )

    async def async_step_irradiance(self, user_input: dict[str, Any] | None = None
                                    ) -> ConfigFlowResult:
        """Where the irradiance sensor lies, and what it is."""
        if user_input is not None:
            self._irradiance[CONF_IRRADIANCE_PLANE] = user_input[CONF_IRRADIANCE_PLANE]
            self._irradiance[CONF_IRRADIANCE_TYPE] = user_input[CONF_IRRADIANCE_TYPE]
            if user_input[CONF_IRRADIANCE_PLANE] == PLANE_MODULE:
                return await self.async_step_irradiance_module()
            return self._finish()
        current = self._current_irradiance()
        plane = current.get(CONF_IRRADIANCE_PLANE)
        sensor_type = current.get(CONF_IRRADIANCE_TYPE)
        return self.async_show_form(
            step_id="irradiance",
            data_schema=vol.Schema({
                vol.Required(CONF_IRRADIANCE_PLANE,
                             default=plane if plane in IRRADIANCE_PLANES else PLANE_HORIZONTAL):
                    SelectSelector(SelectSelectorConfig(
                        options=list(IRRADIANCE_PLANES), mode=SelectSelectorMode.LIST,
                        translation_key=CONF_IRRADIANCE_PLANE)),
                vol.Required(CONF_IRRADIANCE_TYPE,
                             default=sensor_type if sensor_type in IRRADIANCE_TYPES
                             else DEFAULT_IRRADIANCE_TYPE):
                    SelectSelector(SelectSelectorConfig(
                        options=list(IRRADIANCE_TYPES), mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_IRRADIANCE_TYPE)),
            }),
            description_placeholders={"entity": self._irradiance[CONF_IRRADIANCE_SENSOR]},
        )

    async def async_step_irradiance_module(self, user_input: dict[str, Any] | None = None
                                           ) -> ConfigFlowResult:
        """The tilt and azimuth of a sensor in the module plane."""
        if user_input is not None:
            self._irradiance[CONF_IRRADIANCE_TILT] = user_input[CONF_IRRADIANCE_TILT]
            self._irradiance[CONF_IRRADIANCE_AZIMUTH] = user_input[CONF_IRRADIANCE_AZIMUTH]
            return self._finish()
        tilt, azimuth = await self._default_position()
        degrees = {"min": 0, "step": 1, "unit_of_measurement": "°",
                   "mode": NumberSelectorMode.BOX}
        return self.async_show_form(
            step_id="irradiance_module",
            data_schema=vol.Schema({
                vol.Required(CONF_IRRADIANCE_TILT, default=tilt):
                    NumberSelector(NumberSelectorConfig(max=90, **degrees)),
                vol.Required(CONF_IRRADIANCE_AZIMUTH, default=azimuth):
                    NumberSelector(NumberSelectorConfig(max=360, **degrees)),
            }),
        )

    def _current_irradiance(self) -> dict[str, Any]:
        """The stored irradiance options, when they are for the sensor chosen now."""
        options = self.config_entry.options
        if options.get(CONF_IRRADIANCE_SENSOR) != self._irradiance.get(CONF_IRRADIANCE_SENSOR):
            return {}
        return dict(options)

    async def _default_position(self) -> tuple[int, int]:
        """The stored position, else the system's largest roof face (one GET
        system/view, outside the forecast limit), else 30° tilt facing south."""
        current = self._current_irradiance()
        if current.get(CONF_IRRADIANCE_PLANE) == PLANE_MODULE:
            tilt, azimuth = current.get(CONF_IRRADIANCE_TILT), current.get(CONF_IRRADIANCE_AZIMUTH)
            if isinstance(tilt, int) and isinstance(azimuth, int):
                return tilt, azimuth
        client = SolarianceApiClient(async_get_clientsession(self.hass),
                                     self.config_entry.data[CONF_API_TOKEN])
        try:
            face = roof_face(await client.async_get_system(self.config_entry.data[CONF_SYSTEM_ID]))
        except SolarianceError as err:
            LOGGER.debug("No roof to prefill the irradiance sensor's position: %s", err)
            face = None
        except Exception:  # noqa: BLE001 -- a prefill must never break the form
            LOGGER.exception("Unexpected error while reading the system's roof")
            face = None
        return face or (DEFAULT_IRRADIANCE_TILT, DEFAULT_IRRADIANCE_AZIMUTH)

    def _finish(self) -> ConfigFlowResult:
        irradiance = self._irradiance
        self._options.update(irradiance_options(
            self.hass, self.config_entry.options, irradiance[CONF_IRRADIANCE_SENSOR],
            irradiance[CONF_IRRADIANCE_PLANE], irradiance[CONF_IRRADIANCE_TYPE],
            irradiance.get(CONF_IRRADIANCE_TILT), irradiance.get(CONF_IRRADIANCE_AZIMUTH)))
        return self.async_create_entry(data=self._options)
