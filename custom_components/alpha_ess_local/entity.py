"""Base entity for AlphaESSControl."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN


class AlphaEssLocalEntity(CoordinatorEntity[DataUpdateCoordinator[Any]]):
    """Common base for all entities of this integration.

    Shared by entities backed by either of this integration's coordinators
    (Modbus polling, price derivation) — same device, different data streams.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator[Any]) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="AlphaESS",
            manufacturer="AlphaESS",
        )
