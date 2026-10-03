"""Shared fixtures: a synthetic Solariance account and its forecast."""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solariance.const import (
    API_BASE,
    CONF_API_TOKEN,
    CONF_SYSTEM_ID,
    DOMAIN,
)

BERLIN = ZoneInfo("Europe/Berlin")
TOKEN = "sol_test_token_1234567890"
SYSTEM_ID = "PVTEST000001"
SYSTEMS = [
    {"system_id": SYSTEM_ID, "name": "Roof", "city": "Berlin", "country": "Germany",
     "kWp": 9.8, "locked_reason": None},
    {"system_id": "PVTEST000002", "name": "Garage", "city": "Potsdam", "country": "Germany",
     "kWp": 4.2, "locked_reason": "system_limit"},
]
# Frozen "now" for every test: 1 July 2026, 10:07 in Berlin (CEST, UTC+2).
NOW = datetime(2026, 7, 1, 10, 7, tzinfo=BERLIN)
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/solariance."""
    yield


def power_w(local: datetime, peak_w: float) -> float:
    """A clear-sky bell from 05:00 to 21:00, peaking at 13:00 local."""
    hour = local.hour + local.minute / 60
    if not 5 <= hour <= 21:
        return 0.0
    return round(peak_w * math.sin(math.pi * (hour - 5) / 16) ** 2, 1)


def make_day(day: date, step: int = 15, peak_w: float = 8000.0) -> list[dict]:
    """One local day of START-labelled rows, as GET forecast/power sends them."""
    rows, total = [], 0.0
    local = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    while local.date() == day:
        # Mean power over the interval, approximated at its midpoint.
        watts = power_w(local + timedelta(minutes=step / 2), peak_w)
        energy = watts * step / 60 / 1000
        total += energy
        rows.append({
            "local_time": local.strftime("%Y-%m-%dT%H:%M:%S"),
            "utc_time": local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "step": step,
            "ac_output_of_system": watts,
            "energy_in_time_interval": round(energy, 4),
            "energy_aggregated_sum_intraday": round(total, 4),
        })
        local = (local.astimezone(UTC) + timedelta(minutes=step)).astimezone(BERLIN)
    return rows


def make_payload(first: date = TODAY, days: int = 2, peak_w: float = 8000.0) -> dict:
    """The `data` of forecast/power: days 0-1 quarter-hourly, later hourly."""
    return {
        "system_id": SYSTEM_ID,
        "time_zone": "Europe/Berlin",
        "now_local": NOW.strftime("%Y-%m-%dT%H:%M:%S"),
        "utc_offset_minutes": 120,
        "forecast": [make_day(first + timedelta(days=i), 15 if i < 2 else 60, peak_w)
                     for i in range(days)],
    }


def envelope(data) -> dict:
    return {"code": 200, "message": "OK", "data": data}


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="Roof", unique_id=SYSTEM_ID,
                           data={CONF_API_TOKEN: TOKEN, CONF_SYSTEM_ID: SYSTEM_ID})


@pytest.fixture
def mock_forecast(aioclient_mock):
    """GET forecast/power answers with two days; returns the mocker."""
    aioclient_mock.get(f"{API_BASE}/forecast/power", json=envelope(make_payload()))
    return aioclient_mock


@pytest.fixture
async def setup_entry(hass, freezer, config_entry, mock_forecast):
    """The entry set up at NOW with the two-day forecast."""
    freezer.move_to(NOW)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
