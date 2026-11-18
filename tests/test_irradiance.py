"""The irradiance upload (0.3): the sensor's 5-minute means every 10 minutes,
fill-only (source ha_sensor), its options, its id, its schedule and what a
refusal leaves behind."""
from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.solariance import irradiance, repairs, upload
from custom_components.solariance.api import SolarianceApiClient
from custom_components.solariance.const import (
    API_BASE,
    CONF_API_TOKEN,
    CONF_IRRADIANCE_AZIMUTH,
    CONF_IRRADIANCE_GENERATION,
    CONF_IRRADIANCE_PLANE,
    CONF_IRRADIANCE_SENSOR,
    CONF_IRRADIANCE_SENSOR_ID,
    CONF_IRRADIANCE_TILT,
    CONF_IRRADIANCE_TYPE,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    CONF_UPLOAD_SENSOR,
    DOMAIN,
)
from custom_components.solariance.diagnostics import async_get_config_entry_diagnostics

from .conftest import NOW, SYSTEM_ID, TOKEN, envelope, make_payload

IRRADIANCE_URL = f"{API_BASE}/measured/irradiance/upload"
PRODUCTION_URL = f"{API_BASE}/measured/production/upload"
VIEW_URL = f"{API_BASE}/system/view"
ENTITY = "sensor.station_irradiance"
ENTRY_ID = "01JSOLARIANCETESTENTRY0001"
# NOW is 08:07 UTC on 1 July 2026.
NOW_UTC = NOW.astimezone(UTC)
IRRADIANCE_ATTRS = {"device_class": "irradiance", "state_class": "measurement",
                    "unit_of_measurement": "W/m²"}

HORIZONTAL = {CONF_IRRADIANCE_SENSOR: ENTITY, CONF_IRRADIANCE_PLANE: "horizontal",
              CONF_IRRADIANCE_TYPE: "weather_station",
              CONF_IRRADIANCE_SENSOR_ID: "ha-0123456789ab-abcdef"}
MODULE = {CONF_IRRADIANCE_SENSOR: ENTITY, CONF_IRRADIANCE_PLANE: "module",
          CONF_IRRADIANCE_TILT: 30, CONF_IRRADIANCE_AZIMUTH: 180,
          CONF_IRRADIANCE_TYPE: "pyranometer", CONF_IRRADIANCE_SENSOR_ID: "ha-0123456789ab-123456"}


def rows5(*means, start=datetime(2026, 7, 1, 6, 0, tzinfo=UTC)):
    """Short-term statistics rows: `start` as a timestamp, `mean` in W/m²."""
    return [{"start": (start + timedelta(minutes=5 * i)).timestamp(), "mean": m}
            for i, m in enumerate(means)]


# --- the sensor id -------------------------------------------------------------------------

def test_the_sensor_id_is_short_stable_and_follows_the_position():
    key = "met_station:A4C1-38F2"
    flat = irradiance.sensor_id(key, "horizontal", None, None)
    assert flat == irradiance.sensor_id(key, "horizontal", 30, 180), "horizontal has no position"
    assert irradiance.SENSOR_ID_RE.fullmatch(flat) and len(flat) == 22 and flat.startswith("ha-")
    assert "A4C1" not in flat and "met" not in flat[3:], "nothing of the entity is readable"
    tilted = irradiance.sensor_id(key, "module", 30, 180)
    assert tilted != flat and tilted[:15] == flat[:15], "same entity part, other position part"
    others = {irradiance.sensor_id(key, "module", 31, 180), irradiance.sensor_id(key, "module", 30, 181),
              irradiance.sensor_id(key, "module", 30, 180, generation=1), tilted}
    assert len(others) == 4
    assert irradiance.sensor_id("met_station:other", "horizontal", None, None)[:15] != flat[:15]


