"""Forecast sensors, named like Forecast.Solar's so dashboards port over."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import APP_URL, DOMAIN, EXTRA_DAYS
from .coordinator import SolarianceConfigEntry, SolarianceCoordinator
from .forecast import SolarForecast

# Coordinator-driven: the entities never poll on their own.
PARALLEL_UPDATES = 0

type ValueFn = Callable[[SolarForecast, datetime], StateType | datetime]


@dataclass(frozen=True, kw_only=True)
class SolarianceSensorEntityDescription(SensorEntityDescription):
    """A forecast figure, computed from the stored forecast at `now` (UTC)."""

    value_fn: ValueFn
    # Whether the forecast covers what this sensor reports (e.g. day 3 on the
    # free plan does not); otherwise the entity is unavailable, not 0.
    available_fn: Callable[[SolarForecast, datetime], bool] = lambda forecast, now: True
    attributes_fn: Callable[[SolarForecast, datetime], dict[str, Any]] | None = None


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _has_day(offset: int) -> Callable[[SolarForecast, datetime], bool]:
    return lambda forecast, now: forecast.day(offset, now) is not None


def _day_energy(offset: int) -> ValueFn:
    def value(forecast: SolarForecast, now: datetime) -> float | None:
        day = forecast.day(offset, now)
        return None if day is None else round(day.energy_kwh, 3)
    return value


def _hourly(offset: int) -> Callable[[SolarForecast, datetime], dict[str, Any]]:
    """The day's forecast per hour, for chart cards. Keyed by UTC hour so the
    repeated autumn hour stays two entries; shown in the system's time."""
    def attributes(forecast: SolarForecast, now: datetime) -> dict[str, Any]:
        day = forecast.day(offset, now)
        if day is None:
            return {}
        hours: dict[datetime, list[float]] = {}
        for interval in day.intervals:
            key = interval.start.replace(minute=0, second=0, microsecond=0)
            energy, weighted, seconds = hours.get(key, [0.0, 0.0, 0.0])
            length = (interval.end - interval.start).total_seconds()
            hours[key] = [energy + interval.energy_kwh,
                          weighted + interval.power_w * length, seconds + length]
        return {"hourly": [
            {"period_start": key.astimezone(forecast.time_zone).isoformat(),
             "energy_kwh": round(energy, 3),
             "power_w": round(weighted / seconds) if seconds else 0}
            for key, (energy, weighted, seconds) in sorted(hours.items())
        ]}
    return attributes


def _power_in(delta: timedelta) -> ValueFn:
    def value(forecast: SolarForecast, now: datetime) -> float | None:
        power = forecast.power_at(now + delta)
        return None if power is None else round(power)
    return value


def _clock_hour_energy(offset_hours: int) -> ValueFn:
    """kWh in the current (0) or next (1) clock hour of the system's zone."""
    def value(forecast: SolarForecast, now: datetime) -> float | None:
        local = now.astimezone(forecast.time_zone)
        start = local.replace(minute=0, second=0, microsecond=0).astimezone(UTC)
        start += timedelta(hours=offset_hours)
        if not forecast.covers(start):
            return None
        return round(forecast.energy_between(start, start + timedelta(hours=1)), 3)
    return value


def _peak_time(offset: int) -> ValueFn:
    def value(forecast: SolarForecast, now: datetime) -> datetime | None:
        day = forecast.day(offset, now)
        peak = day.peak if day else None
        return peak.start if peak else None
    return value


def _peak_power(offset: int) -> ValueFn:
    def value(forecast: SolarForecast, now: datetime) -> float | None:
        day = forecast.day(offset, now)
        peak = day.peak if day else None
        return round(peak.power_w) if peak else 0
    return value


def _production(edge: str) -> ValueFn:
    def value(forecast: SolarForecast, now: datetime) -> datetime | None:
        day = forecast.day(0, now)
        if day is None:
            return None
        return day.production_start if edge == "start" else day.production_end
    return value


_ENERGY = {"device_class": SensorDeviceClass.ENERGY,
           "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
           "suggested_display_precision": 2}
_POWER = {"device_class": SensorDeviceClass.POWER,
          "native_unit_of_measurement": UnitOfPower.WATT,
          "suggested_display_precision": 0}
_TIMESTAMP = {"device_class": SensorDeviceClass.TIMESTAMP}

