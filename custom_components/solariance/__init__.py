"""The Solariance integration: PV forecasts for your Solariance systems."""
from __future__ import annotations

import hashlib
from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change, async_track_utc_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_TOKEN,
    CONF_IRRADIANCE_SENSOR,
    CONF_SYSTEM_ID,
    CONF_UPLOAD_SENSOR,
    DOMAIN,
    LOGGER,
    UPLOAD_MINUTE,
)
from .coordinator import (
    SolarianceConfigEntry,
    SolarianceCoordinator,
    WaitForSolariance,
    async_delete_entry_issues,
)
from .irradiance import (
    IrradianceConfig,
    IrradianceUploader,
    account_systems,
    cadence_minutes,
    sensor_changed_issue_id,
    slots,
)
from .services import async_setup_services
from .upload import UploadState, async_upload

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
        # A spent hourly limit, no forecast yet, a paused system or another
        # refusal (a system gone from the account, ...): retrying
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
    _schedule_upload(hass, entry, coordinator)
    _schedule_irradiance(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _schedule_upload(hass: HomeAssistant, entry: SolarianceConfigEntry,
                     coordinator: SolarianceCoordinator) -> None:
    """The production upload (0.3), once an hour at UPLOAD_MINUTE past, when
    the options name an energy sensor. The second within that minute comes
    from the entry's id, so many installations do not arrive at once."""
    sensor = entry.options.get(CONF_UPLOAD_SENSOR)
    if not sensor:
        return
    coordinator.upload_state = UploadState(sensor=sensor)
    second = int(hashlib.sha256(entry.entry_id.encode()).hexdigest(), 16) % 60

    async def _upload(_now: datetime) -> None:
        await async_upload(hass, coordinator.client, entry.data[CONF_SYSTEM_ID], sensor,
                           coordinator.upload_state, dt_util.utcnow())

    entry.async_on_unload(
        async_track_time_change(hass, _upload, minute=UPLOAD_MINUTE, second=second))


def _schedule_irradiance(hass: HomeAssistant, entry: SolarianceConfigEntry,
                         coordinator: SolarianceCoordinator) -> None:
    """The irradiance upload (0.3), every 10 minutes (20, 30 or 60 when more of
    the account's systems upload irradiance), when the options name an
    irradiance sensor. A sensor-changed issue left from before is closed: a
    reload follows a change of the options or the issue's fix, and the next
    run raises it again if Solariance still refuses."""
    ir.async_delete_issue(hass, DOMAIN, sensor_changed_issue_id(entry.entry_id))
    config = IrradianceConfig.from_options(entry.options)
    if config is None:
        if entry.options.get(CONF_IRRADIANCE_SENSOR):
            LOGGER.warning("%s: the irradiance sensor's options are incomplete; nothing is "
                           "uploaded until it is configured again.", entry.title)
        return
    cadence = cadence_minutes(account_systems(hass, entry.data[CONF_API_TOKEN]))
    uploader = IrradianceUploader(hass, coordinator.client, entry_id=entry.entry_id,
                                  title=entry.title, system_id=entry.data[CONF_SYSTEM_ID],
                                  config=config, cadence=cadence)
    coordinator.irradiance = uploader
    minutes, second = slots(entry.entry_id, cadence)

    async def _run(now: datetime) -> None:
        await uploader.async_run(now)

    entry.async_on_unload(
        async_track_utc_time_change(hass, _run, minute=minutes, second=second))


async def async_unload_entry(hass: HomeAssistant, entry: SolarianceConfigEntry) -> bool:
    """Unload a config entry. The paused notice goes with it (a reload sets
    it again if the system is still paused); the system-gone issue stays
    until the system answers or the entry is removed."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_delete_entry_issues(hass, entry.entry_id, gone=False)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SolarianceConfigEntry) -> None:
    """A removed entry's repair issues go with it."""
    async_delete_entry_issues(hass, entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, sensor_changed_issue_id(entry.entry_id))
