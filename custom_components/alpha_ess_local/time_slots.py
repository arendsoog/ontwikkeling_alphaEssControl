"""Fixed time slots: an action the user sets for given hours and weekdays.

E.g. every night from 02:00 to 05:00 charge from the grid up to 80% at
1200 W. Stored in the options (CONF_TIME_SLOTS) as a list of dicts, set
from the panel's Controls tab. The planner treats a slot's hours as fixed
(schedule.ScheduleConfig.fixed_hours) and plans the rest of the day around
them; the dispatch applies the slot's SOC and power in its hours. A manual
override still goes before a slot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol

from .const import CONF_TIME_SLOTS
from .data import MAX_HOURS, SOC_MAX_BATTERY, Charge

MAX_TIME_SLOTS = 8
MIN_SLOT_POWER = 100

# The panel's names for the actions.
ACTIONS: dict[str, Charge] = {
    "charge_grid": Charge.CHARGING_ON_GRID,
    "charge_pv": Charge.CHARGING_ON_PV,
    "discharge": Charge.CHARGING_DISCHARGE,
    "no_charging": Charge.NO_CHARGING,
    "no_discharging": Charge.NO_DISCHARGING,
}
# The actions a SOC applies to (charge: up to it, discharge: down to it)
# and the ones a power applies to.
SOC_ACTIONS = frozenset({"charge_grid", "charge_pv", "discharge"})
POWER_ACTIONS = frozenset({"charge_grid", "discharge"})

SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.All(str, vol.Length(min=1, max=40)),
        vol.Required("start"): vol.All(int, vol.Range(min=0, max=23)),
        vol.Required("end"): vol.All(int, vol.Range(min=1, max=24)),
        vol.Required("days"): vol.All(
            [vol.All(int, vol.Range(min=0, max=6))], vol.Length(min=1, max=7)
        ),
        vol.Required("action"): vol.In(ACTIONS),
        vol.Optional("soc"): vol.Any(None, vol.All(int, vol.Range(min=0, max=100))),
        vol.Optional("power"): vol.Any(
            None, vol.All(int, vol.Range(min=MIN_SLOT_POWER, max=50000))
        ),
        vol.Optional("enabled", default=True): bool,
    }
)
SLOTS_SCHEMA = vol.All([SLOT_SCHEMA], vol.Length(max=MAX_TIME_SLOTS))


@dataclass(frozen=True)
class FixedHour:
    """One hour a slot fixes: its action, the SOC to charge up to or
    discharge down to (0.1% units, None: as planned) and the power (W,
    None: as planned)."""

    action: Charge
    soc: int | None = None
    power: int | None = None


def time_slots(options: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The configured slots (each normalised: only its action's fields)."""
    slots = []
    for slot in options.get(CONF_TIME_SLOTS) or []:
        slot = dict(slot)
        if slot.get("action") not in SOC_ACTIONS:
            slot["soc"] = None
        if slot.get("action") not in POWER_ACTIONS:
            slot["power"] = None
        slots.append(slot)
    return slots


def _covers(slot: Mapping[str, Any], day: date, hour: int) -> bool:
    """Whether an enabled slot covers `hour` of `day`. A slot ending before
    it starts runs past midnight: its last hours fall on the next day."""
    if not slot.get("enabled", True):
        return False
    start, end, days = slot["start"], slot["end"], slot["days"]
    weekday = day.weekday()
    if start < end:
        return weekday in days and start <= hour < end
    return (weekday in days and hour >= start) or ((weekday - 1) % 7 in days and hour < end)


def _fixed(slot: Mapping[str, Any]) -> FixedHour:
    soc = slot.get("soc")
    return FixedHour(
        action=ACTIONS[slot["action"]],
        soc=None if soc is None else round(soc * SOC_MAX_BATTERY / 100),
        power=slot.get("power"),
    )


def slot_at(options: Mapping[str, Any], when: datetime) -> FixedHour | None:
    """The slot covering `when`'s hour (the first one, if several do)."""
    for slot in time_slots(options):
        if _covers(slot, when.date(), when.hour):
            return _fixed(slot)
    return None


def fixed_hours(options: Mapping[str, Any], today: date) -> dict[int, FixedHour]:
    """The planner's fixed hours for today and tomorrow, keyed by its hour
    index (0..47, tomorrow's hours from MAX_HOURS on)."""
    fixed: dict[int, FixedHour] = {}
    slots = time_slots(options)
    for offset in (0, 1):
        day = today + timedelta(days=offset)
        for hour in range(MAX_HOURS):
            slot = next((s for s in slots if _covers(s, day, hour)), None)
            if slot is not None:
                fixed[offset * MAX_HOURS + hour] = _fixed(slot)
    return fixed
