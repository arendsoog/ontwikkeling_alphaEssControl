"""Sensor platform for AlphaESSControl.

Three families of sensors, backed by this integration's three coordinators:
- `MODBUS_SENSOR_DESCRIPTIONS`: keys match the dict returned by
  `api.AlphaEssLocalApiClient.async_get_data`.
- `PRICE_SENSOR_DESCRIPTIONS` / `SOLAR_SENSOR_DESCRIPTIONS`: derived from the
  `data.Day`/`data.Hour` objects the prices/solar coordinators build (see
  `prices.py`/`solar.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import AlphaEssLocalConfigEntry
from .const import (
    CONF_PROVIDER_USE_FEE,
    CONF_VAT_PERCENTAGE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_VAT_PERCENTAGE,
)
from .data import MAX_HOURS, Day
from .dispatch import set_dispatch_msg
from .entity import AlphaEssLocalEntity
from .prices import mk_use_price
from .schedule import set_charging_msg

PRICE_UNIT = "EUR/kWh"


@dataclass(frozen=True, kw_only=True)
class AlphaEssLocalSensorDescription(SensorEntityDescription):
    """Describes an AlphaESS sensor backed by a coordinator data key."""


MODBUS_SENSOR_DESCRIPTIONS: tuple[AlphaEssLocalSensorDescription, ...] = (
    AlphaEssLocalSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AlphaEssLocalSensorDescription(
        key="pv_power",
        translation_key="pv_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AlphaEssLocalSensorDescription(
        key="grid_power",
        translation_key="grid_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AlphaEssLocalSensorDescription(
        key="battery_power",
        translation_key="battery_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AlphaEssLocalSensorDescription(
        key="total_energy_feed_to_grid",
        translation_key="total_energy_feed_to_grid",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    AlphaEssLocalSensorDescription(
        key="total_energy_consume_from_grid",
        translation_key="total_energy_consume_from_grid",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    AlphaEssLocalSensorDescription(
        key="pv_total_energy_feed_to_grid",
        translation_key="pv_total_energy_feed_to_grid",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    AlphaEssLocalSensorDescription(
        key="pv_total_energy_consume_from_grid",
        translation_key="pv_total_energy_consume_from_grid",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    AlphaEssLocalSensorDescription(
        key="pv_total_energy",
        translation_key="pv_total_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)


def _current_hour_price(day: Day, options: Mapping[str, Any]) -> float | None:
    """The price to use energy right now (raw market price + fee + VAT)."""
    if not day.valid:
        return None
    hour = day.hour[dt_util.now().hour]
    if not hour.valid:
        return None
    use_fee = options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE)
    vat_percentage = options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE)
    return round(mk_use_price(hour.price, use_fee, vat_percentage), 5)


def _lowest_price(day: Day, _options: Mapping[str, Any]) -> float | None:
    """Today's lowest raw market price (used to identify the cheapest hour)."""
    if not day.valid or day.index_lowest < 0:
        return None
    return day.hour[day.index_lowest].price


def _highest_price(day: Day, _options: Mapping[str, Any]) -> float | None:
    """Today's highest raw market price (used to identify the priciest hour)."""
    if not day.valid or day.index_highest < 0:
        return None
    return day.hour[day.index_highest].price


def _current_hour_earning(day: Day, _options: Mapping[str, Any]) -> str | None:
    """Today's current hour's earning classification, as a translation key."""
    if not day.valid:
        return None
    hour = day.hour[dt_util.now().hour]
    if not hour.valid:
        return None
    return hour.earning.name.lower()


def _current_hour_solar_power(day: Day, _options: Mapping[str, Any]) -> int | None:
    """Estimated solar production for the current local hour."""
    if not day.valid:
        return None
    hour = day.hour[dt_util.now().hour]
    if not hour.valid:
        return None
    return hour.estimated_solar_power


def _total_solar_energy(day: Day, _options: Mapping[str, Any]) -> int | None:
    """Estimated total solar production across the day's valid hours."""
    if not day.valid:
        return None
    valid_hours = [hour for hour in day.hour if hour.valid]
    if not valid_hours:
        return None
    return sum(hour.estimated_solar_power for hour in valid_hours)


