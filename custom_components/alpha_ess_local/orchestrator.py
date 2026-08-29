"""Wires storage.py/schedule.py into real measurements and coordinators.

Port of the measurement/orchestration parts of AlphaESSControl.c's main
loop: `CalculatePower`'s 5-minute PV/battery/grid sampling and averaging,
`DoHourlyWork`/`DoDailyWork`'s "recompute the schedule"/"recompute the
weighted estimates" triggers, and `ReadPricesAndEstimates`/`ReadEstimates`'s
combining of price + solar + house-load-estimate sources into one Day ready
for the scheduler.

Deliberately does **not** include `CheckAndSetChargingMode` — no dispatch
write ever happens here. This is Phase 7a: real-time measurement, storage,
and schedule computation only, fully live-testable since nothing here
writes to the inverter. The write-capable control loop is a later, separate
phase (see the plan doc).
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import dispatch, storage
from .api import AlphaEssLocalApiClientAuthenticationError, AlphaEssLocalApiClientError
from .config_flow import SMA_PV_POWER_STRING_KEY
from .const import (
    CONF_ALLOW_PROVIDER_CONTROL_HOURS,
    CONF_APPLY_VAT_ON_RETURN,
    CONF_CONTROL_ENABLED,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_EXTRA_PV_MODBUS_ADDRESS,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_MODBUS_OFF_VALUE,
    CONF_EXTRA_PV_MODBUS_ON_VALUE,
    CONF_EXTRA_PV_MODBUS_SLAVE,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_HOUSE_LOAD_POWER_ENTITY,
    CONF_INVERTER_NOMINAL_POWER,
    CONF_NETWORK_USE_FEE_LOW,
    CONF_NETWORK_USE_FEE_NORMAL,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_POWER,
    CONF_USABLE_BATTERY_CAPACITY,
    CONF_VAT_PERCENTAGE,
    DEFAULT_ALLOW_PROVIDER_CONTROL_HOURS,
    DEFAULT_APPLY_VAT_ON_RETURN,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_DAILY_MIN_PROFIT,
    DEFAULT_EXTRA_PV_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_MODBUS_ADDRESS,
    DEFAULT_EXTRA_PV_MODBUS_OFF_VALUE,
    DEFAULT_EXTRA_PV_MODBUS_ON_VALUE,
    DEFAULT_EXTRA_PV_MODBUS_SLAVE,
    DEFAULT_MAX_GRID_LOAD,
    DEFAULT_MAX_SOC_NEGATIVE_PRICE,
    DEFAULT_MAX_SOC_POSITIVE_PRICE,
    DEFAULT_MIN_SOC_DISCHARGE,
    DEFAULT_NETWORK_USE_FEE,
    DEFAULT_PERSIST_DAILY_CHARGE_LIMIT,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
)
from .data import (
    MAX_FIVE_MINS,
    MAX_HOURS,
    SOC_MAX_BATTERY,
    SOC_MAX_CHARGE_PROVIDER,
    Charge,
    Day,
    Earning,
    FiveMin,
    Hour,
)
from .prices import mk_return_price, mk_use_price
from .protocol import DispatchMode, DispatchParam
from .schedule import ScheduleConfig
from .schedule import set_schedule as run_scheduler
from .storage import HourMean

if TYPE_CHECKING:
    # Deferred to break the coordinator.py <-> orchestrator.py import cycle
    # (coordinator.py imports helpers from here for the solar hour_correction
    # wiring) — these are only ever used as type hints, never at runtime.
    from .coordinator import AlphaEssLocalDataUpdateCoordinator, AlphaEssLocalPricesCoordinator
    from .coordinator import AlphaEssLocalSolarCoordinator as SolarCoordinator

# Wh; floor applied to the house-load estimate fed into the scheduler, so a
# thin/absent history never produces an implausibly-confident near-zero
# estimate. Lives here (not storage.py) — it's how an *estimate consumer*
# treats the data, not something storage.py itself needs to know.
MIN_SIGMA_WH = 200


def get_db_path(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """The per-inverter SQLite file storage.py reads/writes.

    Keyed by `entry.unique_id` (host:port, set by config_flow.py) rather
    than `entry.entry_id` (a random ID HA generates fresh on every add), so
    removing and re-adding the integration against the same inverter picks
    its existing history back up instead of starting from an empty
    database. Falls back to entry_id for the rare case of no unique_id
    (e.g. entries predating this, or in tests).
    """
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", entry.unique_id or entry.entry_id)
    return hass.config.path(f"alpha_ess_local_{safe_id}.db")


def migrate_legacy_db_if_needed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Adopt a pre-existing history file left over from before db paths were
    keyed by host:port (previously keyed by entry_id, a random ID HA
    generates fresh on every add -- so removing and re-adding the
    integration orphaned the old history under a name the new entry could
    never guess on its own).

    Best-effort and conservative, since this is blind file-based recovery:
    only touches files that don't already belong to another currently
    configured entry, and only acts when exactly one such orphan exists --
    with more than one, guessing which inverter it belonged to would risk
    silently merging two different inverters' histories, so it's left
    alone (and logged) for the user to sort out by hand instead. Must run
    (via executor) before anything reads/writes this entry's db.
    """
    new_path = get_db_path(hass, entry)
    if os.path.exists(new_path):
        return

    claimed_paths = {
        get_db_path(hass, other)
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    }
    orphans = [
        path
        for path in glob.glob(hass.config.path("alpha_ess_local_*.db"))
        if path not in claimed_paths
    ]
    if len(orphans) == 1:
        LOGGER.info("Adopting existing AlphaESSControl history file %s as %s", orphans[0], new_path)
        os.rename(orphans[0], new_path)
    elif len(orphans) > 1:
        LOGGER.warning(
            "Found %d orphaned AlphaESSControl history files and can't tell which (if "
            "any) belongs to this inverter, so none were adopted: %s",
            len(orphans),
            orphans,
        )


def hour_correction_from_mean(
    mean: dict[int, HourMean] | None,
) -> dict[int, tuple[float, float]] | None:
    """Extract solar.py's {hour: (factor, offset)} shape from a retrieve_mean_data result."""
    if not mean:
        return None
    return {hour: (entry.solar_factor, entry.solar_offset) for hour, entry in mean.items()}


def merge_day_sources(
    target_date: date,
    price_day: Day,
    solar_day: Day,
    house_load: dict[int, HourMean] | None,
) -> Day:
    """Port of ReadPricesAndEstimates/ReadEstimates's combining step.

    Builds one fresh Day for target_date, carrying over price/earning fields
    from price_day, solar fields from solar_day, and house-load estimates
    from `house_load` (storage.retrieve_mean_data's output) — floored at
    MIN_SIGMA_WH, defaulting to that floor when no history exists yet
    (matching the original's "no database history" default path).
    """
    day = Day(year=target_date.year, mon=target_date.month, day=target_date.day)

    for i in range(MAX_HOURS):
        target = day.hour[i]
        price_hour = price_day.hour[i]
        solar_hour = solar_day.hour[i]

        if price_hour.valid:
            target.valid = True
            target.price = price_hour.price
            target.earning = price_hour.earning
            target.highest = price_hour.highest
            target.lowest = price_hour.lowest

        if solar_hour.valid:
            target.valid = True
            target.estimated_solar_power = solar_hour.estimated_solar_power
            target.estimated_solar_power_raw = solar_hour.estimated_solar_power_raw

        mean = house_load.get(i) if house_load else None
        if mean is not None:
            target.estimated_house_load = max(round(mean.house_load), MIN_SIGMA_WH)
            target.estimated_house_load_sigma = mean.house_load_sigma
        else:
            target.estimated_house_load = MIN_SIGMA_WH

    day.valid = price_day.valid or solar_day.valid
    day.index_lowest = price_day.index_lowest
    day.index_highest = price_day.index_highest
    day.earning_on_return_all_day = price_day.earning_on_return_all_day
    return day


