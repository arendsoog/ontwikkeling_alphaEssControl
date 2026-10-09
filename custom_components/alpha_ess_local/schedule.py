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
from .storage import lead_bucket

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
    # Applied in _evaluate_hour_action's CHARGING_ON_GRID math as a per-hour
    # rate limit on *how much* of the (solar-reservation-driven) target a
    # single hour can deliver — the same cap dispatch.py's live ~20s power
    # command throttles by (see dispatch.DispatchConfig.max_grid_load,
    # orchestrator.py's shared _effective_max_grid_load_wh). When it binds,
    # the once-per-day charge/discharge "used" flag only flips once the
    # target is actually reached, so the DP's own search naturally spreads
    # a grid-charge session across however many hours that takes.
    max_grid_load: float = CHARGE_LIMIT
    # Overrides vat_percentage for the return-price side only (e.g. a
    # BTW-exempt teruglevering formula) -- None means "no override, apply
    # vat_percentage to both sides", same as prior behavior.
    return_vat_percentage: float | None = None
    # False: one grid charge and one grid discharge per day at most (the
    # original's limit). True: grid charging in any hours of the day, as
    # long as all that goes into the battery that day (solar included, see
    # Day.charged_today_wh) stays within one full battery (BUDGET_STEPS),
    # and no discharging to the grid -- the battery only supplies the house.
    # Either way the plan must beat the no-grid-charge baseline by
    # daily_min_profit.
    multiple_per_day: bool = False
    # Learned solar uncertainty: per lead-time group (storage.LEAD_BUCKETS),
    # the factor real/forecast solar at each scenario's percentile
    # (scenario_percentiles). None, or a missing group: the fixed factors.
    solar_spread: dict[int, tuple[float, ...]] | None = None


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


def _net_solar_wh(the_hour: Hour, config: ScheduleConfig) -> float:
    """An hour's expected solar (AC, as _evaluate_hour_action counts it)
    minus its expected house load, in Wh -- negative when the house needs
    more than the sun gives."""
    solar_dc = 0 if the_hour.earning == Earning.EARNING_ON_USE else the_hour.estimated_solar_power
    nominal = config.inverter_nominal_power
    solar_ac = min(solar_dc * _inverter_efficiency(min(solar_dc, nominal), nominal), nominal)
    return solar_ac - the_hour.estimated_house_load