@dataclass(frozen=True, kw_only=True)
class AlphaEssLocalDaySensorDescription(SensorEntityDescription):
    """Describes a sensor derived from one of a coordinator's Day objects."""

    day_key: str = "today"
    value_fn: Callable[[Day, Mapping[str, Any]], Any]
    attributes_fn: Callable[[Day, Mapping[str, Any]], Mapping[str, Any] | None] | None = None


PRICE_SENSOR_DESCRIPTIONS: tuple[AlphaEssLocalDaySensorDescription, ...] = (
    AlphaEssLocalDaySensorDescription(
        key="price_current_hour",
        translation_key="price_current_hour",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_current_hour_price,
    ),
    AlphaEssLocalDaySensorDescription(
        key="price_lowest_today",
        translation_key="price_lowest_today",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_lowest_price,
    ),
    AlphaEssLocalDaySensorDescription(
        key="price_highest_today",
        translation_key="price_highest_today",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_highest_price,
    ),
    AlphaEssLocalDaySensorDescription(
        key="price_earning_status",
        translation_key="price_earning_status",
        device_class=SensorDeviceClass.ENUM,
        options=["no_earning", "earning_on_return", "earning_on_use"],
        value_fn=_current_hour_earning,
    ),
)

SOLAR_SENSOR_DESCRIPTIONS: tuple[AlphaEssLocalDaySensorDescription, ...] = (
    AlphaEssLocalDaySensorDescription(
        key="solar_forecast_current_hour",
        translation_key="solar_forecast_current_hour",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_current_hour_solar_power,
    ),
    AlphaEssLocalDaySensorDescription(
        key="solar_forecast_today",
        translation_key="solar_forecast_today",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # No state_class: this is a forecast/estimate, not an accumulating
        # meter reading, so it can go up or down between refreshes — 'total'/
        # 'total_increasing' don't apply. Matches HA core's own
        # forecast_solar integration's energy_production_today sensor.
        value_fn=_total_solar_energy,
    ),
    AlphaEssLocalDaySensorDescription(
        key="solar_forecast_tomorrow",
        translation_key="solar_forecast_tomorrow",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        day_key="tomorrow",
        value_fn=_total_solar_energy,
    ),
)


def _current_hour_action(day: Day, _options: Mapping[str, Any]) -> str | None:
    """The scheduler's chosen action for the current local hour, as a label."""
    if not day.valid:
        return None
    hour = day.hour[dt_util.now().hour]
    if not hour.valid:
        return None
    return set_charging_msg(hour.charge)


def _next_hour_action(day: Day, _options: Mapping[str, Any]) -> str | None:
    """The scheduler's chosen action for the next local hour, as a label.

    Reads from the same "today" Day the current-hour sensor uses — at 23:00
    this wraps to index 0 of *today's* hours (not tomorrow's actual next
    hour). Observational sensor only, so this edge case is left as-is rather
    than threading a second Day through value_fn.
    """
    if not day.valid:
        return None
    hour = day.hour[(dt_util.now().hour + 1) % MAX_HOURS]
    if not hour.valid:
        return None
    return set_charging_msg(hour.charge)


def _remaining_solar_surplus_today(day: Day, _options: Mapping[str, Any]) -> int | None:
    """Expected solar surplus (solar minus house load) for the rest of today.

    Sum of `max(0, estimated_solar_power - estimated_house_load)` from the
    current hour onward — the same quantity `schedule.py`'s
    `_calculate_best_schedule` sums (per later hour) to decide how much
    headroom to leave when grid-charging, so this shows what's driving that
    reservation. Hours with no solar/load estimate yet contribute 0, not an
    error, since forecasts fill in progressively.
    """
    if not day.valid:
        return None
    current_hour = dt_util.now().hour
    return sum(
        max(0, hour.estimated_solar_power - hour.estimated_house_load)
        for hour in day.hour[current_hour:]
        if hour.valid
    )


