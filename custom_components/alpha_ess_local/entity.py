"""Base entity for AlphaESS Local Control."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AlphaEssLocalDataUpdateCoordinator


class AlphaEssLocalEntity(CoordinatorEntity[AlphaEssLocalDataUpdateCoordinator]):
    """Common base for all entities of this integration."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AlphaEssLocalDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="AlphaESS",
            manufacturer="AlphaESS",
        )