# No state_class on purpose: these are forecasts, not meter readings, and an
# energy sensor may only carry total/total_increasing -- neither fits.
SENSORS: tuple[SolarianceSensorEntityDescription, ...] = (
    SolarianceSensorEntityDescription(
        key="energy_production_today", translation_key="energy_production_today",
        value_fn=_day_energy(0), available_fn=_has_day(0), attributes_fn=_hourly(0),
        **_ENERGY),
    SolarianceSensorEntityDescription(
        key="energy_production_today_remaining",
        translation_key="energy_production_today_remaining",
        value_fn=lambda f, now: _round(f.energy_remaining_today(now), 3),
        available_fn=_has_day(0), **_ENERGY),
    SolarianceSensorEntityDescription(
        key="energy_production_tomorrow", translation_key="energy_production_tomorrow",
        value_fn=_day_energy(1), available_fn=_has_day(1), attributes_fn=_hourly(1),
        **_ENERGY),
    SolarianceSensorEntityDescription(
        key="power_production_now", translation_key="power_production_now",
        value_fn=_power_in(timedelta(0)), **_POWER),
    SolarianceSensorEntityDescription(
        key="power_production_next_hour", translation_key="power_production_next_hour",
        value_fn=_power_in(timedelta(hours=1)), **_POWER),
    SolarianceSensorEntityDescription(
        key="power_production_next_12hours", translation_key="power_production_next_12hours",
        value_fn=_power_in(timedelta(hours=12)), entity_registry_enabled_default=False,
        **_POWER),
    SolarianceSensorEntityDescription(
        key="power_production_next_24hours", translation_key="power_production_next_24hours",
        value_fn=_power_in(timedelta(hours=24)), entity_registry_enabled_default=False,
        **_POWER),
    SolarianceSensorEntityDescription(
        key="energy_current_hour", translation_key="energy_current_hour",
        value_fn=_clock_hour_energy(0), **_ENERGY),
    SolarianceSensorEntityDescription(
        key="energy_next_hour", translation_key="energy_next_hour",
        value_fn=_clock_hour_energy(1), **_ENERGY),
    SolarianceSensorEntityDescription(
        key="power_highest_peak_time_today", translation_key="power_highest_peak_time_today",
        value_fn=_peak_time(0), available_fn=_has_day(0), **_TIMESTAMP),
    SolarianceSensorEntityDescription(
        key="power_highest_peak_time_tomorrow",
        translation_key="power_highest_peak_time_tomorrow",
        value_fn=_peak_time(1), available_fn=_has_day(1), **_TIMESTAMP),
    SolarianceSensorEntityDescription(
        key="power_peak_today", translation_key="power_peak_today",
        value_fn=_peak_power(0), available_fn=_has_day(0), **_POWER),
    SolarianceSensorEntityDescription(
        key="power_peak_tomorrow", translation_key="power_peak_tomorrow",
        value_fn=_peak_power(1), available_fn=_has_day(1), **_POWER),
    SolarianceSensorEntityDescription(
        key="production_start_today", translation_key="production_start_today",
        value_fn=_production("start"), available_fn=_has_day(0), **_TIMESTAMP),
    SolarianceSensorEntityDescription(
        key="production_end_today", translation_key="production_end_today",
        value_fn=_production("end"), available_fn=_has_day(0), **_TIMESTAMP),
    *(
        SolarianceSensorEntityDescription(
            key=f"energy_production_day_{offset + 1}",
            translation_key=f"energy_production_day_{offset + 1}",
            value_fn=_day_energy(offset), available_fn=_has_day(offset),
            entity_registry_enabled_default=False, **_ENERGY)
        for offset in EXTRA_DAYS
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: SolarianceConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(SolarianceSensor(coordinator, description) for description in SENSORS)


class SolarianceSensor(CoordinatorEntity[SolarianceCoordinator], SensorEntity):
    """One forecast figure of one system."""

    _attr_has_entity_name = True
    # The hourly series is for cards, not for the history database.
    _unrecorded_attributes = frozenset({"hourly"})
    entity_description: SolarianceSensorEntityDescription

    def __init__(self, coordinator: SolarianceCoordinator,
                 description: SolarianceSensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.system_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.system_id)},
            name=coordinator.config_entry.title,
            manufacturer="Solariance",
            model="PV forecast",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=APP_URL,
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (super().available and data is not None
                and self.entity_description.available_fn(data, dt_util.utcnow()))

    @property
    def native_value(self) -> StateType | datetime:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.value_fn(data, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attributes_fn
        data = self.coordinator.data
        if fn is None or data is None:
            return None
        return fn(data, dt_util.utcnow())
