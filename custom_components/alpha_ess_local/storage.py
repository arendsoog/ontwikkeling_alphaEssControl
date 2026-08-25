"""Historical data storage and weighted-mean/regression estimation.

Port of Data.c. SQLite replaces the original's Berkeley DB. The per-hour
solar-production regression is generalized to work against Forecast.Solar/
Solcast's raw estimate instead of NED.nl's forecast percentage (see the
port's plan doc) — same weighted-least-squares math and reliability gates,
just a different `x` input.

This module is standalone: it takes a `db_path` and does its own blocking
sqlite3 I/O, with no dependency on `hass`. Nothing calls these functions yet
— wiring (hourly `store_hour_data`, a daily `calculate_and_store_mean_data`
trigger, and seeding estimates via `retrieve_mean_data`) is a later phase,
once the control loop produces real hourly measurements to store.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime

from .const import LOGGER
from .data import MAX_HOURS, MAX_WEEK_DAYS, MIN_EXPECTED_SOLAR_POWER, Day, Earning
from .prices import mk_return_price, mk_use_price

MAX_MONTHS = 12

# recency tuning (days)
LOAD_TAU_DAYS = 21.0
SOLAR_TAU_DAYS = 28.0
_MIN_SOLAR_WEIGHT = 0.2

# shrinkage / fallback tuning
PRIOR_W = 3.0
DEFAULT_SIGMA_REL = 0.25

# solar regression reliability gates
MIN_SAMPLES = 4.0
MIN_ENERGY_RATIO = 0.15


@contextmanager
def _connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """sqlite3.connect() as a context manager only handles the transaction,
    not closing — this closes it too."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """`ALTER TABLE ... ADD COLUMN`, tolerating an already-existing column.

    SQLite has no `ADD COLUMN IF NOT EXISTS` — needed because
    `CREATE TABLE IF NOT EXISTS` is a no-op against a DB file created by an
    earlier version of this schema, so new columns must be migrated in
    explicitly rather than just added to the `CREATE TABLE` statement.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exception:
        if "duplicate column name" not in str(exception):
            raise


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hour_data (
            year INTEGER, mon INTEGER, day INTEGER, hour INTEGER,
            house_load REAL,
            solar_power_roof REAL,
            estimated_solar_power_raw REAL,
            extra_pv_power REAL,
            feed_in REAL,
            price REAL,
            use_fee REAL,
            return_fee REAL,
            PRIMARY KEY (year, mon, day, hour)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mean_hour (
            month INTEGER, wday INTEGER, hour INTEGER,
            house_load REAL, house_load_sigma REAL,
            PRIMARY KEY (month, wday, hour)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mean_solar_regression (
            hour INTEGER PRIMARY KEY,
            factor REAL, offset REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hour_progress (
            year INTEGER, mon INTEGER, day INTEGER, hour INTEGER,
            house_load REAL, solar_power_roof REAL, extra_pv_power REAL,
            total_active_power REAL, sample_count INTEGER,
            PRIMARY KEY (year, mon, day, hour)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_daily_state (
            year INTEGER, mon INTEGER, day INTEGER,
            charge_on_grid_used INTEGER, discharge_used INTEGER,
            index_charge INTEGER, index_discharge INTEGER,
            PRIMARY KEY (year, mon, day)
        )
        """
    )
    # Migrated in after the fact — solar/grid-to-battery attribution
    # (energy-balance estimate, see orchestrator._solar_to_battery).
    _add_column_if_missing(conn, "hour_data", "solar_to_battery", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "hour_data", "grid_to_battery", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "hour_progress", "solar_to_battery", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "hour_progress", "grid_to_battery", "REAL DEFAULT 0")
    # Migrated in after the fact — needed (alongside use_fee/return_fee,
    # already stored) to recompute historical EUR savings in
    # retrieve_day_savings. Nullable: rows stored before this column existed
    # fall back to a caller-supplied current vat_percentage instead.
    _add_column_if_missing(conn, "hour_data", "vat_percentage", "REAL")
    # Persists orchestrator.AlphaEssLocalRealDataCoordinator's in-progress
    # pv_total_energy hour-start baseline (see its docstring) -- without
    # this, a restart mid-hour would lose the baseline and that hour would
    # silently fall back to the coarser sample-averaged value. Nullable:
    # None until the coordinator's first sample of an hour.
    _add_column_if_missing(conn, "hour_progress", "pv_total_energy_at_hour_start", "REAL")


def store_hour_data(
    db_path: str, day: Day, hour: int, use_fee: float, return_fee: float, vat_percentage: float
) -> bool:
    """Port of StoreHourData: persist one completed hour's measured data."""
    if not day.valid or not (0 <= hour < MAX_HOURS):
        return False

    the_hour = day.hour[hour]
    if not the_hour.valid:
        return False

    house_load = the_hour.real_house_load
    solar_power_roof = the_hour.real_solar_power_roof
    extra_pv_power = the_hour.real_extra_pv_power

    # earning-on-use turns the panels off; zero out solar so it doesn't
    # pollute the regression (ported behavior, "version 1.1.1" comment)
    if the_hour.earning == Earning.EARNING_ON_USE:
        solar_power_roof = 0
        extra_pv_power = 0

    if house_load == 0:
        # Faithful to the original: skipped, not an error -- a real house
        # never draws exactly 0 W for a full hour, so this is almost always
        # a missed/failed measurement (e.g. a Modbus hiccup that hour), not
        # a genuine reading. Still logged (unlike the original) so a gap in
        # retrieve_day_hours/retrieve_day_savings' output has a visible
        # cause instead of just silently missing that hour.
        LOGGER.warning(
            "storage: skipping hour_data for %04d-%02d-%02d %02d:00 -- house_load "
            "measured as exactly 0 W, most likely a missed reading rather than a "
            "real 0 W hour",
            day.year,
            day.mon,
            day.day,
            hour,
        )
        return True

    with _connection(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO hour_data
                (year, mon, day, hour, house_load, solar_power_roof,
                 estimated_solar_power_raw, extra_pv_power, feed_in, price, use_fee, return_fee,
                 solar_to_battery, grid_to_battery, vat_percentage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (year, mon, day, hour) DO UPDATE SET
                house_load=excluded.house_load,
                solar_power_roof=excluded.solar_power_roof,
                estimated_solar_power_raw=excluded.estimated_solar_power_raw,
                extra_pv_power=excluded.extra_pv_power,
                feed_in=excluded.feed_in,
                price=excluded.price,
                use_fee=excluded.use_fee,
                return_fee=excluded.return_fee,
                solar_to_battery=excluded.solar_to_battery,
                grid_to_battery=excluded.grid_to_battery,
                vat_percentage=excluded.vat_percentage
            """,
            (
                day.year,
                day.mon,
                day.day,
                hour,
                house_load,
                solar_power_roof,
                the_hour.estimated_solar_power_raw,
                extra_pv_power,
                the_hour.total_active_power,
                the_hour.price,
                use_fee,
                return_fee,
                the_hour.real_solar_to_battery,
                the_hour.real_grid_to_battery,
                vat_percentage,
            ),
        )
        conn.commit()
    return True


def retrieve_day_hours(
    db_path: str, year: int, mon: int, day: int
) -> dict[int, tuple[float, float, float, float, float]]:
    """Read back already-stored hours for one calendar date.

    Used to rehydrate a coordinator's in-memory "today" accumulator after a
    Home Assistant restart mid-day — without this, hours completed before
    the restart (already safely persisted by `store_hour_data`) would be
    invisible to the running total until the next full day, since that
    accumulator otherwise starts from a blank `Day` on every startup.

    Returns `{hour: (house_load, solar_power_roof, extra_pv_power,
    solar_to_battery, grid_to_battery)}`, empty if the DB has nothing for
    that date yet (e.g. a genuine midnight rollover, or the file doesn't
    exist).
    """
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT hour, house_load, solar_power_roof, extra_pv_power,
                   solar_to_battery, grid_to_battery
            FROM hour_data
            WHERE year = ? AND mon = ? AND day = ?
            """,
            (year, mon, day),
        ).fetchall()
    return {row[0]: (row[1], row[2], row[3], row[4] or 0.0, row[5] or 0.0) for row in rows}


def _net_grid_cost(
    power_w: float,
    price: float,
    use_fee: float,
    return_fee: float,
    vat: float,
    return_vat: float | None = None,
) -> float:
    """EUR spent (positive) or earned (negative) importing (power_w > 0) or
    exporting (power_w < 0) power_w watts for one hour (a DP-style 1-hour
    step, so Wh == W).

    `return_vat` overrides `vat` for the export side only (e.g. a
    BTW-exempt teruglevering formula) -- defaults to `vat` when omitted.
    """
    if power_w > 0:
        return power_w * mk_use_price(price, use_fee, vat) / 1000.0
    return (
        power_w
        * mk_return_price(price, return_fee, return_vat if return_vat is not None else vat)
        / 1000.0
    )


def retrieve_day_savings(
    db_path: str,
    year: int,
    mon: int,
    day: int,
    default_vat_percentage: float,
    return_vat_percentage: float | None = None,
) -> list[dict[str, float]]:
    """Per-hour real solar generation and cost savings for a completed day.

    `return_vat_percentage` overrides each hour's stored VAT rate for the
    export side only (e.g. a BTW-exempt teruglevering formula) -- defaults
    to that hour's own use-side VAT rate when omitted, same as before this
    parameter existed. Deliberately a fresh, current-options value rather
    than something stored per-row like `use_fee`/`vat_percentage` -- this
    toggle is a slow-moving contract fact, not worth a schema migration to
    protect historical rows against a later change.

    For each stored hour, compares three scenarios using that hour's own
    stored price/fees (and `vat_percentage` where stored — rows predating
    that column fall back to `default_vat_percentage`):
    - no solar/battery: `house_load` bought entirely from the grid
    - solar only, no battery: `house_load - solar_power_roof -
      extra_pv_power` (both the AlphaESS's own roof panels and a separate
      second installation, if configured) bought/sold directly, with no
      buffering
    - actual: the real net grid exchange (`feed_in`, i.e. the AlphaESS's
      total active power — already reflects whatever the battery actually
      did that hour, not an estimate)

    Returns one dict per stored hour, sorted by hour: `{hour, solar_wh,
    solar_wh_roof, solar_wh_extra, savings_solar_eur, savings_battery_eur}`.
    `solar_wh` is the combined total (both installations); `solar_wh_roof`/
    `solar_wh_extra` are its two components, exposed separately so a
    dashboard can show generation per installation instead of only the
    combined figure. `savings_battery_eur` is the extra saved (or, if
    negative, lost) beyond solar alone — e.g. from grid-charging cheap and
    using/selling that later.
    """
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT hour, house_load, solar_power_roof, extra_pv_power, feed_in,
                   price, use_fee, return_fee, vat_percentage
            FROM hour_data
            WHERE year = ? AND mon = ? AND day = ?
            ORDER BY hour
            """,
            (year, mon, day),
        ).fetchall()

    results = []
    for (
        hour,
        house_load,
        solar_power_roof,
        extra_pv_power,
        feed_in,
        price,
        use_fee,
        return_fee,
        vat,
    ) in rows:
        vat = vat if vat is not None else default_vat_percentage
        effective_return_vat = return_vat_percentage if return_vat_percentage is not None else vat
        # Total solar from *both* installations — the AlphaESS's own roof
        # panels and a separate second installation (e.g. an SMA Tripower),
        # if configured (see orchestrator._extra_pv_power). `feed_in` (the
        # AlphaESS's own total active power) already nets out extra_pv_power
        # on its own, since that second installation feeds the house/grid
        # independently of the AlphaESS's battery — only cost_solar_only
        # needs it added explicitly.
        total_solar = (solar_power_roof or 0.0) + (extra_pv_power or 0.0)
        cost_no_solar = _net_grid_cost(
            house_load, price, use_fee, return_fee, vat, effective_return_vat
        )
        cost_solar_only = _net_grid_cost(
            house_load - total_solar, price, use_fee, return_fee, vat, effective_return_vat
        )
        cost_actual = _net_grid_cost(feed_in, price, use_fee, return_fee, vat, effective_return_vat)
        results.append(
            {
                "hour": hour,
                "solar_wh": round(total_solar),
                "solar_wh_roof": round(solar_power_roof or 0.0),
                "solar_wh_extra": round(extra_pv_power or 0.0),
                "savings_solar_eur": round(cost_no_solar - cost_solar_only, 4),
                "savings_battery_eur": round(cost_solar_only - cost_actual, 4),
            }
        )
    return results


def store_hour_progress(
    db_path: str,
    year: int,
    mon: int,
    day: int,
    hour: int,
    house_load: float,
    solar_power_roof: float,
    extra_pv_power: float,
    total_active_power: float,
    sample_count: int,
    solar_to_battery: float,
    grid_to_battery: float,
    pv_total_energy_at_hour_start: float | None = None,
) -> None:
    """Persist the still-in-progress hour's running averages + sample count.

    Called after every 5-minute sample (not just on hour completion), so a
    restart mid-hour loses at most the time since the last sample instead of
    every sample gathered so far this hour. Only one row exists at a time in
    practice — `delete_hour_progress` clears it once the hour completes and
    lands in `hour_data` instead.

    `pv_total_energy_at_hour_start` persists orchestrator.
    AlphaEssLocalRealDataCoordinator's cumulative-register baseline for this
    hour, so a restart doesn't lose it (see that coordinator's docstring).
    """
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO hour_progress
                (year, mon, day, hour, house_load, solar_power_roof,
                 extra_pv_power, total_active_power, sample_count,
                 solar_to_battery, grid_to_battery, pv_total_energy_at_hour_start)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (year, mon, day, hour) DO UPDATE SET
                house_load=excluded.house_load,
                solar_power_roof=excluded.solar_power_roof,
                extra_pv_power=excluded.extra_pv_power,
                total_active_power=excluded.total_active_power,
                sample_count=excluded.sample_count,
                solar_to_battery=excluded.solar_to_battery,
                grid_to_battery=excluded.grid_to_battery,
                pv_total_energy_at_hour_start=excluded.pv_total_energy_at_hour_start
            """,
            (
                year,
                mon,
                day,
                hour,
                house_load,
                solar_power_roof,
                extra_pv_power,
                total_active_power,
                sample_count,
                solar_to_battery,
                grid_to_battery,
                pv_total_energy_at_hour_start,
            ),
        )
        conn.commit()


def retrieve_hour_progress(
    db_path: str, year: int, mon: int, day: int, hour: int
) -> tuple[float, float, float, float, int, float, float, float | None] | None:
    """Read back one hour's in-progress running averages + sample count.

    Returns `(house_load, solar_power_roof, extra_pv_power,
    total_active_power, sample_count, solar_to_battery, grid_to_battery,
    pv_total_energy_at_hour_start)`, or `None` if nothing was persisted for
    that hour yet (e.g. its very first sample hasn't landed).
    `pv_total_energy_at_hour_start` is `None` for rows stored before that
    column existed, or when the cumulative register wasn't available yet.
    """
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT house_load, solar_power_roof, extra_pv_power, total_active_power,
                   sample_count, solar_to_battery, grid_to_battery,
                   pv_total_energy_at_hour_start
            FROM hour_progress
            WHERE year = ? AND mon = ? AND day = ? AND hour = ?
            """,
            (year, mon, day, hour),
        ).fetchone()
    if row is None:
        return None
    return (row[0], row[1], row[2], row[3], row[4], row[5] or 0.0, row[6] or 0.0, row[7])


def delete_hour_progress(db_path: str, year: int, mon: int, day: int, hour: int) -> None:
    """Remove a completed hour's progress row — its data now lives in `hour_data`."""
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            "DELETE FROM hour_progress WHERE year = ? AND mon = ? AND day = ? AND hour = ?",
            (year, mon, day, hour),
        )
        conn.commit()


def store_dispatch_daily_state(
    db_path: str,
    year: int,
    mon: int,
    day: int,
    charge_on_grid_used: bool,
    discharge_used: bool,
    index_charge: int,
    index_discharge: int,
) -> None:
    """Persist today's once-per-day grid-charge/discharge budget.

    Without this, a Home Assistant restart mid-day would forget it already
    grid-charged/discharged today (this state was only ever in-memory in the
    original C program) and could do so again once dispatch control is
    enabled — defeating the once-per-day limit `schedule.py`'s DP is built
    around. Only meaningful when the `persist_daily_charge_limit` option is
    on; the caller decides whether to call this at all.
    """
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO dispatch_daily_state
                (year, mon, day, charge_on_grid_used, discharge_used, index_charge, index_discharge)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (year, mon, day) DO UPDATE SET
                charge_on_grid_used=excluded.charge_on_grid_used,
                discharge_used=excluded.discharge_used,
                index_charge=excluded.index_charge,
                index_discharge=excluded.index_discharge
            """,
            (
                year,
                mon,
                day,
                int(charge_on_grid_used),
                int(discharge_used),
                index_charge,
                index_discharge,
            ),
        )
        conn.commit()


def retrieve_dispatch_daily_state(
    db_path: str, year: int, mon: int, day: int
) -> tuple[bool, bool, int, int] | None:
    """Read back today's once-per-day grid-charge/discharge budget.

    Returns `(charge_on_grid_used, discharge_used, index_charge,
    index_discharge)`, or `None` if nothing was stored for that date yet
    (fresh day, or the feature was just turned on).
    """
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT charge_on_grid_used, discharge_used, index_charge, index_discharge
            FROM dispatch_daily_state
            WHERE year = ? AND mon = ? AND day = ?
            """,
            (year, mon, day),
        ).fetchone()
    if row is None:
        return None
    return (bool(row[0]), bool(row[1]), row[2], row[3])


@dataclass
class _HourAccumulator:
    # accumulation phase
    sum_w: float = 0.0
    house_load_sum: float = 0.0
    house_load_sum2: float = 0.0
    # final results
    house_load: float = 0.0
    house_load_sigma: float = 0.0


@dataclass
class _WeekDay:
    valid: bool = False
    hour: list = field(default_factory=lambda: [_HourAccumulator() for _ in range(MAX_HOURS)])


@dataclass
class _Month:
    valid: bool = False
    week_day: list = field(default_factory=lambda: [_WeekDay() for _ in range(MAX_WEEK_DAYS)])


@dataclass
class _SolarRegressionAccumulator:
    sum_w: float = 0.0
    sum_wx: float = 0.0
    sum_wy: float = 0.0
    sum_wxx: float = 0.0
    sum_wxy: float = 0.0
    weighted_count: float = 0.0
    factor: float = -1.0
    offset: float = 0.0


@dataclass
class _MeanData:
    month: list = field(default_factory=lambda: [_Month() for _ in range(MAX_MONTHS)])
    solar_regression: list = field(
        default_factory=lambda: [_SolarRegressionAccumulator() for _ in range(MAX_HOURS)]
    )


def _recency_weight(now: datetime, sample: datetime, tau_days: float) -> float:
    days = (now - sample).total_seconds() / 86400.0
    if days < 0:
        days = 0.0
    return math.exp(-days / tau_days)


def _day_class(c_wday: int) -> int:
    """0 = weekday (Mon-Fri), 1 = weekend (Sat/Sun). c_wday: C convention, 0=Sun..6=Sat."""
    return 1 if c_wday in (0, 6) else 0


def c_weekday(the_date: date_cls) -> int:
    """Convert a Python date (Monday=0) to C's tm_wday convention (Sunday=0)."""
    return (the_date.weekday() + 1) % 7


def _count_regression_data(
    mean_data: _MeanData,
    hour: int,
    estimated_raw: float,
    measured: float,
    w_solar: float,
    pv_power: float,
) -> None:
    min_y = max(MIN_EXPECTED_SOLAR_POWER, 0.02 * pv_power)
    if measured <= min_y:
        return
    r = mean_data.solar_regression[hour]
    r.sum_w += w_solar
    r.sum_wx += w_solar * estimated_raw
    r.sum_wy += w_solar * measured
    r.sum_wxx += w_solar * estimated_raw * estimated_raw
    r.sum_wxy += w_solar * estimated_raw * measured
    r.weighted_count += w_solar


def _count_home_load_data(
    mean_data: _MeanData, mon0: int, c_wday: int, hour: int, w: float, house_load: float
) -> None:
    mean_data.month[mon0].valid = True
    mean_data.month[mon0].week_day[c_wday].valid = True
    acc = mean_data.month[mon0].week_day[c_wday].hour[hour]
    acc.sum_w += w
    acc.house_load_sum += w * house_load
    acc.house_load_sum2 += w * house_load * house_load


def _is_factor_reliable(
    weighted_count: float, sum_wxx: float, factor: float, pv_power: float
) -> bool:
    if weighted_count < MIN_SAMPLES:
        return False
    if pv_power <= 0:
        return False
    energy_ratio = sum_wxx / (pv_power * pv_power)
    if energy_ratio < MIN_ENERGY_RATIO:
        return False
    return 0.2 <= factor <= 2.5


def _calculate_regression(mean_data: _MeanData, pv_power: float) -> None:
    for hour in range(MAX_HOURS):
        r = mean_data.solar_regression[hour]
        if r.sum_w > 0.0:
            denom = r.sum_w * r.sum_wxx - r.sum_wx * r.sum_wx
            if abs(denom) > 1e-9:
                a = (r.sum_w * r.sum_wxy - r.sum_wx * r.sum_wy) / denom
                b = (r.sum_wxx * r.sum_wy - r.sum_wx * r.sum_wxy) / denom
                a = min(max(a, 0.1), 2.5)
                b = min(max(b, 0.0), 0.20 * pv_power)
                r.factor = a
                r.offset = b
            else:
                r.factor = 1.0
                r.offset = 0.0
        else:
            r.factor = 1.0
            r.offset = 0.0

        if not _is_factor_reliable(r.weighted_count, r.sum_wxx, r.factor, pv_power):
            r.factor = -1.0


def _try_candidate(
    mean_data: _MeanData, cm: int, cwd: int, hour: int
) -> tuple[float, float] | None:
    if not mean_data.month[cm].week_day[cwd].valid:
        return None
    acc = mean_data.month[cm].week_day[cwd].hour[hour]
    if acc.sum_w <= 0.0:
        return None
    mean = acc.house_load_sum / acc.sum_w
    var = max(0.0, acc.house_load_sum2 / acc.sum_w - mean * mean)
    sigma = math.sqrt(var)
    if sigma <= 0.0:
        sigma = abs(mean) * DEFAULT_SIGMA_REL + 0.1
    return mean, sigma


def _find_fallback_hour_mean(
    mean_data: _MeanData, mon: int, wday: int, hour: int, best_month: int
) -> tuple[float, float] | None:
    target_class = _day_class(wday)

    for wd in range(MAX_WEEK_DAYS):
        if mean_data.month[mon].week_day[wd].valid and _day_class(wd) == target_class:
            result = _try_candidate(mean_data, mon, wd, hour)
            if result:
                return result

    for wd in range(MAX_WEEK_DAYS):
        if mean_data.month[mon].week_day[wd].valid:
            result = _try_candidate(mean_data, mon, wd, hour)
            if result:
                return result

    for m2 in range(MAX_MONTHS):
        if m2 == mon:
            continue
        for wd in range(MAX_WEEK_DAYS):
            if mean_data.month[m2].week_day[wd].valid and _day_class(wd) == target_class:
                result = _try_candidate(mean_data, m2, wd, hour)
                if result:
                    return result

    if best_month >= 0:
        for wd in range(MAX_WEEK_DAYS):
            if mean_data.month[best_month].week_day[wd].valid:
                result = _try_candidate(mean_data, best_month, wd, hour)
                if result:
                    return result

    return None


def _finalize_hour(mean_data: _MeanData, mon: int, wday: int, hour: int, best_month: int) -> None:
    acc = mean_data.month[mon].week_day[wday].hour[hour]
    fallback = _find_fallback_hour_mean(mean_data, mon, wday, hour, best_month)

    if acc.sum_w > 0.0:
        obs_mean = acc.house_load_sum / acc.sum_w
        obs_var = max(0.0, acc.house_load_sum2 / acc.sum_w - obs_mean * obs_mean)
        obs_sigma = math.sqrt(obs_var)

        if fallback is None:
            acc.house_load = obs_mean
            acc.house_load_sigma = (
                obs_sigma if obs_sigma > 0.0 else abs(obs_mean) * DEFAULT_SIGMA_REL + 0.1
            )
            return

        fallback_mean, fallback_sigma = fallback
        wsum = acc.sum_w
        mean_shrunk = (wsum * obs_mean + PRIOR_W * fallback_mean) / (wsum + PRIOR_W)

        combined_var = (wsum * obs_sigma**2 + PRIOR_W * fallback_sigma**2) / (wsum + PRIOR_W)
        delta = obs_mean - fallback_mean
        mean_diff_var = (wsum * PRIOR_W) / (wsum + PRIOR_W) ** 2 * (delta * delta)
        combined_var = max(0.0, combined_var + mean_diff_var)

        acc.house_load = mean_shrunk
        acc.house_load_sigma = math.sqrt(combined_var)
        return

    if fallback is not None:
        acc.house_load, acc.house_load_sigma = fallback
    else:
        acc.house_load = 0.0
        acc.house_load_sigma = 1.0
        LOGGER.warning(
            "calculate_and_store_mean_data: no fallback for mon=%d wday=%d hour=%d", mon, wday, hour
        )


def _has_partial_weekday_or_weekend(mean_data: _MeanData, mon: int) -> bool:
    has_weekday = any(mean_data.month[mon].week_day[wd].valid for wd in range(1, 6))
    has_weekend = mean_data.month[mon].week_day[6].valid or mean_data.month[mon].week_day[0].valid
    return has_weekday or has_weekend


def _fill_month_fallback(mean_data: _MeanData, mon: int) -> None:
    for wday in range(MAX_WEEK_DAYS):
        if mean_data.month[mon].week_day[wday].valid:
            continue

        target_class = _day_class(wday)
        base_mon = -1
        base_wday = -1

        for wd in range(MAX_WEEK_DAYS):
            if mean_data.month[mon].week_day[wd].valid and _day_class(wd) == target_class:
                base_mon, base_wday = mon, wd
                break

        if base_wday < 0:
            for wd in range(MAX_WEEK_DAYS):
                if mean_data.month[mon].week_day[wd].valid:
                    base_mon, base_wday = mon, wd
                    break

        if base_wday < 0:
            for m2 in range(MAX_MONTHS):
                if m2 == mon:
                    continue
                for wd in range(MAX_WEEK_DAYS):
                    if mean_data.month[m2].week_day[wd].valid and _day_class(wd) == target_class:
                        base_mon, base_wday = m2, wd
                        break
                if base_wday >= 0:
                    break

        if base_wday < 0:
            for m2 in range(MAX_MONTHS):
                for wd in range(MAX_WEEK_DAYS):
                    if mean_data.month[m2].week_day[wd].valid:
                        base_mon, base_wday = m2, wd
                        break
                if base_wday >= 0:
                    break

        if base_wday >= 0:
            for hour in range(MAX_HOURS):
                mean_data.month[mon].week_day[wday].hour[hour] = (
                    mean_data.month[base_mon].week_day[base_wday].hour[hour]
                )
            mean_data.month[mon].week_day[wday].valid = True


def _store_mean_data(db_path: str, mean_data: _MeanData) -> None:
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM mean_hour")
        conn.execute("DELETE FROM mean_solar_regression")
        conn.executemany(
            "INSERT INTO mean_hour (month, wday, hour, house_load, house_load_sigma) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    mon0,
                    wd,
                    hour,
                    mean_data.month[mon0].week_day[wd].hour[hour].house_load,
                    mean_data.month[mon0].week_day[wd].hour[hour].house_load_sigma,
                )
                for mon0 in range(MAX_MONTHS)
                for wd in range(MAX_WEEK_DAYS)
                for hour in range(MAX_HOURS)
            ),
        )
        conn.executemany(
            "INSERT INTO mean_solar_regression (hour, factor, offset) VALUES (?, ?, ?)",
            (
                (
                    hour,
                    mean_data.solar_regression[hour].factor,
                    mean_data.solar_regression[hour].offset,
                )
                for hour in range(MAX_HOURS)
            ),
        )
        conn.commit()


