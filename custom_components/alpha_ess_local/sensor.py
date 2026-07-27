"""Sensor platform for AlphaESS Local Control.

The keys below (battery_soc, pv_power, grid_power) are placeholders that
match the example dict shape in `api.py`. Once the real Modbus/HTTP fields
are known, update `SENSOR_DESCRIPTIONS` (and `api.async_get_data`) to match.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AlphaEssLocalConfigEntry
from .entity import AlphaEssLocalEntity


@dataclass(frozen=True, kw_only=True)
class AlphaEssLocalSensorDescription(SensorEntityDescription):
    """Describes an AlphaESS sensor backed by a coordinator data key."""


SENSOR_DESCRIPTIONS: tuple[AlphaEssLocalSensorDescription, ...] = (
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
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEssLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        AlphaEssLocalSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class AlphaEssLocalSensor(AlphaEssLocalEntity, SensorEntity):
    """Represents a single AlphaESS measurement."""

    entity_description: AlphaEssLocalSensorDescription

    def __init__(self, coordinator, description: AlphaEssLocalSensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self):
        """Return the current value from the coordinator's data dict."""
        return self.coordinator.data.get(self.entity_description.key)
