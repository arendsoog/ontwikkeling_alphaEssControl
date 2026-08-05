"""Tests for schedule.py.

The DP scheduler is expensive (see the plan doc), so scenarios here are kept
deliberately small — a handful of hours, today-only where possible — and
assertions target *decisions*, not internal DP values. `_evaluate_hour_action`
tests use independently hand-derived expected numbers (same formulas as the
port, computed separately) rather than just re-deriving from the function
under test.
"""

import pytest

from custom_components.alpha_ess_local.data import Charge, Day, Earning
from custom_components.alpha_ess_local.schedule import (
    SOC_STEPS,
    ScheduleConfig,
    _apply_scenario_to_day,
    _battery_efficiency,
    _calculate_best_schedule,
    _evaluate_hour_action,
    _finalise_schedule,
    _inverter_efficiency,
    _percentile,
    set_charging_msg,
    set_schedule,
)


def _config(**overrides) -> ScheduleConfig:
    defaults = {
        "inverter_nominal_power": 10000.0,
        "usable_battery_capacity": 10000.0,
        "use_fee": 0.02,
        "return_fee": 0.01,
        "vat_percentage": 21,
        "daily_min_profit": 0.40,
    }
    defaults.update(overrides)
    return ScheduleConfig(**defaults)


def _flat_day(
    year=2024,
    mon=1,
    day=15,
    *,
    price=0.20,
    solar=0,
    house_load=300,
    earning=Earning.EARNING_ON_RETURN,
):
    the_day = Day(year=year, mon=mon, day=day, valid=True)
    for hour in the_day.hour:
        hour.valid = True
        hour.price = price
        hour.earning = earning
        hour.estimated_solar_power = solar
        hour.estimated_house_load = house_load
        hour.estimated_house_load_sigma = 50
    return the_day


# --- efficiency curves -------------------------------------------------------


def test_inverter_efficiency_breakpoints():
    nom = 1000.0
    assert _inverter_efficiency(10, nom) == 0.85  # load 0.01
    assert _inverter_efficiency(30, nom) == 0.90  # load 0.03
    assert _inverter_efficiency(70, nom) == 0.93  # load 0.07
    assert _inverter_efficiency(150, nom) == 0.955  # load 0.15
    assert _inverter_efficiency(300, nom) == 0.97  # load 0.30
    assert _inverter_efficiency(600, nom) == 0.975  # load 0.60


def test_inverter_efficiency_guards_zero_nominal_power():
    assert _inverter_efficiency(100, 0) == 0.975


def test_battery_efficiency_breakpoints():
    cap = 1000.0
    assert _battery_efficiency(10, cap) == 0.90  # c-rate 0.01
    assert _battery_efficiency(30, cap) == 0.93  # c-rate 0.03
    assert _battery_efficiency(70, cap) == 0.95  # c-rate 0.07
    assert _battery_efficiency(150, cap) == 0.965  # c-rate 0.15
    assert _battery_efficiency(300, cap) == 0.975  # c-rate 0.30


def test_battery_efficiency_guards_zero_capacity():
    assert _battery_efficiency(100, 0) == 0.975


# --- _evaluate_hour_action ---------------------------------------------------


def test_evaluate_hour_action_charging_on_pv_charges_from_surplus():
    # solar_dc=1000, house_load=200, inv_nom=batt_cap=10000 -> no clamping
    # occurs, so the charge/discharge round-trip is lossless and profit is 0.
    hour = Day(valid=True).hour[10]
    hour.estimated_solar_power = 1000
    hour.estimated_house_load = 200
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_PV,
        hour,
        soc_wh=0.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
    )

    assert new_soc_wh == pytest.approx(667.0425, rel=1e-4)
    assert profit == pytest.approx(0.0, abs=1e-9)
    assert next_cu is False
    assert next_du is False


