"""The standard forecast beside the AI forecast (plans with the AI forecast in
the API): parsed when the API sends it, three sensors off by default, the
`delivered` attributes, and nothing new at all when the API does not send it."""
from __future__ import annotations

import copy

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.solariance.const import API_BASE
from custom_components.solariance.diagnostics import async_get_config_entry_diagnostics
from custom_components.solariance.forecast import parse_power_forecast

from .conftest import NOW, envelope, make_payload

PREFIX = "sensor.roof_"
STANDARD = ("standard_forecast_energy_production_today",
            "standard_forecast_energy_production_tomorrow",
            "standard_forecast_power_production_now")
SHARE = 0.9


def with_standard(payload: dict, share: float = SHARE) -> dict:
    """The payload as a plan with the AI forecast in the API gets it once an AI
    layer is delivered: the standard forecast beside every row."""
    payload = copy.deepcopy(payload)
    payload["delivered"] = "ai"
    payload["delivered_layers"] = ["weather_model"]
    for day in payload["forecast"]:
        total = 0.0
        for row in day:
            row["standard_ac_output_of_system"] = round(row["ac_output_of_system"] * share, 2)
            row["standard_energy_in_time_interval"] = round(row["energy_in_time_interval"] * share, 4)
            total += row["standard_energy_in_time_interval"]
            row["standard_energy_aggregated_sum_intraday"] = round(total, 4)
    return payload


def test_an_answer_without_it_reads_as_before():
    forecast = parse_power_forecast(make_payload())
    assert not forecast.has_standard
    assert forecast.delivered is None and forecast.delivered_layers == ()
    assert forecast.day(0, NOW).standard_energy_kwh is None
    assert forecast.standard_power_at(NOW) is None


def test_an_answer_with_it():
    forecast = parse_power_forecast(with_standard(make_payload()))
    today = forecast.day(0, NOW)
    assert forecast.has_standard
    assert forecast.delivered == "ai" and forecast.delivered_layers == ("weather_model",)
    assert today.standard_energy_kwh == pytest.approx(today.energy_kwh * SHARE, abs=0.01)
    assert forecast.standard_power_at(NOW) == pytest.approx(forecast.power_at(NOW) * SHARE, abs=0.01)


def test_odd_values_never_break_the_forecast():
    payload = with_standard(make_payload())
    payload["delivered"] = {"no": "word"}
    payload["delivered_layers"] = "weather_model"
    payload["forecast"][0][40]["standard_ac_output_of_system"] = "n/a"
    payload["forecast"][0][41]["standard_energy_in_time_interval"] = float("nan")
    forecast = parse_power_forecast(payload)
    assert forecast.delivered is None and forecast.delivered_layers == ()
    assert forecast.day(0, NOW).standard_energy_kwh is None     # one interval without it
    assert forecast.day(1, NOW).standard_energy_kwh is not None


async def test_nothing_new_while_the_api_does_not_send_it(hass, setup_entry):
    registry = er.async_get(hass)
    for key in STANDARD:
        assert registry.async_get(PREFIX + key) is None
    today = hass.states.get(PREFIX + "estimated_energy_production_today")
    assert "delivered" not in today.attributes
    assert "standard_energy_kwh" not in today.attributes["hourly"][12]
    assert "delivered" not in hass.states.get(PREFIX + "estimated_power_production_now").attributes
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diagnostics["forecast"]["has_standard"] is False
    assert diagnostics["forecast"]["delivered"] is None


@pytest.fixture
def mock_standard_forecast(aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(with_standard(make_payload())))
    return aioclient_mock


async def _setup(hass, freezer, config_entry):
    freezer.move_to(NOW)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_three_sensors_off_by_default_and_the_attributes(
        hass, freezer, config_entry, mock_standard_forecast):
    await _setup(hass, freezer, config_entry)
    registry = er.async_get(hass)
    for key in STANDARD:
        entry = registry.async_get(PREFIX + key)
        assert entry is not None, key
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(PREFIX + key) is None
    today = hass.states.get(PREFIX + "estimated_energy_production_today")
    assert today.attributes["delivered"] == "ai"
    assert today.attributes["delivered_layers"] == ["weather_model"]
    noon = today.attributes["hourly"][12]
    assert noon["standard_energy_kwh"] == pytest.approx(noon["energy_kwh"] * SHARE, abs=0.01)
    assert noon["standard_power_w"] == pytest.approx(noon["power_w"] * SHARE, abs=2)
    assert hass.states.get(PREFIX + "estimated_power_production_now").attributes["delivered"] == "ai"
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["forecast"]["has_standard"] is True
    assert diagnostics["forecast"]["days"][0]["standard_energy_kwh"] is not None


async def test_an_enabled_sensor_shows_the_standard_forecast(
        hass, freezer, config_entry, mock_standard_forecast):
    await _setup(hass, freezer, config_entry)
    registry = er.async_get(hass)
    for key in STANDARD:
        registry.async_update_entity(PREFIX + key, disabled_by=None)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    forecast = parse_power_forecast(with_standard(make_payload()))
    today = float(hass.states.get(PREFIX + STANDARD[0]).state)
    assert today == pytest.approx(round(forecast.day(0, NOW).standard_energy_kwh, 3))
    assert today < float(hass.states.get(PREFIX + "estimated_energy_production_today").state)
    tomorrow = float(hass.states.get(PREFIX + STANDARD[1]).state)
    assert tomorrow == pytest.approx(round(forecast.day(1, NOW).standard_energy_kwh, 3))
    now = float(hass.states.get(PREFIX + STANDARD[2]).state)
    assert now == round(forecast.standard_power_at(NOW))
    assert hass.states.get(PREFIX + STANDARD[2]).attributes["unit_of_measurement"] == "W"


async def test_the_sensors_appear_with_the_first_answer_that_sends_it(
        hass, setup_entry, aioclient_mock):
    registry = er.async_get(hass)
    assert registry.async_get(PREFIX + STANDARD[0]) is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(with_standard(make_payload())))
    await setup_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    for key in STANDARD:
        assert registry.async_get(PREFIX + key) is not None, key
    # A later answer adds nothing twice.
    await setup_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len([e for e in er.async_entries_for_config_entry(registry, setup_entry.entry_id)
                if e.unique_id.endswith("standard_energy_production_today")]) == 1


async def test_once_known_they_stay_and_read_unavailable_without_it(
        hass, freezer, config_entry, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(with_standard(make_payload())))
    await _setup(hass, freezer, config_entry)
    registry = er.async_get(hass)
    registry.async_update_entity(PREFIX + STANDARD[0], disabled_by=None)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(make_payload()))
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(PREFIX + STANDARD[0]).state == "unavailable"
    assert hass.states.get(PREFIX + "estimated_energy_production_today").state != "unavailable"
