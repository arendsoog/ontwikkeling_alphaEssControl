"""DP battery-charge scheduler.

Port of Schedule.c. Given a Day (today, optionally tomorrow) already
populated with prices (prices.py) and solar/load estimates
(solar.py/storage.py), decides per hour which of the 5 `data.Charge` actions
to take, maximizing total profit subject to SOC bounds and a once-per-day
limit on grid-charging and discharging.

Deviations from the original, confirmed with the user:
- `SOC_STEPS` is 100 (1% resolution), not 1000 (0.1%) — ~10x fewer DP cells
  for no real precision loss, keeping this feasible in pure Python.
- The original's column-aligned text-log formatting (ShowSchedule,
  LogExecutionResult, SetProviderOverrideMsg, SetTotalPowerMsg) is replaced
  with structured LOGGER.debug() calls.

Standalone/pure computation: no `hass` dependency. Nothing wires this into a
coordinator yet — `set_schedule` needs the *current* hour and SOC, which
only the live control loop (a later phase) can supply. A full `set_schedule`
call does ~27 DP passes and takes real wall-clock time (order of several
seconds) — that phase must call it via `hass.async_add_executor_job`.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass

from .const import LOGGER
from .data import (
    MAX_HOURS,
    SOC_MAX,
    SOC_MAX_BATTERY,
    SOC_MAX_CHARGE_ON_GRID,
    SOC_MIN,
    SOC_MIN_DISCHARGE_AFTER,
    Charge,
    Day,
    Earning,
    Hour,
)
from .prices import mk_return_price, mk_use_price

SOC_STEPS = 100
CHARGE_LIMIT = 10000.0  # Wh
DISCHARGE_LIMIT = 10000.0  # Wh
MIN_CHARGE_POWER = 100.0
MIN_DISCHARGE_POWER = 100.0
ETA_GRID_TO_BATT = 0.97

SCENARIO_TAU_HOURS = 6.0
SOLAR_SCENARIO_MIN_WH = 300.0
HOUSE_LOAD_SIGMA_GAIN = 0.8

RISK_PERCENTILE = 0.20
LAMBDA = 0.30

_CHARGE_MESSAGES = {
    Charge.CHARGING_ON_PV: "Charge-PV",
    Charge.NO_CHARGING: "No-charging",
    Charge.NO_DISCHARGING: "No-discharging",
    Charge.CHARGING_ON_GRID: "Charge-grid",
    Charge.CHARGING_DISCHARGE: "Discharge",
}


def set_charging_msg(charge: Charge) -> str:
    """Port of SetChargingMsg: a short label for a charge action."""
    return _CHARGE_MESSAGES.get(charge, "Unknown")


@dataclass
class ScheduleConfig:
    """The subset of options-flow config the scheduler needs.

    `max_soc_positive_price`/`max_soc_negative_price`/`min_soc_discharge`
    are in the same 0.1%-unit scale as `data.SOC_MAX`/`SOC_MIN` (e.g. 900 =
    90.0%) — user-configurable versions of what used to be the hardcoded
    SOC_MAX/SOC_MAX_CHARGE_ON_GRID/SOC_MIN_DISCHARGE_AFTER constants;
    defaulted to those same values so a caller that doesn't pass them keeps
    the original behavior.
    """

    inverter_nominal_power: float
    usable_battery_capacity: float
    use_fee: float
    return_fee: float
    vat_percentage: float
    daily_min_profit: float
    max_soc_positive_price: int = SOC_MAX
    max_soc_negative_price: int = SOC_MAX_CHARGE_ON_GRID
    min_soc_discharge: int = SOC_MIN_DISCHARGE_AFTER
    # Master on/off for the once-per-day deliberate CHARGING_DISCHARGE
    # action (switch.py's live device toggle) — off means the DP never
    # considers it at all, same as if discharge_used were already True
    # all day. Normal discharge covering house load elsewhere is
    # unaffected; this only gates this one arbitrage action.
    discharge_enabled: bool = True
    # Cap on house load + grid-charge power *combined*, in Wh (i.e. Wh per
    # the DP's 1-hour steps == average kW that hour) — a live slider
    # (number.py's "Max grid load", 5-10 kW) aimed at Belgian
    # "capaciteitstarief"/capaciteitskost, which bills on your highest
    # 15-minute grid-import peak per month.
    #
    # Deliberately does NOT constrain _evaluate_hour_action's CHARGING_ON_GRID
    # math (cutoff_soc, i.e. *how far* to charge, is purely
    # solar-reservation-driven — see _finalise_schedule) — this cap only
    # throttles *how fast* dispatch.py's live ~20s power command gets there
    # (see dispatch.DispatchConfig.max_grid_load, orchestrator.py's shared
    # _effective_max_grid_load_wh). Kept on ScheduleConfig anyway because
    # _apply_grid_load_shortfall (below) uses it to warn — and flag to
    # orchestrator.py's bookkeeping — when that rate cap may leave a single
    # grid-charge hour short of its own planned target.
    max_grid_load: float = CHARGE_LIMIT


@dataclass(frozen=True)
class _Scenario:
    solar_factor: float
    load_factor: float
    probability: float


SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario(0.6, 1.1, 0.28),  # less sun, higher load (pessimistic)
    _Scenario(0.85, 0.9, 0.27),  # less sun, less load
    _Scenario(1.0, 1.0, 0.15),  # nominal
    _Scenario(1.15, 1.1, 0.18),  # more sun, higher load
    _Scenario(1.25, 0.9, 0.12),  # more sun, less load (optimistic)
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _inverter_efficiency(power: float, inverter_nominal_power: float) -> float:
    if inverter_nominal_power <= 0:
        return 0.975
    load = power / inverter_nominal_power
    if load < 0.02:
        return 0.85
    if load < 0.05:
        return 0.90
    if load < 0.10:
        return 0.93
    if load < 0.20:
        return 0.955
    if load < 0.50:
        return 0.97
    return 0.975


def _battery_efficiency(power: float, battery_capacity: float) -> float:
    if battery_capacity <= 0:
        return 0.975
    c_rate = power / battery_capacity
    if c_rate < 0.02:
        return 0.90
    if c_rate < 0.05:
        return 0.93
    if c_rate < 0.10:
        return 0.95
    if c_rate < 0.20:
        return 0.965
    return 0.975


def _evaluate_hour_action(
    action: Charge,
    the_hour: Hour,
    soc_wh: float,
    charge_used: bool,
    discharge_used: bool,
    config: ScheduleConfig,
    future_solar_surplus_wh: float = 0.0,
) -> tuple[float, float, bool, bool]:
    """Port of EvaluateHourAction.

    Simulates one action for one hour. Returns (new_soc_wh, profit,
    next_charge_used, next_discharge_used).

    `future_solar_surplus_wh`: total expected solar surplus (solar minus
    house load, summed over every later hour in this DP run) — used only by
    CHARGING_ON_GRID, so the once-a-day grid charge doesn't fill the battery
    right up to the cap and leave no room for solar that's still coming
    later today (which would otherwise just spill to a worse-priced feed-in
    instead of being stored).
    """
    inverter_nominal_power = config.inverter_nominal_power
    battery_capacity = config.usable_battery_capacity

    solar_dc = the_hour.estimated_solar_power
    house_load = the_hour.estimated_house_load
    earning = the_hour.earning

    price_use = mk_use_price(the_hour.price, config.use_fee, config.vat_percentage) / 1000.0
    price_return = (
        mk_return_price(the_hour.price, config.return_fee, config.vat_percentage) / 1000.0
    )

    if earning == Earning.EARNING_ON_USE:
        solar_dc = 0

    solar_ac = solar_dc * _inverter_efficiency(
        min(solar_dc, inverter_nominal_power), inverter_nominal_power
    )
    solar_ac = min(solar_ac, inverter_nominal_power)
    feed_in = solar_ac - house_load

    new_soc_wh = soc_wh
    profit = float("-inf")
    next_charge_used = charge_used
    next_discharge_used = discharge_used

    max_soc = config.max_soc_positive_price / SOC_MAX_BATTERY * battery_capacity
    max_soc_eou = config.max_soc_negative_price / SOC_MAX_BATTERY * battery_capacity
    min_soc = SOC_MIN / SOC_MAX_BATTERY * battery_capacity
    max_discharge_after = config.min_soc_discharge / SOC_MAX_BATTERY * battery_capacity

    if action == Charge.CHARGING_ON_PV:
        if earning != Earning.EARNING_ON_USE:
            if feed_in > 0.0:
                space_soc = max_soc_eou - soc_wh
                charge_ac = min(feed_in, CHARGE_LIMIT)
                if charge_ac >= MIN_CHARGE_POWER:
                    inv_eff = _inverter_efficiency(charge_ac, inverter_nominal_power)
                    batt_eff = _battery_efficiency(charge_ac, battery_capacity)
                    charge_soc = min(charge_ac * inv_eff * batt_eff, space_soc)
                    charge_ac = charge_soc / (inv_eff * batt_eff)
                    new_soc_wh = soc_wh + charge_soc
                    profit = (feed_in - charge_ac) * price_return
            else:
                available_soc = soc_wh - min_soc
                discharge_ac = min(-feed_in, DISCHARGE_LIMIT)
                if discharge_ac < MIN_DISCHARGE_POWER:
                    discharge_ac = 0.0
                inv_eff = _inverter_efficiency(discharge_ac, inverter_nominal_power)
                batt_eff = _battery_efficiency(discharge_ac, battery_capacity)
                discharge_soc = min(discharge_ac / (inv_eff * batt_eff), available_soc)
                discharge_ac = discharge_soc * inv_eff * batt_eff
                new_soc_wh = soc_wh - discharge_soc
                profit = (feed_in + discharge_ac) * price_use

    elif action == Charge.NO_CHARGING:
        if earning == Earning.EARNING_ON_USE:
            profit = house_load * -price_use
        elif feed_in > 0.0:
            profit = feed_in * price_return
        else:
            available_soc = soc_wh - min_soc
            discharge_ac = min(-feed_in, DISCHARGE_LIMIT)
            if discharge_ac < MIN_DISCHARGE_POWER:
                discharge_ac = 0.0
            inv_eff = _inverter_efficiency(discharge_ac, inverter_nominal_power)
            batt_eff = _battery_efficiency(discharge_ac, battery_capacity)
            discharge_soc = min(discharge_ac / (inv_eff * batt_eff), available_soc)
            discharge_ac = discharge_soc * inv_eff * batt_eff
            new_soc_wh = soc_wh - discharge_soc
            profit = (feed_in + discharge_ac) * price_use

    elif action == Charge.NO_DISCHARGING:
        if feed_in > 0.0:
            space_soc = max_soc_eou - soc_wh
            charge_ac = min(feed_in, CHARGE_LIMIT)
            if charge_ac >= MIN_CHARGE_POWER:
                inv_eff = _inverter_efficiency(charge_ac, inverter_nominal_power)
                batt_eff = _battery_efficiency(charge_ac, battery_capacity)
                charge_soc = min(charge_ac * inv_eff * batt_eff, space_soc)
                charge_ac = charge_soc / (inv_eff * batt_eff)
                new_soc_wh = soc_wh + charge_soc
                profit = (feed_in - charge_ac) * price_return
        else:
            profit = feed_in * price_use

    elif action == Charge.CHARGING_ON_GRID:
        if not charge_used:
            # max_grid_load is deliberately NOT applied here: it only
            # throttles how fast dispatch.py actually gets there (see
            # decide_dispatch), never how far the DP plans to charge. The
            # target itself (what _finalise_schedule turns into cutoff_soc)
            # is purely solar-reservation-driven, below.
            max_capacity = max_soc_eou if earning == Earning.EARNING_ON_USE else max_soc
            available_gap = max_capacity - soc_wh
            reserved_for_solar = min(future_solar_surplus_wh, max(available_gap, 0.0))
            charge_soc = min(available_gap - reserved_for_solar, CHARGE_LIMIT)
            if charge_soc > 0.0:
                charge_ac_est = charge_soc / ETA_GRID_TO_BATT
                batt_eff = _battery_efficiency(charge_ac_est, battery_capacity)
                inv_eff = _inverter_efficiency(charge_ac_est, inverter_nominal_power)
                charge_ac = charge_soc / (inv_eff * batt_eff * ETA_GRID_TO_BATT)
                if charge_ac >= MIN_CHARGE_POWER:
                    new_soc_wh = soc_wh + charge_soc
                    feed_in -= charge_ac
                    profit = feed_in * (price_return if feed_in > 0.0 else price_use)
                    next_charge_used = True

    elif (
        action == Charge.CHARGING_DISCHARGE
        and earning != Earning.EARNING_ON_USE
        and not discharge_used
        and config.discharge_enabled
    ):
        discharge_soc = min(soc_wh - max(min_soc, max_discharge_after), DISCHARGE_LIMIT)
        if discharge_soc > 0.0:
            batt_eff = _battery_efficiency(discharge_soc, battery_capacity)
            inv_eff = _inverter_efficiency(discharge_soc, inverter_nominal_power)
            discharge_ac = discharge_soc * batt_eff * inv_eff
            if discharge_ac >= MIN_DISCHARGE_POWER:
                new_soc_wh = soc_wh - discharge_soc
                feed_in += discharge_ac
                profit = feed_in * (price_return if feed_in > 0.0 else price_use)
                next_discharge_used = True

    new_soc_wh = _clamp(new_soc_wh, 0.0, float(battery_capacity))
    return new_soc_wh, profit, next_charge_used, next_discharge_used


@dataclass
class _DPEntry:
    result: float = 0.0
    hour_result: float = 0.0
    charge: Charge = Charge.NO_CHARGING
    next_soc_index: int = 0
    next_charge_used: bool = False
    next_discharge_used: bool = False


def _calculate_best_schedule(
    cur_soc_index: int,
    start_hour: int,
    today: Day,
    tomorrow: Day,
    charge_used_before: bool,
    discharge_used_before: bool,
    forced_action: Charge | None,
    config: ScheduleConfig,
) -> tuple[bool, float]:
    """Port of CalculateBestSchedule.

    Backward-induction DP over (hour, SOC index, charge-used, discharge-used),
    then forward-traces from (start_hour, cur_soc_index, ...) to fill
    today/tomorrow's hour[].charge/.estimated_start_soc/.estimated_result in
    place for [start_hour, end_hour). Returns (valid, dp_result).
    """
    if forced_action is not None:
        if charge_used_before and forced_action == Charge.CHARGING_ON_GRID:
            return False, 0.0
        if discharge_used_before and forced_action == Charge.CHARGING_DISCHARGE:
            return False, 0.0

    end_hour = 0
    if today.valid:
        end_hour = MAX_HOURS
    if tomorrow.valid:
        end_hour = MAX_HOURS * 2

    battery_capacity = config.usable_battery_capacity
    step_wh = battery_capacity / SOC_STEPS if SOC_STEPS else 0.0

    dp: list[list[list[list[_DPEntry]]]] = []
    for _h in range(MAX_HOURS * 2 + 1):
        hour_layer = []
        for si in range(SOC_STEPS + 1):
            soc_layer = []
            for cu in (False, True):
                cu_layer = [
                    _DPEntry(next_soc_index=si, next_charge_used=cu, next_discharge_used=du)
                    for du in (False, True)
                ]
                soc_layer.append(cu_layer)
            hour_layer.append(soc_layer)
        dp.append(hour_layer)

    future_solar_surplus_after = 0.0
    for hour in range(end_hour - 1, start_hour - 1, -1):
        the_hour = tomorrow.hour[hour - MAX_HOURS] if hour >= MAX_HOURS else today.hour[hour]
        for si in range(SOC_STEPS + 1):
            soc_wh = si * step_wh
            for cu in (False, True):
                for du in (False, True):
                    best_result = float("-inf")
                    best_hour_profit = float("-inf")
                    best_action = Charge.NO_CHARGING
                    best_next_soc_index = si
                    best_next_cu = cu
                    best_next_du = du

                    for action in Charge:
                        new_soc_wh, profit, next_cu, next_du = _evaluate_hour_action(
                            action, the_hour, soc_wh, cu, du, config, future_solar_surplus_after
                        )
                        if (
                            forced_action is not None
                            and hour == start_hour
                            and action != forced_action
                        ):
                            profit = float("-inf")

                        ns = round(new_soc_wh / step_wh) if step_wh else 0
                        ns = int(_clamp(ns, 0, SOC_STEPS))

                        reset_cu, reset_du = next_cu, next_du
                        if (hour + 1) % 24 == 0:
                            reset_cu, reset_du = False, False

                        result = profit + dp[hour + 1][ns][reset_cu][reset_du].result

                        if result > best_result:
                            best_result = result
                            best_hour_profit = profit
                            best_action = action
                            best_next_soc_index = ns
                            best_next_cu = next_cu
                            best_next_du = next_du

                    dp[hour][si][cu][du] = _DPEntry(
                        result=best_result,
                        hour_result=best_hour_profit,
                        charge=best_action,
                        next_soc_index=best_next_soc_index,
                        next_charge_used=best_next_cu,
                        next_discharge_used=best_next_du,
                    )

        future_solar_surplus_after += max(
            0.0, the_hour.estimated_solar_power - the_hour.estimated_house_load
        )

    si = cur_soc_index
    cu = charge_used_before
    du = discharge_used_before
    dp_result = dp[start_hour][si][cu][du].result

    for hour in range(start_hour, end_hour):
        the_hour = tomorrow.hour[hour - MAX_HOURS] if hour >= MAX_HOURS else today.hour[hour]
        if hour != start_hour and hour % 24 == 0:
            cu, du = False, False

        the_hour.estimated_start_soc = si
        entry = dp[hour][si][cu][du]
        the_hour.charge = entry.charge
        the_hour.estimated_result = entry.hour_result

        si = entry.next_soc_index
        cu = entry.next_charge_used
        du = entry.next_discharge_used

    if forced_action is not None and today.hour[start_hour].charge != forced_action:
        return False, dp_result
    if dp_result < -100000:
        return False, dp_result
    return True, dp_result


def _apply_scenario_to_day(
    today: Day, tomorrow: Day, scenario: _Scenario, start_hour: int
) -> tuple[Day, Day]:
    """Port of ApplyScenarioToDay: returns scaled *copies*, originals untouched."""
    today_sc = copy.deepcopy(today)
    tomorrow_sc = copy.deepcopy(tomorrow)

    end_hour = 0
    if today_sc.valid:
        end_hour = MAX_HOURS
    if tomorrow_sc.valid:
        end_hour = MAX_HOURS * 2

    for hour in range(end_hour):
        the_hour = tomorrow_sc.hour[hour - MAX_HOURS] if hour >= MAX_HOURS else today_sc.hour[hour]
        dh = max(0, hour - start_hour)
        decay = math.exp(-dh / SCENARIO_TAU_HOURS)
        solar_factor = 1.0 + decay * (scenario.solar_factor - 1.0)
        load_factor = 1.0 + decay * (scenario.load_factor - 1.0)

        solar = the_hour.estimated_solar_power
        solar_weight = solar / (solar + SOLAR_SCENARIO_MIN_WH)
        effective_solar_factor = 1.0 + solar_weight * (solar_factor - 1.0)
        the_hour.estimated_solar_power = int(
            the_hour.estimated_solar_power * effective_solar_factor
        )

        load_mean = the_hour.estimated_house_load
        load_sigma = the_hour.estimated_house_load_sigma
        rel_sigma = load_sigma / load_mean if load_mean > 1.0 else 0.0
        sigma_amplifier = _clamp(1.0 + HOUSE_LOAD_SIGMA_GAIN * rel_sigma, 1.0, 1.6)
        effective_load_factor = _clamp(1.0 + sigma_amplifier * (load_factor - 1.0), 0.5, 2.0)
        the_hour.estimated_house_load = int(the_hour.estimated_house_load * effective_load_factor)

    return today_sc, tomorrow_sc


def _percentile(entries: list[tuple[float, float] | None], p: float) -> float | None:
    """Port of Percentile: probability-weighted percentile over (profit,
    probability) pairs. `None` entries (invalid scenarios) are dropped.
    Returns `None` if nothing valid remains (matches the C `false` return)."""
    valid = [entry for entry in entries if entry is not None and entry[1] > 0.0]
    if not valid:
        return None

    prob_sum = sum(prob for _, prob in valid)
    normalized = sorted(
        ((profit, prob / prob_sum) for profit, prob in valid), key=lambda item: item[0]
    )

    cum = 0.0
    for profit, prob in normalized:
        cum += prob
        if cum >= p:
            return profit
    return normalized[-1][0]


def _finalise_schedule(today: Day, tomorrow: Day, config: ScheduleConfig) -> None:
    """Port of FinaliseSchedule: sets feed_in/cutoff_soc per hour and
    index_charge from the chosen schedule.

    Takes both days (not just one) so CHARGING_ON_GRID's cutoff_soc can look
    at the *next* hour even when that's tomorrow's hour 0 — see below.
    """
    end_hour = 0
    if today.valid:
        end_hour = MAX_HOURS
    if tomorrow.valid:
        end_hour = MAX_HOURS * 2

    def _hour_at(index: int) -> Hour:
        return tomorrow.hour[index - MAX_HOURS] if index >= MAX_HOURS else today.hour[index]

    if today.valid:
        today.index_charge = -1
    if tomorrow.valid:
        tomorrow.index_charge = -1
    for i in range(end_hour):
        if _hour_at(i).charge == Charge.CHARGING_ON_GRID:
            if i >= MAX_HOURS:
                tomorrow.index_charge = i - MAX_HOURS
            else:
                today.index_charge = i

    for i in range(end_hour):
        hour = _hour_at(i)
        hour.feed_in = hour.earning == Earning.EARNING_ON_RETURN
        if hour.charge == Charge.NO_CHARGING:
            hour.cutoff_soc = SOC_MIN
        elif hour.charge == Charge.CHARGING_DISCHARGE:
            hour.cutoff_soc = config.min_soc_discharge
        elif hour.charge in (Charge.NO_DISCHARGING, Charge.CHARGING_ON_PV):
            # Solar charging (not grid) — always allowed up to the
            # negative-price ceiling, regardless of the hour's own price
            # sign: the energy is free either way, so there's no
            # lifespan-driven reason to cap it lower like grid charging.
            hour.cutoff_soc = config.max_soc_negative_price
        elif hour.charge == Charge.CHARGING_ON_GRID:
            # cutoff_soc is the *target* SOC to charge up to — not the flat
            # SOC ceiling. Derived from what the DP actually planned (the
            # next hour's estimated_start_soc, already net of
            # reserved_for_solar — see _evaluate_hour_action), converted
            # from the DP's SOC_STEPS-index scale to cutoff_soc's native
            # 0.1%-unit scale. This is deliberately independent of
            # max_grid_load, which only throttles how *fast* dispatch.py
            # gets there (see dispatch.py's decide_dispatch), never how far.
            # Falls back to the flat ceiling for the schedule's very last
            # hour, where there's no "next hour" to know a target from.
            target_index = _hour_at(i + 1).estimated_start_soc if i + 1 < end_hour else SOC_STEPS
            hour.cutoff_soc = round(target_index / SOC_STEPS * SOC_MAX_BATTERY)


def _assign_day(dest: Day, src: Day) -> None:
    """Port of C's struct assignment (`*today = todayOpt;`): copies all of
    src's fields into dest in place, preserving dest's identity."""
    dest.__dict__.update(src.__dict__)


def _apply_grid_load_shortfall(today: Day, tomorrow: Day, config: ScheduleConfig) -> None:
    """Estimates, for each CHARGING_ON_GRID hour, whether the max_grid_load
    rate cap will leave it short of its own cutoff_soc target — and if so,
    records the shortfall on `hour.estimated_grid_charge_shortfall_wh` (read
    by orchestrator.py's dispatch coordinator, see that field's docstring)
    and logs it. Does not change the chosen schedule itself.

    cutoff_soc (how far a CHARGING_ON_GRID hour targets, see
    _finalise_schedule) is purely solar-reservation-driven and knows nothing
    about max_grid_load — that cap only throttles dispatch.py's live power
    command, i.e. how *fast* the battery can actually get there within that
    single hour. This estimates the resulting shortfall (ignoring inverter/
    battery efficiency losses — a rough, conservative check, not a re-run of
    the DP).
    """
    if math.isinf(config.max_grid_load):
        return

    end_hour = MAX_HOURS if today.valid else 0
    if tomorrow.valid:
        end_hour = MAX_HOURS * 2

    def _hour_at(index: int) -> Hour:
        return tomorrow.hour[index - MAX_HOURS] if index >= MAX_HOURS else today.hour[index]

    for charge_hour in range(end_hour):
        hour = _hour_at(charge_hour)
        if hour.charge != Charge.CHARGING_ON_GRID:
            continue

        start_wh = hour.estimated_start_soc / SOC_STEPS * config.usable_battery_capacity
        target_wh = hour.cutoff_soc / SOC_MAX_BATTERY * config.usable_battery_capacity
        needed_wh = target_wh - start_wh
        if needed_wh <= 0.0:
            continue

        available_power_w = max(0.0, config.max_grid_load - hour.estimated_house_load)
        shortfall = needed_wh - available_power_w  # 1-hour DP step: Wh == W here
        if shortfall > 1.0:  # ignore floating-point noise
            hour.estimated_grid_charge_shortfall_wh = shortfall
            LOGGER.info(
                "set_schedule: max_grid_load cap may leave grid-charge at hour %s short of "
                "its target: needs %.0f Wh, only ~%.0f Wh reachable at the capped rate "
                "(~%.0f Wh short) — not marking the once-per-day budget used",
                charge_hour % MAX_HOURS,
                needed_wh,
                available_power_w,
                shortfall,
            )


def set_schedule(
    cur_soc: int, cur_hour: int, today: Day, tomorrow: Day, config: ScheduleConfig
) -> None:
    """Port of SetSchedule — the public entry point.

    `cur_soc` is the AlphaESS's native SOC reading (0..1000, i.e. 0.1%
    units — same scale as `data.SOC_MAX`/`SOC_MIN`), converted internally to
    a `SOC_STEPS`-resolution index. `today`/`tomorrow` must already carry
    price/solar/house-load estimates (from prices.py/solar.py/storage.py)
    and, for `today`, the real executed-so-far state
    (`charge_on_grid_used`/`discharge_used`/`index_charge`/`index_discharge`,
    plus `hour[].real_result` for hours before `cur_hour`) — set by the
    control loop, not by this function. Mutates `today`/`tomorrow` in place
    with the chosen schedule.
    """
    cur_soc_index = int(_clamp(round(cur_soc / SOC_MAX_BATTERY * SOC_STEPS), 0, SOC_STEPS))

    today_bl = copy.deepcopy(today)
    tomorrow_bl = copy.deepcopy(tomorrow)
    baseline_valid, result_bl = _calculate_best_schedule(
        cur_soc_index, cur_hour, today_bl, tomorrow_bl, True, True, None, config
    )

    if not baseline_valid:
        LOGGER.debug("set_schedule: no baseline found, falling back to Charge-PV")
        for hour in today.hour[cur_hour:]:
            hour.charge = Charge.CHARGING_ON_PV
            hour.estimated_result = 0.0
        _finalise_schedule(today, tomorrow, config)
        return

    if today.charge_on_grid_used and today.discharge_used:
        LOGGER.debug("set_schedule: charge and discharge already used today, selecting baseline")
        _assign_day(today, today_bl)
        _assign_day(tomorrow, tomorrow_bl)
        _finalise_schedule(today, tomorrow, config)
        return

    expected_profit: dict[Charge, float] = dict.fromkeys(Charge, 0.0)
    profits: dict[Charge, list[tuple[float, float] | None]] = {action: [] for action in Charge}

    for scenario in SCENARIOS:
        today_sc, tomorrow_sc = _apply_scenario_to_day(today, tomorrow, scenario, cur_hour)

        for action in Charge:
            today_opt = copy.deepcopy(today_sc)
            tomorrow_opt = copy.deepcopy(tomorrow_sc)
            valid, result = _calculate_best_schedule(
                cur_soc_index,
                cur_hour,
                today_opt,
                tomorrow_opt,
                today.charge_on_grid_used,
                today.discharge_used,
                action,
                config,
            )
            if valid:
                marginal = result - result_bl
                profits[action].append((marginal, scenario.probability))
                expected_profit[action] += scenario.probability * marginal
            else:
                profits[action].append(None)

    have_valid_action = False
    best_score = 0.0
    best_action = Charge.NO_CHARGING
    for action in Charge:
        p20 = _percentile(profits[action], RISK_PERCENTILE)
        if p20 is None:
            continue
        risk_penalty = max(0.0, -p20)
        score = expected_profit[action] - LAMBDA * risk_penalty
        if not have_valid_action or score > best_score:
            best_score = score
            best_action = action
        have_valid_action = True

    if not have_valid_action:
        LOGGER.debug("set_schedule: no valid action/scenario found, selecting baseline")
        _assign_day(today, today_bl)
        _assign_day(tomorrow, tomorrow_bl)
        _finalise_schedule(today, tomorrow, config)
        return

    today_opt = copy.deepcopy(today)
    tomorrow_opt = copy.deepcopy(tomorrow)
    opt_valid, result_opt = _calculate_best_schedule(
        cur_soc_index,
        cur_hour,
        today_opt,
        tomorrow_opt,
        today.charge_on_grid_used,
        today.discharge_used,
        best_action,
        config,
    )

    if opt_valid:
        if today.charge_on_grid_used and 0 <= today.index_charge < cur_hour:
            result_opt += today.hour[today.index_charge].real_result
            result_bl += today_bl.hour[today.index_charge].estimated_result
        if today.discharge_used and 0 <= today.index_discharge < cur_hour:
            result_opt += today.hour[today.index_discharge].real_result
            result_bl += today_bl.hour[today.index_discharge].estimated_result

        opt_extra = result_opt - result_bl
        if opt_extra < config.daily_min_profit:
            LOGGER.debug(
                "set_schedule: optimum extra %.2f < minimum %.2f, selecting baseline",
                opt_extra,
                config.daily_min_profit,
            )
            _assign_day(today, today_bl)
            _assign_day(tomorrow, tomorrow_bl)
        else:
            LOGGER.debug(
                "set_schedule: optimum extra %.2f >= minimum %.2f, selecting optimum (%s)",
                opt_extra,
                config.daily_min_profit,
                set_charging_msg(best_action),
            )
            _assign_day(today, today_opt)
            _assign_day(tomorrow, tomorrow_opt)
    else:
        LOGGER.debug("set_schedule: final schedule failed, selecting baseline")
        _assign_day(today, today_bl)
        _assign_day(tomorrow, tomorrow_bl)

    _finalise_schedule(today, tomorrow, config)
    _apply_grid_load_shortfall(today, tomorrow, config)