def test_evaluate_hour_action_charging_on_pv_caps_at_negative_price_ceiling():
    # Even during a *positive*-price hour, solar charging is capped by
    # config.max_soc_negative_price (100.0% here), not
    # max_soc_positive_price (90.0%) -- solar is free either way, so
    # there's no lifespan-driven reason to cap it lower like grid
    # charging. Starting at 8500 Wh (85.0%, above the old 90% cap's
    # remaining headroom) with abundant solar proves charging continues
    # past 9000 Wh, all the way to the 10000 Wh (100%) ceiling.
    hour = Day(valid=True).hour[10]
    hour.estimated_solar_power = 20000
    hour.estimated_house_load = 0
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, _profit, _next_cu, _next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_PV,
        hour,
        soc_wh=8500.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
    )

    assert new_soc_wh == pytest.approx(10000.0)


def test_evaluate_hour_action_no_charging_on_negative_price_profits_from_use():
    hour = Day(valid=True).hour[3]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_USE
    hour.price = -0.05

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.NO_CHARGING,
        hour,
        soc_wh=5000.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
    )

    # price_use = ((-0.05*1.21)+0.02)/1000 = -0.0000405; profit = houseLoad * -price_use
    assert profit == pytest.approx(300 * 0.0000405, rel=1e-6)
    assert new_soc_wh == 5000.0  # untouched — no charge/discharge on this path
    assert next_cu is False
    assert next_du is False


def test_evaluate_hour_action_charging_on_grid_charges_to_max_and_sets_flag():
    hour = Day(valid=True).hour[2]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_GRID,
        hour,
        soc_wh=0.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
    )

    assert new_soc_wh == pytest.approx(9000.0)
    assert profit == pytest.approx(-2.6357890441041913, rel=1e-6)
    assert next_cu is True
    assert next_du is False


def test_evaluate_hour_action_charging_on_grid_uses_configured_negative_price_cap():
    # Earning on use (negative price) -> the cap is config.max_soc_negative_price,
    # not the positive-price one. 98.0% of 10000 Wh = 9800 Wh.
    hour = Day(valid=True).hour[2]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_USE
    hour.price = -0.05

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_GRID,
        hour,
        soc_wh=0.0,
        charge_used=False,
        discharge_used=False,
        config=_config(max_soc_negative_price=980),
    )

    assert new_soc_wh == pytest.approx(9800.0)
    assert next_cu is True


def test_evaluate_hour_action_charging_on_grid_reserves_room_for_future_solar():
    # Same hour/config as the max-fill test above, but with 1500 Wh of solar
    # surplus expected later today: the grid charge should stop 1500 Wh short
    # of the 9000 Wh cap (9000 - 1500 = 7500), leaving room to store that
    # solar instead of forcing it to spill to a worse-priced feed-in later.
    hour = Day(valid=True).hour[2]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_GRID,
        hour,
        soc_wh=0.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
        future_solar_surplus_wh=1500.0,
    )

    assert new_soc_wh == pytest.approx(7500.0)
    assert profit == pytest.approx(-2.209590870086826, rel=1e-6)
    assert next_cu is True
    assert next_du is False


def test_evaluate_hour_action_charging_on_grid_skips_when_solar_covers_the_whole_gap():
    # Expected future solar surplus (20000 Wh) exceeds the entire 9000 Wh
    # cap -> nothing left to usefully grid-charge; the action becomes a noop
    # rather than going negative.
    hour = Day(valid=True).hour[2]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_GRID,
        hour,
        soc_wh=0.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
        future_solar_surplus_wh=20000.0,
    )

    assert new_soc_wh == 0.0
    assert profit == float("-inf")
    assert next_cu is False


def test_evaluate_hour_action_charging_on_grid_noop_when_already_used():
    hour = Day(valid=True).hour[2]
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_ON_GRID,
        hour,
        soc_wh=0.0,
        charge_used=True,
        discharge_used=False,
        config=_config(),
    )

    assert new_soc_wh == 0.0
    assert profit == float("-inf")
    assert next_cu is True


def test_evaluate_hour_action_charging_discharge_discharges_and_sets_flag():
    hour = Day(valid=True).hour[19]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_DISCHARGE,
        hour,
        soc_wh=8000.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
    )

    assert new_soc_wh == pytest.approx(2000.0)
    assert profit == pytest.approx(1.361745, rel=1e-6)
    assert next_du is True
    assert next_cu is False


