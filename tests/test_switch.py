"""Tests for switch.py.

The live on/off control (on the device, same "Bediening" pattern as
number.py's sliders) that gates the scheduler's once-per-day deliberate
CHARGING_DISCHARGE action (see orchestrator._switch_entity_value for the
read side).
"""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.alpha_ess_local.const import DOMAIN
from custom_components.alpha_ess_local.switch import AlphaEssLocalDischargeEnabledSwitch


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, entry_id="test-entry")


def test_discharge_switch_defaults_on():
    switch = AlphaEssLocalDischargeEnabledSwitch(_entry())

    assert switch.is_on is True


def test_discharge_switch_unique_id_includes_entry_id():
    entry = _entry()
    switch = AlphaEssLocalDischargeEnabledSwitch(entry)

    assert switch.unique_id == f"{entry.entry_id}_discharge_enabled"


def test_discharge_switch_device_info_matches_entry(hass: HomeAssistant):
    entry = _entry()
    switch = AlphaEssLocalDischargeEnabledSwitch(entry)

    assert switch.device_info["identifiers"] == {(DOMAIN, entry.entry_id)}


async def test_discharge_switch_turn_off_updates_state(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    switch = AlphaEssLocalDischargeEnabledSwitch(entry)
    switch.hass = hass
    switch.entity_id = "switch.test_discharge_enabled"
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_off()

    assert switch.is_on is False
    switch.async_write_ha_state.assert_called_once()


async def test_discharge_switch_turn_on_updates_state(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    switch = AlphaEssLocalDischargeEnabledSwitch(entry)
    switch.hass = hass
    switch.entity_id = "switch.test_discharge_enabled"
    switch.async_write_ha_state = MagicMock()
    switch._attr_is_on = False

    await switch.async_turn_on()

    assert switch.is_on is True
    switch.async_write_ha_state.assert_called_once()


async def test_discharge_switch_restores_last_state_after_restart(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    entity_id = "switch.test_discharge_enabled"

    mock_restore_cache(hass, [State(entity_id, "off")])

    switch = AlphaEssLocalDischargeEnabledSwitch(entry)
    switch.hass = hass
    switch.entity_id = entity_id
    await switch.async_added_to_hass()

    assert switch.is_on is False
