"""Consumers left out of the learned house load (EV chargers, a heat pump, ...).

Big, irregular consumers would make the learned load per hour wrong, so
their power is taken off the measured house load first. Two groups:
- EV chargers, each a subentry of the config entry ("Add EV charger"): a
  power sensor (summed in the panel's energy flow) and, optionally, a
  total-energy counter (its kWh today in the panel) and a power sensor per
  phase (its card in the panel);
- other consumers (a heat pump, ...), each a subentry too ("Add
  consumer"): a power sensor, for the learning only, and optionally a
  total-energy counter.

The rest of the integration reads them folded into the options by
`consumer_options`, under the same keys as the lists the options had
before the subentries. Those lists -- and older still, one charger's own
power/energy fields -- move into one subentry per charger/consumer when the
entry is migrated (`ev_chargers_from_options`, `consumers_from_options`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ICON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CONSUMER_ENERGY_ENTITY,
    CONF_CONSUMER_POWER_ENTITY,
    CONF_EV_CHARGER_ENERGY_ENTITIES,
    CONF_EV_CHARGER_ENERGY_ENTITY,
    CONF_EV_CHARGER_PHASE_ENTITIES,
    CONF_EV_CHARGER_POWER_ENTITIES,
    CONF_EV_CHARGER_POWER_ENTITY,
    CONF_EXCLUDE_FROM_LEARNING,
    CONF_EXCLUDED_LOAD_ENTITIES,
    CONF_EXCLUDED_POWER_ENTITIES,
    DEFAULT_EXCLUDE_FROM_LEARNING,
    SUBENTRY_CONSUMER,
    SUBENTRY_EV_CHARGER,
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
    chargers' and the other consumers' -- from subentries only those set to
    be left out."""
    if CONF_EXCLUDED_POWER_ENTITIES in options:
        return list(options[CONF_EXCLUDED_POWER_ENTITIES])
    return _unique(
        [*ev_charger_power_entities(options), *(options.get(CONF_EXCLUDED_LOAD_ENTITIES) or [])]
    )


def _subentries(entry: ConfigEntry, subentry_type: str) -> list[Mapping[str, Any]]:
    return [
        {**subentry.data, "name": subentry.title}
        for subentry in entry.subentries.values()
        if subentry.subentry_type == subentry_type
    ]


def ev_chargers(entry: ConfigEntry) -> list[Mapping[str, Any]]:
    """The EV chargers' settings, each with its name under "name"."""
    return _subentries(entry, SUBENTRY_EV_CHARGER)


def other_consumers(entry: ConfigEntry) -> list[Mapping[str, Any]]:
    """The other consumers' settings, each with its name under "name"."""
    return _subentries(entry, SUBENTRY_CONSUMER)


def consumer_options(entry: ConfigEntry) -> dict[str, Any]:
    """The entry's options with its EV chargers and other consumers folded
    in under the list keys. Without any, the options as they are (an entry
    not migrated yet still has them there)."""
    options = dict(entry.options)
    consumers = other_consumers(entry)
    chargers = ev_chargers(entry)
    if not (consumers or chargers):
        return options

    def left_out(item: Mapping[str, Any]) -> bool:
        return item.get(CONF_EXCLUDE_FROM_LEARNING, DEFAULT_EXCLUDE_FROM_LEARNING)

    options[CONF_EXCLUDED_POWER_ENTITIES] = _unique(
        [e for charger in chargers if left_out(charger) for e in ev_charger_power_entities(charger)]
        + [consumer.get(CONF_CONSUMER_POWER_ENTITY) for consumer in consumers if left_out(consumer)]
    )
    options[CONF_EXCLUDED_LOAD_ENTITIES] = _unique(
        [consumer.get(CONF_CONSUMER_POWER_ENTITY) for consumer in consumers]
    )
    if not chargers:
        return options
    for key in (CONF_EV_CHARGER_POWER_ENTITY, CONF_EV_CHARGER_ENERGY_ENTITY):
        options.pop(key, None)
    options[CONF_EV_CHARGER_POWER_ENTITIES] = _unique(
        [e for charger in chargers for e in ev_charger_power_entities(charger)]
    )
    options[CONF_EV_CHARGER_ENERGY_ENTITIES] = _unique(
        [e for charger in chargers for e in ev_charger_energy_entities(charger)]
    )
    options[CONF_EV_CHARGER_PHASE_ENTITIES] = _unique(
        [e for charger in chargers for e in ev_charger_phase_entities(charger)]
    )
    return options


