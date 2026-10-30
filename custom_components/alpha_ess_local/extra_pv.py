"""The PV installations next to the AlphaESS (a second solar installation, ...).

Each is a subentry of the config entry ("Add PV installation"), picked by
its sensors and a switch:
- its power: its inverter's power sensor (migrated installations may have
  several, summed);
- its total production in kWh, for the panel's daily total;
- its power per string, MPPT or phase, for the panel only;
- its panels (Wp and count), for its peak power;
- a switch that turns it off at negative prices (on = producing): any
  switch, e.g. a Modbus switch writing the inverter's power limit.

The rest of the integration reads all installations together, folded into
the options by `extra_pv_options`: their power sensors summed, their peak
power added up, all their switches switched.

Before the subentries the one installation was in the options; it's moved
into a subentry when the entry is migrated (`installation_from_options`).
Older still, its power source was one sensor or "sma:<config entry>" for the
SMA integration's per-string sensors summed, and its switching a Modbus hub,
slave, register and on/off values -- the hub settings stay in the options
and are used as long as no installation has a switch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EXTRA_PV_ENERGY_ENTITIES,
    CONF_EXTRA_PV_ENERGY_ENTITY,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_PANEL_COUNT,
    CONF_EXTRA_PV_PANEL_WP,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITIES,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_EXTRA_PV_STRING_ENTITIES,
    CONF_EXTRA_PV_SWITCH_ENTITIES,
    CONF_EXTRA_PV_SWITCH_ENTITY,
    SUBENTRY_PV_INSTALLATION,
)

# pysma's internal register keys for its PV-power sensors, verified directly
# from pysma/definitions/webconnect.py — pysma doesn't give these entities a
# readable unique_id suffix the way HomeWizard/DSMR do, so there's nothing
# more semantic to match on:
# - "6100_0046C200" ("pv_power"): the *total* PV power — disabled by default
#   in pysma, so often doesn't exist as a live entity at all.
# - "6380_40251E00" ("pv_power_a"/"_b"/"_c"): per-MPPT-string power —
#   enabled by default (a/b; c is off). Summed when the total isn't there.
SMA_PV_POWER_AGGREGATE_KEY = "6100_0046C200"
SMA_PV_POWER_STRING_KEY = "6380_40251E00"


def _sma_strings(hass: HomeAssistant, config_entry_id: str) -> list[str]:
    """The SMA integration's enabled per-string power sensors (A, B, ...)."""
    registry = er.async_get(hass)
    strings = [
        reg_entry
        for reg_entry in er.async_entries_for_config_entry(registry, config_entry_id)
        if f"-{SMA_PV_POWER_STRING_KEY}_" in reg_entry.unique_id and not reg_entry.disabled
    ]
    return [reg_entry.entity_id for reg_entry in sorted(strings, key=lambda e: e.unique_id)]


def extra_pv_power_entities(hass: HomeAssistant, options: Mapping[str, Any]) -> list[str]:
    """The power sensors summed for the extra source's power."""
    if CONF_EXTRA_PV_POWER_ENTITIES in options:
        return list(dict.fromkeys(e for e in options[CONF_EXTRA_PV_POWER_ENTITIES] or [] if e))
    legacy = options.get(CONF_EXTRA_PV_POWER_ENTITY)
    if not legacy:
        return []
    if legacy.startswith("sma:"):
        return _sma_strings(hass, legacy.removeprefix("sma:"))
    return [legacy]


def detected_extra_pv_power_entities(hass: HomeAssistant) -> list[str]:
    """An SMA integration's power sensors, to pre-fill a new installation's
    options: its total PV power when enabled, else its per-string ones."""
    registry = er.async_get(hass)
    for entry in hass.config_entries.async_entries("sma"):
        aggregate = next(
            (
                reg_entry
                for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
                if reg_entry.unique_id.endswith(f"-{SMA_PV_POWER_AGGREGATE_KEY}_0")
                and not reg_entry.disabled
            ),
            None,
        )
        if aggregate:
            return [aggregate.entity_id]
        if strings := _sma_strings(hass, entry.entry_id):
            return strings
    return []


