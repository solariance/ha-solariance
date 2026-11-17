"""The PV production upload (0.2): the chosen energy sensor's hourly statistics
to Solariance, once an hour.

WHAT IS SENT. The recorder's long-term statistics of the sensor (any sensor
with a `total`/`total_increasing` state class and an energy unit keeps them):
the `change` of each full hour, in kWh, of the last UPLOAD_HOURS hours, labelled
by the hour's start in UTC. A negative change (a meter reset) is left out.
Statistics are compiled a few minutes after the hour, so the upload runs at
UPLOAD_MINUTE past the hour, spread over that minute per entry.

HOW SOLARIANCE STORES IT. POST measured/production/upload with source
"ha_sensor": fill-only -- an hour already stored (by an earlier upload, or by
the owner's own file) is kept, so re-sending the last 48 hours every hour costs
nothing and fills what a restart missed.

WHEN IT IS REFUSED, nothing is retried before the next hour and the state says
why (diagnostics): no Upload/ReadWrite scope on the token (403 without a
reason), the account has no measured-data opt-in or feature (403
measured_opt_in_required / feature_required), a spent limit (429), or the
server or network (logged once per change of state, never per hour).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .api import (
    SolarianceApiClient,
    SolarianceAuthError,
    SolarianceError,
    SolarianceRateLimitError,
    SolarianceRefusedError,
    SolarianceRequestError,
)
from .const import LOGGER, UPLOAD_HOURS, UPLOAD_SOURCE


@dataclass
class UploadState:
    """The last upload, for diagnostics and for logging only on a change."""

    sensor: str | None = None
    last_run_utc: datetime | None = None
    outcome: str | None = None        # ok | nothing | refused | rate_limited | auth | failed
    reason: str | None = None
    hours_sent: int = 0
    stored: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"sensor": self.sensor,
                "last_run_utc": None if self.last_run_utc is None else self.last_run_utc.isoformat(),
                "outcome": self.outcome, "reason": self.reason, "hours_sent": self.hours_sent,
                "stored": self.stored}


def _start(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    return None


def hourly_values(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """[[start ISO UTC, kWh], ...] of the hours whose change reads and is not
    negative, in time order."""
    out: list[tuple[datetime, float]] = []
    for row in rows or []:
        start, change = _start(row.get("start")), row.get("change")
        if start is None or not isinstance(change, (int, float)) or change < 0:
            continue
        out.append((start, round(float(change), 4)))
    out.sort(key=lambda pair: pair[0])
    return [[start.strftime("%Y-%m-%dT%H:%M:%SZ"), kwh] for start, kwh in out]


def payload(system_id: str, values: list[list[Any]]) -> dict[str, Any]:
    """The contract's JSON for one system's hours."""
    return {"quantity": "generation", "unit": "kWh", "interval_minutes": 60, "interval_label": "start",
            "source": UPLOAD_SOURCE, "series": [{"system_id": system_id, "values": values}]}


async def async_hourly_rows(hass: HomeAssistant, sensor: str, now: datetime) -> list[dict[str, Any]]:
    """The recorder's hourly statistics of `sensor` for the last UPLOAD_HOURS
    full hours before `now`."""
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=UPLOAD_HOURS)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {sensor}, "hour",
        {"energy": UnitOfEnergy.KILO_WATT_HOUR}, {"change"})
    return list(stats.get(sensor, []))


async def async_upload(hass: HomeAssistant, client: SolarianceApiClient, system_id: str, sensor: str,
                       state: UploadState, now: datetime) -> UploadState:
    """One hourly run. Never raises: the state says what happened."""
    previous = (state.outcome, state.reason)
    state.sensor, state.last_run_utc = sensor, now.astimezone(UTC)
    state.reason, state.hours_sent, state.stored = None, 0, 0
    try:
        values = hourly_values(await async_hourly_rows(hass, sensor, now))
        if not values:
            state.outcome = "nothing"
        else:
            answer = await client.async_upload_measured(payload(system_id, values))
            state.outcome, state.hours_sent, state.stored = "ok", len(values), answer.stored
    except SolarianceAuthError:
        state.outcome = "auth"
    except SolarianceRateLimitError:
        state.outcome = "rate_limited"
    except SolarianceRefusedError as err:
        # No reason: the token's scope (ReadOnly). Otherwise the account's.
        state.outcome, state.reason = "refused", err.reason or "token_scope"
    except SolarianceRequestError as err:
        state.outcome, state.reason = "failed", err.reason
    except SolarianceError as err:
        state.outcome, state.reason = "failed", type(err).__name__
    except Exception as err:  # noqa: BLE001 -- a recorder problem must not break the entry
        state.outcome, state.reason = "failed", type(err).__name__
    if (state.outcome, state.reason) != previous:
        _log_change(system_id, sensor, state)
    return state


def _log_change(system_id: str, sensor: str, state: UploadState) -> None:
    if state.outcome in ("ok", "nothing"):
        LOGGER.info("Solariance upload for %s from %s: %s", system_id, sensor, state.outcome)
        return
    hint = {
        "token_scope": "the API token needs the Upload (or ReadWrite) scope",
        "measured_opt_in_required": "the Solariance account has not opted in to measured data",
        "feature_required": "measured data is not switched on for the Solariance account",
    }.get(state.reason or "", state.reason or "")
    LOGGER.warning("Solariance upload for %s from %s: %s%s. Trying again next hour.", system_id, sensor,
                   state.outcome, f" ({hint})" if hint else "")
