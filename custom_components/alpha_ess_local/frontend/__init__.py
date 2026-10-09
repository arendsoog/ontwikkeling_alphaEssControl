"""Sidebar panel for AlphaESSControl.

Everything panel-related lives in this folder: the web component
(`alpha-ess-panel.js`, plain JS — no build step) and the code below that
serves it, adds it to Home Assistant's sidebar, and answers the panel's
`alpha_ess_local/panel_status` websocket command (integration state that
isn't exposed as an entity, such as the options and a manual override).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .. import storage
from ..config_flow import SMA_PV_POWER_STRING_KEY
from ..const import (
    CONF_APPLY_VAT_ON_RETURN,
    CONF_CONTROL_ENABLED,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_EV_CHARGER_ENERGY_ENTITY,
    CONF_EV_CHARGER_POWER_ENTITY,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_EXTRA_PV_STRING_ENTITIES,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_INVERTER_NOMINAL_POWER,
    CONF_NETWORK_USE_FEE_LOW,
    CONF_NETWORK_USE_FEE_NORMAL,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    CONF_PRICE_SOURCE_PRIMARY,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_POWER,
    CONF_SOLAR_SOURCE_PRIMARY,
    CONF_SOLCAST_ENTRIES,
    CONF_USABLE_BATTERY_CAPACITY,
    CONF_VAT_PERCENTAGE,
    DEFAULT_APPLY_VAT_ON_RETURN,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_CONTROL_ENABLED,
    DEFAULT_NETWORK_USE_FEE,
    DEFAULT_PERSIST_DAILY_CHARGE_LIMIT,
    DEFAULT_PRICE_SOURCE_PRIMARY,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_SOLAR_SOURCE_PRIMARY,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
    PRICE_SOURCE_ENTSOE,
    PRICE_SOURCE_FRANK_ENERGIE,
    SOLAR_SOURCE_FORECAST_SOLAR,
    SOLAR_SOURCE_SOLCAST,
)
from ..data import MAX_FIVE_MINS, Charge, Day
from ..orchestrator import get_db_path
from ..prices import PRICE_SOURCES
from ..schedule import set_charging_msg
from ..solar import SOLAR_SOURCES

# Specific enough not to collide with a user's own dashboard: a Lovelace
# dashboard named "AlphaESS" readily gets "alpha-ess" as its URL, which made
# the panel silently not register at all.
PANEL_URL_PATH = "alpha-ess-control"
PANEL_TITLE = "AlphaESS Control"
PANEL_COMPONENT = "alpha-ess-panel"
PANEL_FILE = Path(__file__).parent / f"{PANEL_COMPONENT}.js"
STATIC_URL = f"/{DOMAIN}/frontend"

_DATA_REGISTERED = f"{DOMAIN}_panel_registered"


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/panel_status"})
@callback
def _ws_panel_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Integration state the panel shows but that has no entity of its own."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_result(msg["id"], None)
        return

    entry = entries[0]
    options = entry.options
    runtime = entry.runtime_data
    override = runtime.dispatch_coordinator.manual_override
    price_sources = source_list(
        PRICE_SOURCES,
        {
            PRICE_SOURCE_ENTSOE: options.get(CONF_ENTSOE_PRICE_ENTITY),
            PRICE_SOURCE_FRANK_ENERGIE: options.get(CONF_FRANK_ENERGIE_PRICE_ENTITY),
        },
        options.get(CONF_PRICE_SOURCE_PRIMARY, DEFAULT_PRICE_SOURCE_PRIMARY),
        _today_source(runtime.prices_coordinator),
    )
    solar_sources = source_list(
        SOLAR_SOURCES,
        {
            SOLAR_SOURCE_FORECAST_SOLAR: _entry_titles(
                hass, options.get(CONF_FORECAST_SOLAR_ENTRIES)
            ),
            SOLAR_SOURCE_SOLCAST: _entry_titles(hass, options.get(CONF_SOLCAST_ENTRIES)),
        },
        options.get(CONF_SOLAR_SOURCE_PRIMARY, DEFAULT_SOLAR_SOURCE_PRIMARY),
        _today_source(runtime.solar_coordinator),
    )

    connection.send_result(
        msg["id"],
        {
            "control_enabled": options.get(CONF_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED),
            "extra_pv_control_enabled": options.get(
                CONF_EXTRA_PV_CONTROL_ENABLED, DEFAULT_EXTRA_PV_CONTROL_ENABLED
            ),
            "persist_daily_charge_limit": options.get(
                CONF_PERSIST_DAILY_CHARGE_LIMIT, DEFAULT_PERSIST_DAILY_CHARGE_LIMIT
            ),
            "usable_battery_capacity": options.get(CONF_USABLE_BATTERY_CAPACITY, 0),
            "inverter_nominal_power": options.get(CONF_INVERTER_NOMINAL_POWER, 0),
            "pv_power": options.get(CONF_PV_POWER, 0),
            # Price settings, so the panel can show all-in prices the same
            # way prices.mk_use_price/mk_return_price compute them.
            "use_fee": options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE),
            "return_fee": options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE),
            "vat_percentage": options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE),
            "apply_vat_on_return": options.get(
                CONF_APPLY_VAT_ON_RETURN, DEFAULT_APPLY_VAT_ON_RETURN
            ),
            "network_use_fee_normal": options.get(
                CONF_NETWORK_USE_FEE_NORMAL, DEFAULT_NETWORK_USE_FEE
            ),
            "network_use_fee_low": options.get(CONF_NETWORK_USE_FEE_LOW, DEFAULT_NETWORK_USE_FEE),
            "ev_charger_power_entity": options.get(CONF_EV_CHARGER_POWER_ENTITY) or None,
            "ev_charger_energy_entity": options.get(CONF_EV_CHARGER_ENERGY_ENTITY) or None,
            "extra_pv_configured": bool(options.get(CONF_EXTRA_PV_POWER_ENTITY)),
            "extra_pv_string_entities": extra_pv_string_entities(hass, options),
            "extra_pv_power_capacity": options.get(CONF_EXTRA_PV_POWER_CAPACITY, 0),
            "extra_pv_modbus_hub": options.get(CONF_EXTRA_PV_MODBUS_HUB) or None,
            "manual_override": None if override is None else set_charging_msg(override),
            "price_sources": price_sources,
            "solar_sources": solar_sources,
        },
    )


def _today_source(coordinator: Any) -> str:
    """Which source today's prices/forecast were read from ("" if none)."""
    data = coordinator.data or {}
    today = data.get("today")
    return today.source if today is not None else ""