async def test_the_options_keep_the_id_for_the_same_sensor_and_position(hass):
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "met_station", "A4C1-38F2",
                                 suggested_object_id="station_irradiance")
    first = irradiance.irradiance_options(hass, {}, ENTITY, "module", "pyranometer", 30.0, 180.0)
    expected = irradiance.sensor_id("met_station:A4C1-38F2", "module", 30, 180)
    assert first == {CONF_IRRADIANCE_SENSOR: ENTITY, CONF_IRRADIANCE_PLANE: "module",
                     CONF_IRRADIANCE_TYPE: "pyranometer", CONF_IRRADIANCE_TILT: 30,
                     CONF_IRRADIANCE_AZIMUTH: 180, CONF_IRRADIANCE_SENSOR_ID: expected}
    # Same sensor and position, another type, after a rotation: id and generation stay.
    rotated = {**first, CONF_IRRADIANCE_SENSOR_ID: "ha-0123456789ab-fedcba", CONF_IRRADIANCE_GENERATION: 2}
    again = irradiance.irradiance_options(hass, rotated, ENTITY, "module", "other", 30, 180)
    assert again[CONF_IRRADIANCE_SENSOR_ID] == "ha-0123456789ab-fedcba"
    assert again[CONF_IRRADIANCE_GENERATION] == 2 and again[CONF_IRRADIANCE_TYPE] == "other"
    # Moved: a new id, generation back to none.
    moved = irradiance.irradiance_options(hass, rotated, ENTITY, "module", "other", 35, 180)
    assert moved[CONF_IRRADIANCE_SENSOR_ID] == irradiance.sensor_id(
        "met_station:A4C1-38F2", "module", 35, 180)
    assert CONF_IRRADIANCE_GENERATION not in moved
    # Horizontal stores no position; azimuth 360 is 0.
    flat = irradiance.irradiance_options(hass, first, ENTITY, "horizontal", "weather_station", 30, 180)
    assert CONF_IRRADIANCE_TILT not in flat and CONF_IRRADIANCE_AZIMUTH not in flat
    assert irradiance.irradiance_options(hass, {}, ENTITY, "module", "other", 10, 360)[
        CONF_IRRADIANCE_AZIMUTH] == 0
    # No registry entry: the entity_id is the key.
    assert irradiance.irradiance_options(hass, {}, "sensor.yaml_only", "horizontal", "other")[
        CONF_IRRADIANCE_SENSOR_ID] == irradiance.sensor_id("sensor.yaml_only", "horizontal", None, None)
    # Renamed entity: the registry id is what counts.
    registry.async_update_entity(ENTITY, new_entity_id="sensor.renamed")
    assert irradiance.irradiance_options(hass, {}, "sensor.renamed", "module", "other", 30, 180)[
        CONF_IRRADIANCE_SENSOR_ID] == expected


def test_a_rotation_is_a_new_id_for_the_same_sensor_and_position():
    once = irradiance.rotated_options(MODULE)
    assert once[CONF_IRRADIANCE_GENERATION] == 1
    assert once[CONF_IRRADIANCE_SENSOR_ID].startswith("ha-0123456789ab-")
    assert once[CONF_IRRADIANCE_SENSOR_ID] == "ha-0123456789ab-" + irradiance.geometry_part(
        "module", 30, 180, 1)
    twice = irradiance.rotated_options(once)
    assert twice[CONF_IRRADIANCE_GENERATION] == 2
    assert len({MODULE[CONF_IRRADIANCE_SENSOR_ID], once[CONF_IRRADIANCE_SENSOR_ID],
                twice[CONF_IRRADIANCE_SENSOR_ID]}) == 3


def test_options_that_do_not_describe_a_sensor_upload_nothing():
    assert irradiance.IrradianceConfig.from_options({}) is None
    assert irradiance.IrradianceConfig.from_options({**MODULE, CONF_IRRADIANCE_TILT: None}) is None
    assert irradiance.IrradianceConfig.from_options({**MODULE, CONF_IRRADIANCE_AZIMUTH: 400}) is None
    assert irradiance.IrradianceConfig.from_options({**MODULE, CONF_IRRADIANCE_SENSOR_ID: "x" * 41}) is None
    assert irradiance.IrradianceConfig.from_options({**HORIZONTAL, CONF_IRRADIANCE_PLANE: "roof"}) is None
    assert irradiance.IrradianceConfig.from_options({**HORIZONTAL, CONF_IRRADIANCE_TYPE: "?"}).sensor_type == (
        "weather_station")


# --- what is sent ----------------------------------------------------------------------------

def test_values_are_the_readable_means_in_time_order():
    got = irradiance.five_minute_values(rows5(0.0, 412.54, None, float("nan"), True, 800))
    assert got == [["2026-07-01T06:00:00Z", 0.0], ["2026-07-01T06:05:00Z", 412.5],
                   ["2026-07-01T06:25:00Z", 800.0]]
    assert irradiance.five_minute_values(list(reversed(rows5(1.0, 2.0)))) == [
        ["2026-07-01T06:00:00Z", 1.0], ["2026-07-01T06:05:00Z", 2.0]]


def test_only_irradiance_units_are_converted():
    btu = irradiance.UNIT_FACTORS["BTU/(h⋅ft²)"]
    assert irradiance.five_minute_values(rows5(100.0), btu) == [["2026-07-01T06:00:00Z", 315.5]]
    assert irradiance.UNIT_FACTORS["W/m²"] == irradiance.UNIT_FACTORS["W/m2"] == 1.0
    assert "lx" not in irradiance.UNIT_FACTORS


def test_the_payload_is_the_contracts_json():
    values = [["2026-07-01T06:00:00Z", 412.5]]
    flat = irradiance.IrradianceConfig.from_options(HORIZONTAL)
    assert irradiance.payload(SYSTEM_ID, flat, values) == {
        "unit": "W/m2", "interval_minutes": 5, "interval_label": "start", "source": "ha_sensor",
        "series": [{"system_id": SYSTEM_ID,
                    "sensor": {"id": "ha-0123456789ab-abcdef", "plane": "horizontal",
                               "type": "weather_station"},
                    "values": values}],
    }
    tilted = irradiance.IrradianceConfig.from_options(MODULE)
    assert irradiance.payload(SYSTEM_ID, tilted, values)["series"][0]["sensor"] == {
        "id": "ha-0123456789ab-123456", "plane": "module", "tilt_deg": 30, "azimuth_deg": 180,
        "type": "pyranometer"}