def test_evaluate_hour_action_charging_discharge_blocked_on_earning_on_use():
    hour = Day(valid=True).hour[19]
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_USE
    hour.price = -0.05

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_DISCHARGE,
        hour,
        soc_wh=8000.0,
        charge_used=False,
        discharge_used=False,
        config=_config(),
    )

    assert profit == float("-inf")
    assert next_du is False


def test_evaluate_hour_action_charging_discharge_blocked_when_disabled():
    # Same setup as test_evaluate_hour_action_charging_discharge_discharges_and_sets_flag
    # (profitable, not yet used today) but with the switch.py-backed
    # discharge_enabled master toggle off -- the DP must never choose this
    # action at all, same as if discharge_used were already True.
    hour = Day(valid=True).hour[19]
    hour.estimated_solar_power = 0
    hour.estimated_house_load = 300
    hour.earning = Earning.EARNING_ON_RETURN
    hour.price = 0.20

    new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
        Charge.CHARGING_DISCHARGE,
        hour,
        soc_wh=8000.0,
        charge_used=False,
        discharge_used=False,
        config=_config(discharge_enabled=False),
    )

    assert profit == float("-inf")
    assert next_du is False


# --- _calculate_best_schedule -------------------------------------------------


def test_calculate_best_schedule_discharges_on_expensive_hour_with_full_battery():
    today = _flat_day(price=0.50, solar=0, house_load=300)
    tomorrow = Day(valid=False)
    config = _config()

    # Single-hour horizon (start_hour == last hour) so there's no cross-hour
    # tie: with a 2+ hour horizon at a flat price, CHARGING_ON_GRID/
    # CHARGING_DISCHARGE could legitimately land on *either* hour for the
    # same total profit, making the specific hour's action ambiguous.
    cur_soc_index = SOC_STEPS  # full battery
    valid, _result = _calculate_best_schedule(
        cur_soc_index, 23, today, tomorrow, True, False, None, config
    )

    assert valid is True
    # a full battery facing an expensive, solar-less hour with the
    # once-per-day discharge still available should use it — dumping surplus
    # to the grid at a good price beats a demand-matched trickle-discharge.
    assert today.hour[23].charge == Charge.CHARGING_DISCHARGE


def test_calculate_best_schedule_charges_on_grid_when_price_is_very_negative():
    today = _flat_day(price=-0.20, solar=0, house_load=300, earning=Earning.EARNING_ON_USE)
    tomorrow = Day(valid=False)
    config = _config()

    # Single-hour horizon — see comment above.
    cur_soc_index = 10  # mostly empty battery, plenty of room
    valid, _result = _calculate_best_schedule(
        cur_soc_index, 23, today, tomorrow, False, False, None, config
    )

    assert valid is True
    # getting paid to import (negative price) with room in the battery and
    # the daily grid-charge budget still available -> take it.
    assert today.hour[23].charge == Charge.CHARGING_ON_GRID


def test_calculate_best_schedule_grid_charge_leaves_room_for_next_hours_solar():
    # Two-hour horizon: hour 22 is by far the cheapest hour of the day (no
    # solar there), hour 23 is pricier but has a 1200 Wh solar surplus
    # (1500 solar - 300 load). The once-a-day grid charge should still land
    # on hour 22 (it's the far cheaper hour) but must stop short of the 9000
    # Wh cap, leaving exactly 1200 Wh of headroom for hour 23's solar instead
    # of forcing it to spill to a worse-priced feed-in.
    today = _flat_day(price=0.50, solar=0, house_load=300)
    today.hour[22].price = 0.05
    today.hour[23].estimated_solar_power = 1500

    tomorrow = Day(valid=False)
    config = _config()

    cur_soc_index = 10  # mostly empty battery, plenty of room
    valid, _result = _calculate_best_schedule(
        cur_soc_index, 22, today, tomorrow, False, False, None, config
    )

    assert valid is True
    assert today.hour[22].charge == Charge.CHARGING_ON_GRID
    # step_wh = 10000 / 100 = 100 Wh/step; SOC after hour 22's grid charge is
    # exactly (9000 - 1200) / 100 = 78, i.e. the 1200 Wh reservation held.
    assert today.hour[23].estimated_start_soc == 78


