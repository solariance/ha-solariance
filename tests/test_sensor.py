"""Sensor values at a frozen time, and the quarter-hour recompute."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.solariance.forecast import parse_power_forecast

from .conftest import BERLIN, NOW, make_payload

PREFIX = "sensor.roof_"


def state(hass, key):
    return hass.states.get(PREFIX + key)


async def test_energy_sensors(hass, setup_entry):
    forecast = parse_power_forecast(make_payload())
    today, tomorrow = forecast.day(0, NOW), forecast.day(1, NOW)
    assert float(state(hass, "estimated_energy_production_today").state) == pytest.approx(
        round(today.energy_kwh, 3))
    assert float(state(hass, "estimated_energy_production_tomorrow").state) == pytest.approx(
        round(tomorrow.energy_kwh, 3))
    remaining = float(state(hass, "estimated_energy_production_remaining_today").state)
    assert remaining == pytest.approx(round(forecast.energy_remaining_today(NOW), 3))
    assert 0 < remaining < today.energy_kwh
    sensor = state(hass, "estimated_energy_production_today")
    assert sensor.attributes["unit_of_measurement"] == "kWh"
    assert sensor.attributes["device_class"] == "energy"
    assert "state_class" not in sensor.attributes
    hourly = sensor.attributes["hourly"]
    assert len(hourly) == 24
    assert hourly[10]["period_start"] == "2026-07-01T10:00:00+02:00"


async def test_power_and_time_sensors(hass, setup_entry):
    forecast = parse_power_forecast(make_payload())
    assert float(state(hass, "estimated_power_production_now").state) == round(
        forecast.power_at(NOW))
    assert float(state(hass, "estimated_power_production_next_hour").state) == round(
        forecast.power_at(NOW + timedelta(hours=1)))
    assert state(hass, "production_start_today").state == "2026-07-01T03:00:00+00:00"
    peak = datetime.fromisoformat(state(hass, "highest_power_peak_time_today").state)
    assert peak.astimezone(BERLIN).hour in (12, 13)
    assert float(state(hass, "highest_power_peak_today").state) > 7000
    this_hour = float(state(hass, "estimated_energy_production_this_hour").state)
    start = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)   # 10:00 Berlin
    assert this_hour == pytest.approx(
        round(forecast.energy_between(start, start + timedelta(hours=1)), 3))


async def test_later_days_are_off_by_default_and_unavailable_on_a_two_day_plan(
        hass, setup_entry):
    registry = er.async_get(hass)
    entry = registry.async_get(PREFIX + "estimated_energy_production_day_3")
    assert entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert state(hass, "estimated_power_production_next_24hours") is None   # disabled too


async def test_power_now_moves_with_the_clock_without_a_new_request(
        hass, freezer, setup_entry, mock_forecast):
    before = state(hass, "estimated_power_production_now").state
    calls = mock_forecast.call_count
    freezer.move_to(datetime(2026, 7, 1, 10, 15, 6, tzinfo=BERLIN))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    after = state(hass, "estimated_power_production_now").state
    assert after != before
    assert mock_forecast.call_count == calls
