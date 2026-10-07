"""Sidebar panel for AlphaESSControl.

Everything panel-related lives in this folder: the web component
(`alpha-ess-panel.js`, plain JS — no build step) and the code below that
serves it, adds it to Home Assistant's sidebar, and answers the panel's
`alpha_ess_local/panel_status` websocket command (integration state that
isn't exposed as an entity, such as the options and a manual override).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .. import storage
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
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_INVERTER_NOMINAL_POWER,
    CONF_NETWORK_USE_FEE_LOW,
    CONF_NETWORK_USE_FEE_NORMAL,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_POWER,
    CONF_SOLCAST_ENTRIES,
    CONF_USABLE_BATTERY_CAPACITY,
    CONF_VAT_PERCENTAGE,
    DEFAULT_APPLY_VAT_ON_RETURN,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_CONTROL_ENABLED,
    DEFAULT_NETWORK_USE_FEE,
    DEFAULT_PERSIST_DAILY_CHARGE_LIMIT,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
)
from ..data import MAX_FIVE_MINS, Day
from ..orchestrator import get_db_path
from ..schedule import set_charging_msg

PANEL_URL_PATH = "alpha-ess"
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
    override = entry.runtime_data.dispatch_coordinator.manual_override
    if options.get(CONF_FRANK_ENERGIE_PRICE_ENTITY):
        price_source = "Frank Energie"
    elif options.get(CONF_ENTSOE_PRICE_ENTITY):
        price_source = "ENTSO-E"
    else:
        price_source = None

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
            "extra_pv_power_capacity": options.get(CONF_EXTRA_PV_POWER_CAPACITY, 0),
            "extra_pv_modbus_hub": options.get(CONF_EXTRA_PV_MODBUS_HUB) or None,
            "manual_override": None if override is None else set_charging_msg(override),
            "price_source": price_source,
            "solar_forecast_sources": len(options.get(CONF_FORECAST_SOLAR_ENTRIES) or [])
            + len(options.get(CONF_SOLCAST_ENTRIES) or []),
        },
    )


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
    summary = await hass.async_add_executor_job(
        storage.retrieve_period_summary,
        get_db_path(hass, entry),
        msg["start"],
        msg["end"],
        msg["group"],
        vat,
        return_vat,
    )
    connection.send_result(msg["id"], summary)


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
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
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

    # The file's mtime as a cache-buster, so the browser picks up a changed
    # panel after a restart instead of serving its cached copy.
    mtime = int(await hass.async_add_executor_job(lambda: PANEL_FILE.stat().st_mtime))
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        module_url=f"{STATIC_URL}/{PANEL_FILE.name}?v={mtime}",
        sidebar_title="AlphaESS",
        sidebar_icon="mdi:home-battery",
        require_admin=False,
    )
    LOGGER.debug("Registered sidebar panel at /%s", PANEL_URL_PATH)


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the panel from the sidebar."""
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
