"""Switch platform for AlphaESS Local Control.

A live on/off control directly on this integration's own device — same
"Bediening"/Controls pattern as number.py's sliders.

`orchestrator.py` reads this entity's current state directly (via the
entity registry, looked up by unique_id, same as number.py's sliders)
every schedule refresh — flipping the switch takes effect on the next
refresh, no reload/restart needed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import AlphaEssLocalConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlphaEssLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities from a config entry."""
    async_add_entities([AlphaEssLocalDischargeEnabledSwitch(entry)])


class AlphaEssLocalDischargeEnabledSwitch(SwitchEntity, RestoreEntity):
    """Master on/off for the scheduler's once-per-day deliberate discharge
    (`Charge.CHARGING_DISCHARGE`, schedule.py's arbitrage action — see
    `discharge_used`). Normal battery discharge covering house load when
    solar is insufficient is unaffected; this only gates the scheduler's
    own choice to deliberately sell battery energy back during the day's
    priciest hour.

    Defaults on (matches the original, unrestricted behavior); persisted
    across restarts via `RestoreEntity`.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "discharge_enabled"

    def __init__(self, entry: AlphaEssLocalConfigEntry) -> None:
        """Initialize the switch."""
        self._attr_unique_id = f"{entry.entry_id}_discharge_enabled"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="AlphaESS",
            manufacturer="AlphaESS",
        )
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        """Restore the last known state, if any."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow the scheduler's deliberate discharge action again."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Block the scheduler from ever choosing the deliberate discharge."""
        self._attr_is_on = False
        self.async_write_ha_state()