def _evaluate_hour_action(
    action: Charge,
    the_hour: Hour,
    soc_wh: float,
    charge_used: bool,
    discharge_used: bool,
    config: ScheduleConfig,
    future_solar_surplus_wh: float = 0.0,
    grid_limit_wh: float | None = None,
) -> tuple[float, float, bool, bool]:
    """Port of EvaluateHourAction.

    Simulates one action for one hour. Returns (new_soc_wh, profit,
    next_charge_used, next_discharge_used).

    `future_solar_surplus_wh`: the room to keep free for solar still coming
    later the same day (the largest rise of a running solar-minus-house-load
    sum over the later hours, see _calculate_best_schedule) — used only by
    CHARGING_ON_GRID, so the once-a-day grid charge doesn't fill the battery
    right up to the cap and leave no room for that solar (which would
    otherwise just spill to a worse-priced feed-in instead of being stored).

    `grid_limit_wh`: at most this much into the battery this hour on a grid
    charge (one of the GRID_LEVELS amounts the DP chooses between), so it
    can charge just what's worth it -- e.g. until the next cheap hours --
    instead of always filling up. A session reaching its chosen amount is
    finished; only one held back by the grid-load cap carries on.
    """
    inverter_nominal_power = config.inverter_nominal_power
    battery_capacity = config.usable_battery_capacity

    solar_dc = the_hour.estimated_solar_power
    house_load = the_hour.estimated_house_load
    earning = the_hour.earning

    price_use = mk_use_price(the_hour.price, config.use_fee, config.vat_percentage) / 1000.0
    return_vat_percentage = (
        config.return_vat_percentage
        if config.return_vat_percentage is not None
        else config.vat_percentage
    )
    price_return = (
        mk_return_price(the_hour.price, config.return_fee, return_vat_percentage) / 1000.0
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
        if not charge_used or config.multiple_per_day:
            # The target itself (what _finalise_schedule turns into
            # cutoff_soc) is purely solar-reservation-driven. max_grid_load
            # *does* throttle how much of that target a single hour can
            # actually deliver (same cap dispatch.py's decide_dispatch
            # applies live) -- when it binds, next_charge_used stays False
            # below so a later hour's DP state can pick up where this one
            # left off, letting the backward-induction search itself weigh
            # a multi-hour session against a single expensive hour.
            max_capacity = max_soc_eou if earning == Earning.EARNING_ON_USE else max_soc
            available_gap = max_capacity - soc_wh
            reserved_for_solar = min(future_solar_surplus_wh, max(available_gap, 0.0))
            target_soc = available_gap - reserved_for_solar
            if grid_limit_wh is not None:
                target_soc = min(target_soc, grid_limit_wh)
            rate_cap_soc = max(0.0, config.max_grid_load - house_load)
            charge_soc = min(target_soc, CHARGE_LIMIT, rate_cap_soc)
            if charge_soc > 0.0:
                charge_ac_est = charge_soc / ETA_GRID_TO_BATT
                batt_eff = _battery_efficiency(charge_ac_est, battery_capacity)
                inv_eff = _inverter_efficiency(charge_ac_est, inverter_nominal_power)
                charge_ac = charge_soc / (inv_eff * batt_eff * ETA_GRID_TO_BATT)
                if charge_ac >= MIN_CHARGE_POWER:
                    new_soc_wh = soc_wh + charge_soc
                    feed_in -= charge_ac
                    profit = feed_in * (price_return if feed_in > 0.0 else price_use)
                    next_charge_used = (
                        True if config.multiple_per_day else charge_soc >= target_soc - 1e-6
                    )

    elif (
        action == Charge.CHARGING_DISCHARGE
        and earning != Earning.EARNING_ON_USE
        and not discharge_used
        and config.discharge_enabled
        and not config.multiple_per_day  # then the battery only supplies the house
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

    if config.multiple_per_day and action != Charge.CHARGING_ON_GRID:
        next_charge_used = False  # a grid-charge session ends with any other hour

    new_soc_wh = _clamp(new_soc_wh, 0.0, float(battery_capacity))
    return new_soc_wh, profit, next_charge_used, next_discharge_used


# How much a grid-charge hour may put into the battery: the DP chooses
# between 1/GRID_LEVELS, 2/GRID_LEVELS, ... of the battery (capped by the
# solar room and the grid-load cap), so it charges what is worth it -- e.g.
# until the next cheap hours, today or tomorrow -- rather than always full.
GRID_LEVELS = 10
# With several charges a day (multiple_per_day): what goes into the battery
# in a day, solar and grid together, is at most one full battery -- one
# cycle a day for the battery's life. Counted in 1/BUDGET_STEPS units.
# Solar goes first: it is never refused for the budget, and a grid charge
# must leave the solar surplus still expected later that day.
BUDGET_STEPS = 20
_SOC_PER_BUDGET_STEP = SOC_STEPS // BUDGET_STEPS


@dataclass(frozen=True)
class _Option:
    """One way to spend an hour from a given SOC: the action, the SOC index
    it ends at, its profit, whether a grid-charge session is finished
    (once-per-day mode) and the daily-budget units it charges."""

    action: Charge
    next_soc_index: int
    profit: float
    charge_done: bool
    budget_units: int


def _hour_options(
    the_hour: Hour,
    config: ScheduleConfig,
    room_wh: float,
    step_wh: float,
    grid_allowed: bool,
) -> list[list[_Option]]:
    """Every feasible _Option per SOC index for one hour."""
    capacity = config.usable_battery_capacity
    grid_limits = [capacity * level / GRID_LEVELS for level in range(1, GRID_LEVELS + 1)]
    table = []
    for si in range(SOC_STEPS + 1):
        soc_wh = si * step_wh
        options: list[_Option] = []
        for action in Charge:
            if action == Charge.CHARGING_ON_GRID and not grid_allowed:
                continue
            limits = grid_limits if action == Charge.CHARGING_ON_GRID else [None]
            seen: set[int] = set()
            for limit in limits:
                new_soc_wh, profit, done, _du = _evaluate_hour_action(
                    action, the_hour, soc_wh, False, False, config, room_wh, limit
                )
                if profit == float("-inf"):
                    continue
                ns = int(_clamp(round(new_soc_wh / step_wh) if step_wh else 0, 0, SOC_STEPS))
                if ns in seen:
                    continue  # a higher level the caps cut back to the same SOC
                seen.add(ns)
                charged = max(0, ns - si)
                units = round(charged / _SOC_PER_BUDGET_STEP)
                options.append(_Option(action, ns, profit, done, units))
        table.append(options)
    return table


class _Plan:
    """Backward-induction DP over (hour, SOC index, state), from `start_hour`
    to the end of the known days.

    The state is the once-per-day bookkeeping -- (grid charge spent,
    discharge spent) -- or, with multiple_per_day, the daily budget units
    already charged. Both reset at midnight. The hours after start_hour are
    solved once; the start hour itself is kept open so every forced action
    (see set_schedule) is evaluated against the same table.
    """

    def __init__(
        self,
        start_hour: int,
        today: Day,
        tomorrow: Day,
        config: ScheduleConfig,
        grid_blocked_today: bool = False,
    ) -> None:
        self.start_hour = start_hour
        self.today = today
        self.tomorrow = tomorrow
        self.multiple = config.multiple_per_day
        self.n_states = BUDGET_STEPS + 1 if self.multiple else 4
        self.end_hour = 0
        if today.valid:
            self.end_hour = MAX_HOURS
        if tomorrow.valid:
            self.end_hour = MAX_HOURS * 2
        step_wh = config.usable_battery_capacity / SOC_STEPS if SOC_STEPS else 0.0

        self.options: dict[int, list[list[_Option]]] = {}
        self.choice: dict[int, list[list[int]]] = {}
        later = [[0.0] * self.n_states for _ in range(SOC_STEPS + 1)]
        # Room the battery must keep free at the end of each hour for solar
        # still to come *that same day*: the largest rise of a running sum
        # of (solar - house load) over its later hours -- a deficit hour
        # lowers it, since the house then drains the battery first.
        # Tomorrow's sun doesn't count (the battery empties overnight
        # anyway). Built backwards: room(h) = max(0, net(h+1) + room(h+1)),
        # 0 for the last hour of a day.
        room = 0.0
        # Solar goes before the grid: in the daily budget (multiple_per_day)
        # a grid charge must leave the solar surplus still expected later
        # that day, in budget units -- solar charging itself is never
        # refused for the budget.
        budget_step_wh = config.usable_battery_capacity / BUDGET_STEPS
        solar_after = 0.0
        self.solar_units_after: dict[int, int] = {}
        for hour in range(self.end_hour - 1, start_hour - 1, -1):
            the_hour = self.hour_at(hour)
            self.solar_units_after[hour] = (
                math.ceil(solar_after / budget_step_wh - 1e-9) if budget_step_wh else 0
            )
            grid_allowed = not (grid_blocked_today and hour < MAX_HOURS)
            table = _hour_options(the_hour, config, room, step_wh, grid_allowed)
            self.options[hour] = table
            if hour == start_hour:
                self.later = later
            else:
                later = self._solve_hour(hour, table, later)
            # The hour before a day's first is another day's last: no room.
            room = 0.0 if hour % 24 == 0 else max(0.0, _net_solar_wh(the_hour, config) + room)
            if hour % 24 == 0:
                solar_after = 0.0
            else:
                solar_after += max(0.0, _net_solar_wh(the_hour, config))

    def hour_at(self, hour: int) -> Hour:
        if hour >= MAX_HOURS:
            return self.tomorrow.hour[hour - MAX_HOURS]
        return self.today.hour[hour]

    def _next_state(self, hour: int, state: int, option: _Option, reset: bool = True) -> int | None:
        """The state after `option` (fresh again after a day's last hour,
        unless `reset` is False), or None when the state rules it out."""
        if self.multiple:
            nxt = state + option.budget_units
            if option.action == Charge.CHARGING_ON_GRID:
                if nxt + self.solar_units_after[hour] > BUDGET_STEPS:
                    return None
            else:
                nxt = min(nxt, BUDGET_STEPS)  # solar is never refused
        else:
            charge_spent, discharge_spent = state >> 1, state & 1
            if option.action == Charge.CHARGING_ON_GRID:
                if charge_spent:
                    return None
                nxt = (int(option.charge_done) << 1) | discharge_spent
            elif option.action == Charge.CHARGING_DISCHARGE:
                if discharge_spent:
                    return None
                nxt = (charge_spent << 1) | 1
            else:
                nxt = state
        return 0 if reset and (hour + 1) % 24 == 0 else nxt

    def _best(
        self,
        hour: int,
        options: list[_Option],
        state: int,
        later: list[list[float]],
        forced: Charge | None = None,
    ) -> tuple[float, int]:
        best, best_k = float("-inf"), -1
        for k, option in enumerate(options):
            if forced is not None and option.action != forced:
                continue
            nxt = self._next_state(hour, state, option)
            if nxt is None:
                continue
            value = option.profit + later[option.next_soc_index][nxt]
            if value > best:
                best, best_k = value, k
        return best, best_k

    def _solve_hour(
        self, hour: int, table: list[list[_Option]], later: list[list[float]]
    ) -> list[list[float]]:
        values, choices = [], []
        for options in table:
            row_v, row_c = [], []
            for state in range(self.n_states):
                value, k = self._best(hour, options, state, later)
                row_v.append(value)
                row_c.append(k)
            values.append(row_v)
            choices.append(row_c)
        self.choice[hour] = choices
        return values

    def result(self, soc_index: int, state: int, forced: Charge | None) -> float:
        hour = self.start_hour
        return self._best(hour, self.options[hour][soc_index], state, self.later, forced)[0]

    def trace(self, soc_index: int, state: int, forced: Charge | None) -> None:
        """Write the plan from (start_hour, soc_index, state) into the days'
        hour[].charge/.estimated_start_soc/.estimated_result."""
        si = soc_index
        for hour in range(self.start_hour, self.end_hour):
            options = self.options[hour][si]
            if hour == self.start_hour:
                _value, k = self._best(hour, options, state, self.later, forced)
            else:
                k = self.choice[hour][si][state]
            the_hour = self.hour_at(hour)
            the_hour.estimated_start_soc = si
            if k < 0:
                the_hour.charge = Charge.NO_CHARGING
                the_hour.estimated_result = float("-inf")
                continue
            option = options[k]
            the_hour.charge = option.action
            the_hour.estimated_result = option.profit
            if hour == self.start_hour:
                raw = self._next_state(hour, state, option, reset=False)
                # The DP's own answer to "is the once-per-day grid-charge/
                # discharge session still open after this hour" -- a
                # multi-hour session (see _evaluate_hour_action's rate cap)
                # only "spends" the slot once its target is actually
                # reached, so this is the live/persisted charge_on_grid_used's
                # source of truth going forward.
                multiple = self.multiple
                self.today.charge_on_grid_used = not multiple and bool(raw >> 1)
                self.today.discharge_used = not multiple and bool(raw & 1)
            state = self._next_state(hour, state, option)
            si = option.next_soc_index


def _start_state(
    config: ScheduleConfig, today: Day, charge_used_before: bool, discharge_used_before: bool
) -> int:
    if config.multiple_per_day:
        step_wh = config.usable_battery_capacity / BUDGET_STEPS
        units = round(today.charged_today_wh / step_wh) if step_wh else 0
        return int(_clamp(units, 0, BUDGET_STEPS))
    return (int(charge_used_before) << 1) | int(discharge_used_before)


def _calculate_best_schedule(
    cur_soc_index: int,
    start_hour: int,
    today: Day,
    tomorrow: Day,
    charge_used_before: bool,
    discharge_used_before: bool,
    forced_action: Charge | None,
    config: ScheduleConfig,
    grid_blocked_today: bool = False,
) -> tuple[bool, float]:
    """Port of CalculateBestSchedule.

    Solves the DP (see _Plan) and forward-traces from (start_hour,
    cur_soc_index, ...) to fill today/tomorrow's hour[].charge/
    .estimated_start_soc/.estimated_result in place for [start_hour,
    end_hour). Returns (valid, dp_result). `grid_blocked_today`: no grid
    charging at all for the rest of today (the multiple_per_day baseline).
    """
    plan = _Plan(start_hour, today, tomorrow, config, grid_blocked_today)
    state = _start_state(config, today, charge_used_before, discharge_used_before)
    dp_result = plan.result(cur_soc_index, state, forced_action)
    if dp_result == float("-inf"):
        return False, dp_result
    plan.trace(cur_soc_index, state, forced_action)
    if dp_result < -100000:
        return False, dp_result
    return True, dp_result


def scenario_percentiles() -> list[float]:
    """Each scenario's place in the solar distribution, in SCENARIOS order:
    the middle of its probability mass with the scenarios sorted from least
    to most sun (e.g. the pessimistic one, 28% likely, sits at 0.14)."""
    order = sorted(range(len(SCENARIOS)), key=lambda i: SCENARIOS[i].solar_factor)
    total = sum(s.probability for s in SCENARIOS)
    percentiles = [0.0] * len(SCENARIOS)
    cumulative = 0.0
    for i in order:
        share = SCENARIOS[i].probability / total
        percentiles[i] = cumulative + share / 2
        cumulative += share
    return percentiles


def _apply_scenario_to_day(
    today: Day,
    tomorrow: Day,
    scenario: _Scenario,
    start_hour: int,
    solar_spread: dict[int, tuple[float, ...]] | None = None,
    scenario_index: int | None = None,
) -> tuple[Day, Day]:
    """Port of ApplyScenarioToDay: returns scaled *copies*, originals untouched.

    `solar_spread`: learned solar factors per lead-time group, one per
    scenario (see ScheduleConfig.solar_spread); used for the hours whose
    group has them, in place of the fixed scenario.solar_factor."""
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
        # Learned from how far off the forecast really was this many hours
        # ahead (storage.learn_forecast_spread), once there's enough data.
        learned = solar_spread.get(lead_bucket(dh)) if solar_spread else None
        if learned is not None and scenario_index is not None:
            solar_factor = learned[scenario_index]

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


# Why set_schedule ended up with the schedule it chose (Day.selection).
SELECTION_NO_BASELINE = "no_baseline"  # no valid schedule at all: charge from PV
SELECTION_BUDGET_USED = "budget_used"  # grid charge and discharge both done today
SELECTION_NO_OPTION = "no_option"  # no action beat the baseline in any scenario
SELECTION_BELOW_MINIMUM = "below_minimum"  # optimum's extra < daily minimum profit
SELECTION_OPTIMUM = "optimum"
SELECTION_OPTIMUM_FAILED = "optimum_failed"  # optimum couldn't be computed


def _assign_day(dest: Day, src: Day) -> None:
    """Port of C's struct assignment (`*today = todayOpt;`): copies all of
    src's fields into dest in place, preserving dest's identity."""
    dest.__dict__.update(src.__dict__)


def _select_baseline(
    today: Day,
    tomorrow: Day,
    today_bl: Day,
    tomorrow_bl: Day,
    real_charge_on_grid_used: bool,
    real_discharge_used: bool,
) -> None:
    """Adopt today_bl/tomorrow_bl as the chosen schedule, without letting
    their DP's hardcoded "as if the once-per-day levers are already spent"
    charge_used_before/discharge_used_before (see set_schedule's baseline
    call) leak into the *real* charge_on_grid_used/discharge_used -- opting
    not to deviate from baseline this recompute must never itself spend the
    budget.
    """
    _assign_day(today, today_bl)
    _assign_day(tomorrow, tomorrow_bl)
    today.charge_on_grid_used = real_charge_on_grid_used
    today.discharge_used = real_discharge_used


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
    real_charge_on_grid_used = today.charge_on_grid_used
    real_discharge_used = today.discharge_used

    today_bl = copy.deepcopy(today)
    tomorrow_bl = copy.deepcopy(tomorrow)
    # The baseline: no grid charge or discharge left for today.
    baseline_valid, result_bl = _calculate_best_schedule(
        cur_soc_index,
        cur_hour,
        today_bl,
        tomorrow_bl,
        True,
        True,
        None,
        config,
        grid_blocked_today=config.multiple_per_day,
    )

    if not baseline_valid:
        LOGGER.debug("set_schedule: no baseline found, falling back to Charge-PV")
        for hour in today.hour[cur_hour:]:
            hour.charge = Charge.CHARGING_ON_PV
            hour.estimated_result = 0.0
        today.selection, today.opt_extra = SELECTION_NO_BASELINE, None
        _finalise_schedule(today, tomorrow, config)
        return

    if today.charge_on_grid_used and today.discharge_used:
        LOGGER.debug("set_schedule: charge and discharge already used today, selecting baseline")
        _select_baseline(
            today, tomorrow, today_bl, tomorrow_bl, real_charge_on_grid_used, real_discharge_used
        )
        today.selection, today.opt_extra = SELECTION_BUDGET_USED, None
        _finalise_schedule(today, tomorrow, config)
        return

    expected_profit: dict[Charge, float] = dict.fromkeys(Charge, 0.0)
    profits: dict[Charge, list[tuple[float, float] | None]] = {action: [] for action in Charge}

    for index, scenario in enumerate(SCENARIOS):
        today_sc, tomorrow_sc = _apply_scenario_to_day(
            today, tomorrow, scenario, cur_hour, config.solar_spread, index
        )

        # One DP per scenario; each forced first action is weighed against it.
        plan = _Plan(cur_hour, today_sc, tomorrow_sc, config)
        state = _start_state(config, today, today.charge_on_grid_used, today.discharge_used)
        for action in Charge:
            result = plan.result(cur_soc_index, state, action)
            if result > -100000:
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
        _select_baseline(
            today, tomorrow, today_bl, tomorrow_bl, real_charge_on_grid_used, real_discharge_used
        )
        today.selection, today.opt_extra = SELECTION_NO_OPTION, None
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

    minimum = config.daily_min_profit

    if opt_valid:
        if not config.multiple_per_day:
            if today.charge_on_grid_used and 0 <= today.index_charge < cur_hour:
                result_opt += today.hour[today.index_charge].real_result
                result_bl += today_bl.hour[today.index_charge].estimated_result
            if today.discharge_used and 0 <= today.index_discharge < cur_hour:
                result_opt += today.hour[today.index_discharge].real_result
                result_bl += today_bl.hour[today.index_discharge].estimated_result

        opt_extra = result_opt - result_bl
        if opt_extra < minimum:
            LOGGER.debug(
                "set_schedule: optimum extra %.2f < minimum %.2f, selecting baseline",
                opt_extra,
                minimum,
            )
            _select_baseline(
                today,
                tomorrow,
                today_bl,
                tomorrow_bl,
                real_charge_on_grid_used,
                real_discharge_used,
            )
            today.selection = SELECTION_BELOW_MINIMUM
        else:
            LOGGER.debug(
                "set_schedule: optimum extra %.2f >= minimum %.2f, selecting optimum (%s)",
                opt_extra,
                minimum,
                set_charging_msg(best_action),
            )
            _assign_day(today, today_opt)
            _assign_day(tomorrow, tomorrow_opt)
            today.selection = SELECTION_OPTIMUM
        today.opt_extra = opt_extra
    else:
        LOGGER.debug("set_schedule: final schedule failed, selecting baseline")
        _select_baseline(
            today, tomorrow, today_bl, tomorrow_bl, real_charge_on_grid_used, real_discharge_used
        )
        today.selection, today.opt_extra = SELECTION_OPTIMUM_FAILED, None

    _finalise_schedule(today, tomorrow, config)