def _hour_plan_entries(day: Day) -> list[dict[str, Any]]:
    """The full day's hour-by-hour plan, as plain dicts for a sensor attribute.

    Lets you see the whole day's price/solar/house-load/chosen-action
    breakdown from Home Assistant itself (e.g. a dashboard table), instead
    of only ever being visible in schedule.py's DEBUG log output.
    """
    return [
        {
            "hour": h,
            "price": hour.price,
            "solar_forecast_w": hour.estimated_solar_power,
            "house_load_w": hour.estimated_house_load,
            "action": set_charging_msg(hour.charge),
        }
        for h, hour in enumerate(day.hour)
        if hour.valid
    ]


def _day_plan_hour_count(day: Day, _options: Mapping[str, Any]) -> int | None:
    """Number of hours with a planned action today — the state for
    `schedule_today_plan` (the full breakdown lives in its `hours`
    attribute, see `_hour_plan_entries`)."""
    if not day.valid:
        return None
    return len(_hour_plan_entries(day))


def _day_plan_attributes(day: Day, _options: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not day.valid:
        return None
    return {"hours": _hour_plan_entries(day)}


SCHEDULE_SENSOR_DESCRIPTIONS: tuple[AlphaEssLocalDaySensorDescription, ...] = (
    AlphaEssLocalDaySensorDescription(
        key="schedule_current_hour_action",
        translation_key="schedule_current_hour_action",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_current_hour_action,
    ),
    AlphaEssLocalDaySensorDescription(
        key="schedule_next_hour_action",
        translation_key="schedule_next_hour_action",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_next_hour_action,
    ),
    AlphaEssLocalDaySensorDescription(
        key="schedule_remaining_solar_surplus_today",
        translation_key="schedule_remaining_solar_surplus_today",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_remaining_solar_surplus_today,
    ),
    AlphaEssLocalDaySensorDescription(
        key="schedule_today_plan",
        translation_key="schedule_today_plan",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_day_plan_hour_count,
        attributes_fn=_day_plan_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEssLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    runtime_data = entry.runtime_data
    async_add_entities(
        [
            *(
                AlphaEssLocalSensor(runtime_data.modbus_coordinator, description)
                for description in MODBUS_SENSOR_DESCRIPTIONS
            ),
            *(
                AlphaEssLocalDaySensor(runtime_data.prices_coordinator, description)
                for description in PRICE_SENSOR_DESCRIPTIONS
            ),
            *(
                AlphaEssLocalDaySensor(runtime_data.solar_coordinator, description)
                for description in SOLAR_SENSOR_DESCRIPTIONS
            ),
            *(
                AlphaEssLocalDaySensor(runtime_data.schedule_coordinator, description)
                for description in SCHEDULE_SENSOR_DESCRIPTIONS
            ),
            AlphaEssLocalExtraPvPowerSensor(runtime_data.real_data_coordinator),
            AlphaEssLocalTotalPvPowerSensor(
                runtime_data.real_data_coordinator, runtime_data.modbus_coordinator
            ),
            AlphaEssLocalHouseLoadTodaySensor(runtime_data.real_data_coordinator),
            AlphaEssLocalSolarEnergyTodaySensor(runtime_data.real_data_coordinator),
            AlphaEssLocalSolarToBatteryTodaySensor(runtime_data.real_data_coordinator),
            AlphaEssLocalGridToBatteryTodaySensor(runtime_data.real_data_coordinator),
            AlphaEssLocalYesterdaySavingsSensor(runtime_data.real_data_coordinator),
            AlphaEssLocalDispatchModeSensor(runtime_data.dispatch_coordinator),
        ]
    )


class AlphaEssLocalSensor(AlphaEssLocalEntity, SensorEntity):
    """Represents a single AlphaESS measurement."""

    entity_description: AlphaEssLocalSensorDescription

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: AlphaEssLocalSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self):
        """Return the current value from the coordinator's data dict."""
        return self.coordinator.data.get(self.entity_description.key)


class AlphaEssLocalDaySensor(AlphaEssLocalEntity, SensorEntity):
    """Represents a single value derived from a coordinator's Day of data."""

    entity_description: AlphaEssLocalDaySensorDescription

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: AlphaEssLocalDaySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self):
        """Return the current value, derived from the configured day's Day."""
        day = self.coordinator.data[self.entity_description.day_key]
        return self.entity_description.value_fn(day, self.coordinator.config_entry.options)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return extra attributes, if this description defines any."""
        if self.entity_description.attributes_fn is None:
            return None
        day = self.coordinator.data[self.entity_description.day_key]
        return self.entity_description.attributes_fn(day, self.coordinator.config_entry.options)


class AlphaEssLocalExtraPvPowerSensor(AlphaEssLocalEntity, SensorEntity):
    """The second/separate PV installation's real-time power, mirroring `pv_power`.

    `None` (unavailable) when no `extra_pv_power_entity`/SMA source is
    configured at all — distinct from a configured source currently reading
    0 W (e.g. at night).
    """

    _attr_translation_key = "extra_pv_power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_extra_pv_power"

    @property
    def native_value(self) -> float | None:
        """Return this cycle's raw extra-PV reading."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.extra_pv_power


class AlphaEssLocalTotalPvPowerSensor(AlphaEssLocalEntity, SensorEntity):
    """Combined real-time PV power: the AlphaESS's own panels + the extra
    installation, if one is configured (else equal to `pv_power` alone)."""

    _attr_translation_key = "total_pv_power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        real_data_coordinator: DataUpdateCoordinator,
        modbus_coordinator: DataUpdateCoordinator,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(real_data_coordinator)
        self._modbus_coordinator = modbus_coordinator
        self._attr_unique_id = f"{real_data_coordinator.config_entry.entry_id}_total_pv_power"

    @property
    def native_value(self) -> float | None:
        """Return the AlphaESS's own PV power plus the extra installation's, if any."""
        pv_power = (self._modbus_coordinator.data or {}).get("pv_power")
        if pv_power is None:
            return None
        extra_pv_power = self.coordinator.data.extra_pv_power if self.coordinator.data else None
        return round(pv_power + (extra_pv_power or 0.0))


class AlphaEssLocalHouseLoadTodaySensor(AlphaEssLocalEntity, SensorEntity):
    """Total measured house consumption so far today.

    Sums each completed (or in-progress) hour's average power
    (`Hour.real_house_load`, in Watts) — since each covers exactly one hour,
    that average *is* that hour's Wh consumption, no separate integration
    needed. Resets to 0 at midnight (a fresh `Day` — see
    `AlphaEssLocalRealDataCoordinator`), which `state_class: total` expects.
    """

    _attr_translation_key = "house_load_today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_house_load_today"

    @property
    def native_value(self) -> float | None:
        """Return today's total measured house consumption, in kWh."""
        if self.coordinator.data is None:
            return None
        day = self.coordinator.data.day
        if not day.valid:
            return None
        total_wh = sum(hour.real_house_load for hour in day.hour if hour.valid)
        return round(total_wh / 1000, 2)


class AlphaEssLocalSolarEnergyTodaySensor(AlphaEssLocalEntity, SensorEntity):
    """Total measured solar production so far today (roof + extra PV, combined).

    Mirrors `AlphaEssLocalHouseLoadTodaySensor`: sums each completed (or
    in-progress) hour's average power (`Hour.real_solar_power_roof` +
    `Hour.real_extra_pv_power`, in Watts) into that hour's Wh production.
    Resets to 0 at midnight (a fresh `Day`).
    """

    _attr_translation_key = "solar_energy_today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_solar_energy_today"

    @property
    def native_value(self) -> float | None:
        """Return today's total measured solar production, in kWh."""
        if self.coordinator.data is None:
            return None
        day = self.coordinator.data.day
        if not day.valid:
            return None
        total_wh = sum(
            hour.real_solar_power_roof + hour.real_extra_pv_power for hour in day.hour if hour.valid
        )
        return round(total_wh / 1000, 2)


class AlphaEssLocalSolarToBatteryTodaySensor(AlphaEssLocalEntity, SensorEntity):
    """Total energy charged into the battery from solar surplus so far today.

    Mirrors `AlphaEssLocalHouseLoadTodaySensor`: sums each completed (or
    in-progress) hour's average power (`Hour.real_solar_to_battery`, in
    Watts) into that hour's Wh contribution. This is an energy-balance
    estimate, not a separately-metered value — the inverter doesn't report
    a per-source battery-charge split; see `orchestrator._solar_to_battery`.
    Resets to 0 at midnight (a fresh `Day`).
    """

    _attr_translation_key = "solar_to_battery_today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_solar_to_battery_today"

    @property
    def native_value(self) -> float | None:
        """Return today's total solar-to-battery energy, in kWh."""
        if self.coordinator.data is None:
            return None
        day = self.coordinator.data.day
        if not day.valid:
            return None
        total_wh = sum(hour.real_solar_to_battery for hour in day.hour if hour.valid)
        return round(total_wh / 1000, 2)


class AlphaEssLocalGridToBatteryTodaySensor(AlphaEssLocalEntity, SensorEntity):
    """Total energy charged into the battery from the grid so far today.

    Mirrors `AlphaEssLocalSolarToBatteryTodaySensor` — the remainder of each
    hour's battery charging not covered by solar surplus
    (`Hour.real_grid_to_battery`). Same energy-balance-estimate caveat.
    Resets to 0 at midnight (a fresh `Day`).
    """

    _attr_translation_key = "grid_to_battery_today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_grid_to_battery_today"

    @property
    def native_value(self) -> float | None:
        """Return today's total grid-to-battery energy, in kWh."""
        if self.coordinator.data is None:
            return None
        day = self.coordinator.data.day
        if not day.valid:
            return None
        total_wh = sum(hour.real_grid_to_battery for hour in day.hour if hour.valid)
        return round(total_wh / 1000, 2)


class AlphaEssLocalYesterdaySavingsSensor(AlphaEssLocalEntity, SensorEntity):
    """Yesterday's real per-hour solar generation and cost savings.

    State is total savings (solar + battery combined), in EUR, derived from
    `storage.retrieve_day_savings` — not an estimate: it's computed from the
    actual measured net grid exchange that day, compared against what the
    same hours would have cost with no solar, and with solar but no
    battery. The `hours` attribute carries the full per-hour breakdown for
    a dashboard table.
    """

    _attr_translation_key = "yesterday_savings"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_yesterday_savings"

    @property
    def native_value(self) -> float | None:
        """Return yesterday's total savings (solar + battery), in EUR."""
        if self.coordinator.data is None:
            return None
        hours = self.coordinator.data.yesterday_savings
        if not hours:
            return None
        return round(sum(h["savings_solar_eur"] + h["savings_battery_eur"] for h in hours), 2)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the per-hour breakdown, plus each column's daily total."""
        if self.coordinator.data is None:
            return None
        hours = self.coordinator.data.yesterday_savings
        if not hours:
            return None
        return {
            "hours": hours,
            "solar_wh_total": sum(h["solar_wh"] for h in hours),
            "solar_wh_roof_total": sum(h["solar_wh_roof"] for h in hours),
            "solar_wh_extra_total": sum(h["solar_wh_extra"] for h in hours),
            "savings_solar_eur_total": round(sum(h["savings_solar_eur"] for h in hours), 2),
            "savings_battery_eur_total": round(sum(h["savings_battery_eur"] for h in hours), 2),
        }


class AlphaEssLocalDispatchModeSensor(AlphaEssLocalEntity, SensorEntity):
    """The dispatch coordinator's last *decided* mode, as a friendly label.

    Shows the decision regardless of whether `control_enabled` actually let
    it be written — this is the at-a-glance summary of "what it wants to
    do"; the detailed reasoning is in the log (see `orchestrator.py`'s
    `AlphaEssLocalDispatchCoordinator`).
    """

    _attr_translation_key = "dispatch_current_mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_dispatch_current_mode"

    @property
    def native_value(self) -> str | None:
        """Return the last decided dispatch mode's friendly label."""
        if self.coordinator.data is None:
            return None
        return set_dispatch_msg(self.coordinator.data.param.mode)
