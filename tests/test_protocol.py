"""Unit tests for the AlphaESS Modbus register encode/decode helpers."""

from custom_components.alpha_ess_local import protocol
from custom_components.alpha_ess_local.protocol import DispatchMode, DispatchParam


def test_to_unsigned_int():
    assert protocol.to_unsigned_int(0x0001, 0x0002) == 0x00010002
    assert protocol.to_unsigned_int(0x0000, 0xFFFF) == 0xFFFF


def test_to_signed_int_positive_and_negative():
    assert protocol.to_signed_int(0x0000, 0x0064) == 100
    assert protocol.to_signed_int(0xFFFF, 0xFF9C) == -100


def test_to_signed_short_positive_and_negative():
    assert protocol.to_signed_short(100) == 100
    assert protocol.to_signed_short(0xFF9C) == -100


def test_decode_dispatch_power_charging_below_offset():
    # 32000 - 500 = charging 500 W
    assert protocol.decode_dispatch_power(31500) == 500


def test_decode_dispatch_power_discharging_at_and_above_offset():
    assert protocol.decode_dispatch_power(32000) == 0
    assert protocol.decode_dispatch_power(32500) == -500


def test_encode_dispatch_power_charging_is_offset_minus_power():
    """Regression: charging was written as the bare value, which the
    inverter reads as 32000 - value -- 5000 W became 27 kW (its maximum)
    and 0 W ("stop") 32 kW. Same encoding as the community package's
    force-charge script: 32000 - power."""
    assert protocol.encode_dispatch_power(5000) == 27000
    assert protocol.encode_dispatch_power(0) == 32000
    assert protocol.encode_dispatch_power(-1500) == 33500


def test_encode_dispatch_power_round_trips_with_decode():
    for power in (0, 500, 10484, 32000, -1, -500, -32000):
        raw = protocol.encode_dispatch_power(power)
        assert protocol.decode_dispatch_power(raw) == power


def test_decode_dispatch_param():
    registers = [0] * protocol.REG_DISPATCH_PARAM_COUNT
    registers[0] = 1  # started
    registers[1] = 0
    registers[2] = 32500  # discharging 500 W
    registers[5] = DispatchMode.STATE_OF_CHARGE_CONTROL
    registers[6] = 200  # cutoff_soc = 200 * 4 = 800 (80.0%)
    registers[7] = 0
    registers[8] = 3600  # duration seconds
    registers[9] = 42
    registers[10] = 1  # PV on

    param = protocol.decode_dispatch_param(registers)

    assert param.started is True
    assert param.power == -500
    assert param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    assert param.cutoff_soc == 800
    assert param.duration == 3600
    assert param.para7 == 42
    assert param.pv_on is True


def test_decode_dispatch_param_ignores_high_word_of_power_register():
    """The C source truncates the combined power to uint16_t, discarding the high word."""
    registers = [0] * protocol.REG_DISPATCH_PARAM_COUNT
    registers[1] = 0xFFFF  # high word: must be ignored
    registers[2] = 500  # outside SOC control: the legacy bare value
    registers[5] = DispatchMode.DEFAULT
    registers[10] = 2  # PV off

    param = protocol.decode_dispatch_param(registers)

    assert param.power == 500
    assert param.pv_on is False


def test_encode_dispatch_param_charging():
    param = DispatchParam(
        mode=DispatchMode.NORMAL,
        started=True,
        power=1500,
        cutoff_soc=900,
        duration=3600,
        para7=0,
        pv_on=True,
    )

    registers = protocol.encode_dispatch_param(param)

    assert registers[0] == 1  # started
    # Outside State-of-Charge control the power isn't a setpoint; kept in
    # the legacy bare-value form these modes have always been written with.
    assert registers[2] == 1500  # power, low word
    assert registers[5] == DispatchMode.NORMAL
    assert registers[6] == 225  # 900 / 4
    assert registers[8] == 3600  # duration, low word
    assert registers[9] == 255  # always 255, per C source
    assert registers[10] == 1  # PV on -> 1


def test_encode_dispatch_param_grid_charge_setpoint():
    """State-of-Charge control charging: 32000 - power (5077 W -> 26923),
    not the bare 5077 that the inverter read as a 27 kW charge."""
    param = DispatchParam(
        mode=DispatchMode.STATE_OF_CHARGE_CONTROL,
        started=True,
        power=5077,
        cutoff_soc=350,
        duration=3600,
        para7=0,
        pv_on=True,
    )

    registers = protocol.encode_dispatch_param(param)

    assert registers[1] == 0
    assert registers[2] == 26923
    assert protocol.decode_dispatch_param(registers).power == 5077


def test_encode_dispatch_param_grid_charge_stop_is_zero_watts():
    """Regulation's "stop" (0 W) must be raw 32000, not raw 0 (= 32 kW)."""
    param = DispatchParam(
        mode=DispatchMode.STATE_OF_CHARGE_CONTROL,
        started=True,
        power=0,
        cutoff_soc=350,
        duration=3600,
        para7=0,
        pv_on=True,
    )

    assert protocol.encode_dispatch_param(param)[2] == 32000


def test_encode_dispatch_param_discharging_and_pv_off():
    param = DispatchParam(
        mode=DispatchMode.STATE_OF_CHARGE_CONTROL,
        started=True,
        power=-500,
        cutoff_soc=200,
        duration=3600,
        para7=0,
        pv_on=False,
    )

    registers = protocol.encode_dispatch_param(param)

    assert registers[2] == 32500  # 32000 + 500
    assert registers[10] == 2  # PV off -> 2, not 0


def test_encode_dispatch_param_clamps_cutoff_soc_to_1000():
    param = DispatchParam(
        mode=DispatchMode.DEFAULT,
        started=False,
        power=0,
        cutoff_soc=1500,
        duration=0,
        para7=0,
        pv_on=False,
    )

    registers = protocol.encode_dispatch_param(param)

    assert registers[6] == 250  # 1000 / 4, clamped


def test_encode_time_period_control_discharging():
    registers = protocol.encode_time_period_control(
        start_hour=6, start_min=15, stop_hour=9, stop_min=45, cutoff_soc=200, charge=False
    )

    assert registers[0] == 2  # discharge
    assert registers[1] == 200  # cutoff SOC
    assert registers[2] == 6
    assert registers[3] == 9
    assert registers[11] == 15
    assert registers[12] == 45
    assert registers[6] == 0  # charging block untouched


def test_encode_time_period_control_charging():
    registers = protocol.encode_time_period_control(
        start_hour=1, start_min=0, stop_hour=5, stop_min=30, cutoff_soc=900, charge=True
    )

    assert registers[0] == 1  # charge
    assert registers[1] == 10  # default, not overwritten when charging
    assert registers[6] == 900
    assert registers[7] == 1
    assert registers[8] == 5
    assert registers[15] == 0
    assert registers[16] == 30
