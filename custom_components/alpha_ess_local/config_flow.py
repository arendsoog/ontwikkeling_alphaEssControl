"""Config flow for AlphaESS Local Control."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    AlphaEssLocalApiClient,
    AlphaEssLocalApiClientAuthenticationError,
    AlphaEssLocalApiClientCommunicationError,
    AlphaEssLocalApiClientError,
)
from .const import DEFAULT_PORT, DOMAIN, LOGGER

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class AlphaEssLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AlphaESS Local Control."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step, entered from the "Add integration" UI."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()

            try:
                await self._async_test_credentials(
                    host=user_input[CONF_HOST],
                    port=user_input[CONF_PORT],
                )
            except AlphaEssLocalApiClientAuthenticationError:
                errors["base"] = "invalid_auth"
            except AlphaEssLocalApiClientCommunicationError:
                errors["base"] = "cannot_connect"
            except AlphaEssLocalApiClientError:
                LOGGER.exception("Unexpected error validating AlphaESS connection")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_HOST],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_test_credentials(self, host: str, port: int) -> None:
        """Validate that the device is reachable at host:port."""
        session = async_create_clientsession(self.hass)
        client = AlphaEssLocalApiClient(host=host, port=port, session=session)
        await client.async_test_connection()