def source_list(
    names: Mapping[str, str], configured: Mapping[str, Any], primary: str, active: str
) -> list[dict[str, Any]]:
    """The configured sources in the order they're tried, for the panel.

    `configured` maps each source to its setting: an entity_id, or a list
    of location names, shown after the name when there's more than one
    (those locations' forecasts are summed).
    """
    result = []
    for source in sorted(names, key=lambda s: s != primary):
        setting = configured.get(source)
        if not setting:
            continue
        name = names[source]
        if isinstance(setting, list) and len(setting) > 1:
            name = f"{name} ({' + '.join(setting)})"
        result.append({"name": name, "active": source == active})
    return result


def _entry_titles(hass: HomeAssistant, entry_ids: list[str] | None) -> list[str]:
    """The titles of the given config entries (skipping ones that are gone)."""
    titles = []
    for entry_id in entry_ids or []:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None:
            titles.append(entry.title)
    return titles


def extra_pv_string_entities(hass: HomeAssistant, options: Mapping[str, Any]) -> list[str]:
    """The extra PV installation's power-per-string sensors, for the panel.

    The ones picked in the options, else -- when the extra PV comes from the
    SMA integration (its "sma:<entry>" sum, or one of its own sensors) --
    that integration's enabled per-string power sensors (A, B, ...).
    """
    configured = options.get(CONF_EXTRA_PV_STRING_ENTITIES)
    if configured:
        return list(configured)
    source = options.get(CONF_EXTRA_PV_POWER_ENTITY)
    if not source:
        return []
    registry = er.async_get(hass)
    if source.startswith("sma:"):
        sma_entry_id = source.removeprefix("sma:")
    else:
        reg_entry = registry.async_get(source)
        if reg_entry is None or reg_entry.platform != "sma":
            return []
        sma_entry_id = reg_entry.config_entry_id
    strings = [
        reg_entry
        for reg_entry in er.async_entries_for_config_entry(registry, sma_entry_id)
        if f"-{SMA_PV_POWER_STRING_KEY}_" in reg_entry.unique_id and not reg_entry.disabled
    ]
    return [reg_entry.entity_id for reg_entry in sorted(strings, key=lambda e: e.unique_id)]


