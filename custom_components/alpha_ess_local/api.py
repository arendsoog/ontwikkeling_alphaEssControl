"""Modbus TCP client for an AlphaESS inverter/battery on the local network.

Port of AlphaESS.c/AlphaESS.h. Uses pymodbus's async TCP client to talk to
the inverter directly (function codes 3/16, holding registers), preserving
the original's register map, retry behavior, and on-wire encoding quirks
(see protocol.py).
"""

from __future__ import annotations

import asyncio

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

from . import protocol
from .protocol import DispatchParam

RESPONSE_TIMEOUT = 5
RECONNECT_DELAY = 1  # matches the C source's sleep(1) to avoid TIME_WAIT


class AlphaEssLocalApiClientError(Exception):
    """General API client error."""


class AlphaEssLocalApiClientCommunicationError(AlphaEssLocalApiClientError):
    """Raised when the device cannot be reached."""


class AlphaEssLocalApiClientAuthenticationError(AlphaEssLocalApiClientError):
    """Raised when authentication with the device fails."""


class AlphaEssLocalApiClient:
    """Talks to a single AlphaESS device over Modbus TCP."""

    def __init__(self, host: str, port: int) -> None:
        """Initialize the API client."""
        self._host = host
        self._port = port
        self._client = AsyncModbusTcpClient(host, port=port, timeout=RESPONSE_TIMEOUT)

    async def async_close(self) -> None:
        """Close the Modbus connection."""
        self._client.close()

    async def _ensure_connected(self) -> None:
        if not self._client.connected and not await self._client.connect():
            raise AlphaEssLocalApiClientCommunicationError(
                f"Could not connect to {self._host}:{self._port}"
            )

    async def _reconnect(self) -> None:
        self._client.close()
        await asyncio.sleep(RECONNECT_DELAY)
        await self._ensure_connected()

    async def _read_registers(self, address: int, count: int) -> list[int]:
        """Read holding registers, retrying once after a reconnect on failure."""
        await self._ensure_connected()
        try:
            result = await self._client.read_holding_registers(
                address, count=count, device_id=protocol.SLAVE_ID
            )
            if not (result.isError() or isinstance(result, ExceptionResponse)):
                return result.registers
        except ModbusException:
            pass

        try:
            await self._reconnect()
            result = await self._client.read_holding_registers(
                address, count=count, device_id=protocol.SLAVE_ID
            )
            if result.isError() or isinstance(result, ExceptionResponse):
                raise AlphaEssLocalApiClientCommunicationError(
                    f"Error reading register {address:#06x} from {self._host}"
                )
            return result.registers
        except ModbusException as exception:
            raise AlphaEssLocalApiClientCommunicationError(
                f"Error reading register {address:#06x} from {self._host}"
            ) from exception

    async def _write_registers(self, address: int, values: list[int]) -> None:
        """Write holding registers. Not retried, matching the C source."""
        await self._ensure_connected()
        try:
            result = await self._client.write_registers(
                address, values, device_id=protocol.SLAVE_ID
            )
        except ModbusException as exception:
            raise AlphaEssLocalApiClientCommunicationError(
                f"Error writing register {address:#06x} to {self._host}"
            ) from exception
        if result.isError() or isinstance(result, ExceptionResponse):
            raise AlphaEssLocalApiClientCommunicationError(
                f"Error writing register {address:#06x} to {self._host}"
            )

    async def async_test_connection(self) -> None:
        """Verify that the device is reachable, raising on failure.

        Used by the config flow to validate user input before creating a
        config entry. Closes the connection afterward.
        """
        try:
            await self.async_get_soc()
        finally:
            await self.async_close()

    async def async_get_soc(self) -> float:
        """State of charge, in percent (0.1% units on the wire)."""
        registers = await self._read_registers(protocol.REG_SOC, 1)
        return registers[0] / 10.0

    async def async_get_pv_power(self) -> int:
        """Roof PV generated power, in Watts (PV1 + PV2 + PV3 -- this
        AlphaESS model has exactly three PV inputs, confirmed directly by
        the user; no PV4 exists on this hardware. PV1/PV2 are both real
        Alpha-owned roof strings (an earlier "PV2 is actually the separate
        SMA installation" theory was wrong and has been retracted). PV3
        reads 0 on this specific installation (unused input) but is
        included for correctness -- see protocol.py's REG_PV_POWER comment.
        """
        registers = await self._read_registers(protocol.REG_PV_POWER, protocol.REG_PV_POWER_COUNT)
        pv1 = protocol.to_unsigned_int(registers[0], registers[1])
        pv2 = protocol.to_unsigned_int(registers[4], registers[5])
        pv3 = protocol.to_unsigned_int(registers[8], registers[9])
        return pv1 + pv2 + pv3

    async def async_get_battery_power(self) -> int:
        """Battery power, in Watts. Negative = charge, positive = discharge."""
        registers = await self._read_registers(protocol.REG_BATTERY_POWER, 1)
        return protocol.to_signed_short(registers[0])

    async def async_get_total_active_power(self) -> int:
        """Grid power, in Watts. Negative = feed to grid, positive = use from grid."""
        registers = await self._read_registers(protocol.REG_TOTAL_ACTIVE_POWER, 2)
        return protocol.to_signed_int(registers[0], registers[1])

    async def async_get_total_energy_feed_to_grid(self) -> float:
        """Total energy fed to grid, in kWh."""
        registers = await self._read_registers(protocol.REG_TOTAL_ENERGY_FEED_TO_GRID, 2)
        return protocol.to_unsigned_int(registers[0], registers[1]) * 0.01

    async def async_get_total_energy_consume_from_grid(self) -> float:
        """Total energy consumed from grid, in kWh."""
        registers = await self._read_registers(protocol.REG_TOTAL_ENERGY_CONSUME_FROM_GRID, 2)
        return protocol.to_unsigned_int(registers[0], registers[1]) * 0.01

    async def async_get_pv_total_energy_feed_to_grid(self) -> float:
        """Total PV energy fed to grid, in kWh."""
        registers = await self._read_registers(protocol.REG_PV_TOTAL_ENERGY_FEED_TO_GRID, 2)
        return protocol.to_unsigned_int(registers[0], registers[1]) * 0.01

    async def async_get_pv_total_energy_consume_from_grid(self) -> float:
        """Total PV energy consumed from grid, in kWh."""
        registers = await self._read_registers(protocol.REG_PV_TOTAL_ENERGY_CONSUME_FROM_GRID, 2)
        return protocol.to_unsigned_int(registers[0], registers[1]) * 0.01

    async def async_get_pv_total_energy(self) -> float:
        """The inverter's own cumulative PV1/PV2/PV3 string generation, in kWh.

        Scale is 0.1 here, not 0.01 like the other total-energy registers
        above -- see protocol.py's REG_PV_TOTAL_ENERGY comment.
        """
        registers = await self._read_registers(protocol.REG_PV_TOTAL_ENERGY, 2)
        return protocol.to_unsigned_int(registers[0], registers[1]) * 0.1

    async def async_get_dispatch_param(self) -> DispatchParam:
        """Read the current dispatch mode/power/cutoff-SOC/duration."""
        registers = await self._read_registers(
            protocol.REG_DISPATCH_PARAM, protocol.REG_DISPATCH_PARAM_COUNT
        )
        return protocol.decode_dispatch_param(registers)

    async def async_set_dispatch_param(self, param: DispatchParam) -> None:
        """Write a new dispatch mode/power/cutoff-SOC/duration."""
        registers = protocol.encode_dispatch_param(param)
        await self._write_registers(protocol.REG_DISPATCH_PARAM, registers)

    async def async_get_max_feed_into_grid(self) -> int:
        """Max feed-into-grid percentage (0-100)."""
        registers = await self._read_registers(protocol.REG_MAX_FEED_INTO_GRID, 1)
        return registers[0]

    async def async_set_max_feed_into_grid(self, max_feed: int) -> None:
        """Set max feed-into-grid percentage (0-100, clamped)."""
        max_feed = min(max_feed, 100)
        await self._write_registers(protocol.REG_MAX_FEED_INTO_GRID, [max_feed])

    async def async_set_time_period_control(
        self,
        start_hour: int,
        start_min: int,
        stop_hour: int,
        stop_min: int,
        cutoff_soc: int,
        charge: bool,
    ) -> None:
        """Set a charge/discharge time-window with a cut-off SOC."""
        registers = protocol.encode_time_period_control(
            start_hour, start_min, stop_hour, stop_min, cutoff_soc, charge
        )
        await self._write_registers(protocol.REG_TIME_PERIOD_CONTROL, registers)

    async def async_get_data(self) -> dict:
        """Fetch the coordinator-facing snapshot of live inverter data."""
        return {
            "battery_soc": await self.async_get_soc(),
            "pv_power": await self.async_get_pv_power(),
            "battery_power": await self.async_get_battery_power(),
            "grid_power": await self.async_get_total_active_power(),
            "total_energy_feed_to_grid": await self.async_get_total_energy_feed_to_grid(),
            "total_energy_consume_from_grid": (
                await self.async_get_total_energy_consume_from_grid()
            ),
            "pv_total_energy_feed_to_grid": (await self.async_get_pv_total_energy_feed_to_grid()),
            "pv_total_energy_consume_from_grid": (
                await self.async_get_pv_total_energy_consume_from_grid()
            ),
            "pv_total_energy": await self.async_get_pv_total_energy(),
        }