def _solar_to_battery(sample: FiveMin) -> float:
    """Energy-balance estimate: how much of this sample's battery charging
    (if any) is attributable to solar surplus, vs. `_grid_to_battery`'s
    remainder attributed to the grid. Not a separately-metered value — the
    inverter doesn't report a per-source charge split; solar surplus (solar
    minus house load) is charged to solar first, matching what actually
    happened at that moment."""
    charge_power = max(0.0, -sample.battery_power)
    solar_total = sample.real_solar_power_roof + sample.real_extra_pv_power
    solar_surplus = max(0.0, solar_total - sample.real_house_load)
    return min(charge_power, solar_surplus)


def _grid_to_battery(sample: FiveMin) -> float:
    """See `_solar_to_battery` — the remainder of this sample's battery
    charging not covered by solar surplus."""
    charge_power = max(0.0, -sample.battery_power)
    return charge_power - _solar_to_battery(sample)


def _effective_use_fee(
    use_fee: float, tariff: str | None, network_use_fee_normal: float, network_use_fee_low: float
) -> float:
    """`use_fee` plus the day/night network Afname fee for `tariff` (DSMR's
    "normal"/"low" indicator) -- no network fee added when tariff is
    unknown (non-DSMR house-load source, or an orphan-recovered hour that
    predates Hour.real_tariff).
    """
    network_fee = (
        network_use_fee_normal
        if tariff == "normal"
        else network_use_fee_low
        if tariff == "low"
        else 0.0
    )
    return use_fee + network_fee


def _sample_hour(
    hour: Hour,
    pv_roof: float,
    extra_pv: float,
    total_active_power: float,
    battery_power: float,
    use_fee: float,
    return_fee: float,
    vat_percentage: float,
    return_vat_percentage: float | None = None,
) -> None:
    """Port of CalculatePower: accumulate one 5-minute sample into hour.five_min[].

    `return_vat_percentage` overrides `vat_percentage` for the return-price
    side of `hour.real_result` only -- defaults to `vat_percentage` when
    omitted (apply VAT to both sides, prior behavior).
    """
    real_house_load = pv_roof + extra_pv + total_active_power + battery_power
    if real_house_load < 0.0:
        LOGGER.debug(
            "orchestrator: ignoring 5-minute sample with negative house load (%.0f)",
            real_house_load,
        )
        return

    count = hour.five_min_count
    if not (0 <= count < MAX_FIVE_MINS):
        LOGGER.warning("orchestrator: five_min_count out of bounds (%d)", count)
        return

    sample = hour.five_min[count]
    sample.real_solar_power_roof = pv_roof
    sample.real_extra_pv_power = extra_pv
    sample.total_active_power = total_active_power
    sample.real_house_load = real_house_load
    sample.battery_power = battery_power
    sample.solar_to_battery = _solar_to_battery(sample)
    sample.grid_to_battery = _grid_to_battery(sample)

    if hour.five_min_count < MAX_FIVE_MINS:
        hour.five_min_count += 1
        count = hour.five_min_count

    samples = hour.five_min[:count]
    hour.real_solar_power_roof = round(sum(s.real_solar_power_roof for s in samples) / count)
    hour.real_extra_pv_power = round(sum(s.real_extra_pv_power for s in samples) / count)
    hour.real_house_load = round(sum(s.real_house_load for s in samples) / count)
    hour.total_active_power = round(sum(s.total_active_power for s in samples) / count)
    hour.real_solar_to_battery = round(sum(s.solar_to_battery for s in samples) / count)
    hour.real_grid_to_battery = round(sum(s.grid_to_battery for s in samples) / count)

    active_power = hour.total_active_power
    if active_power > 0.0:
        hour.real_result = (
            -active_power * mk_use_price(hour.price, use_fee, vat_percentage) / 1000.0
        )
    else:
        effective_return_vat = (
            return_vat_percentage if return_vat_percentage is not None else vat_percentage
        )
        hour.real_result = (
            -active_power * mk_return_price(hour.price, return_fee, effective_return_vat) / 1000.0
        )


# SMA's Modbus profile represents "no measurement" for a signed 32-bit
# register as 0x80000000 (S32 NaN) rather than an actual unavailable state —
# e.g. an AC-power register reads this while the inverter is asleep at
# night, since its controller isn't actively sampling that channel then.
# HA's core `modbus` integration passes that raw value straight through as
# the entity's state, so it must be filtered here rather than relying on the
# "unknown"/"unavailable" state check below. Treated as 0 W (not None) —
# the inverter genuinely is producing nothing while asleep, matching the
# same "configured source reading 0 W at night" case a real reading would
# give; None stays reserved for "no source configured at all".
_SMA_S32_NAN = -2147483648