def today_power_payload(day: Day, now: datetime) -> dict[str, Any] | None:
    """Today's 5-minute power samples and per-hour energy, for the panel.

    Comes from the real-data coordinator's own in-memory Day rather than
    the recorder, so the charts work even when the raw power sensors are
    excluded from the recorder (as they are on this installation, to keep
    the database small). Sample i of hour h is stamped at h:00 + 5*i min.
    The in-progress hour's energy is weighted by how much of it has been
    sampled, so it isn't counted as a full hour yet.
    """
    if not day.valid:
        return None
    midnight = dt_util.start_of_local_day(date(day.year, day.mon, day.day))
    samples: list[dict[str, float]] = []
    hours: list[dict[str, float]] = []
    for h, hour in enumerate(day.hour):
        if not hour.valid:
            continue
        hour_start = midnight + timedelta(hours=h)
        if hour.five_min_count == 0:
            # A completed hour restored from storage without its individual
            # samples (stored before five_min_data existed): show its hour
            # averages for PV and house, flat across the hour. Grid and
            # battery aren't kept per hour, so they're left out (None).
            for minutes in (0, 55):
                samples.append(
                    {
                        "t": (hour_start + timedelta(minutes=minutes)).timestamp() * 1000,
                        "pv_roof": hour.real_solar_power_roof,
                        "extra_pv": hour.real_extra_pv_power,
                        "grid": None,
                        "battery": None,
                        "house": hour.real_house_load,
                    }
                )
        for i, sample in enumerate(hour.five_min[: hour.five_min_count]):
            samples.append(
                {
                    "t": (hour_start + timedelta(minutes=5 * i)).timestamp() * 1000,
                    "pv_roof": sample.real_solar_power_roof,
                    "extra_pv": sample.real_extra_pv_power,
                    "grid": sample.total_active_power,
                    "battery": sample.battery_power,
                    "house": sample.real_house_load,
                }
            )
        in_progress = hour_start <= now < hour_start + timedelta(hours=1)
        weight = hour.five_min_count / MAX_FIVE_MINS if in_progress else 1.0
        hours.append(
            {
                "hour": h,
                "solar_roof_wh": hour.real_solar_power_roof * weight,
                "extra_pv_wh": hour.real_extra_pv_power * weight,
                "house_wh": hour.real_house_load * weight,
            }
        )
    return {
        "samples": samples,
        "hours": hours,
        "solar_roof_wh": sum(h["solar_roof_wh"] for h in hours),
        "extra_pv_wh": sum(h["extra_pv_wh"] for h in hours),
    }


