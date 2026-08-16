"""Tests for dispatch.py.

`decide_dispatch` tests use independently hand-derived expected values (same
formulas as the port, computed separately) rather than just re-deriving from
the function under test — same style as `test_schedule.py`'s
`_evaluate_hour_action` coverage.
"""

from custom_components.alpha_ess_local.data import SOC_MAX, SOC_MAX_BATTERY, Charge, Earning, Hour
from custom_components.alpha_ess_local.dispatch import (
    DispatchConfig,
    ExtraPvState,
    _full_dispatch_power,
    decide_dispatch,
    decide_extra_pv_state,
    dispatch_params_equal,
    set_dispatch_msg,
)
from custom_components.alpha_ess_local.protocol import DispatchMode, DispatchParam

CAPACITY = 10000.0
FULL_POWER = 9460  # (10000 * (1000 - 104)) // 1000 + 500, hand-computed


def _config() -> DispatchConfig:
    return DispatchConfig(usable_battery_capacity=CAPACITY)


def _hour(**overrides) -> Hour:
    defaults = {
        "charge": Charge.NO_CHARGING,
        "cutoff_soc": SOC_MAX,
        "earning": Earning.NO_EARNING,
        "feed_in": False,
    }
    defaults.update(overrides)
    return Hour(**defaults)


# --- _full_dispatch_power -----------------------------------------------------


def test_full_dispatch_power_matches_hand_computed_formula():
    assert _full_dispatch_power(10000.0) == 9460
    assert _full_dispatch_power(5000.0) == (5000 * 896) // 1000 + 500


# --- decide_dispatch: CHARGING_ON_PV ------------------------------------------


def test_decide_dispatch_charging_on_pv_below_cutoff_uses_normal_mode():
    hour = _hour(charge=Charge.CHARGING_ON_PV, cutoff_soc=900, feed_in=True)
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.NORMAL
    assert decision.param.power == FULL_POWER
    assert decision.param.cutoff_soc == 0
    assert decision.param.pv_on is True
    assert decision.target_feed_in_percentage == 100


def test_decide_dispatch_charging_on_pv_at_cutoff_stops_charging():
    hour = _hour(charge=Charge.CHARGING_ON_PV, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=900, config=_config())

    assert decision.param.mode == DispatchMode.NO_BATTERY_CHARGE
    assert decision.param.power == 0
    assert decision.param.pv_on is True


def test_decide_dispatch_charging_on_pv_full_battery_cutoff_keeps_charging():
    # cutoff_soc == SOC_MAX_BATTERY (100%) -> always "below cutoff", regardless of SOC.
    hour = _hour(charge=Charge.CHARGING_ON_PV, cutoff_soc=SOC_MAX_BATTERY)
    decision = decide_dispatch(hour, cur_soc=999, config=_config())

    assert decision.param.mode == DispatchMode.NORMAL
    assert decision.param.power == FULL_POWER


# --- decide_dispatch: NO_CHARGING ---------------------------------------------


def test_decide_dispatch_no_charging_normal_price():
    hour = _hour(charge=Charge.NO_CHARGING, earning=Earning.NO_EARNING)
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.NO_BATTERY_CHARGE
    assert decision.param.power == 0
    assert decision.param.pv_on is True


def test_decide_dispatch_no_charging_earning_on_use_below_cutoff_keeps_pv_on():
    # Deliberate deviation from the original C (which forces PVOn off
    # unconditionally on negative prices, see dispatch.py's comment): while
    # the battery still has room (below this hour's cutoff), PV stays on so
    # it can charge for free/at a negative price instead of being curtailed.
    hour = _hour(
        charge=Charge.NO_CHARGING,
        earning=Earning.EARNING_ON_USE,
        cutoff_soc=900,
        feed_in=True,
    )
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.ONLY_CHARGE_FROM_PV
    assert decision.param.power == FULL_POWER  # below cutoff
    assert decision.param.pv_on is True  # below cutoff -> not curtailed
    assert decision.target_feed_in_percentage == 0  # security check: never feed in
    assert hour.feed_in is False


def test_decide_dispatch_no_charging_earning_on_use_at_cutoff_disables_pv():
    # Once the battery has reached this hour's cutoff (effectively "full"),
    # PV is curtailed just like the original — nowhere left to put it.
    hour = _hour(
        charge=Charge.NO_CHARGING,
        earning=Earning.EARNING_ON_USE,
        cutoff_soc=900,
        feed_in=True,
    )
    decision = decide_dispatch(hour, cur_soc=900, config=_config())

    assert decision.param.mode == DispatchMode.ONLY_CHARGE_FROM_PV
    assert decision.param.pv_on is False  # security check: battery full, curtail PV
    assert decision.target_feed_in_percentage == 0  # security check: never feed in
    assert hour.feed_in is False


