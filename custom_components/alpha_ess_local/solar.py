"""Solar production forecast handling for AlphaESSControl.

Port of Solar.c. Unlike the original (NED.nl percentage forecast + our own
orientation/tilt correction), forecasts are read from other HA integrations
(Forecast.Solar primary, Solcast fallback) via HA's "energy platform"
cross-integration hook, which already returns absolute Wh estimates,
corrected for the panels' own configured orientation/tilt and local weather
— see homeassistant.components.energy.types.SolarForecastType. Each source
may have multiple config entries (e.g. one per roof orientation); those are
summed together before falling back to the next source.

The options flow's solar_forecast_entities is an ordered list of sensors,
one per location, each standing for its config entry: a source is an
integration (the locations of the same integration summed), tried in the
order its first location appears.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_SOURCE_PRIMARY,
    CONF_SOLCAST_ENTRIES,
    DEFAULT_SOLAR_SOURCE_PRIMARY,
    LOGGER,
    SOLAR_SOURCE_SOLCAST,
)
from .data import Day

# The integrations with a solar forecast (an energy platform's
# async_get_solar_forecast) offered in the options, by domain, with their
# display names.
SOLAR_SOURCES = {
    "forecast_solar": "Forecast.Solar",
    "solcast_solar": "Solcast",
    "open_meteo_solar_forecast": "Open-Meteo Solar Forecast",
}

# A source: its integration's domain and the config entries (locations)
# summed for it.
SolarSource = tuple[str, list[str]]


def solar_source_name(source: str) -> str:
    """A source's display name."""
    return SOLAR_SOURCES.get(source, source)


def configured_solar_sources(hass: HomeAssistant, options: Mapping[str, Any]) -> list[SolarSource]:
    """The solar forecast sources in the order they're tried.

    Installations set up before solar_forecast_entities existed still have
    the Forecast.Solar and Solcast location lists plus which goes first;
    those are read the same way until the solar forecast is saved again.
    """
    if CONF_SOLAR_FORECAST_ENTITIES not in options:
        legacy = [
            ("forecast_solar", list(options.get(CONF_FORECAST_SOLAR_ENTRIES) or [])),
            ("solcast_solar", list(options.get(CONF_SOLCAST_ENTRIES) or [])),
        ]
        if options.get(CONF_SOLAR_SOURCE_PRIMARY, DEFAULT_SOLAR_SOURCE_PRIMARY) == (
            SOLAR_SOURCE_SOLCAST
        ):
            legacy.reverse()
        return [(source, entry_ids) for source, entry_ids in legacy if entry_ids]

    registry = er.async_get(hass)
    sources: dict[str, list[str]] = {}
    for entity_id in options[CONF_SOLAR_FORECAST_ENTITIES] or []:
        reg_entry = registry.async_get(entity_id)
        entry = (
            hass.config_entries.async_get_entry(reg_entry.config_entry_id)
            if reg_entry is not None and reg_entry.config_entry_id
            else None
        )
        if entry is None:
            LOGGER.debug("Solar forecast: %s belongs to no location (any more)", entity_id)
            continue
        entry_ids = sources.setdefault(entry.domain, [])
        if entry.entry_id not in entry_ids:
            entry_ids.append(entry.entry_id)
    return list(sources.items())


async def get_solar_forecast(hass: HomeAssistant, config_entry_id: str) -> dict | None:
    """Call another integration's "energy platform" async_get_solar_forecast hook.

    Works for any integration implementing HA's SolarForecastType contract
    (not hardcoded to Forecast.Solar/Solcast specifically).
    """
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if entry is None:
        LOGGER.debug("Solar forecast: config entry %s not found", config_entry_id)
        return None

    try:
        integration = await async_get_integration(hass, entry.domain)
        platform = await integration.async_get_platform("energy")
    except (IntegrationNotFound, ImportError) as exception:
        LOGGER.warning(
            "Solar forecast: could not load energy platform for %s (%s)",
            entry.domain,
            exception,
        )
        return None

    if not hasattr(platform, "async_get_solar_forecast"):
        LOGGER.debug("Solar forecast: %s has no async_get_solar_forecast", entry.domain)
        return None

    try:
        # Right after a restart the entry may not be set up yet and its hook
        # raises (e.g. Forecast.Solar's missing runtime_data): no forecast
        # from it this time, rather than an error failing the whole refresh.
        result = await platform.async_get_solar_forecast(hass, config_entry_id)
    except Exception as exception:  # noqa: BLE001 -- another integration's code: never fail on it
        LOGGER.debug("Solar forecast: %s gave an error (%s)", entry.title, exception)
        return None
    if result is None:
        return None
    return result.get("wh_hours")


