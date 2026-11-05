"""Polls one system's forecast."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ForecastAnswer,
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

# "No forecast yet" with a Retry-After (0.1.3): ask again when the API says,
# but never sooner than a minute nor later than half an hour. While the 204s
# go on, the earliest retry doubles (1, 2, 4 ... 30 minutes), so a long wait
# (a delayed queue, a pipeline outage) never turns into a poll a minute on
# the budget the Solariance app shares.
RETRY_AFTER_MIN_S = 60.0
RETRY_AFTER_MAX_S = 1800.0

# The paused repair issue's text per locked_reason; any other reason gets the
# text without one.
PAUSED_ISSUE_KEYS = {
    "kwp_limit": "system_paused_kwp_limit",
    "system_limit": "system_paused_system_limit",
}

# The INFO line of a 204, per status (English: Home Assistant's log is).
_WAITING_LOG = {
    "queued": "Solariance is calculating the first forecast for %s",
    "delayed": "Solariance is taking longer than usual to calculate the first forecast for %s",
    "failed": "Solariance's first forecast calculation for %s did not work and is retried "
              "automatically",
    "stale": "Solariance has no current forecast for %s",
}


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


@dataclass(frozen=True)
class ForecastState:
    """What the last answer said about the system's forecast. A field is
    None while the API does not say (before the 2026-11 API, or after an
    answer that says nothing about it)."""

    # queued, delayed, failed, stale (204); provisional, ready (200); paused (403)
    status: str | None = None
    eta: datetime | None = None            # 204: when the forecast is expected
    retry_after_s: float | None = None     # 204: Retry-After as the API sent it
    locked_reason: str | None = None       # paused: kwp_limit, system_limit


def system_gone_issue_id(entry_id: str) -> str:
    """The repair issue that says an entry's system has left the account."""
    return f"system_not_in_account_{entry_id}"


def system_paused_issue_id(entry_id: str) -> str:
    """The (informational) repair issue that says an entry's system is paused."""
    return f"system_paused_{entry_id}"


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry_id: str, *,
                              gone: bool = True) -> None:
    """Close an entry's repair issues: the paused notice, and unless told
    otherwise the system-gone issue."""
    ir.async_delete_issue(hass, DOMAIN, system_paused_issue_id(entry_id))
    if gone:
        ir.async_delete_issue(hass, DOMAIN, system_gone_issue_id(entry_id))


class SolarianceCoordinator(DataUpdateCoordinator[SolarForecast]):
    """One config entry = one Solariance system."""

    config_entry: SolarianceConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SolarianceConfigEntry) -> None:
        minutes = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self.regular_interval = timedelta(minutes=minutes)
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_SYSTEM_ID]}",
            update_interval=self.regular_interval,
        )
        self.system_id: str = entry.data[CONF_SYSTEM_ID]
        self.client = SolarianceApiClient(async_get_clientsession(hass),
                                          entry.data[CONF_API_TOKEN])
        self.forecast_state = ForecastState()
        # 204s with a Retry-After in a row: each one doubles the earliest retry.
        self._early_retries = 0

    async def _async_update_data(self) -> SolarForecast:
        # Whatever comes back but a 204 that asks for an early retry polls at
        # the configured interval again. Set here, before the fetch, because
        # Home Assistant schedules the next refresh from it right after this
        # returns -- also after the first refresh at setup, where it ignores
        # an UpdateFailed's retry_after.
        self.update_interval = self.regular_interval
        early_retries, self._early_retries = self._early_retries, 0
        entry_id = self.config_entry.entry_id
        try:
            answer = await self.client.async_get_power_forecast(self.system_id)
        except SolarianceAuthError as err:
            # Revoked or expired token: Home Assistant asks for a new one.
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN,
                                        translation_key="auth_failed") from err
        except SolarianceSystemPausedError as err:
            # Not the token's fault: a new token would be paused too. Say why
            # and look again hourly (an upgraded plan lifts the pause). The
            # repair notice only informs: nothing to fix in Home Assistant.
            self.forecast_state = ForecastState(status="paused", locked_reason=err.reason)
            ir.async_delete_issue(self.hass, DOMAIN, system_gone_issue_id(entry_id))
            ir.async_create_issue(
                self.hass, DOMAIN, system_paused_issue_id(entry_id),
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=PAUSED_ISSUE_KEYS.get(err.reason or "", "system_paused"),
                translation_placeholders={"name": self.config_entry.title})
            raise SystemPaused(translation_domain=DOMAIN, translation_key="system_paused",
                               retry_after=PAUSED_RETRY_S) from err
        except SolarianceSystemGoneError as err:
            # Deleted at solariance.de, or the token is another account's: no
            # token for THIS account brings it back, so no re-authentication.
            # A repair issue says what to do; look again hourly in case it
            # was a mistake that gets undone.
            self.forecast_state = ForecastState()
            ir.async_delete_issue(self.hass, DOMAIN, system_paused_issue_id(entry_id))
            ir.async_create_issue(
                self.hass, DOMAIN, system_gone_issue_id(entry_id),
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
        # Answered: the system is in the account and not paused, so whatever
        # said otherwise is over.
        async_delete_entry_issues(self.hass, entry_id)
        if answer.data is None:
            self._wait_for_forecast(answer, early_retries)
            raise NoForecastYet(translation_domain=DOMAIN, translation_key="no_forecast_yet")
        try:
            forecast = parse_power_forecast(answer.data)
        except ForecastFormatError as err:
            self.forecast_state = ForecastState()
            raise UpdateFailed(translation_domain=DOMAIN,
                               translation_key="unreadable_forecast") from err
        if forecast.forecast_status is None and answer.status is not None:
            # The body says nothing; the header does.
            forecast = dataclasses.replace(forecast, forecast_status=answer.status)
        self.forecast_state = ForecastState(status=forecast.forecast_status)
        return forecast

    def _wait_for_forecast(self, answer: ForecastAnswer, early_retries: int) -> None:
        """A 204: ask again when the API says (within bounds), and say once
        per status what Solariance is doing."""
        previous = self.forecast_state
        self.forecast_state = ForecastState(status=answer.status, eta=answer.eta,
                                            retry_after_s=answer.retry_after)
        if answer.retry_after is not None:
            self._early_retries = early_retries + 1
            # Capped exponent: 60 * 2**5 is past the 30 minutes already, and a
            # wait of weeks must not overflow the float.
            earliest = min(RETRY_AFTER_MIN_S * 2 ** min(early_retries, 5), RETRY_AFTER_MAX_S)
            self.update_interval = timedelta(
                seconds=min(max(answer.retry_after, earliest), RETRY_AFTER_MAX_S))
        if answer.status is None or answer.status == previous.status:
            return
        lead = _WAITING_LOG.get(answer.status, "Solariance has no forecast for %s yet")
        detail = f"status {answer.status}"
        if answer.eta is not None:
            eta = dt_util.as_local(answer.eta).isoformat(timespec="seconds")
            detail += f", expected by {eta}"
        LOGGER.info(lead + " (%s); checking again in %d s.", self.config_entry.title, detail,
                    round(self.update_interval.total_seconds()))