def calculate_and_store_mean_data(db_path: str, pv_power: float) -> bool:
    """Port of CalculateAndStoreMeanData: recompute the weighted house-load
    mean/sigma and solar-regression correction from all stored history."""
    with _connection(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT year, mon, day, hour, house_load, solar_power_roof, estimated_solar_power_raw "
            "FROM hour_data"
        ).fetchall()

    if not rows:
        LOGGER.info("calculate_and_store_mean_data: no history yet")
        return False

    mean_data = _MeanData()
    now = datetime.now()

    for year, mon, day, hour, house_load, solar_power_roof, estimated_solar_power_raw in rows:
        try:
            sample_time = datetime(year, mon, day, hour)
        except ValueError:
            continue

        w_solar = max(_MIN_SOLAR_WEIGHT, _recency_weight(now, sample_time, SOLAR_TAU_DAYS))
        _count_regression_data(
            mean_data, hour, estimated_solar_power_raw, solar_power_roof, w_solar, pv_power
        )

        w = _recency_weight(now, sample_time, LOAD_TAU_DAYS)
        c_wday = c_weekday(sample_time.date())
        _count_home_load_data(mean_data, mon - 1, c_wday, hour, w, house_load)

    LOGGER.info("calculate_and_store_mean_data: found %d hour_data rows", len(rows))

    _calculate_regression(mean_data, pv_power)

    best_month = -1
    best_score = -1
    for mon0 in range(MAX_MONTHS):
        valid_days = sum(
            1 for wd in range(MAX_WEEK_DAYS) if mean_data.month[mon0].week_day[wd].valid
        )
        valid_hours = sum(
            1
            for wd in range(MAX_WEEK_DAYS)
            if mean_data.month[mon0].week_day[wd].valid
            for hour in range(MAX_HOURS)
            if mean_data.month[mon0].week_day[wd].hour[hour].sum_w > 0.0
        )
        score = valid_days * 24 + valid_hours
        if score > best_score:
            best_score = score
            best_month = mon0

    if best_month < 0:
        LOGGER.info("calculate_and_store_mean_data: no valid weekdays found, not storing")
        return False

    for mon0 in range(MAX_MONTHS):
        for wday in range(MAX_WEEK_DAYS):
            if not mean_data.month[mon0].week_day[wday].valid:
                continue
            for hour in range(MAX_HOURS):
                _finalize_hour(mean_data, mon0, wday, hour, best_month)

    for mon0 in range(MAX_MONTHS):
        if _has_partial_weekday_or_weekend(mean_data, mon0):
            _fill_month_fallback(mean_data, mon0)

    for mon0 in range(MAX_MONTHS):
        for wd in range(MAX_WEEK_DAYS):
            if mean_data.month[mon0].week_day[wd].valid:
                continue
            for hour in range(MAX_HOURS):
                mean_data.month[mon0].week_day[wd].hour[hour] = (
                    mean_data.month[best_month].week_day[wd].hour[hour]
                )
            mean_data.month[mon0].week_day[wd].valid = True

    _store_mean_data(db_path, mean_data)
    return True


