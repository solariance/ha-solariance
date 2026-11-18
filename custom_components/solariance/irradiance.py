"""The irradiance upload (0.3): the chosen irradiance sensor's 5-minute means
to Solariance, every 10 minutes, fill-only.

WHAT IS SENT. The recorder's short-term statistics of the sensor: the `mean`
of each 5-minute period, which the recorder keeps for every sensor with the
state class `measurement` (Home Assistant allows no other for the irradiance
device class), labelled by the period's start in UTC. The values go in W/m²;
a sensor in BTU/(h⋅ft²) is converted. The unit is the one in the statistics'
metadata, which the recorder keeps fixed (it stops compiling a sensor whose
unit changes), so it is reliable; any other unit is not sent. An illuminance
sensor (lux) is a different device class: it is never offered and never sent.

ONE REQUEST PER RUN AND SYSTEM: POST measured/irradiance/upload, interval 5
minutes, label "start", source "ha_sensor", with the sensor's id and position.
A run sends the last IRRADIANCE_RECENT_HOURS hours; the first run of each UTC
hour, the first run after setup and the first run after a gap send the last
IRRADIANCE_FILL_HOURS hours instead -- in the same request, not in an extra
one. Solariance stores them fill-only, so a repeat stores nothing.

WHEN. Every 10 minutes while the account has up to 10 systems uploading
irradiance from this Home Assistant (entries with the same token), every 20,
30 or 60 minutes beyond that: the route allows 120 requests an hour per
account, and this keeps Home Assistant at 60 at most for up to 60 systems. The
minute and second within the cycle come from the entry's id, so installations
do not arrive at once. Nothing is sent at setup.

THE SENSOR ID. "ha-" + 12 hex digits of the entity's registry id (stable when
the entity is renamed) + "-" + 6 hex digits of the sensor's position, fixed
when the options are saved. Solariance fixes a sensor's position once it has
values (409 sensor_changed otherwise), so a sensor moved in the options gets a
new id and starts a new series; moving it back continues the old one. Nothing
of the entity's name leaves Home Assistant.

WHEN IT IS REFUSED, one log line per change, never one per run, and no retry
storm on the account's budget: Solariance has the uploads switched off (503
irradiance_uploads_off) or the account lacks the feature (403
feature_required) -> INFO, next try in 6 hours; no measured-data opt-in, no
Upload scope, a paused system -> WARNING, 6 hours; 429 -> its Retry-After
(1 minute to 6 hours, 1 hour without one); 401 or another 4xx -> 1 hour; 409
sensor_changed -> a repair issue whose fix starts a new id, and no upload
until then; a server or network error -> the next run. A 200 whose series
Solariance rejected stored nothing either: a ninth sensor id of the system
(too_many_sensors), a system gone from the account or paused -> WARNING, 6
hours; any other reason (no plausible value, ...) -> WARNING, 1 hour. None of
it touches the forecast sensors or the production upload.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, UnitOfIrradiance
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .api import (
    SolarianceApiClient,
    SolarianceAuthError,
    SolarianceError,
    SolarianceRateLimitError,
    SolarianceRefusedError,
    SolarianceRequestError,
    SolarianceSystemPausedError,
    SolarianceUnavailableError,
    normalise_token,
)
from .const import (
    CONF_API_TOKEN,
    CONF_IRRADIANCE_AZIMUTH,
    CONF_IRRADIANCE_GENERATION,
    CONF_IRRADIANCE_PLANE,
    CONF_IRRADIANCE_SENSOR,
    CONF_IRRADIANCE_SENSOR_ID,
    CONF_IRRADIANCE_TILT,
    CONF_IRRADIANCE_TYPE,
    DEFAULT_IRRADIANCE_TYPE,
    DOMAIN,
    IRRADIANCE_CADENCES,
    IRRADIANCE_FILL_HOURS,
    IRRADIANCE_INTERVAL_MINUTES,
    IRRADIANCE_MAX_CADENCE,
    IRRADIANCE_PLANES,
    IRRADIANCE_RECENT_HOURS,
    IRRADIANCE_TYPES,
    LOGGER,
    PLANE_HORIZONTAL,
    PLANE_MODULE,
    UPLOAD_SOURCE,
)

# W/m² per unit of what the statistics are kept in. "W/m2" is how some
# integrations spell it. 1 BTU/(h⋅ft²) = 1055.05585262 J / 3600 s / 0.09290304 m².
UNIT_FACTORS: dict[str, float] = {
    UnitOfIrradiance.WATTS_PER_SQUARE_METER: 1.0,
    "W/m2": 1.0,
    UnitOfIrradiance.BTUS_PER_HOUR_SQUARE_FOOT: 1055.05585262 / 3600 / 0.09290304,
}
WIRE_UNIT = "W/m2"

SENSOR_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")

# How long a refusal keeps the upload quiet. A refused request counts against
# the account's 120 an hour like any other.
SWITCHED_OFF_RETRY = timedelta(hours=6)
REFUSED_RETRY = timedelta(hours=6)
AUTH_RETRY = timedelta(hours=1)
REQUEST_RETRY = timedelta(hours=1)
RATE_LIMIT_RETRY = timedelta(hours=1)          # a 429 without Retry-After
RATE_LIMIT_MIN = timedelta(minutes=1)
RATE_LIMIT_MAX = timedelta(hours=6)

# Refusals that only Solariance can lift: said at INFO, not as a warning.
_QUIET = {("switched_off", "irradiance_uploads_off"), ("refused", "feature_required")}

# Reasons of a rejected series (in a 200) that the next run cannot change: the
# system's eight sensor ids are taken, the system is not the account's, or the
# plan has paused it. Quiet as long as a refusal; any other reason 1 hour.
_SERIES_REFUSALS = {"too_many_sensors", "unknown_system", "system_locked"}

_HINTS = {
    "irradiance_uploads_off": "Solariance does not take irradiance uploads yet",
    "feature_required": "irradiance uploads are not part of the Solariance account yet",
    "token_scope": "the API token needs the Upload (or ReadWrite) scope",
    "measured_opt_in_required": "the Solariance account has not opted in to measured data",
    "system_locked": "the system is paused by the account's plan",
    "not_owner": "the system is not in the token's account",
    "unknown_system": "the system is not in the token's account",
    "too_many_sensors": "Solariance keeps at most 8 irradiance sensor ids per system and every new "
                        "position takes one; erase an unused id first (DELETE measured/irradiance)",
    "nothing_valid": "Solariance found no plausible value in what was sent; check the sensor's "
                     "unit and the clock",
    "sensor_changed":"Solariance holds values for this sensor id with another position; "
                      "see Settings > Repairs",
}


def sensor_changed_issue_id(entry_id: str) -> str:
    """The repair issue that says the sensor id is taken by another position."""
    return f"irradiance_sensor_changed_{entry_id}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def geometry_part(plane: str, tilt: int | None, azimuth: int | None, generation: int = 0) -> str:
    """6 hex digits for the sensor's position (and the id's generation, which
    the repair raises to start a new id for the same position)."""
    geometry = plane if plane == PLANE_HORIZONTAL else f"{plane}:{tilt}:{azimuth}"
    if generation:
        geometry += f"#{generation}"
    return _digest(geometry)[:6]


def sensor_id(key: str, plane: str, tilt: int | None, azimuth: int | None,
              generation: int = 0) -> str:
    """'ha-<12 hex of the entity>-<6 hex of the position>', 22 characters."""
    return f"ha-{_digest(key)[:12]}-{geometry_part(plane, tilt, azimuth, generation)}"


@callback
def entity_key(hass: HomeAssistant, entity_id: str) -> str:
    """What identifies the entity across renames: its registry id when it has
    one (platform and unique_id), its entity_id otherwise."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.unique_id:
        return f"{entry.platform}:{entry.unique_id}"
    return entity_id