def ev_charger_cards(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """The EV chargers' sensors per charger, for the panel's cards: one per
    subentry, named after it (an entry not migrated yet: grouped by device)."""
    chargers = ev_chargers(entry)
    if not chargers:
        return ev_charger_groups(hass, entry.options)
    return [
        {
            "name": charger["name"],
            "icon": charger.get(CONF_ICON),
            "power": ev_charger_power_entities(charger),
            "energy": ev_charger_energy_entities(charger),
            "phases": ev_charger_phase_entities(charger),
        }
        for charger in chargers
    ]


def consumer_cards(entry: ConfigEntry) -> list[dict[str, Any]]:
    """The other consumers' sensors per consumer, for the panel's cards."""
    return [
        {
            "name": consumer["name"],
            "icon": consumer.get(CONF_ICON),
            "power": consumer.get(CONF_CONSUMER_POWER_ENTITY),
            "energy": consumer.get(CONF_CONSUMER_ENERGY_ENTITY),
        }
        for consumer in other_consumers(entry)
    ]


# The EV chargers' settings in the options before the subentries.
_EV_CHARGER_OPTION_KEYS = (
    CONF_EV_CHARGER_POWER_ENTITY,
    CONF_EV_CHARGER_ENERGY_ENTITY,
    CONF_EV_CHARGER_POWER_ENTITIES,
    CONF_EV_CHARGER_ENERGY_ENTITIES,
    CONF_EV_CHARGER_PHASE_ENTITIES,
)


def ev_chargers_from_options(
    hass: HomeAssistant, options: Mapping[str, Any], default_name: str
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Split the EV chargers from options saved before the subentries: per
    charger (grouped by device, as the panel showed them) its name and
    settings, and the options left."""
    rest = {k: v for k, v in options.items() if k not in _EV_CHARGER_OPTION_KEYS}
    chargers = []
    for group in ev_charger_groups(hass, options):
        if not (group["power"] or group["energy"]):
            continue
        data: dict[str, Any] = {}
        for key, single, many in (
            ("power", CONF_EV_CHARGER_POWER_ENTITY, CONF_EV_CHARGER_POWER_ENTITIES),
            ("energy", CONF_EV_CHARGER_ENERGY_ENTITY, CONF_EV_CHARGER_ENERGY_ENTITIES),
        ):
            # One sensor in the single field; several (one device with two
            # power sensors) kept as a list.
            if len(group[key]) == 1:
                data[single] = group[key][0]
            elif group[key]:
                data[many] = group[key]
        if group["phases"]:
            data[CONF_EV_CHARGER_PHASE_ENTITIES] = group["phases"]
        chargers.append((group["name"] or default_name, data))
    return chargers, rest


def consumers_from_options(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Split the other consumers from options saved before the subentries:
    per power sensor a consumer named after it, and the options left."""
    registry = er.async_get(hass)
    consumers = []
    for entity_id in _unique(list(options.get(CONF_EXCLUDED_LOAD_ENTITIES) or [])):
        state = hass.states.get(entity_id)
        reg_entry = registry.async_get(entity_id)
        name = (
            (state.name if state else None)
            or (reg_entry and (reg_entry.name or reg_entry.original_name))
            or entity_id
        )
        consumers.append((name, {CONF_CONSUMER_POWER_ENTITY: entity_id}))
    rest = {k: v for k, v in options.items() if k != CONF_EXCLUDED_LOAD_ENTITIES}
    return consumers, rest