def _wh_hours_to_day_hours(wh_hours: dict, target_date: date) -> dict[int, float] | None:
    """Bucket/sum {iso_timestamp: wh} entries by local hour for one calendar date."""
    hours: dict[int, float] = {}
    for timestamp_str, wh in wh_hours.items():
        timestamp = dt_util.parse_datetime(timestamp_str)
        if timestamp is None:
            LOGGER.warning("Solar forecast: could not parse timestamp %r", timestamp_str)
            continue
        local = dt_util.as_local(timestamp)
        if local.date() != target_date:
            continue
        hours[local.hour] = hours.get(local.hour, 0.0) + float(wh)

    return hours or None


async def get_combined_solar_forecast(
    hass: HomeAssistant, config_entry_ids: list[str], target_date: date
) -> dict[int, float] | None:
    """Sum the hourly forecast across multiple config entries.

    A multi-orientation roof (e.g. east + west) needs one Forecast.Solar
    entry per plane; their per-hour forecasts are added together here to get
    the whole system's estimate. If a plane gives nothing (not loaded,
    unreachable), the source has no forecast at all -- half the roof would
    look like a dull day; the next source is tried instead.
    """
    combined: dict[int, float] = {}
    for config_entry_id in config_entry_ids:
        wh_hours = await get_solar_forecast(hass, config_entry_id)
        hours = _wh_hours_to_day_hours(wh_hours, target_date) if wh_hours else None
        if not hours:
            return None
        for hour, wh in hours.items():
            combined[hour] = combined.get(hour, 0.0) + wh

    return combined or None


async def read_solar_forecast(
    hass: HomeAssistant, sources: list[SolarSource], target_date: date
) -> dict[int, float] | None:
    """Port of ReadEstimatedSolarPower: the first source's forecast, else the next."""
    return (await read_solar_forecast_with_source(hass, sources, target_date))[0]


async def read_solar_forecast_with_source(
    hass: HomeAssistant, sources: list[SolarSource], target_date: date
) -> tuple[dict[int, float] | None, str]:
    """The forecast of the first source in `sources` that has one.

    Each source's locations are summed. Also returns which source the
    forecast came from ("" if none had one).
    """
    for source, entry_ids in sources:
        if not entry_ids:
            continue
        hours = await get_combined_solar_forecast(hass, entry_ids, target_date)
        if hours:
            LOGGER.debug(
                "Found estimated solar power for %s (using %s)",
                target_date,
                solar_source_name(source),
            )
            return hours, source
        LOGGER.debug("No %s estimate for %s", solar_source_name(source), target_date)

    return None, ""


async def build_day(
    hass: HomeAssistant,
    target_date: date,
    sources: list[SolarSource],
    hour_correction: dict[int, tuple[float, float]] | None = None,
) -> Day:
    """Build a Day of solar estimates for target_date from the configured sources.

    `hour_correction` is an optional {hour: (factor, offset)} map — the
    locally-learned regression correction from storage.py's
    calculate_and_store_mean_data, applied on top of the raw source estimate
    as `raw * factor + offset` (skipped when factor < 0, i.e. not yet
    reliable). Nothing passes this yet; it's inert until a later phase wires
    it through from storage.
    """
    day = Day(year=target_date.year, mon=target_date.month, day=target_date.day)

    hours, day.source = await read_solar_forecast_with_source(hass, sources, target_date)
    if not hours:
        return day

    for hour_index, wh in hours.items():
        day.hour[hour_index].valid = True
        day.hour[hour_index].estimated_solar_power_raw = wh
        correction = hour_correction.get(hour_index) if hour_correction else None
        if correction is not None and correction[0] >= 0:
            factor, offset = correction
            day.hour[hour_index].estimated_solar_power = round(wh * factor + offset)
        else:
            day.hour[hour_index].estimated_solar_power = round(wh)

    day.valid = True
    return day