@dataclass(frozen=True)
class IrradianceConfig:
    """The chosen sensor as the options store it."""

    entity_id: str
    sensor_id: str
    plane: str
    sensor_type: str
    tilt: int | None = None
    azimuth: int | None = None

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> IrradianceConfig | None:
        """None when no sensor is chosen, or the options do not describe one
        completely (then nothing is uploaded)."""
        entity = options.get(CONF_IRRADIANCE_SENSOR)
        sid = options.get(CONF_IRRADIANCE_SENSOR_ID)
        plane = options.get(CONF_IRRADIANCE_PLANE)
        if (not isinstance(entity, str) or not entity or not isinstance(sid, str)
                or not SENSOR_ID_RE.fullmatch(sid) or plane not in IRRADIANCE_PLANES):
            return None
        sensor_type = options.get(CONF_IRRADIANCE_TYPE)
        if sensor_type not in IRRADIANCE_TYPES:
            sensor_type = DEFAULT_IRRADIANCE_TYPE
        if plane == PLANE_HORIZONTAL:
            return cls(entity, sid, plane, sensor_type)
        tilt, azimuth = options.get(CONF_IRRADIANCE_TILT), options.get(CONF_IRRADIANCE_AZIMUTH)
        if not (_whole(tilt) and _whole(azimuth) and 0 <= tilt <= 90 and 0 <= azimuth <= 360):
            return None
        return cls(entity, sid, plane, sensor_type, int(tilt), int(azimuth))

    def sensor(self) -> dict[str, Any]:
        """The contract's `sensor` object: tilt and azimuth only for the module plane."""
        out: dict[str, Any] = {"id": self.sensor_id, "plane": self.plane}
        if self.plane == PLANE_MODULE:
            out["tilt_deg"], out["azimuth_deg"] = self.tilt, self.azimuth
        out["type"] = self.sensor_type
        return out


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@callback
def irradiance_options(hass: HomeAssistant, current: Mapping[str, Any], entity_id: str, plane: str,
                       sensor_type: str, tilt: Any = None, azimuth: Any = None) -> dict[str, Any]:
    """The options for a sensor chosen in the options flow. The same entity in
    the same position keeps its id (and generation); anything else gets a new
    id, so Solariance never sees a known id in another position."""
    if plane == PLANE_MODULE:
        tilt, azimuth = round(float(tilt)), round(float(azimuth)) % 360
    else:
        tilt = azimuth = None
    old = IrradianceConfig.from_options(current)
    same = old is not None and (old.entity_id, old.plane, old.tilt, old.azimuth) == (
        entity_id, plane, tilt, azimuth)
    options: dict[str, Any] = {CONF_IRRADIANCE_SENSOR: entity_id, CONF_IRRADIANCE_PLANE: plane,
                               CONF_IRRADIANCE_TYPE: sensor_type}
    if plane == PLANE_MODULE:
        options[CONF_IRRADIANCE_TILT], options[CONF_IRRADIANCE_AZIMUTH] = tilt, azimuth
    if same:
        options[CONF_IRRADIANCE_SENSOR_ID] = old.sensor_id
        if generation := int(current.get(CONF_IRRADIANCE_GENERATION) or 0):
            options[CONF_IRRADIANCE_GENERATION] = generation
    else:
        options[CONF_IRRADIANCE_SENSOR_ID] = sensor_id(entity_key(hass, entity_id), plane, tilt,
                                                       azimuth)
    return options


