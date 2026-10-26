"""The extra energy source next to the AlphaESS (a second solar installation, ...).

Picked in the options by its sensors and a switch:
- its power: one or more power sensors, summed (a second inverter, micro
  inverters, or an inverter's power per string);
- its power per string, MPPT or phase, for the panel only;
- a switch that turns it off at negative prices (on = producing): any
  switch, e.g. a Modbus switch writing the inverter's power limit.

Installations set up before had one power source -- a sensor, or
"sma:<config entry>" for the SMA integration's per-string sensors summed --
and the switching as a Modbus hub, slave, register and on/off values. Both
are read the same way until the section is saved again (the hub until a
switch is picked).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EXTRA_PV_POWER_ENTITIES,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_EXTRA_PV_STRING_ENTITIES,
    CONF_EXTRA_PV_SWITCH_ENTITY,
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


def extra_pv_switch(options: Mapping[str, Any]) -> str | None:
    """The switch that turns the extra source off at negative prices."""
    return options.get(CONF_EXTRA_PV_SWITCH_ENTITY) or None