async def test_the_statistics_read_is_the_5_minute_means_of_the_window(hass):
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=("W/m²", rows5(1.0)))
    with patch.object(irradiance, "get_instance", return_value=instance):
        got = await irradiance.async_five_minute_rows(
            hass, ENTITY, datetime(2026, 7, 1, 8, 7, 30, tzinfo=UTC), 2)
    assert got == ("W/m²", rows5(1.0))
    _fn, _hass, entity, start, end = instance.async_add_executor_job.call_args.args
    assert (entity, start, end) == (ENTITY, datetime(2026, 7, 1, 6, 5, tzinfo=UTC),
                                    datetime(2026, 7, 1, 8, 7, 30, tzinfo=UTC))
    metadata = {ENTITY: (1, {"unit_of_measurement": "W/m²"})}
    with (patch.object(irradiance, "get_metadata", return_value=metadata) as meta,
          patch.object(irradiance, "statistics_during_period",
                       return_value={ENTITY: rows5(2.0)}) as stats):
        assert irradiance._read(hass, ENTITY, start, end) == ("W/m²", rows5(2.0))
    assert meta.call_args.kwargs == {"statistic_ids": {ENTITY}}
    assert stats.call_args.args[1:] == (start, end, {ENTITY}, "5minute", None, {"mean"})
    with patch.object(irradiance, "get_metadata", return_value={}):
        assert irradiance._read(hass, ENTITY, start, end) is None


# --- one run ---------------------------------------------------------------------------------

def _uploader(hass, options=HORIZONTAL, cadence=10):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    client = SolarianceApiClient(async_get_clientsession(hass), TOKEN)
    return irradiance.IrradianceUploader(
        hass, client, entry_id=ENTRY_ID, title="Roof", system_id=SYSTEM_ID,
        config=irradiance.IrradianceConfig.from_options(options), cadence=cadence)


async def _run(uploader, aioclient_mock, now, read=("W/m²", None), **answer):
    """One run at `now`; `answer` is the mocked POST. Returns the hours read."""
    aioclient_mock.clear_requests()
    aioclient_mock.post(IRRADIANCE_URL, **(answer or {"json": envelope({"totals": {}})}))
    rows = AsyncMock(return_value=None if read is None else (read[0], read[1] or rows5(400.0, 410.0)))
    with patch.object(irradiance, "async_five_minute_rows", rows):
        await uploader.async_run(now)
    return rows.call_args.args[3] if rows.call_args else None


def _posts(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if c[0] == "POST"]


async def test_a_run_sends_the_contracts_json_with_the_token(hass, aioclient_mock, caplog):
    caplog.set_level(logging.INFO)
    answer = {"dry_run": False, "upload_id": "u1",
              "totals": {"series": 1, "values": 2, "stored": 1, "replaced": 0, "kept": 1, "rejected": 0},
              "series": [{"system_id": SYSTEM_ID, "sensor_id": "ha-0123456789ab-123456",
                          "status": "stored", "stored": 1, "kept": 1, "rejected": 0,
                          "rejections": [], "warnings": ["night_values"]}]}
    up = _uploader(hass, MODULE)
    hours = await _run(up, aioclient_mock, NOW_UTC + timedelta(minutes=10), json=envelope(answer))
    assert hours == 48, "the first run fills the last 48 hours"
    (_method, url, sent, headers), = _posts(aioclient_mock)
    assert str(url) == IRRADIANCE_URL
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert sent == {
        "unit": "W/m2", "interval_minutes": 5, "interval_label": "start", "source": "ha_sensor",
        "series": [{"system_id": SYSTEM_ID,
                    "sensor": {"id": "ha-0123456789ab-123456", "plane": "module", "tilt_deg": 30,
                               "azimuth_deg": 180, "type": "pyranometer"},
                    "values": [["2026-07-01T06:00:00Z", 400.0], ["2026-07-01T06:05:00Z", 410.0]]}],
    }
    state = up.state
    assert (state.outcome, state.values_sent, state.stored, state.kept) == ("ok", 2, 1, 1)
    assert state.series_status == "stored" and state.warnings == ("night_values",)
    assert "Solariance irradiance upload for PVTEST000001 from sensor.station_irradiance: ok" in caplog.text


