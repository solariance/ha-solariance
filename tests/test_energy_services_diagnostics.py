"""The Energy dashboard hook, the two actions, and diagnostics."""
from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.solariance.const import CONF_API_TOKEN, DOMAIN
from custom_components.solariance.diagnostics import async_get_config_entry_diagnostics
from custom_components.solariance.energy import async_get_solar_forecast
from custom_components.solariance.forecast import parse_power_forecast

from .conftest import TOKEN, make_payload


async def test_energy_dashboard_forecast(hass, setup_entry):
    result = await async_get_solar_forecast(hass, setup_entry.entry_id)
    wh_hours = result["wh_hours"]
    assert all(key.endswith("+00:00") and key[14:19] == "00:00" for key in wh_hours)
    total_kwh = sum(d.energy_kwh for d in parse_power_forecast(make_payload()).days)
    assert sum(wh_hours.values()) / 1000 == pytest.approx(total_kwh, rel=1e-3)
    assert await async_get_solar_forecast(hass, "not-an-entry") is None


async def test_get_forecast_action(hass, setup_entry):
    response = await hass.services.async_call(
        DOMAIN, "get_forecast", {"config_entry_id": setup_entry.entry_id},
        blocking=True, return_response=True)
    assert response["time_zone"] == "Europe/Berlin"
    assert [d["date"] for d in response["days"]] == ["2026-07-01", "2026-07-02"]
    assert len(response["days"][0]["series"]) == 24
    assert response["days"][0]["series"][10]["start"] == "2026-07-01T10:00:00+02:00"

    quarter = await hass.services.async_call(
        DOMAIN, "get_forecast",
        {"config_entry_id": setup_entry.entry_id, "day": 1, "resolution": "quarter_hour"},
        blocking=True, return_response=True)
    assert [d["date"] for d in quarter["days"]] == ["2026-07-02"]
    assert len(quarter["days"][0]["series"]) == 96


async def test_find_surplus_window_today_starts_after_now(hass, setup_entry):
    response = await hass.services.async_call(
        DOMAIN, "find_surplus_window",
        {"config_entry_id": setup_entry.entry_id, "load_kw": 2, "duration_hours": 2,
         "electricity_price_eur_per_kwh": 0.32, "feed_in_tariff_eur_per_kwh": 0.08},
        blocking=True, return_response=True)
    assert response["found"] is True
    # It is 10:07: the first interval that has not started is 10:15.
    assert response["window_start"] == "2026-07-01T10:15:00+02:00"
    assert response["window_end"] == "2026-07-01T12:15:00+02:00"
    assert response["pv_coverage_percent"] == 100
    assert response["estimated_saving_eur"] == pytest.approx(
        round(response["pv_covered_kwh"] * 0.24, 2), abs=0.011)


async def test_find_surplus_window_without_a_fitting_window(hass, setup_entry):
    response = await hass.services.async_call(
        DOMAIN, "find_surplus_window",
        {"config_entry_id": setup_entry.entry_id, "load_kw": 2, "duration_hours": 16},
        blocking=True, return_response=True)
    assert response == {"found": False, "reason": "no_window_left"}
    response = await hass.services.async_call(
        DOMAIN, "find_surplus_window",
        {"config_entry_id": setup_entry.entry_id, "load_kw": 2, "day": 3},
        blocking=True, return_response=True)
    assert response == {"found": False, "reason": "no_forecast_for_day"}


async def test_actions_refuse_an_unknown_entry(hass, setup_entry):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "get_forecast", {"config_entry_id": "nope"},
            blocking=True, return_response=True)


async def test_diagnostics_redact_the_token(hass, setup_entry):
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diagnostics["entry"]["data"][CONF_API_TOKEN] == "**REDACTED**"
    assert TOKEN not in repr(diagnostics)
    assert [d["date"] for d in diagnostics["forecast"]["days"]] == ["2026-07-01", "2026-07-02"]
    assert diagnostics["forecast"]["days"][0]["steps_min"] == [15]