def _entity_power(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a configured power-sensor entity's state in Watts, or None.

    Normalizes kW-reporting sensors (e.g. DSMR's usage/delivery) to Watts —
    unlike HomeWizard's `active_power_w`, not every power sensor reports in
    Watts already.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except ValueError:
        LOGGER.warning("orchestrator: entity %s has a non-numeric state %r", entity_id, state.state)
        return None
    if value == _SMA_S32_NAN:
        return 0.0
    if state.attributes.get("unit_of_measurement") == UnitOfPower.KILO_WATT:
        value *= 1000
    return value


def _number_entity_value(
    hass: HomeAssistant, entry: ConfigEntry, key: str, default: float
) -> float:
    """Read one of this integration's own live-adjustable `number` entities
    (number.py — an SOC bound, or the minimum daily profit threshold) — its
    current value, or `default`.

    Resolved via the entity registry by unique_id (`f"{entry.entry_id}_
    {key}"`, matching `AlphaEssLocalSchedulerNumber`'s own `_attr_unique_id`)
    rather than a stored entity_id, since these entities belong to this
    same config entry — no per-installation linking needed, unlike
    `_extra_pv_power`'s external-entity options.

    `default` covers every reason the live value isn't usable: the entity
    hasn't been created yet (e.g. this is the very first refresh before
    the number platform finishes setup), its state is unknown/unavailable,
    or a non-numeric state — the scheduler always needs *some* value here,
    so falling back silently (rather than failing the whole schedule run)
    is the right behavior, unlike `_entity_power`'s None-propagation.
    """
    entity_id = er.async_get(hass).async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
    if entity_id is None:
        return default
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return default
    try:
        return float(state.state)
    except ValueError:
        LOGGER.warning(
            "orchestrator: entity %s has a non-numeric state %r, using default %s",
            entity_id,
            state.state,
            default,
        )
        return default


def _switch_entity_value(hass: HomeAssistant, entry: ConfigEntry, key: str, default: bool) -> bool:
    """Read one of this integration's own live-adjustable `switch` entities
    (switch.py) — its current on/off state, or `default`.

    Same entity-registry-by-unique_id resolution as `_number_entity_value`,
    for the same reason (the entity belongs to this config entry, no
    per-installation linking needed).
    """
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{key}")
    if entity_id is None:
        return default
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return default
    return state.state == "on"


def _effective_max_grid_load_wh(hass: HomeAssistant, entry: ConfigEntry) -> float:
    """The max-grid-load cap actually in effect, in Wh (== average kW over a
    1-hour DP step).

    Whichever is higher of the live "Max grid load" slider (number.py, kW)
    and an optional external "peak load this month" sensor
    (CONF_PEAK_LOAD_THIS_MONTH_ENTITY, e.g. a P1/DSMR-derived template
    sensor) — the slider is a floor, not a ceiling: under the Belgian
    capaciteitstarief (billed on the month's single highest 15-min grid-
    import peak), once house load alone has already pushed that peak above
    the slider this month, charging up to that already-paid-for level costs
    nothing extra. Shared by the schedule coordinator (hourly planning) and
    the dispatch coordinator (the live ~20s power command), so both layers
    always agree on the same cap.
    """
    max_grid_load_wh = (
        _number_entity_value(hass, entry, "max_grid_load", DEFAULT_MAX_GRID_LOAD) * 1000
    )
    peak_load_this_month_w = _entity_power(
        hass, entry.options.get(CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
    )
    if peak_load_this_month_w is not None:
        max_grid_load_wh = max(max_grid_load_wh, peak_load_this_month_w)
    return max_grid_load_wh


def _dsmr_net_power(hass: HomeAssistant, config_entry_id: str) -> float | None:
    """Net power (positive=use, negative=feed) from DSMR's usage/delivery pair.

    Neither DSMR sensor alone is signed (both are always >= 0) — unlike
    HomeWizard P1's single signed `active_power_w`, this is the difference
    of the two sibling entities on the same config entry.
    """
    registry = er.async_get(hass)
    usage_id = delivery_id = None
    for reg_entry in er.async_entries_for_config_entry(registry, config_entry_id):
        if reg_entry.unique_id.endswith("_current_electricity_usage"):
            usage_id = reg_entry.entity_id
        elif reg_entry.unique_id.endswith("_current_electricity_delivery"):
            delivery_id = reg_entry.entity_id

    usage = _entity_power(hass, usage_id)
    delivery = _entity_power(hass, delivery_id)
    if usage is None or delivery is None:
        return None
    return usage - delivery


def _house_load_power(hass: HomeAssistant, value: str | None) -> float | None:
    """Resolve the configured house-load source to a signed net power value.

    `value` is either a plain entity_id (HomeWizard P1-style, already
    signed) or a "dsmr:<config_entry_id>" reference from the auto-detect
    checklist (config_flow.py's `_house_load_candidates`), resolved to
    DSMR's usage/delivery pair here.
    """
    if not value:
        return None
    if value.startswith("dsmr:"):
        return _dsmr_net_power(hass, value.removeprefix("dsmr:"))
    return _entity_power(hass, value)


def _dsmr_tariff_indicator(hass: HomeAssistant, config_entry_id: str) -> str | None:
    """DSMR's active-tariff indicator ("normal"/"low") for the day/night
    network fee -- same sibling-entity lookup pattern as `_dsmr_net_power`,
    matched by HA core's stable `dsmr` unique_id suffix.
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, config_entry_id):
        if reg_entry.unique_id.endswith("_electricity_active_tariff"):
            state = hass.states.get(reg_entry.entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                return state.state
    return None


def _house_load_tariff(hass: HomeAssistant, value: str | None) -> str | None:
    """Resolve the configured house-load source to a DSMR tariff indicator.

    Only DSMR ("dsmr:<config_entry_id>") is supported -- HomeWizard P1/plain
    -entity house-load sources have no known tariff-indicator convention in
    this codebase, so this returns None for those (no day/night network fee
    applied, same as an installation that hasn't configured one).
    """
    if value and value.startswith("dsmr:"):
        return _dsmr_tariff_indicator(hass, value.removeprefix("dsmr:"))
    return None


def _sma_pv_power(hass: HomeAssistant, config_entry_id: str) -> float | None:
    """Sum pysma's per-MPPT-string power entities for one SMA inverter.

    Used when the aggregate "pv_power" entity isn't available (it's disabled
    by default in pysma) — see config_flow.py's SMA_PV_POWER_*_KEY for where
    these register-key constants come from.
    """
    registry = er.async_get(hass)
    total = 0.0
    found = False
    for reg_entry in er.async_entries_for_config_entry(registry, config_entry_id):
        if f"-{SMA_PV_POWER_STRING_KEY}_" in reg_entry.unique_id:
            power = _entity_power(hass, reg_entry.entity_id)
            if power is not None:
                total += power
                found = True
    return total if found else None


def _extra_pv_power(hass: HomeAssistant, value: str | None) -> float | None:
    """Resolve the configured extra-PV source to a power value.

    `value` is either a plain entity_id or a "sma:<config_entry_id>"
    reference from the auto-detect checklist (config_flow.py's
    `_extra_pv_candidates`), resolved by summing SMA's per-string entities.
    """
    if not value:
        return None
    if value.startswith("sma:"):
        return _sma_pv_power(hass, value.removeprefix("sma:"))
    return _entity_power(hass, value)


@dataclass
class RealPowerData:
    """One 5-minute sampling cycle's result.

    `day` is the accumulating today-so-far Day (hourly averages — see
    `_sample_hour`); `extra_pv_power` is that cycle's raw instantaneous
    reading (not hour-averaged), `None` specifically when no extra-PV source
    is configured at all (vs. `0.0` when configured but currently producing
    nothing) — sensor.py uses that distinction to show "unavailable" rather
    than a misleading 0 W for an unconfigured second installation.
    `yesterday_savings` is `storage.retrieve_day_savings`'s per-hour result
    for the previous calendar day — re-fetched every cycle (a single cheap
    SELECT) rather than cached, since a completed day's figures never
    change under normal operation.
    """

    day: Day
    extra_pv_power: float | None
    yesterday_savings: list[dict[str, float]] = field(default_factory=list)


class AlphaEssLocalRealDataCoordinator(DataUpdateCoordinator[RealPowerData]):
    """Samples real PV/battery/grid/extra-PV/house-load power every 5 minutes.

    Reads PV/battery/grid power from `modbus_coordinator.data` (already
    polled at 30s — no redundant Modbus traffic) plus the configured
    extra-PV/house-load entities. Accumulates into the current hour's
    `five_min[]` (port of `CalculatePower`); on an hour boundary, persists
    the just-completed hour via `storage.store_hour_data` (executor job).
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        modbus_coordinator: AlphaEssLocalDataUpdateCoordinator,
        prices_coordinator: AlphaEssLocalPricesCoordinator,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_real_data",
            update_interval=timedelta(minutes=5),
        )
        self._modbus_coordinator = modbus_coordinator
        self._prices_coordinator = prices_coordinator
        self._today: Day | None = None
        self._last_hour: int | None = None
        # The inverter's own cumulative "Total Energy from PV" register
        # (modbus_data["pv_total_energy"], PV1/PV2/PV3 strings only),
        # captured at the start of the current hour -- its delta at the
        # next hour boundary overrides real_solar_power_roof with the
        # device's own continuous internal accumulation instead of our
        # coarse periodic power-sampling average (confirmed to under-count
        # roof generation by roughly half on days with fluctuating solar).
        # In-memory only (not persisted); a restart mid-hour loses this
        # baseline and that one hour silently falls back to the
        # sample-averaged value -- see _async_update_data.
        self._pv_total_energy_at_hour_start: float | None = None
        # Same pattern as _pv_total_energy_at_hour_start, for the inverter's
        # own cumulative battery charge/discharge counters
        # (modbus_data["battery_total_energy_charge"/"_discharge"]) -- their
        # deltas at the next hour boundary become
        # real_battery_charge_energy/real_battery_discharge_energy, instead
        # of the sample-averaged battery_power, which both under-counts
        # charging and over-counts discharging (see protocol.py's
        # REG_BATTERY_TOTAL_ENERGY_CHARGE comment). Also in-memory only.
        self._battery_charge_energy_at_hour_start: float | None = None
        self._battery_discharge_energy_at_hour_start: float | None = None

    async def _async_update_data(self) -> RealPowerData:
        """Sample current power values and roll the hour/day over as needed."""
        now = dt_util.now()
        previous_day = self._today
        previous_hour = self._last_hour
        db_path = get_db_path(self.hass, self.config_entry)
        options = self.config_entry.options
        use_fee = options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE)
        return_fee = options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE)
        vat_percentage = options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE)
        return_vat_percentage = (
            vat_percentage
            if options.get(CONF_APPLY_VAT_ON_RETURN, DEFAULT_APPLY_VAT_ON_RETURN)
            else 0.0
        )
        network_use_fee_normal = options.get(CONF_NETWORK_USE_FEE_NORMAL, DEFAULT_NETWORK_USE_FEE)
        network_use_fee_low = options.get(CONF_NETWORK_USE_FEE_LOW, DEFAULT_NETWORK_USE_FEE)

        is_new_day = previous_day is None or (
            previous_day.year,
            previous_day.mon,
            previous_day.day,
        ) != (now.year, now.month, now.day)
        if is_new_day:
            self._today = Day(year=now.year, mon=now.month, day=now.day, valid=True)
            # Rehydrate any hours already persisted for today — without this,
            # a Home Assistant restart mid-day would wipe hours completed
            # before the restart from the running "today" totals (e.g.
            # house_load_today), even though they're already safely stored.
            # On a genuine midnight rollover this just finds nothing yet.
            stored_hours = await self.hass.async_add_executor_job(
                storage.retrieve_day_hours, db_path, now.year, now.month, now.day
            )
            for hour_index, (
                house_load,
                solar_power_roof,
                extra_pv,
                solar_to_battery,
                grid_to_battery,
            ) in stored_hours.items():
                rehydrated_hour = self._today.hour[hour_index]
                rehydrated_hour.valid = True
                rehydrated_hour.real_house_load = house_load
                rehydrated_hour.real_solar_power_roof = solar_power_roof
                rehydrated_hour.real_extra_pv_power = extra_pv
                rehydrated_hour.real_solar_to_battery = solar_to_battery
                rehydrated_hour.real_grid_to_battery = grid_to_battery

            # Recover *yesterday's* last hour (23) the same way, if a
            # restart landed exactly at the day boundary -- the loop below
            # only looks at *today's* earlier hours, so a hung hour_progress
            # row from the previous calendar date's hour 23 would otherwise
            # never get promoted into hour_data (it stops being "today" the
            # moment this block runs, so nothing else ever revisits it).
            yesterday_date = date(now.year, now.month, now.day) - timedelta(days=1)
            yesterday_stored_hours = await self.hass.async_add_executor_job(
                storage.retrieve_day_hours,
                db_path,
                yesterday_date.year,
                yesterday_date.month,
                yesterday_date.day,
            )
            if (MAX_HOURS - 1) not in yesterday_stored_hours:
                orphaned_yesterday = await self.hass.async_add_executor_job(
                    storage.retrieve_hour_progress,
                    db_path,
                    yesterday_date.year,
                    yesterday_date.month,
                    yesterday_date.day,
                    MAX_HOURS - 1,
                )
                if orphaned_yesterday is not None:
                    (
                        house_load,
                        solar_power_roof,
                        extra_pv,
                        active_power,
                        _sample_count,
                        solar_to_battery,
                        grid_to_battery,
                        _pv_total_energy_at_hour_start,
                        _battery_charge_energy_at_hour_start,
                        _battery_discharge_energy_at_hour_start,
                    ) = orphaned_yesterday
                    yesterday_day = Day(
                        year=yesterday_date.year,
                        mon=yesterday_date.month,
                        day=yesterday_date.day,
                        valid=True,
                    )
                    recovered_hour = yesterday_day.hour[MAX_HOURS - 1]
                    recovered_hour.valid = True
                    recovered_hour.real_house_load = house_load
                    recovered_hour.real_solar_power_roof = solar_power_roof
                    recovered_hour.real_extra_pv_power = extra_pv
                    recovered_hour.total_active_power = active_power
                    recovered_hour.real_solar_to_battery = solar_to_battery
                    recovered_hour.real_grid_to_battery = grid_to_battery
                    await self.hass.async_add_executor_job(
                        storage.store_hour_data,
                        db_path,
                        yesterday_day,
                        MAX_HOURS - 1,
                        use_fee,
                        return_fee,
                        vat_percentage,
                    )
                    await self.hass.async_add_executor_job(
                        storage.delete_hour_progress,
                        db_path,
                        yesterday_date.year,
                        yesterday_date.month,
                        yesterday_date.day,
                        MAX_HOURS - 1,
                    )

            # Recover any *earlier* hour that finished (a full-count
            # hour_progress row) but never made it into hour_data — this
            # happens if a restart landed exactly between that hour
            # completing and the next update cycle that would normally
            # have migrated it (the previous_hour-transition block further
            # down, which only runs while this same coordinator instance
            # keeps running). Only hours strictly before now.hour are
            # candidates; now.hour's own in-progress row is handled
            # separately just below, since it's still genuinely ongoing.
            for hour_index in range(now.hour):
                if hour_index in stored_hours:
                    continue
                orphaned_progress = await self.hass.async_add_executor_job(
                    storage.retrieve_hour_progress,
                    db_path,
                    now.year,
                    now.month,
                    now.day,
                    hour_index,
                )
                if orphaned_progress is None:
                    continue
                (
                    house_load,
                    solar_power_roof,
                    extra_pv,
                    active_power,
                    _sample_count,
                    solar_to_battery,
                    grid_to_battery,
                    _pv_total_energy_at_hour_start,
                    _battery_charge_energy_at_hour_start,
                    _battery_discharge_energy_at_hour_start,
                ) = orphaned_progress
                recovered_hour = self._today.hour[hour_index]
                recovered_hour.valid = True
                recovered_hour.real_house_load = house_load
                recovered_hour.real_solar_power_roof = solar_power_roof
                recovered_hour.real_extra_pv_power = extra_pv
                recovered_hour.total_active_power = active_power
                recovered_hour.real_solar_to_battery = solar_to_battery
                recovered_hour.real_grid_to_battery = grid_to_battery
                await self.hass.async_add_executor_job(
                    storage.store_hour_data,
                    db_path,
                    self._today,
                    hour_index,
                    use_fee,
                    return_fee,
                    vat_percentage,
                )
                await self.hass.async_add_executor_job(
                    storage.delete_hour_progress,
                    db_path,
                    now.year,
                    now.month,
                    now.day,
                    hour_index,
                )

            # Also rehydrate the *current*, still-in-progress hour's samples
            # so far — otherwise a restart mid-hour would silently drop
            # whatever 5-minute samples had already landed this hour. Since
            # only the running average + count survive (not the individual
            # samples), the synthetic samples below are all set to that same
            # average — mathematically equivalent for `_sample_hour`'s
            # averaging, since sum(count * average) / count == average.
            hour_progress = await self.hass.async_add_executor_job(
                storage.retrieve_hour_progress, db_path, now.year, now.month, now.day, now.hour
            )
            if hour_progress is not None:
                (
                    house_load,
                    solar_power_roof,
                    extra_pv,
                    active_power,
                    sample_count,
                    solar_to_battery,
                    grid_to_battery,
                    pv_total_energy_at_hour_start,
                    battery_charge_energy_at_hour_start,
                    battery_discharge_energy_at_hour_start,
                ) = hour_progress
                current_hour = self._today.hour[now.hour]
                current_hour.valid = True
                current_hour.five_min_count = sample_count
                current_hour.real_house_load = house_load
                current_hour.real_solar_power_roof = solar_power_roof
                current_hour.real_extra_pv_power = extra_pv
                current_hour.total_active_power = active_power
                current_hour.real_solar_to_battery = solar_to_battery
                current_hour.real_grid_to_battery = grid_to_battery
                if pv_total_energy_at_hour_start is not None:
                    self._pv_total_energy_at_hour_start = pv_total_energy_at_hour_start
                if battery_charge_energy_at_hour_start is not None:
                    self._battery_charge_energy_at_hour_start = battery_charge_energy_at_hour_start
                if battery_discharge_energy_at_hour_start is not None:
                    self._battery_discharge_energy_at_hour_start = (
                        battery_discharge_energy_at_hour_start
                    )
                for sample in current_hour.five_min[:sample_count]:
                    sample.real_house_load = house_load
                    sample.real_solar_power_roof = solar_power_roof
                    sample.real_extra_pv_power = extra_pv
                    sample.total_active_power = active_power
                    sample.solar_to_battery = solar_to_battery
                    sample.grid_to_battery = grid_to_battery

        hour = self._today.hour[now.hour]
        hour.valid = True
        # Kept in sync every cycle (not just once) so a same-hour price
        # correction (e.g. Frank Energie republishing) is picked up before
        # this hour is persisted via storage.store_hour_data once it
        # becomes "previous_hour" below -- that's what makes
        # storage.retrieve_day_savings' EUR figures meaningful; before this,
        # Hour.price silently stayed at its 0.0 dataclass default forever,
        # since nothing else in this coordinator ever set it.
        prices_today = (self._prices_coordinator.data or {}).get("today")
        if prices_today is not None and prices_today.valid and prices_today.hour[now.hour].valid:
            hour.price = prices_today.hour[now.hour].price

        modbus_data = self._modbus_coordinator.data or {}
        pv_roof = modbus_data.get("pv_power", 0)
        battery_power = modbus_data.get("battery_power", 0)
        extra_pv_power = _extra_pv_power(self.hass, self._extra_pv_entity_id())

        house_load_power = _house_load_power(self.hass, self._house_load_entity_id())
        total_active_power = (
            house_load_power if house_load_power is not None else modbus_data.get("grid_power", 0)
        )

        tariff = _house_load_tariff(self.hass, self._house_load_entity_id())
        if tariff is not None:
            hour.real_tariff = tariff

        _sample_hour(
            hour,
            pv_roof,
            extra_pv_power or 0.0,
            total_active_power,
            battery_power,
            _effective_use_fee(
                use_fee, hour.real_tariff, network_use_fee_normal, network_use_fee_low
            ),
            return_fee,
            vat_percentage,
            return_vat_percentage,
        )

        pv_total_energy_kwh = modbus_data.get("pv_total_energy")
        battery_charge_energy_kwh = modbus_data.get("battery_total_energy_charge")
        battery_discharge_energy_kwh = modbus_data.get("battery_total_energy_discharge")
        hour_changed = previous_hour is not None and previous_hour != now.hour

        if previous_hour is not None and previous_hour != now.hour and previous_day is not None:
            # Override the just-completed hour's sample-averaged roof
            # generation with the inverter's own cumulative-register delta,
            # if available -- see _pv_total_energy_at_hour_start's comment.
            if pv_total_energy_kwh is not None and self._pv_total_energy_at_hour_start is not None:
                delta_wh = (pv_total_energy_kwh - self._pv_total_energy_at_hour_start) * 1000
                if delta_wh >= 0:  # guards a register reset/rollover, not just noise
                    previous_day.hour[previous_hour].real_solar_power_roof = round(delta_wh)

            # Same override for gross battery charge/discharge -- see
            # _battery_charge_energy_at_hour_start's comment.
            if (
                battery_charge_energy_kwh is not None
                and self._battery_charge_energy_at_hour_start is not None
            ):
                charge_delta_wh = (
                    battery_charge_energy_kwh - self._battery_charge_energy_at_hour_start
                ) * 1000
                if charge_delta_wh >= 0:
                    previous_day.hour[previous_hour].real_battery_charge_energy = round(
                        charge_delta_wh
                    )
            if (
                battery_discharge_energy_kwh is not None
                and self._battery_discharge_energy_at_hour_start is not None
            ):
                discharge_delta_wh = (
                    battery_discharge_energy_kwh - self._battery_discharge_energy_at_hour_start
                ) * 1000
                if discharge_delta_wh >= 0:
                    previous_day.hour[previous_hour].real_battery_discharge_energy = round(
                        discharge_delta_wh
                    )

            previous_hour_use_fee = _effective_use_fee(
                use_fee,
                previous_day.hour[previous_hour].real_tariff,
                network_use_fee_normal,
                network_use_fee_low,
            )
            await self.hass.async_add_executor_job(
                storage.store_hour_data,
                db_path,
                previous_day,
                previous_hour,
                previous_hour_use_fee,
                return_fee,
                vat_percentage,
            )
            await self.hass.async_add_executor_job(
                storage.delete_hour_progress,
                db_path,
                previous_day.year,
                previous_day.mon,
                previous_day.day,
                previous_hour,
            )

        if hour_changed or self._pv_total_energy_at_hour_start is None:
            self._pv_total_energy_at_hour_start = pv_total_energy_kwh
        if hour_changed or self._battery_charge_energy_at_hour_start is None:
            self._battery_charge_energy_at_hour_start = battery_charge_energy_kwh
        if hour_changed or self._battery_discharge_energy_at_hour_start is None:
            self._battery_discharge_energy_at_hour_start = battery_discharge_energy_kwh

        # Persisted after the baseline update above (not right after
        # _sample_hour) so a fresh hour's progress row is always tagged with
        # *that* hour's own baseline, never the just-completed hour's.
        if hour.five_min_count > 0:
            await self.hass.async_add_executor_job(
                storage.store_hour_progress,
                db_path,
                now.year,
                now.month,
                now.day,
                now.hour,
                hour.real_house_load,
                hour.real_solar_power_roof,
                hour.real_extra_pv_power,
                hour.total_active_power,
                hour.five_min_count,
                hour.real_solar_to_battery,
                hour.real_grid_to_battery,
                self._pv_total_energy_at_hour_start,
                self._battery_charge_energy_at_hour_start,
                self._battery_discharge_energy_at_hour_start,
            )

        self._last_hour = now.hour
        yesterday = now.date() - timedelta(days=1)
        yesterday_savings = await self.hass.async_add_executor_job(
            storage.retrieve_day_savings,
            db_path,
            yesterday.year,
            yesterday.month,
            yesterday.day,
            vat_percentage,
            return_vat_percentage,
        )
        return RealPowerData(
            day=self._today, extra_pv_power=extra_pv_power, yesterday_savings=yesterday_savings
        )

    def _extra_pv_entity_id(self) -> str | None:
        return self.config_entry.options.get(CONF_EXTRA_PV_POWER_ENTITY)

    def _house_load_entity_id(self) -> str | None:
        return self.config_entry.options.get(CONF_HOUSE_LOAD_POWER_ENTITY)