async def test_the_window_is_2_hours_and_48_on_the_first_run_of_the_hour(hass, aioclient_mock):
    up = _uploader(hass)
    t = datetime(2026, 7, 1, 8, 13, 20, tzinfo=UTC)
    assert await _run(up, aioclient_mock, t) == 48, "first run"
    assert await _run(up, aioclient_mock, t + timedelta(minutes=10)) == 2
    assert await _run(up, aioclient_mock, t + timedelta(minutes=20)) == 2
    assert await _run(up, aioclient_mock, datetime(2026, 7, 1, 9, 3, 20, tzinfo=UTC)) == 48, (
        "first run of the hour")
    assert await _run(up, aioclient_mock, datetime(2026, 7, 1, 9, 13, 20, tzinfo=UTC)) == 2
    # A gap of more than an hour since the last stored run: 48 again.
    await _run(up, aioclient_mock, datetime(2026, 7, 1, 9, 23, 20, tzinfo=UTC), status=502)
    assert await _run(up, aioclient_mock, datetime(2026, 7, 1, 10, 33, 20, tzinfo=UTC)) == 48


async def test_nothing_readable_sends_nothing(hass, aioclient_mock, caplog):
    up = _uploader(hass)
    await _run(up, aioclient_mock, NOW_UTC, read=("W/m²", [{"start": NOW_UTC.timestamp(), "mean": None}]))
    assert up.state.outcome == "nothing" and _posts(aioclient_mock) == []
    await _run(up, aioclient_mock, NOW_UTC, read=None)
    assert up.state.outcome == "no_statistics" and _posts(aioclient_mock) == []
    await _run(up, aioclient_mock, NOW_UTC, read=("lx", None))
    assert (up.state.outcome, up.state.reason) == ("unsupported_unit", "lx")
    assert _posts(aioclient_mock) == []
    assert "unsupported_unit" in caplog.text


def _rejected(reason):
    """A 200 whose one series Solariance rejected, as irradiance/handler.py answers it."""
    return {"json": envelope({
        "dry_run": False, "upload_id": None,
        "totals": {"series": 1, "values": 2, "stored": 0, "replaced": 0, "kept": 0, "rejected": 2},
        "series": [{"system_id": SYSTEM_ID, "sensor_id": "ha-0123456789ab-abcdef",
                    "status": "rejected", "reason": reason, "stored": 0, "replaced": 0, "kept": 0,
                    "rejected": 2, "rejections": [], "warnings": []}]})}


