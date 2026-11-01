"""Tests for the fixed time slots (time_slots.py) and how the planner and
the dispatch use them."""

from datetime import date, datetime

import pytest
import voluptuous as vol

from custom_components.alpha_ess_local.data import Charge, Day, Earning, Hour
from custom_components.alpha_ess_local.dispatch import (
    DispatchConfig,
    decide_dispatch,
    regulate_grid_charge_power,
)
from custom_components.alpha_ess_local.protocol import DispatchMode
from custom_components.alpha_ess_local.schedule import (
    SOC_STEPS,
    ScheduleConfig,
    _calculate_best_schedule,
    _finalise_schedule,
)
from custom_components.alpha_ess_local.time_slots import (
    SLOTS_SCHEMA,
    FixedHour,
    fixed_hours,
    slot_at,
    time_slots,
)

# 2026-11-02 is a Monday (weekday 0).
MONDAY = date(2026, 11, 2)


def _slot(**overrides):
    slot = {
        "id": "a",
        "start": 2,
        "end": 5,
        "days": [0],
        "action": "charge_grid",
        "soc": 80,
        "power": 1200,
        "enabled": True,
    }
    slot.update(overrides)
    return slot


def _options(*slots):
    return {"time_slots": list(slots)}


# --- which hours a slot covers ---------------------------------------------------


def test_slot_covers_its_hours_on_its_days():
    options = _options(_slot())

    assert slot_at(options, datetime(2026, 11, 2, 2, 30)) == FixedHour(
        Charge.CHARGING_ON_GRID, 800, 1200
    )
    assert slot_at(options, datetime(2026, 11, 2, 4, 59)) is not None
    assert slot_at(options, datetime(2026, 11, 2, 5, 0)) is None  # end is exclusive
    assert slot_at(options, datetime(2026, 11, 3, 3, 0)) is None  # Tuesday


def test_slot_past_midnight_ends_on_the_next_day():
    # Monday 22:00 to Tuesday 06:00.
    options = _options(_slot(start=22, end=6))

    assert slot_at(options, datetime(2026, 11, 2, 23, 0)) is not None
    assert slot_at(options, datetime(2026, 11, 3, 5, 0)) is not None
    assert slot_at(options, datetime(2026, 11, 3, 22, 0)) is None  # not on Tuesday
    assert slot_at(options, datetime(2026, 11, 2, 3, 0)) is None  # Sunday's night


def test_disabled_slot_covers_nothing():
    assert slot_at(_options(_slot(enabled=False)), datetime(2026, 11, 2, 3, 0)) is None


def test_fixed_hours_for_today_and_tomorrow():
    options = _options(_slot(days=[0, 1], soc=None, power=None))

    fixed = fixed_hours(options, MONDAY)

    assert sorted(fixed) == [2, 3, 4, 26, 27, 28]
    assert fixed[26] == FixedHour(Charge.CHARGING_ON_GRID)


def test_time_slots_drop_fields_their_action_has_not():
    slots = time_slots(_options(_slot(action="no_charging")))

    assert slots[0]["soc"] is None
    assert slots[0]["power"] is None


def test_slots_schema_accepts_a_slot():
    assert SLOTS_SCHEMA([_slot()])[0]["action"] == "charge_grid"


@pytest.mark.parametrize(
    "slots",
    [
        [_slot(action="fly")],
        [_slot(power=50)],  # under the 100 W minimum
        [_slot(days=[])],
        [_slot(id=str(i)) for i in range(9)],  # at most 8
    ],
)
def test_slots_schema_rejects_bad_slots(slots):
    with pytest.raises(vol.Invalid):
        SLOTS_SCHEMA(slots)


# --- the planner -----------------------------------------------------------------


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


def _flat_day(price=0.20, solar=0, house_load=300):
    the_day = Day(year=2026, mon=11, day=2, valid=True)
    for hour in the_day.hour:
        hour.valid = True
        hour.price = price
        hour.earning = Earning.EARNING_ON_RETURN
        hour.estimated_solar_power = solar
        hour.estimated_house_load = house_load
        hour.estimated_house_load_sigma = 50
    return the_day


def test_planner_keeps_a_slot_it_would_never_choose():
    # Grid charging at a flat price only loses money: without the slot the
    # plan never does it.
    today = _flat_day()
    config = _config(fixed_hours={20: FixedHour(Charge.CHARGING_ON_GRID, soc=600, power=2000)})

    valid, _result = _calculate_best_schedule(
        20, 20, today, Day(valid=False), True, True, None, config
    )
    _finalise_schedule(today, Day(valid=False), config)

    assert valid is True
    assert today.hour[20].charge == Charge.CHARGING_ON_GRID
    assert today.hour[20].cutoff_soc == 600
    # Started at 20%: the slot's 2000 W put 20% in, not more.
    assert today.hour[21].estimated_start_soc == 40
    assert today.hour[21].charge != Charge.CHARGING_ON_GRID


def test_planner_slot_at_its_soc_holds():
    today = _flat_day()
    config = _config(fixed_hours={20: FixedHour(Charge.CHARGING_ON_GRID, soc=500)})

    valid, _result = _calculate_best_schedule(
        60, 20, today, Day(valid=False), True, True, None, config
    )

    # Already above the slot's SOC: still the slot's hour, nothing bought.
    assert valid is True
    assert today.hour[20].charge == Charge.CHARGING_ON_GRID
    assert today.hour[21].estimated_start_soc <= 60


def test_planner_discharge_slot_down_to_its_soc_even_when_switched_off():
    today = _flat_day()
    config = _config(
        discharge_enabled=False,
        fixed_hours={20: FixedHour(Charge.CHARGING_DISCHARGE, soc=500, power=3000)},
    )

    valid, _result = _calculate_best_schedule(
        SOC_STEPS, 20, today, Day(valid=False), True, True, None, config
    )

    assert valid is True
    assert today.hour[20].charge == Charge.CHARGING_DISCHARGE
    # 3000 W out of a 10 kWh battery: 30% in the hour.
    assert today.hour[21].estimated_start_soc == 70


# --- the dispatch ----------------------------------------------------------------


def test_dispatch_uses_the_slot_power():
    hour = Hour(
        valid=True,
        earning=Earning.EARNING_ON_RETURN,
        charge=Charge.CHARGING_ON_GRID,
        cutoff_soc=800,
    )
    config = DispatchConfig(
        usable_battery_capacity=10000, max_grid_load=8000, max_charge_power=1200
    )

    decision = decide_dispatch(hour, 300, config, 300)

    assert decision.param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    assert decision.param.power == 1200
    assert decision.param.cutoff_soc == 800


def test_dispatch_discharges_at_the_slot_power():
    hour = Hour(
        valid=True,
        earning=Earning.EARNING_ON_RETURN,
        charge=Charge.CHARGING_DISCHARGE,
        cutoff_soc=500,
    )
    config = DispatchConfig(usable_battery_capacity=10000, max_discharge_power=2500)

    decision = decide_dispatch(hour, 900, config)

    assert decision.param.power == -2500


def test_grid_charge_regulation_stays_under_the_slot_power():
    config = DispatchConfig(
        usable_battery_capacity=10000, max_grid_load=8000, max_charge_power=1200
    )

    # Lots of headroom under the cap: still no more than the slot's power.
    assert regulate_grid_charge_power(1000, 2000, config) == 1200
