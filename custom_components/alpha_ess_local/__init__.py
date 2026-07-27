"""The AlphaESS Local Control integration."""

from __future__ import annotations

from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AlphaEssLocalApiClient
from .coordinator import AlphaEssLocalDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

AlphaEssLocalConfigEntry: TypeAlias = ConfigEntry[AlphaEssLocalDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """Set up AlphaESS Local Control from a config entry."""
    client = AlphaEssLocalApiClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        session=async_get_clientsession(hass),
    )

    coordinator = AlphaEssLocalDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
