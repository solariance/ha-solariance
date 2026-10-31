"""Polls one system's forecast."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SolarianceApiClient,
    SolarianceAuthError,
    SolarianceError,
    SolarianceRateLimitError,
    SolarianceRefusedError,
    SolarianceSystemGoneError,
    SolarianceSystemPausedError,
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

# A paused system stays paused until the plan changes; look again hourly.
PAUSED_RETRY_S = 3600.0
# A refusal (a system gone from the account, anything else a 403 says) is not
# fixed by asking again soon either.
REFUSED_RETRY_S = 3600.0


class WaitForSolariance(UpdateFailed):
    """A failed fetch that only waiting fixes: the hourly limit is spent, no
    forecast is computed yet, or the plan has paused the system. Retrying
    sooner adds refused calls to the budget the Solariance app shares."""


class RateLimited(WaitForSolariance):
    """429: the account's hourly request limit is spent."""


class NoForecastYet(WaitForSolariance):
    """204: Solariance has not computed a forecast for the system yet."""


class SystemPaused(WaitForSolariance):
    """403 system_locked: the account's plan has paused the system."""


class SystemGone(WaitForSolariance):
    """403 not_owner: the system is not in the token's account any more."""


class Refused(WaitForSolariance):
    """Any other 403: the account refuses the request; the token is fine."""


def system_gone_issue_id(entry_id: str) -> str:
    """The repair issue that says an entry's system has left the account."""
    return f"system_not_in_account_{entry_id}"


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
        except SolarianceSystemPausedError as err:
            # Not the token's fault: a new token would be paused too. Say why
            # and look again hourly (an upgraded plan lifts the pause).
            raise SystemPaused(translation_domain=DOMAIN, translation_key="system_paused",
                               retry_after=PAUSED_RETRY_S) from err
        except SolarianceSystemGoneError as err:
            # Deleted at solariance.de, or the token is another account's: no
            # token for THIS account brings it back, so no re-authentication.
            # A repair issue says what to do; look again hourly in case it
            # was a mistake that gets undone.
            ir.async_create_issue(
                self.hass, DOMAIN, system_gone_issue_id(self.config_entry.entry_id),
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key="system_not_in_account",
                translation_placeholders={"title": self.config_entry.title})
            raise SystemGone(translation_domain=DOMAIN,
                             translation_key="system_not_in_account",
                             retry_after=REFUSED_RETRY_S) from err
        except SolarianceRefusedError as err:
            # Any other 403 (an unconfirmed address, a route the token may not
            # use ...): reported with its reason, asked again hourly.
            raise Refused(translation_domain=DOMAIN, translation_key="refused",
                          translation_placeholders={"reason": err.reason or "-"},
                          retry_after=REFUSED_RETRY_S) from err
        except SolarianceRateLimitError as err:
            # The account's hourly limit is shared with the Solariance app;
            # wait as long as the API asks instead of adding to it.
            raise RateLimited(translation_domain=DOMAIN, translation_key="rate_limited",
                              retry_after=err.retry_after) from err
        except SolarianceError as err:
            raise UpdateFailed(translation_domain=DOMAIN,
                               translation_key="cannot_connect") from err
        # Answered: whatever said the system had left the account is over.
        ir.async_delete_issue(self.hass, DOMAIN,
                              system_gone_issue_id(self.config_entry.entry_id))
        if data is None:
            raise NoForecastYet(translation_domain=DOMAIN, translation_key="no_forecast_yet")
        try:
            return parse_power_forecast(data)
        except ForecastFormatError as err:
            raise UpdateFailed(translation_domain=DOMAIN,
                               translation_key="unreadable_forecast") from err