@pytest.mark.parametrize(("answer", "outcome", "reason", "quiet", "level"), [
    ({"status": 503, "json": {"code": 503, "data": {"reason": "irradiance_uploads_off"}}},
     "switched_off", "irradiance_uploads_off", timedelta(hours=6), logging.INFO),
    # The live API's own bodies (irradiance/handler.py, auth_utils.auth_route).
    ({"status": 503, "json": {"code": 503, "message": "Irradiance uploads are not open.",
                              "data": {"reason": "irradiance_uploads_off", "detail": "schema_missing"}}},
     "switched_off", "irradiance_uploads_off", timedelta(hours=6), logging.INFO),
    ({"status": 403, "json": {"code": 403, "data": {"reason": "feature_required",
                                                    "feature": "measured_data"}}},
     "refused", "feature_required", timedelta(hours=6), logging.INFO),
    ({"status": 403, "json": {"code": 403, "message": "This token is restricted to ReadOnly accesss",
                              "data": {}}},
     "refused", "token_scope", timedelta(hours=6), logging.WARNING),
    ({"status": 429, "headers": {"Retry-After": "3600"},
      "json": {"code": 429, "data": {"reason": "daily_value_cap", "retry_after_seconds": 3600}}},
     "rate_limited", None, timedelta(hours=1), logging.WARNING),
    # A 200 that stored nothing: the series was rejected.
    (_rejected("too_many_sensors"), "rejected", "too_many_sensors", timedelta(hours=6),
     logging.WARNING),
    (_rejected("unknown_system"), "rejected", "unknown_system", timedelta(hours=6), logging.WARNING),
    (_rejected("system_locked"), "rejected", "system_locked", timedelta(hours=6), logging.WARNING),
    (_rejected("nothing_valid"), "rejected", "nothing_valid", timedelta(hours=1), logging.WARNING),
    ({"status": 403, "json": {"code": 403, "data": {"reason": "feature_required",
                                                    "feature": "ai_irradiance"}}},
     "refused", "feature_required", timedelta(hours=6), logging.INFO),
    ({"status": 403, "json": {"code": 403, "data": {"reason": "measured_opt_in_required"}}},
     "refused", "measured_opt_in_required", timedelta(hours=6), logging.WARNING),
    ({"status": 403, "json": {"code": 403, "message": "ReadOnly", "data": None}},
     "refused", "token_scope", timedelta(hours=6), logging.WARNING),
    ({"status": 429, "headers": {"Retry-After": "900"}},
     "rate_limited", None, timedelta(minutes=15), logging.WARNING),
    ({"status": 429}, "rate_limited", None, timedelta(hours=1), logging.WARNING),
    ({"status": 429, "headers": {"Retry-After": "86400"}},
     "rate_limited", None, timedelta(hours=6), logging.WARNING),
    ({"status": 401}, "auth", None, timedelta(hours=1), logging.WARNING),
    ({"status": 400, "json": {"code": 400, "data": {"reason": "invalid_unit"}}},
     "request", "invalid_unit", timedelta(hours=1), logging.WARNING),
])
async def test_a_refusal_keeps_quiet_for_its_time_and_logs_once(hass, aioclient_mock, caplog,
                                                               answer, outcome, reason, quiet, level):
    caplog.set_level(logging.INFO)
    up = _uploader(hass)
    t = datetime(2026, 7, 1, 8, 13, 20, tzinfo=UTC)
    await _run(up, aioclient_mock, t, **answer)
    assert (up.state.outcome, up.state.reason) == (outcome, reason)
    assert up.state.next_attempt_utc == t + quiet
    records = [r for r in caplog.records if "irradiance upload" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == level
    assert not ir.async_get(hass).issues, "no repair issue for a refusal"
    # Every run before the quiet time is over sends nothing.
    step = t
    while step + timedelta(minutes=10) < t + quiet:
        step += timedelta(minutes=10)
        await _run(up, aioclient_mock, step, **answer)
        assert _posts(aioclient_mock) == [], step
    # Then it asks again, with the 48-hour window, and the same answer logs nothing new.
    assert await _run(up, aioclient_mock, t + quiet, **answer) == 48
    assert len(_posts(aioclient_mock)) == 1
    assert len([r for r in caplog.records if "irradiance upload" in r.getMessage()]) == 1


async def test_a_server_error_tries_again_at_the_next_run(hass, aioclient_mock, caplog):
    up = _uploader(hass)
    await _run(up, aioclient_mock, NOW_UTC, status=502)
    assert (up.state.outcome, up.state.next_attempt_utc) == ("failed", None)
    await _run(up, aioclient_mock, NOW_UTC + timedelta(minutes=10), status=503,
               json={"code": 503, "data": {"reason": "maintenance"}})
    assert (up.state.outcome, up.state.reason) == ("failed", "maintenance")
    assert len(_posts(aioclient_mock)) == 1
    with patch.object(irradiance, "async_five_minute_rows", AsyncMock(side_effect=RuntimeError("db"))):
        await up.async_run(NOW_UTC + timedelta(minutes=20))
    assert (up.state.outcome, up.state.reason) == ("failed", "RuntimeError")


async def test_sensor_changed_raises_one_repair_issue_and_stops(hass, aioclient_mock, caplog):
    up = _uploader(hass, MODULE)
    conflict = {"status": 409, "json": {"code": 409, "data": {"reason": "sensor_changed"}}}
    await _run(up, aioclient_mock, NOW_UTC, **conflict)
    assert (up.state.outcome, up.state.stopped) == ("sensor_changed", True)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, irradiance.sensor_changed_issue_id(ENTRY_ID))
    assert issue is not None and issue.is_fixable
    assert issue.translation_key == "irradiance_sensor_changed"
    assert issue.translation_placeholders == {"title": "Roof", "sensor_id": "ha-0123456789ab-123456"}
    assert issue.data == {"entry_id": ENTRY_ID}
    assert "see Settings > Repairs" in caplog.text
    for minutes in (10, 20, 60 * 7):
        await _run(up, aioclient_mock, NOW_UTC + timedelta(minutes=minutes))
        assert _posts(aioclient_mock) == [], "the same id cannot work: nothing until reconfigured"


async def test_a_stored_upload_closes_a_sensor_changed_issue(hass, aioclient_mock):
    ir.async_create_issue(hass, DOMAIN, irradiance.sensor_changed_issue_id(ENTRY_ID), is_fixable=True,
                          severity=ir.IssueSeverity.WARNING, translation_key="irradiance_sensor_changed")
    await _run(_uploader(hass), aioclient_mock, NOW_UTC)
    assert ir.async_get(hass).async_get_issue(DOMAIN, irradiance.sensor_changed_issue_id(ENTRY_ID)) is None


# --- the schedule ----------------------------------------------------------------------------

def test_the_cadence_keeps_an_account_at_half_the_routes_budget():
    for systems, minutes in ((1, 10), (10, 10), (11, 20), (20, 20), (21, 30), (30, 30), (31, 60),
                             (60, 60)):
        assert irradiance.cadence_minutes(systems) == minutes, systems
        assert systems * 60 / minutes <= 60, "requests an hour for the whole account"
    minutes, second = irradiance.slots(ENTRY_ID, 10)
    assert len(minutes) == 6 and 0 not in minutes and 0 <= second < 60
    assert {b - a for a, b in itertools.pairwise(minutes)} == {10}
    assert minutes[0] < 10
    assert len(irradiance.slots(ENTRY_ID, 60)[0]) == 1
    assert len(irradiance.slots(ENTRY_ID, 20)[0]) == 3


