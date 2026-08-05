"""Number platform for AlphaESS Local Control.

Live-adjustable scheduler settings, as `NumberEntity` sliders directly on
this integration's own device — same "Bediening"/Controls pattern other
integrations (e.g. Alfen Wallbox) use for device settings, rather than a
value tucked away behind this integration's own settings dialog. Replaces
an earlier `input_number` helper-entity approach for the SOC bounds: no
separate helper to create and link via an options-flow entity-selector,
just a control that lives on the device itself.

`orchestrator.py` reads each entity's current state directly (via the
entity registry, looked up by unique_id) every schedule refresh — moving
the slider takes effect on the next refresh, no reload/restart needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AlphaEssLocalConfigEntry
from .const import (
    DEFAULT_DAILY_MIN_PROFIT,
    DEFAULT_MAX_SOC_NEGATIVE_PRICE,
    DEFAULT_MAX_SOC_POSITIVE_PRICE,
    DEFAULT_MIN_SOC_DISCHARGE,
    DOMAIN,
)


@dataclass(frozen=True, kw_only=True)
class AlphaEssLocalNumberDescription(NumberEntityDescription):
    """Describes a live-adjustable scheduler setting."""

    default_value: float = 0.0


NUMBER_DESCRIPTIONS: tuple[AlphaEssLocalNumberDescription, ...] = (
    AlphaEssLocalNumberDescription(
        key="max_soc_positive_price",
        translation_key="max_soc_positive_price",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        default_value=DEFAULT_MAX_SOC_POSITIVE_PRICE,
    ),
    AlphaEssLocalNumberDescription(
        key="max_soc_negative_price",
        translation_key="max_soc_negative_price",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        default_value=DEFAULT_MAX_SOC_NEGATIVE_PRICE,
    ),
    AlphaEssLocalNumberDescription(
        key="min_soc_discharge",
        translation_key="min_soc_discharge",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
        default_value=DEFAULT_MIN_SOC_DISCHARGE,
    ),
    AlphaEssLocalNumberDescription(
        key="daily_min_profit",
        translation_key="daily_min_profit",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement="ct",
        mode=NumberMode.SLIDER,
        # DEFAULT_DAILY_MIN_PROFIT is in EUR (schedule.py's unit); this
        # entity's own scale is whole cents — see orchestrator.py's /100
        # on the read side.
        default_value=round(DEFAULT_DAILY_MIN_PROFIT * 100),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEssLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from a config entry."""
    async_add_entities(
        AlphaEssLocalSchedulerNumber(entry, description) for description in NUMBER_DESCRIPTIONS
    )


class AlphaEssLocalSchedulerNumber(RestoreNumber):
    """A live-adjustable scheduler setting (an SOC bound, or the minimum
    daily profit threshold).

    Persisted across restarts via `RestoreNumber` (no config-entry write/
    reload involved) — the value set here *is* the value the scheduler
    reads on its next refresh.
    """

    _attr_has_entity_name = True

    entity_description: AlphaEssLocalNumberDescription

    def __init__(
        self, entry: AlphaEssLocalConfigEntry, description: AlphaEssLocalNumberDescription
    ) -> None:
        """Initialize the number entity."""
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AlphaESS",
            manufacturer="AlphaESS",
        )
        self._attr_native_value = description.default_value

    async def async_added_to_hass(self) -> None:
        """Restore the last known value, if any."""
        await super().async_added_to_hass()
        last_number_data = await self.async_get_last_number_data()
        if last_number_data is not None and last_number_data.native_value is not None:
            self._attr_native_value = last_number_data.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self.async_write_ha_state()
