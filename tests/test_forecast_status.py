"""The forecast status of the 2026-11 API (0.1.3): Retry-After on a 204, the
status attributes, the INFO line, and the paused repair notice. Every part is
optional: an API without the headers and fields behaves as 0.1.2 did."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.solariance.api import (
    ETA_HEADER,
    STATUS_HEADER,
    ForecastAnswer,
    SolarianceApiClient,
)
from custom_components.solariance.const import (
    API_BASE,
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SYSTEM_ID,
    DOMAIN,
)
from custom_components.solariance.coordinator import PAUSED_ISSUE_KEYS
from custom_components.solariance.diagnostics import async_get_config_entry_diagnostics

from .conftest import NOW, SYSTEM_ID, TOKEN, envelope, make_payload

FORECAST_URL = f"{API_BASE}/forecast/power"
POWER_NOW = "sensor.roof_estimated_power_production_now"
ENERGY_TODAY = "sensor.roof_estimated_energy_production_today"
ENERGY_TOMORROW = "sensor.roof_estimated_energy_production_tomorrow"
ETA = "2026-07-01T08:08:05Z"
GENERATED = "2026-07-01T07:59:03Z"
TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / DOMAIN / "translations"


def waiting(status: str | None = "queued", retry_after: str | None = "12",
            eta: str | None = ETA) -> dict:
    """A 204 as the 2026-11 API sends it."""
    headers = {}
    if status is not None:
        headers[STATUS_HEADER] = status
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    if eta is not None:
        headers[ETA_HEADER] = eta
    return {"status": 204, "headers": headers}


def paused(locked_reason: str | None = "system_limit") -> dict:
    data = {"reason": "system_locked", "system_id": SYSTEM_ID}
    if locked_reason is not None:
        data["locked_reason"] = locked_reason
    return {"status": 403, "json": {"code": 403, "message": "This system is paused.",
                                    "data": data}}


def forecast(**fields) -> dict:
    """A 200; `fields` go into data next to the forecast."""
    return {"json": envelope({**make_payload(), **fields})}


async def _fire(hass, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _answer(aioclient_mock, **answer) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, **answer)


async def _setup(hass, freezer, aioclient_mock, config_entry, **answer) -> None:
    freezer.move_to(NOW)
    aioclient_mock.get(FORECAST_URL, **answer)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED


def _paused_issue(hass, config_entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"system_paused_{config_entry.entry_id}")


def _waiting_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "checking again in" in r.getMessage()]


# --- Retry-After on a 204 -------------------------------------------------------


async def test_retry_after_is_honoured_instead_of_the_interval(
        hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry, **waiting(retry_after="300"))
    assert aioclient_mock.call_count == 1
    await _fire(hass, freezer, 290)
    assert aioclient_mock.call_count == 1          # not before the API asked
    await _fire(hass, freezer, 20)
    assert aioclient_mock.call_count == 2          # but not the 30-minute interval either


async def test_a_short_retry_after_waits_at_least_a_minute(
        hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry, **waiting(retry_after="5"))
    await _fire(hass, freezer, 30)
    assert aioclient_mock.call_count == 1
    await _fire(hass, freezer, 35)
    assert aioclient_mock.call_count == 2


async def test_a_long_retry_after_waits_at_most_half_an_hour(hass, freezer, aioclient_mock):
    # A 15-minute interval: the API's two hours are honoured up to 30 minutes.
    entry = MockConfigEntry(domain=DOMAIN, title="Roof", unique_id=SYSTEM_ID,
                            data={CONF_API_TOKEN: TOKEN, CONF_SYSTEM_ID: SYSTEM_ID},
                            options={CONF_SCAN_INTERVAL: 15})
    await _setup(hass, freezer, aioclient_mock, entry,
                 **waiting(status="failed", retry_after="7200"))
    await _fire(hass, freezer, 20 * 60)
    assert aioclient_mock.call_count == 1
    await _fire(hass, freezer, 9 * 60 + 40)
    assert aioclient_mock.call_count == 1
    await _fire(hass, freezer, 30)
    assert aioclient_mock.call_count == 2


async def test_repeated_204s_ask_again_less_and_less_often(
        hass, freezer, aioclient_mock, config_entry):
    """Retry-After 12 s on every 204 (a long "delayed"): 1, 2, 4 ... minutes,
    never a poll a minute on the budget the Solariance app shares."""
    await _setup(hass, freezer, aioclient_mock, config_entry,
                 **waiting(status="delayed", retry_after="12"))
    await _fire(hass, freezer, 65)
    assert aioclient_mock.call_count == 2
    await _fire(hass, freezer, 65)
    assert aioclient_mock.call_count == 2
    await _fire(hass, freezer, 60)                 # 125 s after the second 204
    assert aioclient_mock.call_count == 3
    await _fire(hass, freezer, 200)
    assert aioclient_mock.call_count == 3
    await _fire(hass, freezer, 45)                 # 245 s after the third
    assert aioclient_mock.call_count == 4


async def test_a_wait_of_weeks_stays_at_half_an_hour(hass, freezer, aioclient_mock,
                                                     config_entry):
    """Thousands of 204s in a row (a system that never gets a forecast) keep
    the wait at its cap instead of overflowing the doubling."""
    await _setup(hass, freezer, aioclient_mock, config_entry, **waiting(retry_after="12"))
    coordinator = config_entry.runtime_data
    coordinator._early_retries = 5000
    await _fire(hass, freezer, 65)
    assert aioclient_mock.call_count == 2
    assert coordinator.last_exception.__class__.__name__ == "NoForecastYet"
    assert coordinator.update_interval == timedelta(seconds=1800)


async def test_the_regular_interval_returns_with_the_forecast(
        hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry, **waiting(retry_after="60"))
    assert hass.states.get(ENERGY_TODAY).state == "unavailable"
    _answer(aioclient_mock, **forecast())
    await _fire(hass, freezer, 65)
    assert aioclient_mock.call_count == 1
    assert hass.states.get(ENERGY_TODAY).state != "unavailable"
    assert config_entry.runtime_data.update_interval == timedelta(minutes=30)

    await _fire(hass, freezer, 29 * 60)
    assert aioclient_mock.call_count == 1
    await _fire(hass, freezer, 2 * 60)
    assert aioclient_mock.call_count == 2


async def test_a_204_without_headers_waits_the_interval_as_before(
        hass, freezer, aioclient_mock, config_entry, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.solariance")
    await _setup(hass, freezer, aioclient_mock, config_entry, status=204)
    await _fire(hass, freezer, 29 * 60)
    assert aioclient_mock.call_count == 1
    await _fire(hass, freezer, 2 * 60)
    assert aioclient_mock.call_count == 2
    assert _waiting_lines(caplog) == []
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["forecast_state"] == {"status": None, "eta_utc": None,
                                             "retry_after_s": None, "locked_reason": None}


# --- The INFO line --------------------------------------------------------------


async def test_one_info_line_per_status(hass, freezer, aioclient_mock, config_entry, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.solariance")
    await _setup(hass, freezer, aioclient_mock, config_entry, **waiting(retry_after="30"))
    eta = dt_util.as_local(datetime(2026, 7, 1, 8, 8, 5, tzinfo=UTC)).isoformat(
        timespec="seconds")
    assert _waiting_lines(caplog) == [
        ("Solariance is calculating the first forecast for Roof (status queued, expected by "
         f"{eta}); checking again in 60 s.")]

    await _fire(hass, freezer, 65)                 # still queued: nothing new
    assert aioclient_mock.call_count == 2
    assert len(_waiting_lines(caplog)) == 1

    _answer(aioclient_mock, **waiting(status="delayed", retry_after="30", eta=None))
    await _fire(hass, freezer, 125)
    assert aioclient_mock.call_count == 1
    lines = _waiting_lines(caplog)
    assert len(lines) == 2
    assert lines[1] == ("Solariance is taking longer than usual to calculate the first "
                        "forecast for Roof (status delayed); checking again in 240 s.")

    # The forecast arrives; a later 204 (a pipeline outage) is a new state.
    _answer(aioclient_mock, **forecast(forecast_status="ready"))
    await _fire(hass, freezer, 245)
    assert aioclient_mock.call_count == 1
    _answer(aioclient_mock, **waiting(status="stale", retry_after="600"))
    await _fire(hass, freezer, 31 * 60)
    assert aioclient_mock.call_count == 1
    lines = _waiting_lines(caplog)
    assert len(lines) == 3
    assert lines[2].startswith("Solariance has no current forecast for Roof (status stale")
    assert lines[2].endswith("checking again in 600 s.")


async def test_a_429_in_between_is_no_new_state(hass, freezer, aioclient_mock, config_entry,
                                                caplog):
    caplog.set_level(logging.INFO, logger="custom_components.solariance")
    await _setup(hass, freezer, aioclient_mock, config_entry, **waiting(retry_after="60"))
    _answer(aioclient_mock, status=429, headers={"Retry-After": "120"})
    await _fire(hass, freezer, 65)
    assert aioclient_mock.call_count == 1
    _answer(aioclient_mock, **waiting(retry_after="60"))
    await _fire(hass, freezer, 125)
    assert aioclient_mock.call_count == 1
    assert len(_waiting_lines(caplog)) == 1


# --- forecast_status / generated_utc ----------------------------------------------


async def test_status_attributes_on_the_main_sensors(
        hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry,
                 **forecast(generated_utc=GENERATED, forecast_status="provisional"))
    generated = datetime(2026, 7, 1, 7, 59, 3, tzinfo=UTC)
    for entity in (POWER_NOW, ENERGY_TODAY):
        attributes = hass.states.get(entity).attributes
        assert attributes["forecast_status"] == "provisional"
        assert attributes["generated_utc"] == generated
    assert len(hass.states.get(ENERGY_TODAY).attributes["hourly"]) == 24
    assert "forecast_status" not in hass.states.get(ENERGY_TOMORROW).attributes

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["forecast"]["forecast_status"] == "provisional"
    assert diagnostics["forecast"]["generated_utc"] == "2026-07-01T07:59:03+00:00"
    assert diagnostics["forecast_state"]["status"] == "provisional"


async def test_no_status_attributes_without_the_fields(hass, setup_entry):
    assert "forecast_status" not in hass.states.get(POWER_NOW).attributes
    assert "generated_utc" not in hass.states.get(POWER_NOW).attributes
    attributes = hass.states.get(ENERGY_TODAY).attributes
    assert "forecast_status" not in attributes and "hourly" in attributes


async def test_the_status_header_stands_in_for_the_body(
        hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry,
                 **forecast(), headers={STATUS_HEADER: "Ready"})
    attributes = hass.states.get(POWER_NOW).attributes
    assert attributes["forecast_status"] == "ready"
    assert "generated_utc" not in attributes


async def test_unreadable_status_fields_are_left_out_not_fatal(
        hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry,
                 **forecast(generated_utc="yesterday", forecast_status="<b>ready</b>"))
    state = hass.states.get(POWER_NOW)
    assert state.state != "unavailable"
    assert "forecast_status" not in state.attributes
    assert "generated_utc" not in state.attributes


# --- The paused repair notice --------------------------------------------------------


@pytest.mark.parametrize(
    ("locked_reason", "translation_key"),
    [("system_limit", "system_paused_system_limit"),
     ("kwp_limit", "system_paused_kwp_limit"),
     (None, "system_paused"),
     ("something_new", "system_paused")],
)
async def test_a_paused_system_gets_an_informational_issue(
        hass, freezer, aioclient_mock, config_entry, locked_reason, translation_key):
    await _setup(hass, freezer, aioclient_mock, config_entry, **paused(locked_reason))
    issue = _paused_issue(hass, config_entry)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == translation_key
    assert issue.translation_placeholders == {"name": "Roof"}
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["forecast_state"]["status"] == "paused"
    assert diagnostics["forecast_state"]["locked_reason"] == locked_reason


@pytest.mark.parametrize("answer", [forecast(), waiting()], ids=["forecast", "no_forecast_yet"])
async def test_the_issue_closes_when_the_system_is_active_again(
        hass, freezer, aioclient_mock, config_entry, answer):
    await _setup(hass, freezer, aioclient_mock, config_entry, **paused())
    # A spent limit says nothing about the pause: the notice stays.
    _answer(aioclient_mock, status=429, headers={"Retry-After": "60"})
    await _fire(hass, freezer, 31 * 60)
    assert aioclient_mock.call_count == 1
    assert _paused_issue(hass, config_entry) is not None

    _answer(aioclient_mock, **answer)
    await _fire(hass, freezer, 65)
    assert aioclient_mock.call_count == 1
    assert _paused_issue(hass, config_entry) is None


async def test_unloading_the_entry_closes_the_issue(hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry, **paused())
    assert _paused_issue(hass, config_entry) is not None
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert _paused_issue(hass, config_entry) is None


async def test_removing_the_entry_closes_the_issue(hass, freezer, aioclient_mock, config_entry):
    await _setup(hass, freezer, aioclient_mock, config_entry, **paused())
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _paused_issue(hass, config_entry) is None


@pytest.mark.parametrize("language", ["en", "de"])
def test_every_paused_issue_text_exists(language):
    issues = json.loads((TRANSLATIONS / f"{language}.json").read_text("utf-8"))["issues"]
    for key in {"system_paused", *PAUSED_ISSUE_KEYS.values()}:
        assert "{name}" in issues[key]["description"]
        assert issues[key]["title"]
    if language == "de":   # the integration's register is "Sie", as everywhere else
        texts = " ".join(issues[k]["description"] for k in PAUSED_ISSUE_KEYS.values())
        assert "Ihrem" in texts and "dein" not in texts.lower()


# --- The client -------------------------------------------------------------------


async def test_the_client_reads_the_status_headers(hass, aioclient_mock):
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = SolarianceApiClient(async_get_clientsession(hass), "token")
    aioclient_mock.get(FORECAST_URL, **waiting())
    assert await client.async_get_power_forecast("PV1") == ForecastAnswer(
        data=None, status="queued", retry_after=12.0,
        eta=datetime(2026, 7, 1, 8, 8, 5, tzinfo=UTC))

    _answer(aioclient_mock, status=204)
    assert await client.async_get_power_forecast("PV1") == ForecastAnswer(data=None)

    _answer(aioclient_mock, status=204,
            headers={STATUS_HEADER: "queued; drop table", "Retry-After": "nan",
                     ETA_HEADER: "soon"})
    assert await client.async_get_power_forecast("PV1") == ForecastAnswer(data=None)

    _answer(aioclient_mock, **forecast(), headers={STATUS_HEADER: "provisional"})
    answer = await client.async_get_power_forecast("PV1")
    assert answer.status == "provisional" and answer.data is not None