async def test_the_account_is_the_enabled_entries_with_the_same_token(hass):
    def add(token, options, disabled_by=None):
        MockConfigEntry(domain=DOMAIN, data={CONF_API_TOKEN: token, CONF_SYSTEM_ID: token},
                        options=options, disabled_by=disabled_by).add_to_hass(hass)
    add(TOKEN, HORIZONTAL)
    add(f"Bearer {TOKEN}", MODULE)
    add(TOKEN, {})
    add(TOKEN, HORIZONTAL, ConfigEntryDisabler.USER)
    add("another-token", HORIZONTAL)
    assert irradiance.account_systems(hass, TOKEN) == 2


def _entry(options=None, token=TOKEN, system_id=SYSTEM_ID, entry_id=ENTRY_ID):
    return MockConfigEntry(domain=DOMAIN, title="Roof", unique_id=system_id, entry_id=entry_id,
                           data={CONF_API_TOKEN: token, CONF_SYSTEM_ID: system_id},
                           options=options or {})


async def _setup(hass, freezer, aioclient_mock, entry):
    freezer.move_to(NOW)
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(make_payload()))
    aioclient_mock.post(IRRADIANCE_URL, json=envelope({"totals": {"stored": 2}}))
    aioclient_mock.post(PRODUCTION_URL, json=envelope({"totals": {"stored": 2, "rejected": 0}}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _fire(hass, freezer, moment):
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _irradiance_posts(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]) == IRRADIANCE_URL]


async def test_with_a_sensor_it_uploads_six_times_an_hour(hass, freezer, aioclient_mock):
    entry = _entry({CONF_SCAN_INTERVAL: 60, **HORIZONTAL})
    rows = AsyncMock(return_value=("W/m²", rows5(400.0)))
    with patch.object(irradiance, "async_five_minute_rows", rows):
        await _setup(hass, freezer, aioclient_mock, entry)
        assert _irradiance_posts(aioclient_mock) == [], "nothing at setup"
        minutes, second = irradiance.slots(ENTRY_ID, 10)
        hour = NOW_UTC.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for minute in minutes:
            await _fire(hass, freezer, hour + timedelta(minutes=minute, seconds=second))
        posts = _irradiance_posts(aioclient_mock)
        assert len(posts) == 6, "one request per 10 minutes, the 48-hour fill included"
        assert {p[2]["source"] for p in posts} == {"ha_sensor"}
        hours = [c.args[3] for c in rows.call_args_list]
        assert hours[0] == 48 and hours[1:] == [2] * 5
        # The next hour, minute by minute: six again, the first of the hour the 48-hour one.
        rows.reset_mock()
        for minute in range(1, 61):
            await _fire(hass, freezer, hour + timedelta(hours=1, minutes=minute))
    assert len(_irradiance_posts(aioclient_mock)) == 12
    assert [c.args[3] for c in rows.call_args_list] == [48, 2, 2, 2, 2, 2]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    status = diagnostics["irradiance_upload"]
    assert status["outcome"] == "ok" and status["sensor_id"] == "ha-0123456789ab-abcdef"
    assert status["cadence_minutes"] == 10 and status["entity_id"] == ENTITY
    assert TOKEN not in str(diagnostics)


async def test_without_an_irradiance_sensor_nothing_is_uploaded(hass, freezer, aioclient_mock):
    entry = _entry({CONF_SCAN_INTERVAL: 60})
    await _setup(hass, freezer, aioclient_mock, entry)
    for minute in range(0, 61, 1):
        await _fire(hass, freezer, NOW_UTC + timedelta(minutes=minute))
    assert _irradiance_posts(aioclient_mock) == []
    assert entry.runtime_data.irradiance is None
    assert (await async_get_config_entry_diagnostics(hass, entry))["irradiance_upload"] is None


async def test_incomplete_options_upload_nothing(hass, freezer, aioclient_mock, caplog):
    entry = _entry({**MODULE, CONF_IRRADIANCE_TILT: None})
    await _setup(hass, freezer, aioclient_mock, entry)
    assert entry.runtime_data.irradiance is None
    assert "options are incomplete" in caplog.text


async def test_more_systems_of_the_account_slow_the_cadence(hass, freezer, aioclient_mock):
    entry = _entry({CONF_SCAN_INTERVAL: 60, **HORIZONTAL})
    rows = AsyncMock(return_value=("W/m²", rows5(400.0)))
    with (patch("custom_components.solariance.account_systems", return_value=11) as counted,
          patch.object(irradiance, "async_five_minute_rows", rows)):
        await _setup(hass, freezer, aioclient_mock, entry)
        counted.assert_called_once_with(hass, TOKEN)
        assert entry.runtime_data.irradiance.state.cadence_minutes == 20
        for minute in range(1, 61):   # the hour after setup, minute by minute
            await _fire(hass, freezer, NOW_UTC + timedelta(minutes=minute))
    assert len(_irradiance_posts(aioclient_mock)) == 3, "every 20 minutes"


