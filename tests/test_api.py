"""Tests for the AlphaESS Modbus API client, with pymodbus mocked out."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymodbus.exceptions import ModbusException

from custom_components.alpha_ess_local.api import (
    AlphaEssLocalApiClient,
    AlphaEssLocalApiClientCommunicationError,
)


def _ok_result(registers):
    result = MagicMock()
    result.isError.return_value = False
    result.registers = registers
    return result


def _error_result():
    result = MagicMock()
    result.isError.return_value = True
    return result


@pytest.fixture(autouse=True)
def no_reconnect_delay():
    """Skip the real reconnect delay so retry tests run instantly."""
    with patch("custom_components.alpha_ess_local.api.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.fixture
def mock_modbus_client():
    """Patch AsyncModbusTcpClient and return the mocked instance."""
    with patch(
        "custom_components.alpha_ess_local.api.AsyncModbusTcpClient", autospec=True
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.connected = False
        instance.connect = AsyncMock(return_value=True)
        instance.close = MagicMock()
        instance.read_holding_registers = AsyncMock()
        instance.write_registers = AsyncMock()
        yield instance


@pytest.fixture
def client(mock_modbus_client):
    return AlphaEssLocalApiClient(host="192.168.1.50", port=502)


async def test_async_get_soc_scales_register_value(client, mock_modbus_client):
    mock_modbus_client.read_holding_registers.return_value = _ok_result([425])

    soc = await client.async_get_soc()

    assert soc == 42.5
    mock_modbus_client.read_holding_registers.assert_awaited_once()


async def test_async_get_battery_power_decodes_signed_short(client, mock_modbus_client):
    mock_modbus_client.read_holding_registers.return_value = _ok_result([0xFF9C])  # -100

    power = await client.async_get_battery_power()

    assert power == -100


async def test_async_get_pv_power_sums_all_three_strings(client, mock_modbus_client):
    # pv1 = regs[0:2] = 1000, pv2 = regs[4:6] = 2000, pv3 = regs[8:10] = 4000
    # (regs 2-3/6-7 are PV2/PV3's voltage/current, not power -- must be
    # ignored, not summed in). This AlphaESS model has exactly three PV
    # inputs -- no PV4 register block exists on this hardware.
    mock_modbus_client.read_holding_registers.return_value = _ok_result(
        [0, 1000, 0, 0, 0, 2000, 0, 0, 0, 4000]
    )

    power = await client.async_get_pv_power()

    assert power == 7000


async def test_async_get_pv_total_energy_uses_point_one_scale(client, mock_modbus_client):
    # Scale here is 0.1, not 0.01 like the other total-energy registers --
    # 616 raw -> 61.6 kWh.
    mock_modbus_client.read_holding_registers.return_value = _ok_result([0, 616])

    energy = await client.async_get_pv_total_energy()

    assert energy == pytest.approx(61.6)


async def test_async_get_battery_total_energy_charge_uses_point_one_scale(
    client, mock_modbus_client
):
    mock_modbus_client.read_holding_registers.return_value = _ok_result([0, 12345])

    energy = await client.async_get_battery_total_energy_charge()

    assert energy == pytest.approx(1234.5)


async def test_async_get_battery_total_energy_discharge_uses_point_one_scale(
    client, mock_modbus_client
):
    mock_modbus_client.read_holding_registers.return_value = _ok_result([0, 54321])

    energy = await client.async_get_battery_total_energy_discharge()

    assert energy == pytest.approx(5432.1)


async def test_read_registers_retries_once_after_reconnect(client, mock_modbus_client):
    mock_modbus_client.read_holding_registers.side_effect = [
        _error_result(),
        _ok_result([425]),
    ]

    soc = await client.async_get_soc()

    assert soc == 42.5
    assert mock_modbus_client.read_holding_registers.await_count == 2
    mock_modbus_client.close.assert_called_once()
    assert mock_modbus_client.connect.await_count == 2  # initial connect + reconnect


async def test_read_registers_raises_after_retry_also_fails(client, mock_modbus_client):
    mock_modbus_client.read_holding_registers.side_effect = ModbusException("boom")

    with pytest.raises(AlphaEssLocalApiClientCommunicationError):
        await client.async_get_soc()

    assert mock_modbus_client.read_holding_registers.await_count == 2


async def test_write_registers_does_not_retry_on_failure(client, mock_modbus_client):
    mock_modbus_client.write_registers.side_effect = ModbusException("boom")

    with pytest.raises(AlphaEssLocalApiClientCommunicationError):
        await client.async_set_max_feed_into_grid(50)

    mock_modbus_client.write_registers.assert_awaited_once()


async def test_async_set_max_feed_into_grid_clamps_to_100(client, mock_modbus_client):
    mock_modbus_client.write_registers.return_value = _ok_result([])

    await client.async_set_max_feed_into_grid(150)

    args, kwargs = mock_modbus_client.write_registers.call_args
    assert args[1] == [100]


async def test_async_close_closes_the_modbus_client(client, mock_modbus_client):
    await client.async_close()

    mock_modbus_client.close.assert_called_once()


async def test_async_test_connection_closes_after_success(client, mock_modbus_client):
    mock_modbus_client.read_holding_registers.return_value = _ok_result([425])

    await client.async_test_connection()

    mock_modbus_client.close.assert_called_once()


async def test_async_test_connection_closes_after_failure(client, mock_modbus_client):
    mock_modbus_client.read_holding_registers.side_effect = ModbusException("boom")

    with pytest.raises(AlphaEssLocalApiClientCommunicationError):
        await client.async_test_connection()

    # once from the read-retry's reconnect, once from async_test_connection's own cleanup
    assert mock_modbus_client.close.call_count == 2
