"""Tests for consumers.py: the consumers left out of the learned house load."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local.consumers import (
    ev_charger_energy_entities,
    ev_charger_groups,
    ev_charger_power_entities,
    excluded_power_entities,
)


def test_lists_take_over_the_single_charger():
    """Saved before the lists: the one charger's sensors are read along."""
    options = {
        "ev_charger_power_entity": "sensor.ev_power",
        "ev_charger_energy_entity": "sensor.ev_energy",
        "ev_charger_power_entities": ["sensor.ev2_power", "sensor.ev_power"],
        "excluded_load_entities": ["sensor.heat_pump"],
    }

    assert ev_charger_power_entities(options) == ["sensor.ev_power", "sensor.ev2_power"]
    assert ev_charger_energy_entities(options) == ["sensor.ev_energy"]
    assert excluded_power_entities(options) == [
        "sensor.ev_power",
        "sensor.ev2_power",
        "sensor.heat_pump",
    ]
    assert excluded_power_entities({}) == []


def test_ev_charger_groups_per_device(hass: HomeAssistant):
    """Each charger (a device in HA) gets its own group, named after it;
    sensors without a device form one unnamed group."""
    config_entry = MockConfigEntry(domain="alfen")
    config_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    entities = {}
    for device_name in ("Oprit", "Garage"):
        device = devices.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={("alfen", device_name)},
            name=device_name,
        )
        for kind in ("power", "energy", "l1", "l2"):
            entities[(device_name, kind)] = registry.async_get_or_create(
                "sensor",
                "alfen",
                f"{device_name}_{kind}",
                config_entry=config_entry,
                device_id=device.id,
            ).entity_id
    options = {
        "ev_charger_power_entities": [
            entities[("Oprit", "power")],
            entities[("Garage", "power")],
            "sensor.modbus_laadpaal_vermogen",
        ],
        "ev_charger_energy_entities": [entities[("Garage", "energy")]],
        "ev_charger_phase_entities": [
            entities[("Oprit", "l1")],
            entities[("Oprit", "l2")],
            "sensor.modbus_laadpaal_vermogen_l1",
        ],
    }

    assert ev_charger_groups(hass, options) == [
        {
            "name": "Oprit",
            "power": [entities[("Oprit", "power")]],
            "energy": [],
            "phases": [entities[("Oprit", "l1")], entities[("Oprit", "l2")]],
        },
        {
            "name": "Garage",
            "power": [entities[("Garage", "power")]],
            "energy": [entities[("Garage", "energy")]],
            "phases": [],
        },
        {
            "name": None,
            "power": ["sensor.modbus_laadpaal_vermogen"],
            "energy": [],
            "phases": ["sensor.modbus_laadpaal_vermogen_l1"],
        },
    ]