class AlphaEssLocalScheduleCoordinator(DataUpdateCoordinator[dict[str, Day]]):
    """Computes today's/tomorrow's battery schedule from the other coordinators' data.

    Not polled on a fixed interval — recomputing the DP scheduler every 30s
    would be wasteful. Refreshed on demand (`async_request_refresh`) by the
    hourly/daily `async_track_time_change` listeners set up in
    `__init__.py`, plus once at startup.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        modbus_coordinator: AlphaEssLocalDataUpdateCoordinator,
        prices_coordinator: AlphaEssLocalPricesCoordinator,
        solar_coordinator: SolarCoordinator,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_schedule",
            update_interval=None,
        )
        self._modbus_coordinator = modbus_coordinator
        self._prices_coordinator = prices_coordinator
        self._solar_coordinator = solar_coordinator

    async def _async_update_data(self) -> dict[str, Day]:
        """Merge price/solar/house-load sources and (re)run the scheduler."""
        db_path = get_db_path(self.hass, self.config_entry)
        options = self.config_entry.options
        now = dt_util.now()
        today_date = now.date()
        tomorrow_date = today_date + timedelta(days=1)

        mean_today = await self.hass.async_add_executor_job(
            storage.retrieve_mean_data, db_path, today_date.month, storage.c_weekday(today_date)
        )
        mean_tomorrow = await self.hass.async_add_executor_job(
            storage.retrieve_mean_data,
            db_path,
            tomorrow_date.month,
            storage.c_weekday(tomorrow_date),
        )

        today = merge_day_sources(
            today_date,
            self._prices_coordinator.data["today"],
            self._solar_coordinator.data["today"],
            mean_today,
        )
        tomorrow = merge_day_sources(
            tomorrow_date,
            self._prices_coordinator.data["tomorrow"],
            self._solar_coordinator.data["tomorrow"],
            mean_tomorrow,
        )

        # Restore today's once-per-day grid-charge/discharge budget (Phase
        # 7b) across restarts, so a Home Assistant reload doesn't forget it
        # already grid-charged/discharged today and risk doing so again.
        # Off by default's opposite — this option defaults ON; disabling it
        # reproduces the original's plain in-memory-only behavior.
        if options.get(CONF_PERSIST_DAILY_CHARGE_LIMIT, DEFAULT_PERSIST_DAILY_CHARGE_LIMIT):
            persisted = await self.hass.async_add_executor_job(
                storage.retrieve_dispatch_daily_state,
                db_path,
                today_date.year,
                today_date.month,
                today_date.day,
            )
            if persisted is not None:
                charge_on_grid_used, discharge_used, index_charge, index_discharge = persisted
                today.charge_on_grid_used = charge_on_grid_used
                today.discharge_used = discharge_used
                today.index_charge = index_charge
                today.index_discharge = index_discharge

        max_grid_load_wh = _effective_max_grid_load_wh(self.hass, self.config_entry)

        vat_percentage = options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE)
        config = ScheduleConfig(
            inverter_nominal_power=options.get(CONF_INVERTER_NOMINAL_POWER, 0),
            usable_battery_capacity=options.get(CONF_USABLE_BATTERY_CAPACITY, 0),
            use_fee=options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE),
            return_fee=options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE),
            vat_percentage=vat_percentage,
            return_vat_percentage=(
                vat_percentage
                if options.get(CONF_APPLY_VAT_ON_RETURN, DEFAULT_APPLY_VAT_ON_RETURN)
                else 0.0
            ),
            # Read live from this integration's own `number` entities
            # (number.py — sliders on the device itself, e.g. the
            # "Bediening"/Controls section) rather than a stored option
            # value, so the user can adjust these without a reload.
            # schedule.py's internal math uses data.py's 0.1%-unit scale
            # (e.g. 900=90.0%) for the SOC bounds, hence the *10; EUR for
            # daily_min_profit, hence the /100 (its own entity is in whole
            # cents — see number.py).
            max_soc_positive_price=round(
                _number_entity_value(
                    self.hass,
                    self.config_entry,
                    "max_soc_positive_price",
                    DEFAULT_MAX_SOC_POSITIVE_PRICE,
                )
                * 10
            ),
            max_soc_negative_price=round(
                _number_entity_value(
                    self.hass,
                    self.config_entry,
                    "max_soc_negative_price",
                    DEFAULT_MAX_SOC_NEGATIVE_PRICE,
                )
                * 10
            ),
            min_soc_discharge=round(
                _number_entity_value(
                    self.hass,
                    self.config_entry,
                    "min_soc_discharge",
                    DEFAULT_MIN_SOC_DISCHARGE,
                )
                * 10
            ),
            daily_min_profit=_number_entity_value(
                self.hass,
                self.config_entry,
                "daily_min_profit",
                round(DEFAULT_DAILY_MIN_PROFIT * 100),
            )
            / 100,
            discharge_enabled=_switch_entity_value(
                self.hass, self.config_entry, "discharge_enabled", True
            ),
            max_grid_load=max_grid_load_wh,
        )

        modbus_data = self._modbus_coordinator.data or {}
        cur_soc = round(modbus_data.get("battery_soc", 0) * SOC_MAX_BATTERY / 100)

        await self.hass.async_add_executor_job(
            run_scheduler, cur_soc, now.hour, today, tomorrow, config
        )

        return {"today": today, "tomorrow": tomorrow}


class AlphaEssLocalDispatchCoordinator(DataUpdateCoordinator[dispatch.DispatchDecision | None]):
    """Decides (and, if `control_enabled`, writes) the inverter's dispatch
    mode roughly every 20 seconds — port of `CheckAndSetChargingMode` plus
    `DoMinuteWork`'s once-per-day charge/discharge bookkeeping.

    Reading the inverter's current dispatch state (`async_get_dispatch_param`/
    `async_get_max_feed_into_grid`) always happens, regardless of
    `control_enabled` — that's what makes "log what it would do" possible
    without ever writing. The only two calls that touch the real inverter
    (`async_set_dispatch_param`/`async_set_max_feed_into_grid`) are gated
    behind that option, which defaults to off.

    Also (optionally) curtails a second, separately-metered "extra PV"
    installation via `modbus.write_register` during negative-price hours —
    see `dispatch.decide_extra_pv_state`. No-op unless both a Modbus hub is
    configured and its own feature toggle is on; the actual write is then
    still gated by `control_enabled`, same as the battery dispatch.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        modbus_coordinator: AlphaEssLocalDataUpdateCoordinator,
        schedule_coordinator: AlphaEssLocalScheduleCoordinator,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_dispatch",
            update_interval=timedelta(seconds=20),
        )
        self._modbus_coordinator = modbus_coordinator
        self._schedule_coordinator = schedule_coordinator
        self._last_hour: int | None = None
        self._last_read_dispatch_param: DispatchParam | None = None
        self._last_set_dispatch_param: DispatchParam | None = None
        self._provider_charging_power = 0
        self._manual_override: tuple[Charge, int | None] | None = None
        self._last_extra_pv_state: dispatch.ExtraPvState | None = None
        self._last_charge_on_grid_used = False
        self._last_discharge_used = False

    def set_manual_override(self, charge: Charge | None, cutoff_soc: int | None = None) -> None:
        """Force the next decision to use `charge` (and optionally its
        cutoff SOC) instead of the schedule's own choice for the current
        hour, until cleared with `charge=None`. Backing implementation for
        the manual-override services — port of `ReadConfigMinute`'s
        `C`/`D`/`P`/`N` commands, kept active (not one-shot) until
        explicitly cleared, matching the original's "config file still says
        the command" persistence but through a usable HA service."""
        self._manual_override = None if charge is None else (charge, cutoff_soc)

    async def _async_update_data(self) -> dispatch.DispatchDecision | None:
        """Decide this cycle's dispatch mode and (maybe) write it."""
        now = dt_util.now()
        hour_start = self._last_hour != now.hour

        schedule_data = self._schedule_coordinator.data
        schedule_day = schedule_data["today"] if schedule_data else None

        if schedule_day is not None and schedule_day.valid and schedule_day.hour[now.hour].valid:
            hour = schedule_day.hour[now.hour]
            if hour_start:
                for override in hour.provider_override:
                    override.valid = False
        else:
            if hour_start:
                LOGGER.warning(
                    "dispatch: no valid schedule for %02d:00 yet, using safe default", now.hour
                )
            hour = dispatch.SAFE_DEFAULT_HOUR

        options = self.config_entry.options
        db_path = get_db_path(self.hass, self.config_entry)
        persist_daily_limit = options.get(
            CONF_PERSIST_DAILY_CHARGE_LIMIT, DEFAULT_PERSIST_DAILY_CHARGE_LIMIT
        )
        control_enabled = options.get(CONF_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED)
        modbus_data = self._modbus_coordinator.data or {}
        cur_soc = round(modbus_data.get("battery_soc", 0) * SOC_MAX_BATTERY / 100)

        # Extra-PV price-based on/off control (optional — a Modbus write to
        # a *different* device, e.g. a second SMA inverter, via HA's core
        # `modbus` integration; not the same connection api.py uses for the
        # AlphaESS inverter itself). Deliberately decoupled from the
        # battery-dispatch logic below: it only depends on this hour's
        # earning/cutoff and the current SOC (all already available here),
        # so it runs unconditionally every cycle rather than being subject
        # to that logic's early returns (already-set, provider-override).
        # Needs both a configured hub AND its own
        # dedicated feature toggle (CONF_EXTRA_PV_CONTROL_ENABLED, separate
        # from the battery's CONF_CONTROL_ENABLED) — that toggle lets the
        # hub/register config be entered once and paused/resumed without
        # clearing it. Even when both are on, an actual write still only
        # happens when CONF_CONTROL_ENABLED is also on (logged either way).
        extra_pv_hub = options.get(CONF_EXTRA_PV_MODBUS_HUB)
        extra_pv_control_enabled = options.get(
            CONF_EXTRA_PV_CONTROL_ENABLED, DEFAULT_EXTRA_PV_CONTROL_ENABLED
        )
        if extra_pv_hub and extra_pv_control_enabled:
            extra_pv_state = dispatch.decide_extra_pv_state(hour, cur_soc)
            if extra_pv_state != self._last_extra_pv_state:
                value = int(
                    options.get(CONF_EXTRA_PV_MODBUS_ON_VALUE, DEFAULT_EXTRA_PV_MODBUS_ON_VALUE)
                    if extra_pv_state == dispatch.ExtraPvState.ON
                    else options.get(
                        CONF_EXTRA_PV_MODBUS_OFF_VALUE, DEFAULT_EXTRA_PV_MODBUS_OFF_VALUE
                    )
                )
                slave = int(options.get(CONF_EXTRA_PV_MODBUS_SLAVE, DEFAULT_EXTRA_PV_MODBUS_SLAVE))
                address = int(
                    options.get(CONF_EXTRA_PV_MODBUS_ADDRESS, DEFAULT_EXTRA_PV_MODBUS_ADDRESS)
                )
                LOGGER.info(
                    "dispatch: extra PV -> %s (hub %s, slave %d, register %d = %d) — %s",
                    extra_pv_state.name,
                    extra_pv_hub,
                    slave,
                    address,
                    value,
                    "writing" if control_enabled else "control disabled, not writing",
                )
                if control_enabled:
                    await self.hass.services.async_call(
                        "modbus",
                        "write_register",
                        {"hub": extra_pv_hub, "slave": slave, "address": address, "value": value},
                        blocking=True,
                    )
                self._last_extra_pv_state = extra_pv_state

        # Once-per-day budget persistence — schedule.py's DP now maintains
        # charge_on_grid_used/discharge_used itself (see
        # _calculate_best_schedule's forward trace), including deciding
        # when a rate-capped, multi-hour grid-charge session has actually
        # completed (see _evaluate_hour_action). This just detects the
        # False->True transition and persists it, so a restart doesn't
        # forget it already happened today.
        if schedule_day is not None:
            budget_newly_used = (
                schedule_day.charge_on_grid_used and not self._last_charge_on_grid_used
            ) or (schedule_day.discharge_used and not self._last_discharge_used)
            self._last_charge_on_grid_used = schedule_day.charge_on_grid_used
            self._last_discharge_used = schedule_day.discharge_used
            if persist_daily_limit and budget_newly_used:
                await self.hass.async_add_executor_job(
                    storage.store_dispatch_daily_state,
                    db_path,
                    now.year,
                    now.month,
                    now.day,
                    schedule_day.charge_on_grid_used,
                    schedule_day.discharge_used,
                    schedule_day.index_charge,
                    schedule_day.index_discharge,
                )

        if self._manual_override is not None:
            # Applied to a *copy* of the hour, not the schedule coordinator's
            # actual Day — a manual override is a deliberate, temporary
            # operator intervention, not a redefinition of what the
            # scheduler decided. The "Schedule current/next hour action"
            # sensors keep showing the scheduler's own choice throughout;
            # only this coordinator's decision (and the log/dispatch_current_
            # mode sensor) reflects the override.
            override_charge, override_cutoff_soc = self._manual_override
            hour = replace(
                hour,
                charge=override_charge,
                cutoff_soc=override_cutoff_soc
                if override_cutoff_soc is not None
                else hour.cutoff_soc,
            )

        client = self._modbus_coordinator.client

        try:
            cur_dispatch_param = await client.async_get_dispatch_param()
            cur_feed_in_percentage = await client.async_get_max_feed_into_grid()
        except AlphaEssLocalApiClientAuthenticationError as exception:
            raise UpdateFailed(exception) from exception
        except AlphaEssLocalApiClientError as exception:
            raise UpdateFailed(exception) from exception

        dispatch_config = dispatch.DispatchConfig(
            usable_battery_capacity=options.get(CONF_USABLE_BATTERY_CAPACITY, 0),
            max_grid_load=_effective_max_grid_load_wh(self.hass, self.config_entry),
        )
        decision = dispatch.decide_dispatch(
            hour, cur_soc, dispatch_config, hour.estimated_house_load
        )

        already_set = (
            not hour_start
            and dispatch.dispatch_params_equal(cur_dispatch_param, decision.param)
            and cur_feed_in_percentage == decision.target_feed_in_percentage
            and (
                decision.param.mode != DispatchMode.STATE_OF_CHARGE_CONTROL
                or cur_dispatch_param.duration > 0
            )
        )
        if already_set:
            LOGGER.debug("dispatch: mode/feed-in already set as required")
            self._last_hour = now.hour
            return decision

        # Provider-override detection (energy provider / grid operator
        # remotely put the inverter into State-of-Charge-Control itself,
        # e.g. FCAS/demand-response participation) — back off and let it
        # stand rather than fight it, unless we're about to run our own
        # planned negative-price grid charge and already have enough SOC.
        allow_provider_control = str(now.hour) in options.get(
            CONF_ALLOW_PROVIDER_CONTROL_HOURS, DEFAULT_ALLOW_PROVIDER_CONTROL_HOURS
        )
        if (
            cur_soc >= SOC_MAX_CHARGE_PROVIDER
            and schedule_day is not None
            and schedule_day.index_charge >= 0
            and schedule_day.hour[schedule_day.index_charge].earning == Earning.EARNING_ON_USE
            and now.hour <= schedule_day.index_charge
            and cur_dispatch_param.power >= 0
        ):
            allow_provider_control = False

        provider_active = (
            allow_provider_control
            and (
                self._last_set_dispatch_param is None
                or self._last_set_dispatch_param.mode != DispatchMode.STATE_OF_CHARGE_CONTROL
            )
            and cur_dispatch_param.started
            and cur_dispatch_param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
            and cur_dispatch_param.power != 0
        )
        if provider_active:
            if hour_start or self._provider_charging_power != cur_dispatch_param.power:
                self._provider_charging_power = cur_dispatch_param.power
                LOGGER.info(
                    "dispatch: provider controlled operation active: %d W (%s)",
                    cur_dispatch_param.power,
                    "charging" if cur_dispatch_param.power > 0 else "discharging",
                )
            if cur_dispatch_param.power < 0:
                # Provider is discharging — turn PV on to maximize revenue.
                if control_enabled:
                    await client.async_set_dispatch_param(replace(cur_dispatch_param, pv_on=True))
                else:
                    LOGGER.info(
                        "dispatch: would enable PV during provider discharge — "
                        "control disabled, not writing"
                    )
            quarter = now.minute // 15
            hour.provider_override[quarter].valid = True
            hour.provider_override[quarter].power_setting = cur_dispatch_param.power
            self._last_hour = now.hour
            return decision

        LOGGER.info(
            "dispatch: setting mode %s (power=%d W, cutoff=%.1f%%, PV %s), feed-in %d%% — %s",
            dispatch.set_dispatch_msg(decision.param.mode),
            decision.param.power,
            decision.param.cutoff_soc / 10,
            "on" if decision.param.pv_on else "off",
            decision.target_feed_in_percentage,
            "writing" if control_enabled else "control disabled, not writing",
        )
        if control_enabled:
            await client.async_set_max_feed_into_grid(decision.target_feed_in_percentage)
            await client.async_set_dispatch_param(decision.param)

        if self._provider_charging_power != 0:
            self._provider_charging_power = 0
            self._last_read_dispatch_param = None
            self._last_set_dispatch_param = None
            LOGGER.info("dispatch: provider controlled operation terminated")

        self._last_read_dispatch_param = cur_dispatch_param
        self._last_set_dispatch_param = decision.param
        self._last_hour = now.hour
        return decision


