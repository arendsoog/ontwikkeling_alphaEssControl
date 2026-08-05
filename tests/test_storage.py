"""Tests for storage.py.

storage.py is standalone (no `hass` dependency), so most of these are plain
function tests against a throwaway SQLite file (`tmp_path`). Recency-weighted
tests use the `freezer` fixture (pytest-freezer/freezegun) to pin "now" so
the weighting math is deterministic.
"""

import sqlite3
from contextlib import closing
from datetime import datetime

from custom_components.alpha_ess_local.data import Day, Earning
from custom_components.alpha_ess_local.storage import (
    LOAD_TAU_DAYS,
    MIN_SAMPLES,
    _day_class,
    _is_factor_reliable,
    _recency_weight,
    c_weekday,
    calculate_and_store_mean_data,
    delete_hour_progress,
    retrieve_day_hours,
    retrieve_dispatch_daily_state,
    retrieve_hour_progress,
    retrieve_mean_data,
    store_dispatch_daily_state,
    store_hour_data,
    store_hour_progress,
)


def _valid_day(year=2024, mon=1, day=15) -> Day:
    day_obj = Day(year=year, mon=mon, day=day, valid=True)
    return day_obj


# --- store_hour_data -------------------------------------------------------


def test_store_hour_data_persists_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 500
    hour.real_solar_power_roof = 300
    hour.estimated_solar_power_raw = 250.0
    hour.price = 0.20
    hour.real_solar_to_battery = 120
    hour.real_grid_to_battery = 30

    assert store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01) is True

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT house_load, solar_power_roof, estimated_solar_power_raw, use_fee, return_fee, "
            "solar_to_battery, grid_to_battery "
            "FROM hour_data WHERE year=2024 AND mon=1 AND day=15 AND hour=10"
        ).fetchone()

    assert row == (500, 300, 250.0, 0.02, 0.01, 120, 30)


def test_store_hour_data_skips_zero_house_load(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.hour[10].valid = True
    day.hour[10].real_house_load = 0

    # Faithful to the original: houseLoad == 0 skips the DB entirely, so the
    # file is never even created.
    assert store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01) is True
    assert not (tmp_path / "test.db").exists()


def test_store_hour_data_returns_false_when_day_invalid(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.valid = False
    day.hour[10].valid = True
    day.hour[10].real_house_load = 500

    assert store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01) is False


def test_store_hour_data_returns_false_when_hour_invalid(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.hour[10].valid = False

    assert store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01) is False


def test_store_hour_data_overwrites_same_key(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.hour[10].valid = True
    day.hour[10].real_house_load = 500
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01)

    day.hour[10].real_house_load = 700
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01)

    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT house_load FROM hour_data").fetchall()
    assert rows == [(700,)]


def test_store_hour_data_zeroes_solar_on_earning_on_use(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 500
    hour.real_solar_power_roof = 300
    hour.real_extra_pv_power = 150
    hour.earning = Earning.EARNING_ON_USE

    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01)

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT solar_power_roof, extra_pv_power FROM hour_data WHERE hour=10"
        ).fetchone()
    assert row == (0, 0)


# --- retrieve_day_hours -------------------------------------------------------