def extra_pv_string_entities(hass: HomeAssistant, options: Mapping[str, Any]) -> list[str]:
    """The extra source's power per string/MPPT/phase, for the panel.

    The ones picked in the options, else -- when the power comes from the
    SMA integration -- that integration's enabled per-string sensors.
    """
    configured = options.get(CONF_EXTRA_PV_STRING_ENTITIES)
    if configured:
        return list(configured)
    registry = er.async_get(hass)
    for entity_id in extra_pv_power_entities(hass, options):
        reg_entry = registry.async_get(entity_id)
        if reg_entry is not None and reg_entry.platform == "sma" and reg_entry.config_entry_id:
            return _sma_strings(hass, reg_entry.config_entry_id)
    return []


def extra_pv_switches(options: Mapping[str, Any]) -> list[str]:
    """The switches that turn the installations off at negative prices."""
    if CONF_EXTRA_PV_SWITCH_ENTITIES in options:
        return list(options[CONF_EXTRA_PV_SWITCH_ENTITIES])
    switch = options.get(CONF_EXTRA_PV_SWITCH_ENTITY)
    return [switch] if switch else []


def pv_installations(entry: ConfigEntry) -> list[Mapping[str, Any]]:
    """The PV installations' settings, each with its name under "name"."""
    return [
        {**subentry.data, "name": subentry.title}
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_PV_INSTALLATION
    ]


def pv_installation_capacity(installation: Mapping[str, Any]) -> float:
    """An installation's peak power in Wp: its panels' Wp times their count."""
    return (installation.get(CONF_EXTRA_PV_PANEL_WP) or 0) * (
        installation.get(CONF_EXTRA_PV_PANEL_COUNT) or 0
    )


def extra_pv_options(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """The entry's options with its PV installations folded in, as the rest
    of the integration reads them: their power and string sensors listed
    together, their peak power added up, their switches listed.

    Without installations the options as they are (an entry not migrated
    yet still has its one installation there).
    """
    installations = pv_installations(entry)
    options = dict(entry.options)
    if not installations:
        return options

    def joined(entities: Any) -> list[str]:
        return list(dict.fromkeys(e for group in entities for e in group or [] if e))

    options[CONF_EXTRA_PV_POWER_ENTITIES] = joined(
        extra_pv_power_entities(hass, i) for i in installations
    )
    options[CONF_EXTRA_PV_ENERGY_ENTITIES] = joined(
        [i.get(CONF_EXTRA_PV_ENERGY_ENTITY)] for i in installations
    )
    options[CONF_EXTRA_PV_STRING_ENTITIES] = joined(
        extra_pv_string_entities(hass, i) for i in installations
    )
    options[CONF_EXTRA_PV_POWER_CAPACITY] = sum(map(pv_installation_capacity, installations))
    options[CONF_EXTRA_PV_SWITCH_ENTITIES] = joined(
        [i.get(CONF_EXTRA_PV_SWITCH_ENTITY)] for i in installations
    )
    return options


# The installation's settings in the options before the subentries.
_INSTALLATION_OPTION_KEYS = (
    CONF_EXTRA_PV_POWER_ENTITIES,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_EXTRA_PV_STRING_ENTITIES,
    CONF_EXTRA_PV_PANEL_WP,
    CONF_EXTRA_PV_PANEL_COUNT,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_SWITCH_ENTITY,
)


def installation_from_options(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Split the one installation from options saved before the subentries:
    its settings (None when there was none) and the options left.

    The old Modbus hub settings stay in the options.
    """
    power = extra_pv_power_entities(hass, options)
    switch = options.get(CONF_EXTRA_PV_SWITCH_ENTITY)
    rest = {k: v for k, v in options.items() if k not in _INSTALLATION_OPTION_KEYS}
    if not (power or switch or options.get(CONF_EXTRA_PV_MODBUS_HUB)):
        return None, rest
    installation: dict[str, Any] = {
        # One sensor in the single field; an old SMA source's strings summed.
        **(
            {CONF_EXTRA_PV_POWER_ENTITY: power[0]}
            if len(power) == 1
            else {CONF_EXTRA_PV_POWER_ENTITIES: power}
        ),
        CONF_EXTRA_PV_STRING_ENTITIES: list(options.get(CONF_EXTRA_PV_STRING_ENTITIES) or []),
        CONF_EXTRA_PV_PANEL_WP: options.get(CONF_EXTRA_PV_PANEL_WP, 0),
        CONF_EXTRA_PV_PANEL_COUNT: options.get(CONF_EXTRA_PV_PANEL_COUNT, 0),
    }
    if switch:
        installation[CONF_EXTRA_PV_SWITCH_ENTITY] = switch
    return installation, rest
