"""Sidebar panel for AlphaESSControl.

Everything panel-related lives in this folder: the web component
(`alpha-ess-panel.js`, plain JS — no build step) and the code below that
serves it and adds it to Home Assistant's sidebar.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from ..const import DOMAIN, LOGGER

PANEL_URL_PATH = "alpha-ess"
PANEL_COMPONENT = "alpha-ess-panel"
PANEL_FILE = Path(__file__).parent / f"{PANEL_COMPONENT}.js"
STATIC_URL = f"/{DOMAIN}/frontend"

_DATA_STATIC_REGISTERED = f"{DOMAIN}_panel_static_registered"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel's JS and add it to the sidebar (idempotent).

    Skipped when the frontend isn't loaded (e.g. in tests or a headless
    setup) — the integration itself works fine without the panel.
    """
    if hass.http is None or "frontend" not in hass.config.components:
        return
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return

    # A static path can't be unregistered again, so only add it once per
    # HA run, even if the panel is removed and re-added on entry reloads.
    if not hass.data.get(_DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(PANEL_FILE.parent), cache_headers=False)]
        )
        hass.data[_DATA_STATIC_REGISTERED] = True

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