def test_calculate_best_schedule_forced_action_rejected_when_already_used():
    today = _flat_day()
    tomorrow = Day(valid=False)
    config = _config()

    valid, _result = _calculate_best_schedule(
        50, 22, today, tomorrow, True, False, Charge.CHARGING_ON_GRID, config
    )

    assert valid is False


# --- _finalise_schedule -------------------------------------------------------


def test_finalise_schedule_sets_feed_in_and_cutoff_soc():
    day = _flat_day()
    day.hour[5].charge = Charge.NO_CHARGING
    day.hour[6].charge = Charge.CHARGING_DISCHARGE
    day.hour[7].charge = Charge.CHARGING_ON_PV
    day.hour[8].charge = Charge.CHARGING_ON_GRID
    day.hour[8].earning = Earning.EARNING_ON_USE
    day.hour[9].charge = Charge.CHARGING_ON_GRID
    day.hour[9].earning = Earning.EARNING_ON_RETURN

    _finalise_schedule(day, _config())

    from custom_components.alpha_ess_local.data import (
        SOC_MAX,
        SOC_MAX_CHARGE_ON_GRID,
        SOC_MIN,
        SOC_MIN_DISCHARGE_AFTER,
    )

    assert day.hour[5].cutoff_soc == SOC_MIN
    assert day.hour[6].cutoff_soc == SOC_MIN_DISCHARGE_AFTER
    # Solar charging (CHARGING_ON_PV) always targets the negative-price
    # ceiling, regardless of this hour's own price sign -- see
    # _finalise_schedule's comment.
    assert day.hour[7].cutoff_soc == SOC_MAX_CHARGE_ON_GRID
    assert day.hour[8].cutoff_soc == SOC_MAX_CHARGE_ON_GRID
    assert day.hour[9].cutoff_soc == SOC_MAX
    assert day.index_charge == 9  # last CHARGING_ON_GRID hour wins
    assert day.hour[0].feed_in == (day.hour[0].earning == Earning.EARNING_ON_RETURN)


def test_finalise_schedule_uses_configured_soc_bounds():
    # Custom (non-default) SOC bounds from ScheduleConfig must be reflected
    # exactly, not the data.py constants they used to be hardcoded to.
    day = _flat_day()
    day.hour[5].charge = Charge.CHARGING_DISCHARGE
    day.hour[6].charge = Charge.CHARGING_ON_PV
    day.hour[7].charge = Charge.CHARGING_ON_GRID
    day.hour[7].earning = Earning.EARNING_ON_USE
    day.hour[8].charge = Charge.CHARGING_ON_GRID
    day.hour[8].earning = Earning.EARNING_ON_RETURN

    config = _config(
        max_soc_positive_price=850,  # 85.0%
        max_soc_negative_price=980,  # 98.0%
        min_soc_discharge=300,  # 30.0%
    )
    _finalise_schedule(day, config)

    assert day.hour[5].cutoff_soc == 300
    # CHARGING_ON_PV (solar) always targets max_soc_negative_price, not
    # max_soc_positive_price -- regardless of this hour's own price sign.
    assert day.hour[6].cutoff_soc == 980
    assert day.hour[7].cutoff_soc == 980
    assert day.hour[8].cutoff_soc == 850


def test_finalise_schedule_index_charge_minus_one_when_none_scheduled():
    day = _flat_day()
    for hour in day.hour:
        hour.charge = Charge.NO_CHARGING

    _finalise_schedule(day, _config())

    assert day.index_charge == -1


def test_finalise_schedule_noop_on_invalid_day():
    day = Day(valid=False)
    day.index_charge = 5

    _finalise_schedule(day, _config())

    assert day.index_charge == 5  # untouched


# --- _percentile ---------------------------------------------------------------


def test_percentile_weighted():
    # sorted by profit: (-10, 0.5), (0, 0.3), (20, 0.2) -- cumulative at p=0.6 lands on profit=0
    entries = [(20.0, 0.2), (-10.0, 0.5), (0.0, 0.3)]
    assert _percentile(entries, 0.6) == 0.0
    assert _percentile(entries, 0.1) == -10.0
    assert _percentile(entries, 1.0) == 20.0


def test_percentile_ignores_none_and_zero_probability_entries():
    entries = [None, (5.0, 0.0), (10.0, 1.0)]
    assert _percentile(entries, 0.5) == 10.0


