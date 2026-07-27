"""Fixtures shared by the AlphaESS Local Control tests."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components discoverable, per pytest-homeassistant-custom-component."""
    return


@pytest.fixture
def mock_api_client():
    """Mock the API client as used by the config flow, so tests don't hit the network.

    Patched at the config_flow module's import site (`from .api import
    AlphaEssLocalApiClient`), not in api.py itself, since that's the name
    actually looked up when the flow instantiates the client.
    """
    with patch(
        "custom_components.alpha_ess_local.config_flow.AlphaEssLocalApiClient",
        autospec=True,
    ) as mock_client:
        client = mock_client.return_value
        client.async_test_connection = AsyncMock(return_value=None)
        client.async_get_data = AsyncMock(
            return_value={"battery_soc": 42, "pv_power": 1234, "grid_power": -300}
        )
        yield client