async def test_an_irradiance_failure_leaves_forecast_and_production_alone(hass, freezer, aioclient_mock):
    entry = _entry({CONF_SCAN_INTERVAL: 60, CONF_UPLOAD_SENSOR: "sensor.pv_energy", **HORIZONTAL})
    hourly = [{"start": (NOW_UTC.replace(minute=0) - timedelta(hours=1)).timestamp(), "change": 1.5}]
    with (patch.object(irradiance, "async_five_minute_rows", AsyncMock(side_effect=RuntimeError("db"))),
          patch.object(upload, "async_hourly_rows", AsyncMock(return_value=hourly))):
        await _setup(hass, freezer, aioclient_mock, entry)
        coordinator = entry.runtime_data
        minutes, second = irradiance.slots(ENTRY_ID, 10)
        hour = NOW_UTC.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        moments = sorted([hour + timedelta(minutes=m, seconds=second) for m in minutes]
                         + [hour + timedelta(minutes=7, seconds=59)])
        for moment in moments:
            await _fire(hass, freezer, moment)
    assert coordinator.irradiance.state.outcome == "failed"
    assert coordinator.upload_state.outcome == "ok", "the production upload went on"
    assert [c for c in aioclient_mock.mock_calls if str(c[1]) == PRODUCTION_URL]
    assert coordinator.last_update_success and coordinator.data is not None
    state = hass.states.get("sensor.roof_estimated_energy_production_today")
    assert state is not None and state.state not in ("unavailable", "unknown")


# --- the options flow ------------------------------------------------------------------------

async def _options_entry(hass, freezer, aioclient_mock, options=None):
    entry = _entry({CONF_SCAN_INTERVAL: 30, **(options or {})})
    await _setup(hass, freezer, aioclient_mock, entry)
    hass.states.async_set(ENTITY, "412.5", IRRADIANCE_ATTRS)
    return entry


def _selector(result, key):
    return next(v for k, v in result["data_schema"].schema.items() if k == key)


def _default(result, key):
    return next(k for k in result["data_schema"].schema if k == key).default()


async def test_only_irradiance_sensors_are_offered(hass, freezer, aioclient_mock):
    entry = await _options_entry(hass, freezer, aioclient_mock)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    config = _selector(result, CONF_IRRADIANCE_SENSOR).config
    assert cv.ensure_list(config["device_class"]) == ["irradiance"], "no illuminance (lux) sensors"
    assert cv.ensure_list(config["domain"]) == ["sensor"]
    # Typed in anyway: a lux sensor is refused.
    hass.states.async_set("sensor.garden_light", "23000",
                          {"device_class": "illuminance", "unit_of_measurement": "lx"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: "sensor.garden_light"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_IRRADIANCE_SENSOR: "not_irradiance"}
    hass.states.async_set("sensor.odd", "1", {"device_class": "irradiance", "unit_of_measurement": "lx"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: "sensor.odd"})
    assert result["errors"] == {CONF_IRRADIANCE_SENSOR: "unsupported_unit"}
    hass.states.async_set("sensor.total", "1", {**IRRADIANCE_ATTRS, "state_class": "total"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: "sensor.total"})
    assert result["errors"] == {CONF_IRRADIANCE_SENSOR: "no_statistics"}
    assert entry.options == {CONF_SCAN_INTERVAL: 30}


async def test_a_horizontal_sensor_takes_one_more_step(hass, freezer, aioclient_mock):
    entry = await _options_entry(hass, freezer, aioclient_mock)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: ENTITY})
    assert result["step_id"] == "irradiance"
    assert result["description_placeholders"] == {"entity": ENTITY}
    assert _default(result, CONF_IRRADIANCE_PLANE) == "horizontal"
    assert _default(result, CONF_IRRADIANCE_TYPE) == "weather_station"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_PLANE: "horizontal", CONF_IRRADIANCE_TYPE: "weather_station"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_SCAN_INTERVAL: 30, CONF_IRRADIANCE_SENSOR: ENTITY, CONF_IRRADIANCE_PLANE: "horizontal",
        CONF_IRRADIANCE_TYPE: "weather_station",
        CONF_IRRADIANCE_SENSOR_ID: irradiance.sensor_id(ENTITY, "horizontal", None, None)}
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(VIEW_URL)]
    await hass.async_block_till_done()
    assert entry.runtime_data.irradiance is not None, "the reload starts the upload"


async def test_a_module_sensor_is_prefilled_with_the_largest_roof_face(hass, freezer, aioclient_mock):
    entry = await _options_entry(hass, freezer, aioclient_mock)
    modules = [{"quantity": 4, "tilt": 10, "azimuth": 90, "custom_details": {"pdc0": 400}},
               {"quantity": 20, "tilt": 35, "azimuth": 200, "custom_details": {"pdc0": 420}},
               {"quantity": 50, "tilt": 30, "azimuth": -20, "custom_details": {"pdc0": 400}}]
    aioclient_mock.get(VIEW_URL, json=envelope({"system_id": SYSTEM_ID, "modules": modules}))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: ENTITY})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_PLANE: "module", CONF_IRRADIANCE_TYPE: "reference_cell"})
    assert result["step_id"] == "irradiance_module"
    assert (_default(result, CONF_IRRADIANCE_TILT), _default(result, CONF_IRRADIANCE_AZIMUTH)) == (35, 200)
    views = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(VIEW_URL)]
    assert len(views) == 1 and views[0][3]["Authorization"] == f"Bearer {TOKEN}"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_TILT: 35.0, CONF_IRRADIANCE_AZIMUTH: 200.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_SCAN_INTERVAL: 30, CONF_IRRADIANCE_SENSOR: ENTITY, CONF_IRRADIANCE_PLANE: "module",
        CONF_IRRADIANCE_TYPE: "reference_cell", CONF_IRRADIANCE_TILT: 35, CONF_IRRADIANCE_AZIMUTH: 200,
        CONF_IRRADIANCE_SENSOR_ID: irradiance.sensor_id(ENTITY, "module", 35, 200)}


