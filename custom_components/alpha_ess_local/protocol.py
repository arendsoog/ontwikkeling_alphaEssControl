"""AlphaESS Modbus register map and on-wire encode/decode helpers.

Port of AlphaESS.h / AlphaESS.c's register layout and dispatch-parameter
encoding. Kept free of any I/O so the quirky on-wire formats (offset-encoded
signed power, asymmetric PVOn, quartered cutoffSOC) can be unit-tested
without mocking Modbus transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

SLAVE_ID = 85

REG_DISPATCH_PARAM = 0x0880
REG_DISPATCH_PARAM_COUNT = 11
REG_MAX_FEED_INTO_GRID = 0x0800
REG_SOC = 0x0102
# PV1 power = regs 0-1, PV2 voltage/current = regs 2-3, PV2 power = regs
# 4-5, PV3 voltage/current = regs 6-7, PV3 power = regs 8-9 -- confirmed
# against both AlphaESS's public register map and the community `modbus:`
# YAML targeting this same inverter (config/packages/
# integration_alpha_ess.yaml's AlphaESS_PV1_Power/_PV2_Power/_PV3_Power,
# addresses 0x041F/0x0423/0x0427). This AlphaESS model has exactly three PV
# inputs (confirmed by the user -- no PV4 register block exists on this
# hardware); api.py's async_get_pv_power sums all three.
REG_PV_POWER = 0x041F
REG_PV_POWER_COUNT = 10
REG_BATTERY_POWER = 0x0126
REG_TOTAL_ACTIVE_POWER = 0x0021
REG_TOTAL_ENERGY_FEED_TO_GRID = 0x0010
REG_TOTAL_ENERGY_CONSUME_FROM_GRID = 0x0012
REG_PV_TOTAL_ENERGY_FEED_TO_GRID = 0x0090
REG_PV_TOTAL_ENERGY_CONSUME_FROM_GRID = 0x0092
# The inverter's own cumulative total PV generation, in kWh -- covers the
# PV1/PV2/PV3 *string* side only (confirmed: matches the community `modbus:`
# YAML's AlphaESS_Total_Energy_from_PV, and cross-checked against the
# device's separate "PV Meter" CT-clamp reading, which together account for
# the full system total). Scale is 0.1 here, not 0.01 like the other
# total-energy registers above -- verified against that same YAML; don't
# "fix" it to match the others. Used by orchestrator.py to derive
# real_solar_power_roof from hour-boundary deltas of the device's own
# continuous internal accumulation, rather than averaging our own coarse
# periodic power samples (confirmed to under-count on days with fluctuating
# solar -- roughly half of the true daily total).
REG_PV_TOTAL_ENERGY = 0x043E
# The inverter's own cumulative battery charge/discharge counters, in kWh
# (scale 0.1, same as REG_PV_TOTAL_ENERGY) -- confirmed against the
# community `modbus:` YAML's AlphaESS_Total_Energy_Charge_Battery/
# _Discharge_Battery. Used the same way as REG_PV_TOTAL_ENERGY: hour-
# boundary deltas instead of averaging 5-minute REG_BATTERY_POWER samples,
# which was found to both under-count gross charging and over-count gross
# discharging -- averaging one signed power sample per 5 minutes nets out
# any charge-then-discharge (or vice versa) that happens within the same
# hour, which these two separately-metered cumulative counters don't.
REG_BATTERY_TOTAL_ENERGY_CHARGE = 0x0120
REG_BATTERY_TOTAL_ENERGY_DISCHARGE = 0x0122
REG_TIME_PERIOD_CONTROL = 0x084F
REG_TIME_PERIOD_CONTROL_COUNT = 17

# Dispatch power on the wire is offset-encoded, not two's complement:
# raw < POWER_DISCHARGE_OFFSET -> charging (positive); raw >= offset ->
# discharging, actual value is -(raw - offset).
POWER_DISCHARGE_OFFSET = 32000


class DispatchMode(IntEnum):
    """Port of the DISPATCH_MODE_* defines in AlphaESS.h."""

    DEFAULT = 0
    ONLY_CHARGE_FROM_PV = 1
    STATE_OF_CHARGE_CONTROL = 2
    LOAD_FOLLOWING = 3
    MAXIMISE_OUTPUT = 4
    NORMAL = 5
    OPTIMISE_CONSUMPTION = 6
    MAXIMISE_CONSUMPTION = 7
    ECO = 8
    FCAS = 9
    PV_POWER_SETTING = 10
    NO_BATTERY_CHARGE = 19


@dataclass
class DispatchParam:
    """Port of dispatch_t."""

    mode: DispatchMode
    started: bool
    power: int  # Watts, positive = charge, negative = discharge
    cutoff_soc: float  # 0.1 % units, e.g. 900 = 90.0%
    duration: int  # seconds
    para7: int
    pv_on: bool


def to_unsigned_int(high_word: int, low_word: int) -> int:
    """Combine two 16-bit registers into an unsigned 32-bit int, high word first."""
    return ((high_word & 0xFFFF) << 16) | (low_word & 0xFFFF)


def to_signed_int(high_word: int, low_word: int) -> int:
    """Combine two 16-bit registers into a signed 32-bit int, high word first."""
    value = to_unsigned_int(high_word, low_word)
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def to_signed_short(word: int) -> int:
    """Interpret a single 16-bit register as a signed 16-bit int."""
    word &= 0xFFFF
    return word - 0x10000 if word >= 0x8000 else word


def decode_dispatch_power(raw: int) -> int:
    """Decode the offset-encoded dispatch power register (see module docstring)."""
    return -(raw - POWER_DISCHARGE_OFFSET) if raw >= POWER_DISCHARGE_OFFSET else raw


def encode_dispatch_power(power: int) -> int:
    """Encode a signed dispatch power value into its on-wire offset form."""
    return POWER_DISCHARGE_OFFSET + abs(power) if power < 0 else power


def decode_dispatch_param(registers: list[int]) -> DispatchParam:
    """Decode the 11-register dispatch-parameter block into a DispatchParam."""
    # C source combines registers[1:3] via ToUnsignedInt() but truncates the
    # result to uint16_t, which discards registers[1] entirely (it only ever
    # carries the high word); the low word alone is preserved.
    power_raw = registers[2] & 0xFFFF
    return DispatchParam(
        mode=DispatchMode(registers[5]),
        started=(registers[0] == 1),
        power=decode_dispatch_power(power_raw),
        cutoff_soc=float(registers[6]) * 4,
        duration=to_unsigned_int(registers[7], registers[8]),
        para7=registers[9],
        pv_on=(registers[10] == 1),
    )


def encode_dispatch_param(param: DispatchParam) -> list[int]:
    """Encode a DispatchParam into the 11-register dispatch-parameter block."""
    cutoff_soc = min(param.cutoff_soc, 1000)
    power = encode_dispatch_power(param.power)
    duration = param.duration

    registers = [0] * REG_DISPATCH_PARAM_COUNT
    registers[0] = 1 if param.started else 0
    registers[1] = (power >> 16) & 0xFFFF
    registers[2] = power & 0xFFFF
    registers[3] = 0
    registers[4] = 0
    registers[5] = int(param.mode)
    registers[6] = int(cutoff_soc / 4)
    registers[7] = (duration >> 16) & 0xFFFF
    registers[8] = duration & 0xFFFF
    registers[9] = 255  # matches C source: para7 is always sent as 255
    registers[10] = 1 if param.pv_on else 2  # asymmetric: on=1, off=2 (not 0)
    return registers


def encode_time_period_control(
    start_hour: int,
    start_min: int,
    stop_hour: int,
    stop_min: int,
    cutoff_soc: int,
    charge: bool,
) -> list[int]:
    """Encode the 17-register time-period-control block."""
    registers = [0] * REG_TIME_PERIOD_CONTROL_COUNT
    registers[0] = 1 if charge else 2
    registers[1] = 10  # default 10%, overwritten below when discharging
    if not charge:
        registers[1] = cutoff_soc
        registers[2] = start_hour
        registers[3] = stop_hour
        registers[11] = start_min
        registers[12] = stop_min
    else:
        registers[6] = cutoff_soc
        registers[7] = start_hour
        registers[8] = stop_hour
        registers[15] = start_min
        registers[16] = stop_min
    return registers
