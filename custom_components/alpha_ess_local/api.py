"""Client for talking to an AlphaESS inverter/battery on the local network.

This is a scaffold: it establishes the client shape (errors, connection
handling, data shape) that the coordinator and config flow depend on.
Replace the body of `async_get_data` / `async_test_connection` with the
actual protocol calls (Modbus TCP via pymodbus, or the inverter's local
HTTP API) once that's confirmed against real hardware.
"""

from __future__ import annotations

import asyncio
import socket

import aiohttp


class AlphaEssLocalApiClientError(Exception):
    """General API client error."""


class AlphaEssLocalApiClientCommunicationError(AlphaEssLocalApiClientError):
    """Raised when the device cannot be reached."""


class AlphaEssLocalApiClientAuthenticationError(AlphaEssLocalApiClientError):
    """Raised when authentication with the device fails."""


class AlphaEssLocalApiClient:
    """Talks to a single AlphaESS device over the local network."""

    def __init__(
        self,
        host: str,
        port: int,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the API client."""
        self._host = host
        self._port = port
        self._session = session

    async def async_test_connection(self) -> None:
        """Verify that the device is reachable, raising on failure.

        Used by the config flow to validate user input before creating
        a config entry.
        """
        await self.async_get_data()

    async def async_get_data(self) -> dict:
        """Fetch the latest data from the device.

        TODO: replace this placeholder with the real Modbus/HTTP call(s)
        and map the raw registers/fields onto a stable dict of values,
        e.g. {"battery_soc": 42, "pv_power": 1234, "grid_power": -300}.
        """
        return await self._api_wrapper(
            method="get",
            # Local inverters/battery gateways typically only expose plain
            # HTTP (or Modbus TCP) on the LAN, not TLS.
            url=f"http://{self._host}:{self._port}/",  # NOSONAR
        )

    async def _api_wrapper(self, method: str, url: str, data: dict | None = None) -> dict:
        """Wrap network calls to normalize errors."""
        try:
            async with async_timeout.timeout(10):
                response = await self._session.request(
                    method=method,
                    url=url,
                    json=data,
                )
                if response.status in (401, 403):
                    raise AlphaEssLocalApiClientAuthenticationError(
                        "Invalid credentials"
                    )
                response.raise_for_status()
                return await response.json()

        except asyncio.TimeoutError as exception:
            raise AlphaEssLocalApiClientCommunicationError(
                f"Timeout communicating with {self._host}"
            ) from exception
        except (aiohttp.ClientError, socket.gaierror) as exception:
            raise AlphaEssLocalApiClientCommunicationError(
                f"Error communicating with {self._host}"
            ) from exception
        except Exception as exception:  # noqa: BLE001
            raise AlphaEssLocalApiClientError(
                f"Unexpected error communicating with {self._host}"
            ) from exception