async def async_handle_hourly_rollover(
    schedule_coordinator: AlphaEssLocalScheduleCoordinator, _now: datetime
) -> None:
    """Port of DoHourlyWork's re-schedule trigger."""
    await schedule_coordinator.async_request_refresh()


def watch_for_source_recovery(
    source_coordinator: DataUpdateCoordinator[dict[str, Day]],
    schedule_coordinator: AlphaEssLocalScheduleCoordinator,
) -> Callable[[], None]:
    """Re-trigger a schedule recompute the first time `source_coordinator`
    (prices or solar) goes from "no valid today" to "valid today".

    Covers the startup race where schedule_coordinator's own first refresh
    runs before prices/solar have real data yet (their own
    `_StartupRaceRetryHelper` retries and recovers within seconds, but that
    only refreshes *their* data -- nothing otherwise tells schedule_coordinator
    to look again, so it would silently keep planning around a
    partial/empty day until the next natural hourly trigger, up to an hour
    later). Only fires on that specific False->True transition, not on
    every regular ~30-minute refresh, so schedule.py's deliberately-not-
    polled DP doesn't start rerunning on that cadence too.

    Returns the unsubscribe callable (pass to `entry.async_on_unload`).
    """
    had_valid = bool(
        (source_coordinator.data or {}).get("today") and source_coordinator.data["today"].valid
    )

    def _on_source_updated() -> None:
        nonlocal had_valid
        today = (source_coordinator.data or {}).get("today")
        now_valid = bool(today and today.valid)
        if now_valid and not had_valid:
            source_coordinator.hass.async_create_task(schedule_coordinator.async_request_refresh())
        had_valid = now_valid

    return source_coordinator.async_add_listener(_on_source_updated)


async def async_handle_daily_rollover(
    hass: HomeAssistant,
    entry: ConfigEntry,
    solar_coordinator: SolarCoordinator,
    schedule_coordinator: AlphaEssLocalScheduleCoordinator,
    _now: datetime,
) -> None:
    """Port of DoDailyWork's mean-data recompute + rebuild-and-reschedule."""
    options = entry.options
    pv_power = options.get(CONF_PV_POWER, 0)
    db_path = get_db_path(hass, entry)
    await hass.async_add_executor_job(storage.calculate_and_store_mean_data, db_path, pv_power)
    await solar_coordinator.async_request_refresh()
    await schedule_coordinator.async_request_refresh()