async def test_without_a_roof_the_position_defaults_to_30_south(hass, freezer, aioclient_mock):
    entry = await _options_entry(hass, freezer, aioclient_mock)
    aioclient_mock.get(VIEW_URL, status=500)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: ENTITY})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_PLANE: "module", CONF_IRRADIANCE_TYPE: "weather_station"})
    assert (_default(result, CONF_IRRADIANCE_TILT), _default(result, CONF_IRRADIANCE_AZIMUTH)) == (30, 180)


async def test_the_stored_sensor_prefills_and_keeps_its_id(hass, freezer, aioclient_mock):
    entry = await _options_entry(hass, freezer, aioclient_mock,
                                 {**MODULE, CONF_IRRADIANCE_GENERATION: 1})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: ENTITY})
    assert (_default(result, CONF_IRRADIANCE_PLANE), _default(result, CONF_IRRADIANCE_TYPE)) == (
        "module", "pyranometer")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_PLANE: "module", CONF_IRRADIANCE_TYPE: "pyranometer"})
    assert (_default(result, CONF_IRRADIANCE_TILT), _default(result, CONF_IRRADIANCE_AZIMUTH)) == (30, 180)
    assert not [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(VIEW_URL)], (
        "the stored position needs no roof")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_TILT: 30, CONF_IRRADIANCE_AZIMUTH: 180})
    assert entry.options[CONF_IRRADIANCE_SENSOR_ID] == MODULE[CONF_IRRADIANCE_SENSOR_ID]
    assert entry.options[CONF_IRRADIANCE_GENERATION] == 1
    await hass.async_block_till_done()
    # Moved in the options: a new id, so Solariance never sees the old id elsewhere.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_IRRADIANCE_SENSOR: ENTITY})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_PLANE: "module", CONF_IRRADIANCE_TYPE: "pyranometer"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IRRADIANCE_TILT: 45, CONF_IRRADIANCE_AZIMUTH: 180})
    assert entry.options[CONF_IRRADIANCE_SENSOR_ID] == irradiance.sensor_id(ENTITY, "module", 45, 180)
    assert CONF_IRRADIANCE_GENERATION not in entry.options
    await hass.async_block_till_done()
    # Emptied: every irradiance option goes, the upload stops.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: "30"})
    assert entry.options == {CONF_SCAN_INTERVAL: 30}
    await hass.async_block_till_done()
    assert entry.runtime_data.irradiance is None


# --- the repair ------------------------------------------------------------------------------

async def test_the_repair_starts_a_new_id_and_reloads(hass, freezer, aioclient_mock):
    entry = await _options_entry(hass, freezer, aioclient_mock, MODULE)
    issue_id = irradiance.sensor_changed_issue_id(ENTRY_ID)
    flow = await repairs.async_create_fix_flow(hass, issue_id, {"entry_id": ENTRY_ID})
    flow.hass, flow.handler, flow.flow_id, flow.issue_id = hass, DOMAIN, "fix", issue_id
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"
    assert result["description_placeholders"] == {"title": "Roof",
                                                  "sensor_id": "ha-0123456789ab-123456"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_confirm({})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload.assert_called_once_with(ENTRY_ID)
    assert entry.options[CONF_IRRADIANCE_GENERATION] == 1
    assert entry.options[CONF_IRRADIANCE_SENSOR_ID] == "ha-0123456789ab-" + irradiance.geometry_part(
        "module", 30, 180, 1)
    gone = await repairs.async_create_fix_flow(hass, issue_id, {"entry_id": "nope"})
    gone.hass, gone.handler, gone.flow_id = hass, DOMAIN, "fix2"
    assert (await gone.async_step_init())["reason"] == "entry_not_found"


async def test_a_reload_closes_the_issue_and_tries_once_more(hass, freezer, aioclient_mock):
    ir.async_create_issue(hass, DOMAIN, irradiance.sensor_changed_issue_id(ENTRY_ID), is_fixable=True,
                          severity=ir.IssueSeverity.WARNING, translation_key="irradiance_sensor_changed")
    entry = await _options_entry(hass, freezer, aioclient_mock, MODULE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, irradiance.sensor_changed_issue_id(ENTRY_ID)) is None
    assert entry.runtime_data.irradiance.state.stopped is False