# --- decide_dispatch: CHARGING_DISCHARGE --------------------------------------


def test_decide_dispatch_charging_discharge_blocked_on_earning_on_use():
    hour = _hour(charge=Charge.CHARGING_DISCHARGE, earning=Earning.EARNING_ON_USE, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=950, config=_config())

    assert decision.param.mode == DispatchMode.ONLY_CHARGE_FROM_PV
    assert hour.feed_in is False


def test_decide_dispatch_charging_discharge_at_or_below_cutoff():
    hour = _hour(
        charge=Charge.CHARGING_DISCHARGE, earning=Earning.EARNING_ON_RETURN, cutoff_soc=200
    )
    decision = decide_dispatch(hour, cur_soc=200, config=_config())

    assert decision.param.mode == DispatchMode.NO_BATTERY_CHARGE
    assert decision.param.power == 0
    assert hour.feed_in is True  # always forced on for CHARGING_DISCHARGE
    assert decision.target_feed_in_percentage == 100


def test_decide_dispatch_charging_discharge_above_cutoff():
    hour = _hour(
        charge=Charge.CHARGING_DISCHARGE, earning=Earning.EARNING_ON_RETURN, cutoff_soc=200
    )
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    assert decision.param.power == -FULL_POWER  # negative = discharge
    assert decision.param.cutoff_soc == 200
    assert decision.param.pv_on is True


# --- decide_dispatch: CHARGING_ON_GRID ----------------------------------------


def test_decide_dispatch_charging_on_grid_below_cutoff_earning_on_use():
    hour = _hour(charge=Charge.CHARGING_ON_GRID, earning=Earning.EARNING_ON_USE, cutoff_soc=1000)
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.MAXIMISE_CONSUMPTION
    assert decision.param.power == 0
    assert decision.param.pv_on is False


