"""The production upload (0.2): the energy sensor's hourly statistics, once an
hour, fill-only (source ha_sensor), and what a refusal leaves behind."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.solariance import upload
from custom_components.solariance.const import (
    API_BASE,
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    CONF_UPLOAD_SENSOR,
    DOMAIN,
)
from custom_components.solariance.diagnostics import async_get_config_entry_diagnostics

from .conftest import NOW, SYSTEM_ID, TOKEN, envelope, make_payload

UPLOAD_URL = f"{API_BASE}/measured/production/upload"
SENSOR = "sensor.pv_energy"


def rows(*changes, start=datetime(2026, 7, 1, 4, 0, tzinfo=UTC)):
    """Recorder statistics rows: `start` as a timestamp, `change` in kWh."""
    return [{"start": (start + timedelta(hours=i)).timestamp(), "change": c} for i, c in enumerate(changes)]


def test_hourly_values_skip_resets_and_gaps():
    got = upload.hourly_values(rows(0.0, 1.25, None, -980.0, 2.5))
    assert got == [["2026-07-01T04:00:00Z", 0.0], ["2026-07-01T05:00:00Z", 1.25],
                   ["2026-07-01T08:00:00Z", 2.5]]


def test_the_payload_is_the_contracts_json_with_the_automatic_source():
    assert upload.payload(SYSTEM_ID, [["2026-07-01T05:00:00Z", 1.25]]) == {
        "quantity": "generation", "unit": "kWh", "interval_minutes": 60, "interval_label": "start",
        "source": "ha_sensor", "series": [{"system_id": SYSTEM_ID, "values": [["2026-07-01T05:00:00Z", 1.25]]}],
    }


async def test_the_statistics_read_is_the_last_48_full_hours(hass):
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={SENSOR: rows(1.0)})
    with patch.object(upload, "get_instance", return_value=instance):
        got = await upload.async_hourly_rows(hass, SENSOR, datetime(2026, 7, 1, 10, 7, 30, tzinfo=UTC))
    assert len(got) == 1
    _fn, _hass, start, end, ids, period, units, types = instance.async_add_executor_job.call_args.args
    assert (start, end) == (datetime(2026, 6, 29, 10, 0, tzinfo=UTC), datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
    assert (ids, period, units, types) == ({SENSOR}, "hour", {"energy": "kWh"}, {"change"})


async def _run(hass, aioclient_mock, state, changes=(1.0, 2.0), **answer):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from custom_components.solariance.api import SolarianceApiClient
    aioclient_mock.clear_requests()
    aioclient_mock.post(UPLOAD_URL, **answer)
    client = SolarianceApiClient(async_get_clientsession(hass), TOKEN)
    with patch.object(upload, "async_hourly_rows", AsyncMock(return_value=rows(*changes))):
        return await upload.async_upload(hass, client, SYSTEM_ID, SENSOR, state, NOW)


async def test_an_upload_sends_the_hours_with_the_token_and_counts_what_was_new(hass, aioclient_mock, caplog):
    caplog.set_level(logging.INFO)
    state = await _run(hass, aioclient_mock, upload.UploadState(),
                       json=envelope({"totals": {"stored": 1, "rejected": 0}}))
    assert (state.outcome, state.hours_sent, state.stored) == ("ok", 2, 1)
    _method, _url, sent, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent["source"] == "ha_sensor" and len(sent["series"][0]["values"]) == 2
    assert "Solariance upload for PVTEST000001 from sensor.pv_energy: ok" in caplog.text


async def test_no_hours_sends_nothing(hass, aioclient_mock):
    state = await _run(hass, aioclient_mock, upload.UploadState(), changes=(None,), json=envelope({}))
    assert state.outcome == "nothing"
    assert aioclient_mock.call_count == 0


async def test_refusals_say_why_and_log_only_when_it_changes(hass, aioclient_mock, caplog):
    state = upload.UploadState()
    readonly = {"code": 403, "message": "This token is restricted to ReadOnly accesss", "data": None}
    state = await _run(hass, aioclient_mock, state, status=403, json=readonly)
    assert (state.outcome, state.reason) == ("refused", "token_scope")
    assert "needs the Upload (or ReadWrite) scope" in caplog.text
    caplog.clear()
    state = await _run(hass, aioclient_mock, state, status=403, json=readonly)
    assert caplog.text == "", "the same refusal is not logged again every hour"
    state = await _run(hass, aioclient_mock, state, status=403,
                       json={"code": 403, "data": {"reason": "measured_opt_in_required"}})
    assert (state.outcome, state.reason) == ("refused", "measured_opt_in_required")
    assert "has not opted in to measured data" in caplog.text
    for answer, outcome in (({"status": 429, "headers": {"Retry-After": "120"}}, "rate_limited"),
                            ({"status": 401}, "auth"),
                            ({"status": 400, "json": {"code": 400, "data": {"reason": "invalid_source"}}}, "failed"),
                            ({"status": 502}, "failed")):
        state = await _run(hass, aioclient_mock, state, **answer)
        assert state.outcome == outcome, answer


def _entry(options=None):
    return MockConfigEntry(domain=DOMAIN, title="Roof", unique_id=SYSTEM_ID,
                           data={CONF_API_TOKEN: TOKEN, CONF_SYSTEM_ID: SYSTEM_ID}, options=options or {})


async def _setup(hass, freezer, aioclient_mock, entry):
    freezer.move_to(NOW)
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(make_payload()))
    aioclient_mock.post(UPLOAD_URL, json=envelope({"totals": {"stored": 2, "rejected": 0}}))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_with_a_sensor_it_uploads_at_seven_past_the_hour(hass, freezer, aioclient_mock):
    entry = _entry({CONF_SCAN_INTERVAL: 60, CONF_UPLOAD_SENSOR: SENSOR})
    with patch.object(upload, "async_hourly_rows", AsyncMock(return_value=rows(1.0, 2.0))):
        await _setup(hass, freezer, aioclient_mock, entry)
        posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
        assert posts == [], "nothing at setup"
        freezer.move_to(NOW.replace(minute=0) + timedelta(hours=1, minutes=7, seconds=59))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["source"] == "ha_sensor"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["upload"]["outcome"] == "ok" and diagnostics["upload"]["sensor"] == SENSOR


async def test_without_a_sensor_nothing_is_uploaded(hass, freezer, aioclient_mock):
    entry = _entry()
    await _setup(hass, freezer, aioclient_mock, entry)
    freezer.move_to(NOW.replace(minute=0) + timedelta(hours=1, minutes=7, seconds=59))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [c for c in aioclient_mock.mock_calls if c[0] == "POST"] == []
    assert (await async_get_config_entry_diagnostics(hass, entry))["upload"] is None


async def test_the_options_choose_the_sensor_or_none(hass, freezer, aioclient_mock):
    entry = _entry()
    await _setup(hass, freezer, aioclient_mock, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_UPLOAD_SENSOR in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: "30", CONF_UPLOAD_SENSOR: SENSOR})
    assert entry.options == {CONF_SCAN_INTERVAL: 30, CONF_UPLOAD_SENSOR: SENSOR}
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: "60"})
    assert entry.options == {CONF_SCAN_INTERVAL: 60}