# Boolean options the panel's control tab may toggle directly. Anything
# else stays in the options flow, where it's validated as a whole.
SETTABLE_OPTIONS = (
    CONF_CONTROL_ENABLED,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_option",
        vol.Required("key"): vol.In(SETTABLE_OPTIONS),
        vol.Required("value"): bool,
    }
)
@websocket_api.require_admin
@callback
def _ws_set_option(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Toggle one boolean option; the entry's update listener reloads it."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "AlphaESSControl is not loaded")
        return
    entry = entries[0]
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, msg["key"]: msg["value"]}
    )
    connection.send_result(msg["id"], {msg["key"]: msg["value"]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Required("start"): cv.date,
        vol.Required("end"): cv.date,
        vol.Required("group"): vol.In(storage.PERIOD_GROUPS),
    }
)
@websocket_api.async_response
async def _ws_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Stored energy and savings for a date range, grouped by hour/day/month.

    Read from the integration's own per-hour database (kept indefinitely),
    with the same savings maths as the "yesterday savings" sensor.
    """
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "AlphaESSControl is not loaded")
        return
    entry = entries[0]
    options = entry.options
    vat = options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE)
    return_vat = vat if options.get(CONF_APPLY_VAT_ON_RETURN, DEFAULT_APPLY_VAT_ON_RETURN) else 0.0
    db_path = get_db_path(hass, entry)
    summary = await hass.async_add_executor_job(
        storage.retrieve_period_summary,
        db_path,
        msg["start"],
        msg["end"],
        msg["group"],
        vat,
        return_vat,
    )
    if msg["group"] == "hour":
        summary.update(await hass.async_add_executor_job(day_details, db_path, msg["start"]))
    connection.send_result(msg["id"], summary)


def day_details(db_path: str, day: date) -> dict[str, Any]:
    """One day's decision log and 5-minute samples, for the history tab.

    The samples are only kept for a few days (storage.FIVE_MIN_KEEP_DAYS);
    for older days the panel draws the hourly averages instead.
    """
    decisions = storage.retrieve_day_decisions(db_path, day.year, day.month, day.day)
    for decision in decisions:
        charge = decision.pop("charge")
        decision["action"] = None if charge is None else set_charging_msg(Charge(charge))
        decision["written"] = bool(decision["written"])
        decision["manual"] = bool(decision["manual"])
        decision["multiple"] = bool(decision["multiple"])
    five_min = storage.retrieve_day_five_min(db_path, day.year, day.month, day.day)
    midnight = dt_util.start_of_local_day(day)
    samples = [
        {
            "t": (midnight + timedelta(hours=hour, minutes=5 * slot)).timestamp() * 1000,
            "pv_roof": sample.real_solar_power_roof,
            "extra_pv": sample.real_extra_pv_power,
            "house": sample.real_house_load,
        }
        for hour, slots in sorted(five_min.items())
        for slot, sample in sorted(slots.items())
    ]
    return {"decisions": decisions, "samples": samples}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/today_power"})
@callback
def _ws_today_power(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Today's power samples from the first loaded entry's real-data coordinator."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    data = entries[0].runtime_data.real_data_coordinator.data if entries else None
    connection.send_result(
        msg["id"], None if data is None else today_power_payload(data.day, dt_util.now())
    )


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel's JS and add it to the sidebar (idempotent).

    Skipped when the frontend isn't loaded (e.g. in tests or a headless
    setup) — the integration itself works fine without the panel.
    """
    if hass.http is None or "frontend" not in hass.config.components:
        return

    # A static path and a websocket command can't be unregistered again,
    # so only add them once per HA run, even if the panel is removed and
    # re-added on entry reloads.
    if not hass.data.get(_DATA_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(PANEL_FILE.parent), cache_headers=False)]
        )
        websocket_api.async_register_command(hass, _ws_panel_status)
        websocket_api.async_register_command(hass, _ws_today_power)
        websocket_api.async_register_command(hass, _ws_set_option)
        websocket_api.async_register_command(hass, _ws_history)
        hass.data[_DATA_REGISTERED] = True

    existing = hass.data.get(frontend.DATA_PANELS, {}).get(PANEL_URL_PATH)
    if existing is not None:
        if not _is_our_panel(existing):
            LOGGER.warning(
                "Sidebar panel not added: /%s is already used by another panel or "
                "dashboard (%s). Change that dashboard's URL to get the AlphaESS panel",
                PANEL_URL_PATH,
                existing.sidebar_title or existing.component_name,
            )
        return

    # The file's mtime as a cache-buster, so the browser picks up a changed
    # panel after a restart instead of serving its cached copy.
    mtime = int(await hass.async_add_executor_job(lambda: PANEL_FILE.stat().st_mtime))
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        module_url=f"{STATIC_URL}/{PANEL_FILE.name}?v={mtime}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon="mdi:home-battery",
        require_admin=False,
    )
    LOGGER.info("Added the %s sidebar panel at /%s", PANEL_TITLE, PANEL_URL_PATH)


def _is_our_panel(panel: frontend.Panel) -> bool:
    """Whether a registered panel is this integration's (not a dashboard)."""
    custom = (panel.config or {}).get("_panel_custom", {})
    return panel.component_name == "custom" and custom.get("name") == PANEL_COMPONENT


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the panel from the sidebar -- only if it is ours."""
    existing = hass.data.get(frontend.DATA_PANELS, {}).get(PANEL_URL_PATH)
    if existing is not None and _is_our_panel(existing):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
