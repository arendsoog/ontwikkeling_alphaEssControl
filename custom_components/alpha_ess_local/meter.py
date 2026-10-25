"""The smart (P1) meter: which sensors give the grid power and the tariff.

Each is its own sensor, picked in the options, so any P1 integration works
(DSMR, HomeWizard, DSMR Reader, P1 Monitor, ESPHome, ...):
- the grid import power -- the net power (positive = import, negative =
  export) when there's no separate export sensor;
- the grid export power, for meters that give import and export apart;
- the active tariff, for the day/night network fee.

Installations set up before had one "house load source": a sensor with the
net power, or "dsmr:<config entry>" for DSMR's import/export pair (and its
tariff); that's read the same way until the meter options are saved again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_GRID_EXPORT_POWER_ENTITY,
    CONF_GRID_IMPORT_POWER_ENTITY,
    CONF_GRID_TARIFF_ENTITY,
    CONF_HOUSE_LOAD_POWER_ENTITY,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
)

# HA core's stable unique_id suffixes of the sensors auto-detected for a
# new installation's meter options (verified against the dsmr and
# homewizard integrations).
_DSMR_KEYS = {
    CONF_GRID_IMPORT_POWER_ENTITY: "_current_electricity_usage",
    CONF_GRID_EXPORT_POWER_ENTITY: "_current_electricity_delivery",
    CONF_GRID_TARIFF_ENTITY: "_electricity_active_tariff",
    # Belgian "5B" meters only: the month's highest 15-minute peak.
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "_belgium_maximum_demand_current_month",
}
_HOMEWIZARD_KEYS = {
    CONF_GRID_IMPORT_POWER_ENTITY: "_active_power_w",
    CONF_GRID_TARIFF_ENTITY: "_active_tariff",
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "_monthly_power_peak_w",
}


@dataclass(frozen=True)
class GridMeter:
    """The configured meter's sensors."""

    import_entity: str
    export_entity: str | None = None
    tariff_entity: str | None = None

    @property
    def power_entities(self) -> list[str]:
        """The sensors the net grid power is computed from."""
        return [entity for entity in (self.import_entity, self.export_entity) if entity]


def _sensors_by_suffix(
    hass: HomeAssistant, config_entry_id: str, keys: Mapping[str, str]
) -> dict[str, str]:
    """{option: entity_id} for a config entry's sensors matching `keys`."""
    found: dict[str, str] = {}
    for reg_entry in er.async_entries_for_config_entry(er.async_get(hass), config_entry_id):
        if reg_entry.domain != "sensor" or reg_entry.disabled_by:
            continue
        for option, suffix in keys.items():
            if reg_entry.unique_id.endswith(suffix):
                found.setdefault(option, reg_entry.entity_id)
    return found


def configured_grid_meter(hass: HomeAssistant, options: Mapping[str, Any]) -> GridMeter | None:
    """The configured meter, or None without one (the inverter's own grid
    reading is used then)."""
    if CONF_GRID_IMPORT_POWER_ENTITY in options or not options.get(CONF_HOUSE_LOAD_POWER_ENTITY):
        import_entity = options.get(CONF_GRID_IMPORT_POWER_ENTITY)
        if not import_entity:
            return None
        return GridMeter(
            import_entity,
            options.get(CONF_GRID_EXPORT_POWER_ENTITY) or None,
            options.get(CONF_GRID_TARIFF_ENTITY) or None,
        )

    legacy = options[CONF_HOUSE_LOAD_POWER_ENTITY]
    if not legacy.startswith("dsmr:"):
        return GridMeter(legacy)
    sensors = _sensors_by_suffix(hass, legacy.removeprefix("dsmr:"), _DSMR_KEYS)
    if CONF_GRID_IMPORT_POWER_ENTITY not in sensors or CONF_GRID_EXPORT_POWER_ENTITY not in sensors:
        return None
    return GridMeter(
        sensors[CONF_GRID_IMPORT_POWER_ENTITY],
        sensors[CONF_GRID_EXPORT_POWER_ENTITY],
        sensors.get(CONF_GRID_TARIFF_ENTITY),
    )


def detected_meter_sensors(hass: HomeAssistant) -> dict[str, str]:
    """A DSMR or HomeWizard P1 meter's sensors, to pre-fill the meter
    options of an installation that has none yet: {option: entity_id}."""
    for domain, keys in (("dsmr", _DSMR_KEYS), ("homewizard", _HOMEWIZARD_KEYS)):
        for entry in hass.config_entries.async_entries(domain):
            sensors = _sensors_by_suffix(hass, entry.entry_id, keys)
            if CONF_GRID_IMPORT_POWER_ENTITY in sensors:
                return sensors
    return {}


def detected_peak_sensor(hass: HomeAssistant) -> str | None:
    """A meter's own "peak load this month" sensor (Belgian meters), if any."""
    for domain, keys in (("dsmr", _DSMR_KEYS), ("homewizard", _HOMEWIZARD_KEYS)):
        for entry in hass.config_entries.async_entries(domain):
            peak = _sensors_by_suffix(
                hass,
                entry.entry_id,
                {CONF_PEAK_LOAD_THIS_MONTH_ENTITY: keys[CONF_PEAK_LOAD_THIS_MONTH_ENTITY]},
            )
            if peak:
                return peak[CONF_PEAK_LOAD_THIS_MONTH_ENTITY]
    return None


# Tariff sensor states, by meaning: DSMR gives "normal"/"low"; others give
# a word or the meter's tariff number.
_TARIFF_WORDS = {
    "normal": "normal",
    "normaal": "normal",
    "high": "normal",
    "hoog": "normal",
    "day": "normal",
    "dag": "normal",
    "peak": "normal",
    "piek": "normal",
    "low": "low",
    "laag": "low",
    "night": "low",
    "nacht": "low",
    "off_peak": "low",
    "offpeak": "low",
    "dal": "low",
}


def tariff_from_state(value: str, country: str | None) -> str | None:
    """A tariff sensor's state as "normal"/"low", or None if unknown.

    A tariff number (HomeWizard's 1/2, a raw P1 "0001"/"0002") means the
    opposite in Belgium and the Netherlands: tariff 1 is the day (normal)
    tariff in Belgium, the low tariff in the Netherlands.
    """
    word = value.strip().lower().replace("-", "_").replace(" ", "_")
    if word in _TARIFF_WORDS:
        return _TARIFF_WORDS[word]
    try:
        number = int(float(word))
    except ValueError:
        return None
    if number not in (1, 2):
        return None
    if country == "BE":
        return "normal" if number == 1 else "low"
    return "low" if number == 1 else "normal"
