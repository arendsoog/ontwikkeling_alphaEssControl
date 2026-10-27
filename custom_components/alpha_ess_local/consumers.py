"""Consumers left out of the learned house load (EV chargers, a heat pump, ...).

Big, irregular consumers would make the learned load per hour wrong, so
their power is taken off the measured house load first. Two groups, each
picked by their sensors in the options, any number of each:
- EV chargers: a power sensor (summed in the panel's energy flow) and,
  optionally, a total-energy counter (its kWh today in the panel) and a
  power sensor per phase (its card in the panel);
- other consumers (a heat pump, ...): a power sensor, for the learning only.

Before these lists, one EV charger had its own power/energy fields; those
are read along until the section is saved again (which drops them).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EV_CHARGER_ENERGY_ENTITIES,
    CONF_EV_CHARGER_ENERGY_ENTITY,
    CONF_EV_CHARGER_PHASE_ENTITIES,
    CONF_EV_CHARGER_POWER_ENTITIES,
    CONF_EV_CHARGER_POWER_ENTITY,
    CONF_EXCLUDED_LOAD_ENTITIES,
)


def _unique(entities: list[Any]) -> list[str]:
    """The given entity ids in order, without blanks or repeats."""
    return list(dict.fromkeys(entity for entity in entities if entity))


def ev_charger_power_entities(options: Mapping[str, Any]) -> list[str]:
    """The EV chargers' power sensors."""
    return _unique(
        [
            options.get(CONF_EV_CHARGER_POWER_ENTITY),
            *(options.get(CONF_EV_CHARGER_POWER_ENTITIES) or []),
        ]
    )


def ev_charger_energy_entities(options: Mapping[str, Any]) -> list[str]:
    """The EV chargers' total-energy counters."""
    return _unique(
        [
            options.get(CONF_EV_CHARGER_ENERGY_ENTITY),
            *(options.get(CONF_EV_CHARGER_ENERGY_ENTITIES) or []),
        ]
    )


def ev_charger_phase_entities(options: Mapping[str, Any]) -> list[str]:
    """The EV chargers' power sensors per phase, one per phase (panel only)."""
    return _unique(list(options.get(CONF_EV_CHARGER_PHASE_ENTITIES) or []))


def ev_charger_groups(hass: HomeAssistant, options: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The EV chargers' sensors grouped per charger, for the panel's cards.

    A charger is a device in HA: its sensors are grouped by the device they
    belong to (named after it), in the order they were picked. Sensors that
    belong to no device (e.g. Modbus sensors from YAML) form one group.
    """
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    groups: dict[str | None, dict[str, Any]] = {}
    for kind, entities in (
        ("power", ev_charger_power_entities(options)),
        ("energy", ev_charger_energy_entities(options)),
        ("phases", ev_charger_phase_entities(options)),
    ):
        for entity_id in entities:
            reg_entry = registry.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            group = groups.get(device_id)
            if group is None:
                device = devices.async_get(device_id) if device_id else None
                name = (device.name_by_user or device.name) if device else None
                group = {"name": name, "power": [], "energy": [], "phases": []}
                groups[device_id] = group
            group[kind].append(entity_id)
    return list(groups.values())


def excluded_power_entities(options: Mapping[str, Any]) -> list[str]:
    """All power sensors left out of the learned house load: the EV
    chargers' and the other consumers'."""
    return _unique(
        [*ev_charger_power_entities(options), *(options.get(CONF_EXCLUDED_LOAD_ENTITIES) or [])]
    )
