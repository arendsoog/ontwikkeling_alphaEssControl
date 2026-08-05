"""Dispatch decision logic — port of CheckAndSetChargingMode (AlphaESSControl.c).

Pure decision functions operating on `data.Hour`/`protocol.DispatchParam` —
no Modbus I/O here, so the whole decision tree is unit-testable without a
live inverter. The coordinator (orchestrator.py) does the I/O and the
`control_enabled` gating around these functions.

Garage-PV relay control (`SetGaragePV`/`GetGaragePVPower`, Shelly-based) is
dropped, not ported — confirmed with the user, their actual "Extra
PV-installatie" is SMA now, not a Shelly-controlled garage array. SMA
extra-PV on/off via Modbus (`decide_extra_pv_state` below) is a fresh,
simpler design rather than a port of `SetGaragePV` — it reuses the same
per-hour `earning` the scheduler already computes instead of watching a
separate price sensor, and writes via HA's core `modbus.write_register`
service (register layout is configurable, not hardcoded — it isn't the same
for every SMA installation).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .const import LOGGER
from .data import (
    SOC_MAX,
    SOC_MAX_BATTERY,
    SOC_MIN,
    Charge,
    Earning,
    Hour,
)
from .protocol import DispatchMode, DispatchParam

_DISPATCH_MESSAGES = {
    DispatchMode.DEFAULT: "Default",
    DispatchMode.ONLY_CHARGE_FROM_PV: "Battery only charges from PV",
    DispatchMode.STATE_OF_CHARGE_CONTROL: "State of Charge control",
    DispatchMode.LOAD_FOLLOWING: "Load Following",
    DispatchMode.MAXIMISE_OUTPUT: "Maximise Output",
    DispatchMode.NORMAL: "Normal",
    DispatchMode.OPTIMISE_CONSUMPTION: "Optimise Consumption",
    DispatchMode.MAXIMISE_CONSUMPTION: "Maximise Consumption",
    DispatchMode.NO_BATTERY_CHARGE: "No Battery Charge",
}


def set_dispatch_msg(mode: DispatchMode) -> str:
    """Port of GetDispatchMsg: a short label for a dispatch mode."""
    return _DISPATCH_MESSAGES.get(mode, "Unknown")


SAFE_DEFAULT_HOUR = Hour(
    earning=Earning.NO_EARNING,
    feed_in=True,
    charge=Charge.CHARGING_ON_PV,
    cutoff_soc=SOC_MAX,
)
"""Port of DoHourlyWork's fallback when the current hour has no valid
schedule yet (no valid price, or the schedule coordinator hasn't run)."""


@dataclass
class DispatchConfig:
    """The subset of options-flow config the dispatch decision needs."""

    usable_battery_capacity: float


@dataclass
class DispatchDecision:
    param: DispatchParam
    target_feed_in_percentage: int


def _full_dispatch_power(usable_battery_capacity: float) -> int:
    """Port of the inline `power` calc in CheckAndSetChargingMode: a fixed
    charge/discharge power close to the full usable capacity, in Watts.

    `long power = (AlphaESSUsableBatteryCapacity * (SOC_MAX_BATTERY -
    SOC_MIN) / 1000) + 500;` — integer division, ported faithfully.
    """
    return (int(usable_battery_capacity) * (SOC_MAX_BATTERY - SOC_MIN)) // 1000 + 500


def decide_dispatch(hour: Hour, cur_soc: int, config: DispatchConfig) -> DispatchDecision:
    """Port of CheckAndSetChargingMode's two switch statements plus both
    security checks.

    Mutates `hour.feed_in` in place — the CHARGING_DISCHARGE branch always
    forces feed-in on, then the earning-on-use security check may force it
    back off; matches the original's direct struct mutation (same pattern
    already used by `schedule._finalise_schedule`).
    """
    cutoff_soc = hour.cutoff_soc
    below_cutoff = cutoff_soc == SOC_MAX_BATTERY or cur_soc < cutoff_soc

    if hour.charge == Charge.CHARGING_ON_PV:
        mode = DispatchMode.NORMAL if below_cutoff else DispatchMode.NO_BATTERY_CHARGE

    elif hour.charge == Charge.NO_CHARGING:
        mode = (
            DispatchMode.ONLY_CHARGE_FROM_PV
            if hour.earning == Earning.EARNING_ON_USE
            else DispatchMode.NO_BATTERY_CHARGE
        )

    elif hour.charge == Charge.CHARGING_DISCHARGE:
        if hour.earning == Earning.EARNING_ON_USE:
            LOGGER.warning(
                "Earning on use: Discharging not allowed, automatically set to No-charging"
            )
            mode = DispatchMode.ONLY_CHARGE_FROM_PV
        else:
            mode = (
                DispatchMode.NO_BATTERY_CHARGE
                if cur_soc <= cutoff_soc
                else DispatchMode.STATE_OF_CHARGE_CONTROL
            )
            hour.feed_in = True

    elif hour.charge == Charge.CHARGING_ON_GRID:
        if below_cutoff:
            mode = (
                DispatchMode.MAXIMISE_CONSUMPTION
                if hour.earning == Earning.EARNING_ON_USE
                else DispatchMode.STATE_OF_CHARGE_CONTROL
            )
        else:
            mode = (
                DispatchMode.ONLY_CHARGE_FROM_PV
                if hour.earning == Earning.EARNING_ON_USE
                else DispatchMode.NO_BATTERY_CHARGE
            )

    else:  # Charge.NO_DISCHARGING
        mode = DispatchMode.ONLY_CHARGE_FROM_PV if below_cutoff else DispatchMode.NO_BATTERY_CHARGE

    # security check: never feed in on negative prices
    if hour.earning == Earning.EARNING_ON_USE:
        hour.feed_in = False

    full_power = _full_dispatch_power(config.usable_battery_capacity)

    if mode == DispatchMode.NO_BATTERY_CHARGE:
        power = 0
        param_cutoff_soc = 0
        pv_on = True
    elif mode in (DispatchMode.NORMAL, DispatchMode.ONLY_CHARGE_FROM_PV):
        power = full_power if below_cutoff else 0
        param_cutoff_soc = 0
        pv_on = True
    elif mode == DispatchMode.STATE_OF_CHARGE_CONTROL:
        power = -full_power if hour.charge == Charge.CHARGING_DISCHARGE else full_power
        param_cutoff_soc = cutoff_soc
        pv_on = True
    elif mode == DispatchMode.MAXIMISE_CONSUMPTION:
        power = 0
        param_cutoff_soc = 0
        pv_on = False
    else:
        # Unreachable: the mode selection above only ever assigns one of the
        # five modes handled here (matches the C `default: Log(...); return;`,
        # which never fires for the same reason).
        raise AssertionError(f"decide_dispatch: unsupported dispatch mode {mode!r}")

    # security check again: never feed in on negative prices. Deliberate
    # deviation from the original C (which also unconditionally forced
    # PVOn=false here, regardless of battery headroom — confirmed against
    # `.old c code/AlphaESSControl.c` lines 713-718): the user asked for PV
    # to only be curtailed once the battery has actually reached this hour's
    # target SOC, so free/negative-price solar can still charge it up until
    # then instead of being wasted. Considered going further and never
    # curtailing at all (e.g. so an EV charger could use the surplus even
    # with a full battery), but the user decided against it — charging the
    # car isn't a sure thing (the car isn't always home), so it's not
    # reliable enough to justify keeping PV on with nowhere guaranteed for
    # the surplus to go.
    if hour.earning == Earning.EARNING_ON_USE:
        hour.feed_in = False
        if not below_cutoff:
            pv_on = False

    target_feed_in_percentage = 100 if hour.feed_in else 0

    return DispatchDecision(
        param=DispatchParam(
            mode=mode,
            started=True,
            power=power,
            cutoff_soc=param_cutoff_soc,
            duration=3600,
            para7=255,
            pv_on=pv_on,
        ),
        target_feed_in_percentage=target_feed_in_percentage,
    )


class ExtraPvState(IntEnum):
    OFF = 0
    ON = 1


def decide_extra_pv_state(hour: Hour, cur_soc: int) -> ExtraPvState:
    """Curtail the extra PV installation during negative-price hours — but,
    like `decide_dispatch`'s own PVOn security check, only once the battery
    has reached this hour's target SOC (`cutoff_soc`). While there's still
    room, keep it on so it can charge the (shared) battery for free/at a
    negative price instead of being wasted."""
    if hour.earning != Earning.EARNING_ON_USE:
        return ExtraPvState.ON
    below_cutoff = hour.cutoff_soc == SOC_MAX_BATTERY or cur_soc < hour.cutoff_soc
    return ExtraPvState.ON if below_cutoff else ExtraPvState.OFF


def dispatch_params_equal(a: DispatchParam, b: DispatchParam) -> bool:
    """Port of IsEqualDispatchParam — compares the 6 fields that matter for
    the "already set" check (excludes `para7`, which the original also
    excludes)."""
    return (
        a.started == b.started
        and a.mode == b.mode
        and a.duration == b.duration
        and a.cutoff_soc == b.cutoff_soc
        and a.power == b.power
        and a.pv_on == b.pv_on
    )
