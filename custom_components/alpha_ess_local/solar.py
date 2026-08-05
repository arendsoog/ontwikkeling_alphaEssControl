"""Solar production forecast handling for AlphaESS Local Control.

Port of Solar.c. Unlike the original (NED.nl percentage forecast + our own
orientation/tilt correction), forecasts are read from other HA integrations
(Forecast.Solar primary, Solcast fallback) via HA's "energy platform"
cross-integration hook, which already returns absolute Wh estimates,
corrected for the panels' own configured orientation/tilt and local weather
— see homeassistant.components.energy.types.SolarForecastType. Each source
may have multiple config entries (e.g. one per roof orientation); those are
summed together before the Forecast.Solar-vs-Solcast fallback is applied.
"""

from __future__ import annotations

from datetime import date

from homeassistant.core import HomeAssistant
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from .const import LOGGER
from .data import Day


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

    result = await platform.async_get_solar_forecast(hass, config_entry_id)
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
    the whole system's estimate.
    """
    combined: dict[int, float] = {}
    found_any = False
    for config_entry_id in config_entry_ids:
        wh_hours = await get_solar_forecast(hass, config_entry_id)
        hours = _wh_hours_to_day_hours(wh_hours, target_date) if wh_hours else None
        if not hours:
            continue
        found_any = True
        for hour, wh in hours.items():
            combined[hour] = combined.get(hour, 0.0) + wh

    return combined if found_any else None


async def read_solar_forecast(
    hass: HomeAssistant,
    forecast_solar_entry_ids: list[str],
    solcast_entry_ids: list[str],
    target_date: date,
) -> dict[int, float] | None:
    """Port of ReadEstimatedSolarPower: try Forecast.Solar planes, fall back to Solcast."""
    if forecast_solar_entry_ids:
        hours = await get_combined_solar_forecast(hass, forecast_solar_entry_ids, target_date)
        if hours:
            LOGGER.debug("Found estimated solar power for %s (using Forecast.Solar)", target_date)
            return hours
        LOGGER.debug("No Forecast.Solar estimate for %s", target_date)

    if solcast_entry_ids:
        hours = await get_combined_solar_forecast(hass, solcast_entry_ids, target_date)
        if hours:
            LOGGER.debug("Found estimated solar power for %s (using Solcast)", target_date)
            return hours
        LOGGER.debug("No Solcast estimate for %s", target_date)

    return None


async def build_day(
    hass: HomeAssistant,
    target_date: date,
    forecast_solar_entry_ids: list[str],
    solcast_entry_ids: list[str],
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

    hours = await read_solar_forecast(
        hass, forecast_solar_entry_ids, solcast_entry_ids, target_date
    )
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