def test_decide_dispatch_charging_on_grid_below_cutoff_normal_price():
    hour = _hour(charge=Charge.CHARGING_ON_GRID, earning=Earning.EARNING_ON_RETURN, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    assert decision.param.power == FULL_POWER  # positive = charge
    assert decision.param.cutoff_soc == 900


def test_decide_dispatch_charging_on_grid_throttled_by_max_grid_load():
    # Belgian capaciteitstarief: house_load_w + power must stay under the
    # live max_grid_load cap, not just the hourly plan's total Wh -- same
    # "total netpiek" reasoning schedule.py already applies at planning
    # time, reapplied here for the real ~20s dispatch command.
    hour = _hour(charge=Charge.CHARGING_ON_GRID, earning=Earning.EARNING_ON_RETURN, cutoff_soc=900)
    config = DispatchConfig(usable_battery_capacity=CAPACITY, max_grid_load=2000.0)

    decision = decide_dispatch(hour, cur_soc=500, config=config, house_load_w=300.0)

    assert decision.param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    assert decision.param.power == 1700  # 2000 - 300, well under FULL_POWER


def test_decide_dispatch_charging_on_grid_uncapped_by_default():
    # config.max_grid_load defaults to float("inf") -- existing callers that
    # don't pass it keep the prior, uncapped behavior.
    hour = _hour(charge=Charge.CHARGING_ON_GRID, earning=Earning.EARNING_ON_RETURN, cutoff_soc=900)

    decision = decide_dispatch(hour, cur_soc=500, config=_config(), house_load_w=5000.0)

    assert decision.param.power == FULL_POWER


def test_decide_dispatch_charging_discharge_ignores_max_grid_load():
    # Discharging increases export, not import -- the capaciteitstarief cap
    # (which only bounds grid *import*) must never throttle it.
    hour = _hour(
        charge=Charge.CHARGING_DISCHARGE, earning=Earning.EARNING_ON_RETURN, cutoff_soc=200
    )
    config = DispatchConfig(usable_battery_capacity=CAPACITY, max_grid_load=0.0)

    decision = decide_dispatch(hour, cur_soc=500, config=config, house_load_w=0.0)

    assert decision.param.power == -FULL_POWER


def test_decide_dispatch_charging_on_grid_above_cutoff_earning_on_use():
    hour = _hour(charge=Charge.CHARGING_ON_GRID, earning=Earning.EARNING_ON_USE, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=900, config=_config())

    assert decision.param.mode == DispatchMode.ONLY_CHARGE_FROM_PV
    assert decision.param.power == 0  # not below cutoff
    assert decision.param.pv_on is False  # earning-on-use security check


def test_decide_dispatch_charging_on_grid_above_cutoff_normal_price():
    hour = _hour(charge=Charge.CHARGING_ON_GRID, earning=Earning.EARNING_ON_RETURN, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=900, config=_config())

    assert decision.param.mode == DispatchMode.NO_BATTERY_CHARGE
    assert decision.param.power == 0


# --- decide_dispatch: NO_DISCHARGING -------------------------------------------


def test_decide_dispatch_no_discharging_below_cutoff():
    hour = _hour(charge=Charge.NO_DISCHARGING, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.mode == DispatchMode.ONLY_CHARGE_FROM_PV
    assert decision.param.power == FULL_POWER


def test_decide_dispatch_no_discharging_above_cutoff():
    hour = _hour(charge=Charge.NO_DISCHARGING, cutoff_soc=900)
    decision = decide_dispatch(hour, cur_soc=900, config=_config())

    assert decision.param.mode == DispatchMode.NO_BATTERY_CHARGE
    assert decision.param.power == 0


# --- decide_dispatch: shared fields --------------------------------------------


def test_decide_dispatch_sets_fixed_started_duration_para7():
    hour = _hour()
    decision = decide_dispatch(hour, cur_soc=500, config=_config())

    assert decision.param.started is True
    assert decision.param.duration == 3600
    assert decision.param.para7 == 255


# --- dispatch_params_equal -----------------------------------------------------


def _param(**overrides) -> DispatchParam:
    defaults = {
        "mode": DispatchMode.NORMAL,
        "started": True,
        "power": 100,
        "cutoff_soc": 900,
        "duration": 3600,
        "para7": 255,
        "pv_on": True,
    }
    defaults.update(overrides)
    return DispatchParam(**defaults)


def test_dispatch_params_equal_ignores_para7():
    a = _param(para7=255)
    b = _param(para7=0)
    assert dispatch_params_equal(a, b) is True


def test_dispatch_params_equal_detects_each_relevant_field_difference():
    base = _param()
    assert dispatch_params_equal(base, _param(started=False)) is False
    assert dispatch_params_equal(base, _param(mode=DispatchMode.NO_BATTERY_CHARGE)) is False
    assert dispatch_params_equal(base, _param(duration=1800)) is False
    assert dispatch_params_equal(base, _param(cutoff_soc=800)) is False
    assert dispatch_params_equal(base, _param(power=200)) is False
    assert dispatch_params_equal(base, _param(pv_on=False)) is False


# --- set_dispatch_msg -----------------------------------------------------------


def test_set_dispatch_msg_known_modes():
    assert set_dispatch_msg(DispatchMode.DEFAULT) == "Default"
    assert set_dispatch_msg(DispatchMode.ONLY_CHARGE_FROM_PV) == "Battery only charges from PV"
    assert set_dispatch_msg(DispatchMode.STATE_OF_CHARGE_CONTROL) == "State of Charge control"
    assert set_dispatch_msg(DispatchMode.LOAD_FOLLOWING) == "Load Following"
    assert set_dispatch_msg(DispatchMode.MAXIMISE_OUTPUT) == "Maximise Output"
    assert set_dispatch_msg(DispatchMode.NORMAL) == "Normal"
    assert set_dispatch_msg(DispatchMode.OPTIMISE_CONSUMPTION) == "Optimise Consumption"
    assert set_dispatch_msg(DispatchMode.MAXIMISE_CONSUMPTION) == "Maximise Consumption"
    assert set_dispatch_msg(DispatchMode.NO_BATTERY_CHARGE) == "No Battery Charge"


def test_set_dispatch_msg_unknown_modes():
    assert set_dispatch_msg(DispatchMode.FCAS) == "Unknown"
    assert set_dispatch_msg(DispatchMode.PV_POWER_SETTING) == "Unknown"


# --- decide_extra_pv_state -------------------------------------------------------


def test_decide_extra_pv_state_off_on_earning_on_use_when_battery_full():
    hour = Hour(earning=Earning.EARNING_ON_USE, cutoff_soc=900)
    assert decide_extra_pv_state(hour, cur_soc=900) == ExtraPvState.OFF


def test_decide_extra_pv_state_on_for_earning_on_use_below_cutoff():
    # Battery still has room -> keep the extra PV on so it can charge for
    # free/at a negative price instead of being curtailed for no reason.
    hour = Hour(earning=Earning.EARNING_ON_USE, cutoff_soc=900)
    assert decide_extra_pv_state(hour, cur_soc=500) == ExtraPvState.ON


def test_decide_extra_pv_state_on_for_no_earning():
    hour = Hour(earning=Earning.NO_EARNING, cutoff_soc=900)
    assert decide_extra_pv_state(hour, cur_soc=900) == ExtraPvState.ON


def test_decide_extra_pv_state_on_for_earning_on_return():
    hour = Hour(earning=Earning.EARNING_ON_RETURN, cutoff_soc=900)
    assert decide_extra_pv_state(hour, cur_soc=900) == ExtraPvState.ON
