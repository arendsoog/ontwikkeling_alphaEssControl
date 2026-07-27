"""DataUpdateCoordinator for AlphaESS Local Control."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AlphaEssLocalApiClient,
    AlphaEssLocalApiClientAuthenticationError,
    AlphaEssLocalApiClientError,
)
from .const import DOMAIN, LOGGER, SCAN_INTERVAL


class AlphaEssLocalDataUpdateCoordinator(DataUpdateCoordinator):
    """Polls the AlphaESS device on a fixed interval."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: AlphaEssLocalApiClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            return await self.client.async_get_data()
        except AlphaEssLocalApiClientAuthenticationError as exception:
            raise UpdateFailed(exception) from exception
        except AlphaEssLocalApiClientError as exception:
            raise UpdateFailed(exception) from exception
