"""Entry setup, the API's error answers, and unloading."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.solariance.const import API_BASE

from .conftest import NOW, SYSTEM_ID, envelope, make_payload

FORECAST_URL = f"{API_BASE}/forecast/power"


async def test_setup_and_unload(hass, setup_entry, mock_forecast):
    assert setup_entry.state is ConfigEntryState.LOADED
    _method, url, _data, headers = mock_forecast.mock_calls[0]
    # No `day` parameter: asking for days by number turns a missing day into a 204.
    assert dict(url.query) == {"system_id": SYSTEM_ID}
    assert headers["Authorization"].startswith("Bearer ")
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED


async def test_no_forecast_yet_retries_setup(hass, freezer, aioclient_mock, config_entry):
    freezer.move_to(NOW)
    aioclient_mock.get(FORECAST_URL, status=204)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_refused_token_starts_reauth(hass, freezer, aioclient_mock, config_entry):
    freezer.move_to(NOW)
    aioclient_mock.get(FORECAST_URL, status=401)
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_rate_limit_marks_entities_unavailable_until_it_recovers(
        hass, freezer, setup_entry, aioclient_mock):
    entity = "sensor.roof_estimated_energy_production_today"
    assert hass.states.get(entity).state != "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, status=429, headers={"Retry-After": "120"})
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity).state == "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FORECAST_URL, json=envelope(make_payload()))
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity).state != "unavailable"