@dataclass
class HourMean:
    """One hour's estimate: weighted house-load mean/sigma + solar correction."""

    house_load: float
    house_load_sigma: float
    solar_factor: float  # -1.0 = not (yet) reliable, matching IsFactorReliable's convention
    solar_offset: float


def retrieve_mean_data(db_path: str, month: int, wday: int) -> dict[int, HourMean] | None:
    """Port of RetrieveMeanData.

    `month` is 1-based (1..12); `wday` is C's tm_wday convention (0=Sun..6=Sat),
    matching how the future control-loop port will call this (`today.mon`,
    the sample date's weekday). Returns {hour: HourMean} for the hours that
    have mean data, or None if nothing has been computed yet.
    """
    if not (1 <= month <= MAX_MONTHS) or not (0 <= wday < MAX_WEEK_DAYS):
        return None

    with _connection(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT hour, house_load, house_load_sigma FROM mean_hour WHERE month = ? AND wday = ?",
            (month - 1, wday),
        ).fetchall()
        if not rows:
            return None
        solar_rows = conn.execute(
            "SELECT hour, factor, offset FROM mean_solar_regression"
        ).fetchall()

    solar_by_hour = {hour: (factor, offset) for hour, factor, offset in solar_rows}

    result: dict[int, HourMean] = {}
    for hour, house_load, house_load_sigma in rows:
        factor, offset = solar_by_hour.get(hour, (-1.0, 0.0))
        result[hour] = HourMean(
            house_load=house_load,
            house_load_sigma=house_load_sigma,
            solar_factor=factor,
            solar_offset=offset,
        )
    return result