def test_retrieve_day_hours_returns_stored_hours_for_the_date(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    for hour_index, load in ((8, 400), (9, 350)):
        hour = day.hour[hour_index]
        hour.valid = True
        hour.real_house_load = load
        hour.real_solar_power_roof = 100
        hour.real_extra_pv_power = 20
        hour.real_solar_to_battery = 50
        hour.real_grid_to_battery = 10
        store_hour_data(db_path, day, hour_index, use_fee=0.02, return_fee=0.01)

    result = retrieve_day_hours(db_path, 2024, 1, 15)

    assert result == {8: (400, 100, 20, 50, 10), 9: (350, 100, 20, 50, 10)}


def test_retrieve_day_hours_excludes_other_dates(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    day.hour[8].valid = True
    day.hour[8].real_house_load = 400
    store_hour_data(db_path, day, 8, use_fee=0.02, return_fee=0.01)

    assert retrieve_day_hours(db_path, 2024, 1, 16) == {}


def test_retrieve_day_hours_empty_on_fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_day_hours(db_path, 2024, 1, 15) == {}


# --- hour_progress -------------------------------------------------------------


def test_store_and_retrieve_hour_progress_roundtrip(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_progress(db_path, 2024, 1, 15, 14, 400.0, 100.0, 20.0, 350.0, 3, 60.0, 15.0)

    assert retrieve_hour_progress(db_path, 2024, 1, 15, 14) == (
        400.0,
        100.0,
        20.0,
        350.0,
        3,
        60.0,
        15.0,
    )


def test_store_hour_progress_overwrites_same_key(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_progress(db_path, 2024, 1, 15, 14, 400.0, 100.0, 20.0, 350.0, 3, 60.0, 15.0)
    store_hour_progress(db_path, 2024, 1, 15, 14, 420.0, 110.0, 25.0, 360.0, 4, 65.0, 18.0)

    assert retrieve_hour_progress(db_path, 2024, 1, 15, 14) == (
        420.0,
        110.0,
        25.0,
        360.0,
        4,
        65.0,
        18.0,
    )


def test_retrieve_hour_progress_none_when_nothing_stored(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_hour_progress(db_path, 2024, 1, 15, 14) is None


def test_delete_hour_progress_removes_the_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_progress(db_path, 2024, 1, 15, 14, 400.0, 100.0, 20.0, 350.0, 3, 60.0, 15.0)

    delete_hour_progress(db_path, 2024, 1, 15, 14)

    assert retrieve_hour_progress(db_path, 2024, 1, 15, 14) is None


def test_delete_hour_progress_noop_on_fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    delete_hour_progress(db_path, 2024, 1, 15, 14)  # must not raise


# --- schema migration (solar_to_battery/grid_to_battery added after the fact) --


def test_ensure_schema_migrates_pre_existing_db_without_new_columns(tmp_path):
    """A DB file created before solar_to_battery/grid_to_battery existed
    must gain the new columns via ALTER TABLE, not silently keep the old
    schema (which `CREATE TABLE IF NOT EXISTS` alone would do)."""
    db_path = str(tmp_path / "test.db")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE hour_data (
                year INTEGER, mon INTEGER, day INTEGER, hour INTEGER,
                house_load REAL, solar_power_roof REAL,
                estimated_solar_power_raw REAL, extra_pv_power REAL,
                feed_in REAL, price REAL, use_fee REAL, return_fee REAL,
                PRIMARY KEY (year, mon, day, hour)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE hour_progress (
                year INTEGER, mon INTEGER, day INTEGER, hour INTEGER,
                house_load REAL, solar_power_roof REAL, extra_pv_power REAL,
                total_active_power REAL, sample_count INTEGER,
                PRIMARY KEY (year, mon, day, hour)
            )
            """
        )
        conn.execute(
            "INSERT INTO hour_data (year, mon, day, hour, house_load, solar_power_roof, "
            "extra_pv_power) VALUES (2024, 1, 15, 8, 400, 100, 20)"
        )
        conn.commit()

    # retrieve_day_hours calls _ensure_schema internally, migrating the file.
    result = retrieve_day_hours(db_path, 2024, 1, 15)
    assert result == {8: (400, 100, 20, 0.0, 0.0)}

    store_hour_progress(db_path, 2024, 1, 15, 9, 300.0, 90.0, 10.0, 250.0, 2, 40.0, 5.0)
    assert retrieve_hour_progress(db_path, 2024, 1, 15, 9) == (
        300.0,
        90.0,
        10.0,
        250.0,
        2,
        40.0,
        5.0,
    )


# --- dispatch_daily_state -------------------------------------------------------


def test_store_and_retrieve_dispatch_daily_state_roundtrip(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_dispatch_daily_state(db_path, 2024, 1, 15, True, False, 6, -1)

    assert retrieve_dispatch_daily_state(db_path, 2024, 1, 15) == (True, False, 6, -1)


def test_store_dispatch_daily_state_overwrites_same_key(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_dispatch_daily_state(db_path, 2024, 1, 15, True, False, 6, -1)
    store_dispatch_daily_state(db_path, 2024, 1, 15, True, True, 6, 21)

    assert retrieve_dispatch_daily_state(db_path, 2024, 1, 15) == (True, True, 6, 21)


def test_retrieve_dispatch_daily_state_none_when_nothing_stored(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_dispatch_daily_state(db_path, 2024, 1, 15) is None


def test_dispatch_daily_state_excludes_other_dates(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_dispatch_daily_state(db_path, 2024, 1, 15, True, False, 6, -1)

    assert retrieve_dispatch_daily_state(db_path, 2024, 1, 16) is None


# --- pure helper functions --------------------------------------------------


def test_recency_weight_is_one_at_zero_days():
    now = datetime(2024, 1, 15, 12)
    assert _recency_weight(now, now, LOAD_TAU_DAYS) == 1.0


def test_recency_weight_decays_with_age():
    now = datetime(2024, 1, 15, 12)
    from datetime import timedelta

    recent = now - timedelta(days=7)
    old = now - timedelta(days=60)
    assert _recency_weight(now, recent, LOAD_TAU_DAYS) > _recency_weight(now, old, LOAD_TAU_DAYS)


def test_recency_weight_floors_at_zero_for_future_samples():
    now = datetime(2024, 1, 15, 12)
    from datetime import timedelta

    future = now + timedelta(days=5)
    assert _recency_weight(now, future, LOAD_TAU_DAYS) == 1.0


def test_day_class_weekday_vs_weekend():
    # C convention: 0=Sun, 1=Mon, ..., 6=Sat
    assert _day_class(0) == 1  # Sunday -> weekend
    assert _day_class(6) == 1  # Saturday -> weekend
    for c_wday in (1, 2, 3, 4, 5):
        assert _day_class(c_wday) == 0  # Mon-Fri -> weekday


def test_c_weekday_matches_c_convention():
    # 2024-01-01 is a Monday -> C tm_wday = 1
    assert c_weekday(datetime(2024, 1, 1).date()) == 1
    # 2024-01-07 is a Sunday -> C tm_wday = 0
    assert c_weekday(datetime(2024, 1, 7).date()) == 0
    # 2024-01-06 is a Saturday -> C tm_wday = 6
    assert c_weekday(datetime(2024, 1, 6).date()) == 6


def test_is_factor_reliable_requires_enough_samples():
    assert _is_factor_reliable(MIN_SAMPLES - 1, 1_000_000, 1.0, 1000) is False
    assert _is_factor_reliable(MIN_SAMPLES, 1_000_000, 1.0, 1000) is True


def test_is_factor_reliable_requires_enough_energy_ratio():
    assert _is_factor_reliable(10, 1.0, 1.0, 1000) is False  # tiny signal energy


def test_is_factor_reliable_requires_factor_in_bounds():
    assert _is_factor_reliable(10, 1_000_000, 0.1, 1000) is False
    assert _is_factor_reliable(10, 1_000_000, 2.6, 1000) is False
    assert _is_factor_reliable(10, 1_000_000, 2.5, 1000) is True


def test_is_factor_reliable_guards_zero_pv_power():
    assert _is_factor_reliable(10, 1_000_000, 1.0, 0) is False


# --- calculate_and_store_mean_data / retrieve_mean_data --------------------


def test_calculate_and_store_mean_data_returns_false_on_empty_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert calculate_and_store_mean_data(db_path, pv_power=5000) is False


def test_retrieve_mean_data_returns_none_when_nothing_computed(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_mean_data(db_path, month=1, wday=1) is None


def test_retrieve_mean_data_returns_none_for_out_of_range_args(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_mean_data(db_path, month=0, wday=1) is None
    assert retrieve_mean_data(db_path, month=13, wday=1) is None
    assert retrieve_mean_data(db_path, month=1, wday=-1) is None
    assert retrieve_mean_data(db_path, month=1, wday=7) is None


def _store(db_path, year, mon, day, hour, house_load, solar_roof=0, solar_raw=0.0):
    the_day = Day(year=year, mon=mon, day=day, valid=True)
    the_day.hour[hour].valid = True
    the_day.hour[hour].real_house_load = house_load
    the_day.hour[hour].real_solar_power_roof = solar_roof
    the_day.hour[hour].estimated_solar_power_raw = solar_raw
    store_hour_data(db_path, the_day, hour, use_fee=0.02, return_fee=0.01)


def test_weighted_mean_favors_recent_samples(tmp_path, freezer):
    # 2024-01-29 is a Monday
    freezer.move_to("2024-01-29 12:00:00")
    db_path = str(tmp_path / "test.db")

    # Both land in the same (month=1, Monday, hour=10) cell.
    _store(db_path, 2024, 1, 22, 10, house_load=1000)  # 7 days ago
    _store(db_path, 2024, 1, 1, 10, house_load=100)  # 28 days ago

    assert calculate_and_store_mean_data(db_path, pv_power=5000) is True

    result = retrieve_mean_data(db_path, month=1, wday=1)
    assert result is not None
    # Weighted toward the more recent (higher) sample, well above the plain
    # average of 550.
    assert 650 < result[10].house_load < 950


def test_fallback_fills_missing_weekday_same_day_class(tmp_path, freezer):
    freezer.move_to("2024-01-29 12:00:00")
    db_path = str(tmp_path / "test.db")

    # 2024-01-02 is a Tuesday (C wday=2, a weekday). Only Tuesday gets data.
    _store(db_path, 2024, 1, 2, 14, house_load=800)

    assert calculate_and_store_mean_data(db_path, pv_power=5000) is True

    tuesday = retrieve_mean_data(db_path, month=1, wday=2)
    # 2024-01-03 is a Wednesday (C wday=3, also a weekday, same day-class) —
    # falls back to Tuesday's observed mean since it has no data of its own.
    wednesday = retrieve_mean_data(db_path, month=1, wday=3)

    assert tuesday is not None
    assert wednesday is not None
    assert wednesday[14].house_load == tuesday[14].house_load


def test_fallback_fills_missing_month_from_baseline(tmp_path, freezer):
    freezer.move_to("2024-01-29 12:00:00")
    db_path = str(tmp_path / "test.db")

    # Only January (month=1) has any data at all -> it's the baseline month.
    _store(db_path, 2024, 1, 1, 10, house_load=500)  # Monday

    assert calculate_and_store_mean_data(db_path, pv_power=5000) is True

    january = retrieve_mean_data(db_path, month=1, wday=1)
    # June has zero data of its own -> Step 3 copies the baseline month's
    # same-weekday cell wholesale.
    june = retrieve_mean_data(db_path, month=6, wday=1)

    assert january is not None
    assert june is not None
    assert june[10].house_load == january[10].house_load


def test_solar_regression_unreliable_with_too_few_samples(tmp_path, freezer):
    freezer.move_to("2024-05-15 12:00:00")
    db_path = str(tmp_path / "test.db")

    _store(db_path, 2024, 1, 10, 14, house_load=500, solar_roof=860, solar_raw=700)

    assert calculate_and_store_mean_data(db_path, pv_power=1000) is True

    result = retrieve_mean_data(db_path, month=1, wday=c_weekday_for(2024, 1, 10))
    assert result is not None
    assert result[14].solar_factor == -1.0


def test_solar_regression_becomes_reliable_with_enough_samples(tmp_path, freezer):
    freezer.move_to("2024-05-15 12:00:00")
    db_path = str(tmp_path / "test.db")

    # y = 1.2*x + 20, sampled at 5 different raw-forecast values, same hour,
    # spaced close enough to "now" that none hit the recency-weight floor
    # (otherwise weighted_count falls below MIN_SAMPLES).
    samples = [
        (2024, 5, 10, 700),
        (2024, 5, 11, 750),
        (2024, 5, 12, 800),
        (2024, 5, 13, 850),
        (2024, 5, 14, 900),
    ]
    for year, mon, day, x in samples:
        y = 1.2 * x + 20
        _store(db_path, year, mon, day, 14, house_load=500, solar_roof=y, solar_raw=x)

    assert calculate_and_store_mean_data(db_path, pv_power=1000) is True

    last_year, last_mon, last_day, _ = samples[-1]
    result = retrieve_mean_data(
        db_path, month=last_mon, wday=c_weekday_for(last_year, last_mon, last_day)
    )
    assert result is not None
    assert result[14].solar_factor != -1.0
    assert 0.2 <= result[14].solar_factor <= 2.5


def c_weekday_for(year, mon, day) -> int:
    return c_weekday(datetime(year, mon, day).date())
