"""The irradiance upload's statistics read against Home Assistant's real
recorder: the 5-minute means it compiles, in the unit it keeps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.solariance import irradiance

ENTITY = "sensor.station_irradiance"
START = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)


@pytest.fixture
async def mock_recorder_before_hass(async_test_recorder) -> None:
    """The recorder's database comes before Home Assistant (phcc's rule)."""


async def _record(hass, freezer, entity, attributes, readings):
    """`readings`: (minutes after START, state). Compiles START's 5 minutes."""
    for minutes, value in readings:
        freezer.move_to(START + timedelta(minutes=minutes))
        hass.states.async_set(entity, value, attributes)
    await async_wait_recording_done(hass)
    freezer.move_to(START + timedelta(minutes=5, seconds=10))
    do_adhoc_statistics(hass, start=START)
    await async_wait_recording_done(hass)


async def test_the_5_minute_means_in_w_per_m2(recorder_mock, hass, freezer):
    assert await async_setup_component(hass, "sensor", {})
    attributes = {"device_class": "irradiance", "state_class": "measurement",
                  "unit_of_measurement": "W/m²"}
    await _record(hass, freezer, ENTITY, attributes, [(0, "400"), (2.5, "600")])
    read = await irradiance.async_five_minute_rows(hass, ENTITY, START + timedelta(minutes=12), 2)
    assert read is not None
    unit, rows = read
    assert unit == "W/m²"
    assert irradiance.five_minute_values(rows, irradiance.UNIT_FACTORS[unit]) == [
        ["2026-07-01T06:00:00Z", 500.0]], "time-weighted: 400 for 2.5 min, 600 for 2.5 min"


async def test_a_sensor_in_btu_is_read_in_its_unit(recorder_mock, hass, freezer):
    assert await async_setup_component(hass, "sensor", {})
    attributes = {"device_class": "irradiance", "state_class": "measurement",
                  "unit_of_measurement": "BTU/(h⋅ft²)"}
    await _record(hass, freezer, ENTITY, attributes, [(0, "100")])
    unit, rows = await irradiance.async_five_minute_rows(hass, ENTITY, START + timedelta(minutes=12), 2)
    assert unit == "BTU/(h⋅ft²)"
    assert irradiance.five_minute_values(rows, irradiance.UNIT_FACTORS[unit]) == [
        ["2026-07-01T06:00:00Z", 315.5]]


async def test_a_sensor_without_statistics_reads_as_none(recorder_mock, hass, freezer):
    assert await async_setup_component(hass, "sensor", {})
    await _record(hass, freezer, ENTITY, {"device_class": "irradiance",
                                          "unit_of_measurement": "W/m²"}, [(0, "400")])
    assert await irradiance.async_five_minute_rows(hass, ENTITY, START + timedelta(minutes=12), 2) is None
