"""Sidebar panel for AlphaESSControl.

Everything panel-related lives in this folder: the web component
(`alpha-ess-panel.js`, plain JS — no build step) and the code below that
serves it, adds it to Home Assistant's sidebar, and answers the panel's
`alpha_ess_local/panel_status` websocket command (integration state that
isn't exposed as an entity, such as the options and a manual override).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from ..const import (
    CONF_CONTROL_ENABLED,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_INVERTER_NOMINAL_POWER,
    CONF_PV_POWER,
    CONF_SOLCAST_ENTRIES,
    CONF_USABLE_BATTERY_CAPACITY,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_CONTROL_ENABLED,
    DOMAIN,
    LOGGER,
)
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
            "usable_battery_capacity": options.get(CONF_USABLE_BATTERY_CAPACITY, 0),
            "inverter_nominal_power": options.get(CONF_INVERTER_NOMINAL_POWER, 0),
            "pv_power": options.get(CONF_PV_POWER, 0),
            "extra_pv_configured": bool(options.get(CONF_EXTRA_PV_POWER_ENTITY)),
            "extra_pv_power_capacity": options.get(CONF_EXTRA_PV_POWER_CAPACITY, 0),
            "extra_pv_modbus_hub": options.get(CONF_EXTRA_PV_MODBUS_HUB) or None,
            "manual_override": None if override is None else set_charging_msg(override),
            "price_source": price_source,
            "solar_forecast_sources": len(options.get(CONF_FORECAST_SOLAR_ENTRIES) or [])
            + len(options.get(CONF_SOLCAST_ENTRIES) or []),
        },
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
