"""Tests for the AlphaESSControl config flow."""

from unittest.mock import AsyncMock

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.alpha_ess_local.api import (
    AlphaEssLocalApiClientCommunicationError,
)
from custom_components.alpha_ess_local.const import DOMAIN


async def test_user_flow_success(hass: HomeAssistant, mock_api_client) -> None:
    """A valid host/port creates a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "192.168.1.50", CONF_PORT: 502},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "192.168.1.50"
    assert result["data"] == {CONF_HOST: "192.168.1.50", CONF_PORT: 502}


async def test_user_flow_cannot_connect(hass: HomeAssistant, mock_api_client) -> None:
    """A connection error is surfaced back on the form instead of crashing."""
    mock_api_client.async_test_connection = AsyncMock(
        side_effect=AlphaEssLocalApiClientCommunicationError("boom")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "192.168.1.50", CONF_PORT: 502},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