def rotated_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options with a new sensor id for the same sensor and position (the
    repair of a 409 sensor_changed): the next generation."""
    config = IrradianceConfig.from_options(options)
    out = dict(options)
    if config is None:
        return out
    generation = int(options.get(CONF_IRRADIANCE_GENERATION) or 0) + 1
    base = config.sensor_id.rsplit("-", 1)[0] if config.sensor_id.count("-") >= 2 else "ha"
    out[CONF_IRRADIANCE_GENERATION] = generation
    out[CONF_IRRADIANCE_SENSOR_ID] = (
        f"{base}-{geometry_part(config.plane, config.tilt, config.azimuth, generation)}")
    return out


@callback
def entity_problem(hass: HomeAssistant, entity_id: str) -> str | None:
    """Why an entity cannot be the irradiance sensor (an options-flow error
    key), or None. The selector offers only the irradiance device class; this
    holds when an entity id is typed or the frontend filter is bypassed."""
    state = hass.states.get(entity_id)
    registry = er.async_get(hass).async_get(entity_id)
    device_class = state.attributes.get(ATTR_DEVICE_CLASS) if state else None
    if device_class is None and registry is not None:
        device_class = registry.device_class or registry.original_device_class
    if device_class != SensorDeviceClass.IRRADIANCE:
        return "not_irradiance"
    if state is None:
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    if unit is not None and unit not in UNIT_FACTORS:
        return "unsupported_unit"
    if state.attributes.get("state_class") not in (None, SensorStateClass.MEASUREMENT):
        return "no_statistics"
    return None


def roof_face(system: Mapping[str, Any]) -> tuple[int, int] | None:
    """(tilt, azimuth) of the system's largest roof face (by modules x rated
    power) as system/view stores it, azimuth clockwise from north; None when
    no face reads."""
    best: tuple[float, int, int] | None = None
    for module in system.get("modules") or []:
        if not isinstance(module, dict):
            continue
        tilt, azimuth = module.get("tilt"), module.get("azimuth")
        if not (isinstance(tilt, (int, float)) and isinstance(azimuth, (int, float))
                and 0 <= tilt <= 90 and 0 <= azimuth <= 360):
            continue
        details = module.get("custom_details")
        details = details if isinstance(details, dict) else {}
        try:
            weight = float(module.get("quantity") or 0) * float(details.get("pdc0") or 1)
        except (TypeError, ValueError):
            weight = 0.0
        if best is None or weight > best[0]:
            best = (weight, round(tilt), round(azimuth) % 360)
    return None if best is None else (best[1], best[2])


def _start(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    return None


def five_minute_values(rows: list[dict[str, Any]], factor: float = 1.0) -> list[list[Any]]:
    """[[period start ISO UTC, W/m²], ...] of the periods whose mean reads, in
    time order, to 0.1 W/m²."""
    out: list[tuple[datetime, float]] = []
    for row in rows or []:
        start, mean = _start(row.get("start")), row.get("mean")
        if (start is None or isinstance(mean, bool) or not isinstance(mean, (int, float))
                or not math.isfinite(mean)):
            continue
        out.append((start, round(float(mean) * factor, 1)))
    out.sort(key=lambda pair: pair[0])
    return [[start.strftime("%Y-%m-%dT%H:%M:%SZ"), value] for start, value in out]


def payload(system_id: str, config: IrradianceConfig, values: list[list[Any]]) -> dict[str, Any]:
    """The contract's JSON for one system's sensor."""
    return {"unit": WIRE_UNIT, "interval_minutes": IRRADIANCE_INTERVAL_MINUTES,
            "interval_label": "start", "source": UPLOAD_SOURCE,
            "series": [{"system_id": system_id, "sensor": config.sensor(), "values": values}]}


def _read(hass: HomeAssistant, entity_id: str, start: datetime,
          end: datetime) -> tuple[str | None, list[dict[str, Any]]] | None:
    """In the recorder's executor: the statistics' unit and 5-minute rows, or
    None when the recorder keeps no statistics for the entity."""
    metadata = get_metadata(hass, statistic_ids={entity_id})
    if entity_id not in metadata:
        return None
    unit = metadata[entity_id][1].get("unit_of_measurement")
    stats = statistics_during_period(hass, start, end, {entity_id}, "5minute", None, {"mean"})
    return unit, list(stats.get(entity_id, []))


async def async_five_minute_rows(hass: HomeAssistant, entity_id: str, now: datetime,
                                 hours: int) -> tuple[str | None, list[dict[str, Any]]] | None:
    """The unit and the 5-minute statistics of the last `hours` hours before
    `now` (from a 5-minute boundary), or None without statistics."""
    end = now.astimezone(UTC)
    start = end.replace(minute=end.minute - end.minute % IRRADIANCE_INTERVAL_MINUTES, second=0,
                        microsecond=0) - timedelta(hours=hours)
    return await get_instance(hass).async_add_executor_job(_read, hass, entity_id, start, end)


def cadence_minutes(systems: int) -> int:
    """Minutes between runs when `systems` of one account upload irradiance."""
    for limit, minutes in IRRADIANCE_CADENCES:
        if systems <= limit:
            return minutes
    return IRRADIANCE_MAX_CADENCE


def slots(entry_id: str, cadence: int) -> tuple[tuple[int, ...], int]:
    """The UTC minutes and the second of an entry's runs: one minute per
    cycle, never on the full hour, spread by the entry's id."""
    digest = int(_digest(entry_id), 16)
    first = 1 + digest % (cadence - 1)
    return tuple(range(first, 60, cadence)), (digest // cadence) % 60


@callback
def account_systems(hass: HomeAssistant, token: str) -> int:
    """How many enabled entries upload irradiance with this token: the
    systems one account's budget is shared by, as far as this Home Assistant
    knows."""
    token = normalise_token(token)
    return sum(1 for entry in hass.config_entries.async_entries(DOMAIN)
               if entry.disabled_by is None and entry.options.get(CONF_IRRADIANCE_SENSOR)
               and normalise_token(str(entry.data.get(CONF_API_TOKEN, ""))) == token)


@dataclass
class IrradianceState:
    """The last run, for diagnostics and for logging only on a change."""

    config: IrradianceConfig
    cadence_minutes: int
    last_run_utc: datetime | None = None
    # ok | nothing | no_statistics | unsupported_unit | switched_off | refused |
    # rejected | rate_limited | auth | request | sensor_changed | failed
    outcome: str | None = None
    reason: str | None = None
    window_hours: int = 0
    values_sent: int = 0
    stored: int = 0
    kept: int = 0
    replaced: int = 0
    rejected: int = 0
    series_status: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    last_ok_utc: datetime | None = None
    next_attempt_utc: datetime | None = None   # a refusal's quiet time
    stopped: bool = False                      # 409: until the options change

    def as_dict(self) -> dict[str, Any]:
        config = self.config
        return {
            "entity_id": config.entity_id, "sensor_id": config.sensor_id, "plane": config.plane,
            "tilt_deg": config.tilt, "azimuth_deg": config.azimuth, "type": config.sensor_type,
            "cadence_minutes": self.cadence_minutes, "last_run_utc": _iso(self.last_run_utc),
            "outcome": self.outcome, "reason": self.reason, "window_hours": self.window_hours,
            "values_sent": self.values_sent, "stored": self.stored, "kept": self.kept,
            "replaced": self.replaced, "rejected": self.rejected,
            "series_status": self.series_status, "warnings": list(self.warnings),
            "last_ok_utc": _iso(self.last_ok_utc), "next_attempt_utc": _iso(self.next_attempt_utc),
            "stopped": self.stopped,
        }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


class IrradianceUploader:
    """One entry's irradiance upload: what a run sends, and what a refusal
    leaves behind."""

    def __init__(self, hass: HomeAssistant, client: SolarianceApiClient, *, entry_id: str,
                 title: str, system_id: str, config: IrradianceConfig, cadence: int) -> None:
        self.hass = hass
        self.client = client
        self.entry_id = entry_id
        self.title = title
        self.system_id = system_id
        self.state = IrradianceState(config=config, cadence_minutes=cadence)

    def window_hours(self, now: datetime) -> int:
        """48 on the first run of a UTC hour, on the first run at all and
        after a gap of more than half the short window; 2 otherwise."""
        last = self.state.last_ok_utc
        if (now.minute < self.state.cadence_minutes or last is None
                or now - last > timedelta(hours=IRRADIANCE_RECENT_HOURS / 2)):
            return IRRADIANCE_FILL_HOURS
        return IRRADIANCE_RECENT_HOURS

    async def async_run(self, now: datetime) -> IrradianceState:
        """One scheduled run. Never raises: the state says what happened."""
        state, config = self.state, self.state.config
        now = now.astimezone(UTC)
        if state.stopped or (state.next_attempt_utc is not None and now < state.next_attempt_utc):
            return state
        previous = (state.outcome, state.reason)
        hours = self.window_hours(now)
        state.last_run_utc, state.window_hours, state.next_attempt_utc = now, hours, None
        state.reason, state.series_status, state.warnings = None, None, ()
        state.values_sent = state.stored = state.kept = state.replaced = state.rejected = 0
        try:
            read = await async_five_minute_rows(self.hass, config.entity_id, now, hours)
            if read is None:
                state.outcome = "no_statistics"
            elif (factor := UNIT_FACTORS.get(read[0] or "")) is None:
                state.outcome, state.reason = "unsupported_unit", str(read[0])
            elif not (values := five_minute_values(read[1], factor)):
                state.outcome = "nothing"
            else:
                answer = await self.client.async_upload_irradiance(
                    payload(self.system_id, config, values))
                state.values_sent = len(values)
                state.stored, state.kept = answer.stored, answer.kept
                state.replaced, state.rejected = answer.replaced, answer.rejected
                state.series_status, state.reason = answer.status, answer.reason
                state.warnings = answer.warnings
                if answer.status == "rejected":
                    # A 200, but the one series sent stored nothing.
                    reason = answer.reason or "rejected"
                    self._quiet(now, REFUSED_RETRY if reason in _SERIES_REFUSALS else REQUEST_RETRY,
                                "rejected", reason)
                else:
                    state.outcome, state.last_ok_utc = "ok", now
                    ir.async_delete_issue(self.hass, DOMAIN, sensor_changed_issue_id(self.entry_id))
        except SolarianceUnavailableError as err:
            if err.reason == "irradiance_uploads_off":
                self._quiet(now, SWITCHED_OFF_RETRY, "switched_off", err.reason)
            else:
                state.outcome, state.reason = "failed", err.reason
        except SolarianceAuthError:
            self._quiet(now, AUTH_RETRY, "auth", None)
        except SolarianceRateLimitError as err:
            wait = RATE_LIMIT_RETRY if err.retry_after is None else min(
                max(timedelta(seconds=err.retry_after), RATE_LIMIT_MIN), RATE_LIMIT_MAX)
            self._quiet(now, wait, "rate_limited", None)
        except SolarianceSystemPausedError:
            self._quiet(now, REFUSED_RETRY, "refused", "system_locked")
        except SolarianceRefusedError as err:
            # No reason: the token's scope (ReadOnly). Otherwise the account's.
            self._quiet(now, REFUSED_RETRY, "refused", err.reason or "token_scope")
        except SolarianceRequestError as err:
            if err.status == 409 and err.reason == "sensor_changed":
                self._sensor_changed()
            else:
                self._quiet(now, REQUEST_RETRY, "request", err.reason or f"http_{err.status}")
        except SolarianceError as err:
            state.outcome, state.reason = "failed", type(err).__name__
        except Exception as err:  # noqa: BLE001 -- a recorder problem must not break the entry
            state.outcome, state.reason = "failed", type(err).__name__
        if (state.outcome, state.reason) != previous:
            self._log_change()
        return state

    def _quiet(self, now: datetime, wait: timedelta, outcome: str, reason: str | None) -> None:
        self.state.outcome, self.state.reason = outcome, reason
        self.state.next_attempt_utc = now + wait

    def _sensor_changed(self) -> None:
        """409: the id holds values with another position. Asking again with
        the same id cannot work: stop until the options change, and say so
        where the owner looks (one repair issue, its fix starts a new id)."""
        state = self.state
        state.outcome, state.reason, state.stopped = "sensor_changed", "sensor_changed", True
        ir.async_create_issue(
            self.hass, DOMAIN, sensor_changed_issue_id(self.entry_id),
            is_fixable=True, severity=ir.IssueSeverity.WARNING,
            translation_key="irradiance_sensor_changed",
            translation_placeholders={"title": self.title, "sensor_id": state.config.sensor_id},
            data={"entry_id": self.entry_id})

    def _log_change(self) -> None:
        state = self.state
        lead = "Solariance irradiance upload for %s from %s: %s"
        args: list[Any] = [self.system_id, state.config.entity_id, state.outcome]
        if state.outcome in ("ok", "nothing"):
            LOGGER.info(lead, *args)
            return
        hint = _HINTS.get(state.reason or "", state.reason or "")
        if state.stopped:
            then = "No more uploads until the sensor is configured again."
        elif state.next_attempt_utc is not None and state.last_run_utc is not None:
            then = f"Trying again in {_duration(state.next_attempt_utc - state.last_run_utc)}."
        else:
            then = "Trying again at the next run."
        log = LOGGER.info if (state.outcome, state.reason) in _QUIET else LOGGER.warning
        log(lead + "%s. %s", *args, f" ({hint})" if hint else "", then)


def _duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds >= 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds} s"