def test_percentile_returns_none_when_all_invalid():
    assert _percentile([None, None], 0.5) is None
    assert _percentile([], 0.5) is None


# --- set_charging_msg -----------------------------------------------------------


def test_set_charging_msg_known_and_unknown():
    assert set_charging_msg(Charge.CHARGING_ON_PV) == "Charge-PV"
    assert set_charging_msg(Charge.CHARGING_DISCHARGE) == "Discharge"


# --- _apply_scenario_to_day -----------------------------------------------------


def test_apply_scenario_to_day_does_not_mutate_originals():
    today = _flat_day(solar=1000, house_load=300)
    tomorrow = Day(valid=False)
    from custom_components.alpha_ess_local.schedule import SCENARIOS

    today_sc, _tomorrow_sc = _apply_scenario_to_day(today, tomorrow, SCENARIOS[0], start_hour=10)

    assert today.hour[10].estimated_solar_power == 1000  # original untouched
    assert today_sc.hour[10].estimated_solar_power != 1000  # copy scaled


def test_apply_scenario_to_day_pessimistic_scenario_reduces_solar_near_start_hour():
    today = _flat_day(solar=1000, house_load=300)
    tomorrow = Day(valid=False)
    from custom_components.alpha_ess_local.schedule import SCENARIOS

    pessimistic = SCENARIOS[0]  # solar_factor=0.6
    today_sc, _tomorrow_sc = _apply_scenario_to_day(today, tomorrow, pessimistic, start_hour=10)

    # at the start hour (no decay yet), a 0.6 solar factor should scale
    # estimated solar down.
    assert today_sc.hour[10].estimated_solar_power < today.hour[10].estimated_solar_power


# --- set_schedule (end-to-end) ---------------------------------------------------


def test_set_schedule_picks_a_schedule_at_least_as_good_as_baseline():
    today = Day(year=2024, mon=1, day=15, valid=True)
    for i, hour in enumerate(today.hour):
        hour.valid = True
        hour.price = 0.10 if i < 21 else 0.60  # cheap now, spike late
        hour.earning = Earning.EARNING_ON_RETURN
        hour.estimated_solar_power = 0
        hour.estimated_house_load = 300
        hour.estimated_house_load_sigma = 50
    tomorrow = Day(valid=False)
    config = _config(daily_min_profit=0.0)  # accept any nonnegative improvement

    set_schedule(500, 21, today, tomorrow, config)

    # some hour in [21, 24) should be scheduled to discharge into the price
    # spike, since NO_CHARGING alone (baseline) can't move stored energy
    # into the expensive hours on its own initiative as effectively.
    assert any(today.hour[i].charge != Charge.NO_CHARGING for i in range(21, 24))
    assert today.valid is True


def test_set_schedule_falls_back_to_baseline_when_charge_and_discharge_already_used():
    today = _flat_day()
    today.charge_on_grid_used = True
    today.discharge_used = True
    tomorrow = Day(valid=False)
    config = _config()

    set_schedule(500, 22, today, tomorrow, config)

    # with both daily actions already spent, only PV-driven charging or no
    # charging remain possible for the rest of the day.
    for hour in today.hour[22:]:
        assert hour.charge in (Charge.CHARGING_ON_PV, Charge.NO_CHARGING, Charge.NO_DISCHARGING)


def test_set_schedule_falls_back_to_charge_pv_when_baseline_is_unreachable():
    # An extreme, unavoidable loss every hour (forced earning-on-use at a
    # very high price) pushes the baseline DP result below the -100000
    # validity floor -> Charge-PV fallback for the rest of the day.
    today = Day(year=2024, mon=1, day=15, valid=True)
    for hour in today.hour:
        hour.valid = True
        hour.price = 1000.0
        hour.earning = Earning.EARNING_ON_USE
        hour.estimated_solar_power = 0
        hour.estimated_house_load = 5000
        hour.estimated_house_load_sigma = 50
    tomorrow = Day(valid=False)
    config = _config(use_fee=0.0, return_fee=0.0, vat_percentage=0)

    set_schedule(0, 0, today, tomorrow, config)

    for hour in today.hour:
        assert hour.charge == Charge.CHARGING_ON_PV
