"""Tests for storage.py.

storage.py is standalone (no `hass` dependency), so most of these are plain
function tests against a throwaway SQLite file (`tmp_path`). Recency-weighted
tests use the `freezer` fixture (pytest-freezer/freezegun) to pin "now" so
the weighting math is deterministic.
"""

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta

import pytest

from custom_components.alpha_ess_local.data import Day, Earning, FiveMin
from custom_components.alpha_ess_local.storage import (
    LOAD_TAU_DAYS,
    MIN_SAMPLES,
    MIN_SPREAD_SAMPLES,
    ForecastRow,
    _day_class,
    _ensure_schema,
    _is_factor_reliable,
    _recency_weight,
    c_weekday,
    calculate_and_store_mean_data,
    delete_hour_progress,
    lead_bucket,
    learn_forecast_spread,
    retrieve_day_actions,
    retrieve_day_decisions,
    retrieve_day_five_min,
    retrieve_day_forecast,
    retrieve_day_hours,
    retrieve_day_savings,
    retrieve_dispatch_daily_state,
    retrieve_forecast_spread,
    retrieve_hour_progress,
    retrieve_mean_data,
    retrieve_period_summary,
    store_dispatch_daily_state,
    store_five_min_sample,
    store_forecasts,
    store_hour_action,
    store_hour_data,
    store_hour_dispatch,
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

    assert (
        store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21) is True
    )

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT house_load, solar_power_roof, estimated_solar_power_raw, use_fee, return_fee, "
            "solar_to_battery, grid_to_battery, vat_percentage "
            "FROM hour_data WHERE year=2024 AND mon=1 AND day=15 AND hour=10"
        ).fetchone()

    assert row == (500, 300, 250.0, 0.02, 0.01, 120, 30, 21)


def test_store_hour_data_skips_zero_house_load(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.hour[10].valid = True
    day.hour[10].real_house_load = 0

    # Faithful to the original: houseLoad == 0 skips the DB entirely, so the
    # file is never even created.
    assert (
        store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21) is True
    )
    assert not (tmp_path / "test.db").exists()


def test_store_hour_data_returns_false_when_day_invalid(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.valid = False
    day.hour[10].valid = True
    day.hour[10].real_house_load = 500

    assert (
        store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21) is False
    )


def test_store_hour_data_returns_false_when_hour_invalid(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.hour[10].valid = False

    assert (
        store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21) is False
    )


def test_store_hour_data_overwrites_same_key(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day()
    day.hour[10].valid = True
    day.hour[10].real_house_load = 500
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    day.hour[10].real_house_load = 700
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

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

    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

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
        store_hour_data(db_path, day, hour_index, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    result = retrieve_day_hours(db_path, 2024, 1, 15)

    assert result == {8: (400, 100, 20, 50, 10), 9: (350, 100, 20, 50, 10)}


def test_retrieve_day_hours_excludes_other_dates(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    day.hour[8].valid = True
    day.hour[8].real_house_load = 400
    store_hour_data(db_path, day, 8, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    assert retrieve_day_hours(db_path, 2024, 1, 16) == {}


def test_retrieve_day_hours_empty_on_fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_day_hours(db_path, 2024, 1, 15) == {}


# --- retrieve_day_savings ------------------------------------------------------


def test_retrieve_day_savings_computes_solar_and_battery_savings(tmp_path):
    # house_load=1000 W, solar_power_roof=600 W, actual net grid exchange
    # (feed_in/total_active_power) = -200 W (net export, after whatever the
    # battery did). price=0.20, use_fee=0.02, return_fee=0.01, vat=21%:
    #   price_use = 0.20*1.21 + 0.02 = 0.262 EUR/kWh
    #   price_return = 0.20*1.21 + 0.01 = 0.252 EUR/kWh
    #   cost_no_solar   = 1000 W import  -> 1000 * 0.262 / 1000 = 0.2620
    #   cost_solar_only = 400 W import   ->  400 * 0.262 / 1000 = 0.1048
    #   cost_actual     = 200 W *export* -> -200 * 0.252 / 1000 = -0.0504
    #   savings_solar   = 0.2620 - 0.1048 = 0.1572
    #   savings_battery = 0.1048 - (-0.0504) = 0.1552
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 1000
    hour.real_solar_power_roof = 600
    hour.total_active_power = -200
    hour.price = 0.20
    hour.real_battery_charge_energy = 700
    hour.real_battery_discharge_energy = 100
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    result = retrieve_day_savings(db_path, 2024, 1, 15, default_vat_percentage=21)

    assert len(result) == 24  # zero-filled for every other hour of the day
    assert result[10] == {
        "hour": 10,
        "solar_wh": 600,
        "solar_wh_roof": 600,
        "solar_wh_extra": 0,
        "battery_charge_wh": 700,
        "battery_discharge_wh": 100,
        "savings_solar_eur": pytest.approx(0.1572, abs=1e-4),
        "savings_battery_eur": pytest.approx(0.1552, abs=1e-4),
    }
    assert result[0] == {
        "hour": 0,
        "solar_wh": 0,
        "solar_wh_roof": 0,
        "solar_wh_extra": 0,
        "battery_charge_wh": 0,
        "battery_discharge_wh": 0,
        "savings_solar_eur": 0.0,
        "savings_battery_eur": 0.0,
    }


def test_retrieve_day_savings_zero_fills_hours_missing_from_storage(tmp_path):
    # A devcontainer/host suspend (or any multi-hour gap in polling) can
    # leave some hours with no stored row at all. The dashboard table built
    # from this list expects a contiguous 0-23 hour column, so missing hours
    # must come back zeroed rather than simply being absent from the list.
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    hour = day.hour[8]
    hour.valid = True
    hour.real_house_load = 1000
    hour.real_solar_power_roof = 600
    hour.total_active_power = -200
    hour.price = 0.20
    store_hour_data(db_path, day, 8, use_fee=0.02, return_fee=0.01, vat_percentage=21)
    # Hours 9, 10, 11 never got stored (simulating the frozen gap).
    hour2 = day.hour[12]
    hour2.valid = True
    hour2.real_house_load = 500
    hour2.total_active_power = 500
    hour2.price = 0.20
    store_hour_data(db_path, day, 12, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    result = retrieve_day_savings(db_path, 2024, 1, 15, default_vat_percentage=21)

    assert [r["hour"] for r in result] == list(range(24))
    for gap_hour in (9, 10, 11):
        assert result[gap_hour] == {
            "hour": gap_hour,
            "solar_wh": 0,
            "solar_wh_roof": 0,
            "solar_wh_extra": 0,
            "battery_charge_wh": 0,
            "battery_discharge_wh": 0,
            "savings_solar_eur": 0.0,
            "savings_battery_eur": 0.0,
        }


def test_retrieve_day_savings_return_vat_percentage_overrides_export_side(tmp_path):
    # Same scenario as test_retrieve_day_savings_computes_solar_and_battery_savings
    # (house_load=1000, solar_power_roof=600, feed_in=-200 export), but with
    # return_vat_percentage=0 (VAT-exempt export/teruglevering):
    #   price_return' = mk_return_price(0.20, 0.01, 0) = 0.21 EUR/kWh
    #   cost_actual = -200 * 0.21 / 1000 = -0.042 (vs -0.0504 with VAT)
    #   savings_battery = cost_solar_only - cost_actual = 0.1048 - (-0.042) = 0.1468
    # savings_solar is unaffected -- both cost_no_solar/cost_solar_only are
    # imports (positive power), so only use-side pricing applies there.
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 1000
    hour.real_solar_power_roof = 600
    hour.total_active_power = -200
    hour.price = 0.20
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    result = retrieve_day_savings(
        db_path, 2024, 1, 15, default_vat_percentage=21, return_vat_percentage=0
    )

    assert result[10] == {
        "hour": 10,
        "solar_wh": 600,
        "solar_wh_roof": 600,
        "solar_wh_extra": 0,
        "battery_charge_wh": 0,
        "battery_discharge_wh": 0,
        "savings_solar_eur": pytest.approx(0.1572, abs=1e-4),
        "savings_battery_eur": pytest.approx(0.1468, abs=1e-4),
    }


def test_retrieve_day_savings_includes_extra_pv_power_in_solar_total(tmp_path):
    # Same scenario as test_retrieve_day_savings_computes_solar_and_battery_savings,
    # but the 600 W is split across the AlphaESS's own roof panels (250 W)
    # and a separate second installation (350 W, e.g. an SMA Tripower) --
    # solar_wh and savings_solar_eur must count *both*, not just roof.
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 1000
    hour.real_solar_power_roof = 250
    hour.real_extra_pv_power = 350
    hour.total_active_power = -200
    hour.price = 0.20
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    result = retrieve_day_savings(db_path, 2024, 1, 15, default_vat_percentage=21)

    assert result[10] == {
        "hour": 10,
        "solar_wh": 600,  # 250 + 350
        "solar_wh_roof": 250,
        "solar_wh_extra": 350,
        "battery_charge_wh": 0,
        "battery_discharge_wh": 0,
        "savings_solar_eur": pytest.approx(0.1572, abs=1e-4),
        "savings_battery_eur": pytest.approx(0.1552, abs=1e-4),
    }


def test_retrieve_day_savings_can_be_negative_when_battery_loses_money(tmp_path):
    # No solar at all; battery (somehow) made the actual grid cost *worse*
    # than just buying house_load directly (e.g. inefficiency, or charged
    # expensive and never got to use it) -- savings_battery_eur should
    # reflect that honestly as a negative number, not floor at 0.
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 1000
    hour.real_solar_power_roof = 0
    hour.total_active_power = 1500  # imported *more* than house_load alone would need
    hour.price = 0.20
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    result = retrieve_day_savings(db_path, 2024, 1, 15, default_vat_percentage=21)

    assert result[10]["savings_solar_eur"] == 0.0
    assert result[10]["savings_battery_eur"] < 0.0


def test_retrieve_day_savings_falls_back_to_default_vat_for_rows_missing_it(tmp_path):
    # Simulates a row stored before the vat_percentage column existed:
    # written directly (bypassing store_hour_data), vat_percentage left
    # NULL -- retrieve_day_savings must fall back to the given default
    # rather than treating a NULL/0% VAT as real.
    db_path = str(tmp_path / "test.db")
    day = _valid_day(year=2024, mon=1, day=15)
    hour = day.hour[10]
    hour.valid = True
    hour.real_house_load = 1000
    hour.real_solar_power_roof = 600
    hour.total_active_power = -200
    hour.price = 0.20
    store_hour_data(db_path, day, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE hour_data SET vat_percentage = NULL WHERE hour = 10")
        conn.commit()

    result = retrieve_day_savings(db_path, 2024, 1, 15, default_vat_percentage=21)

    # Same numbers as test_retrieve_day_savings_computes_solar_and_battery_savings,
    # proving the 21% default was actually applied, not treated as 0%.
    assert result[10]["savings_solar_eur"] == pytest.approx(0.1572, abs=1e-4)
    assert result[10]["savings_battery_eur"] == pytest.approx(0.1552, abs=1e-4)


def test_retrieve_day_savings_empty_on_fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    assert retrieve_day_savings(db_path, 2024, 1, 15, default_vat_percentage=21) == []


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
        None,
        None,
        None,
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
        None,
        None,
        None,
    )


def test_store_and_retrieve_hour_progress_includes_pv_total_energy_baseline(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_progress(
        db_path,
        2024,
        1,
        15,
        14,
        400.0,
        100.0,
        20.0,
        350.0,
        3,
        60.0,
        15.0,
        pv_total_energy_at_hour_start=42.5,
    )

    result = retrieve_hour_progress(db_path, 2024, 1, 15, 14)

    assert result[7] == 42.5


def test_store_and_retrieve_hour_progress_includes_battery_energy_baselines(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_progress(
        db_path,
        2024,
        1,
        15,
        14,
        400.0,
        100.0,
        20.0,
        350.0,
        3,
        60.0,
        15.0,
        battery_charge_energy_at_hour_start=12.5,
        battery_discharge_energy_at_hour_start=3.5,
    )

    result = retrieve_hour_progress(db_path, 2024, 1, 15, 14)

    assert result[8] == 12.5
    assert result[9] == 3.5


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
        None,
        None,
        None,
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


def test_hour_actions_latest_wins_per_hour_and_stay_per_day(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_action(db_path, 2026, 10, 9, 4, 0)
    store_hour_action(db_path, 2026, 10, 9, 4, 3)  # replanned mid-hour
    store_hour_action(db_path, 2026, 10, 9, 5, 2)
    store_hour_action(db_path, 2026, 10, 8, 4, 1)

    assert retrieve_day_actions(db_path, 2026, 10, 9) == {4: 3, 5: 2}
    assert retrieve_day_actions(db_path, 2026, 10, 10) == {}


def test_decision_log_combines_planner_and_dispatch(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_dispatch(
        db_path, 2026, 10, 9, 4, mode="Normal", power=0, power_max=0, written=True, manual=False
    )  # dispatch can come before the planner's record for the hour
    store_hour_action(
        db_path,
        2026,
        10,
        9,
        4,
        3,
        selection="optimum",
        opt_extra=0.62,
        min_profit=0.4,
        price=0.074,
        cutoff_soc=350,
    )
    store_hour_dispatch(
        db_path,
        2026,
        10,
        9,
        4,
        mode="Normal → State of Charge control",
        power=5077,
        power_max=10484,
        written=True,
        manual=False,
    )

    (row,) = retrieve_day_decisions(db_path, 2026, 10, 9)

    assert row["hour"] == 4
    assert row["charge"] == 3
    assert (row["selection"], row["opt_extra"], row["min_profit"]) == ("optimum", 0.62, 0.4)
    assert (row["price"], row["cutoff_soc"]) == (0.074, 350)
    assert row["mode"] == "Normal → State of Charge control"
    assert (row["power"], row["power_max"], row["written"], row["manual"]) == (5077, 10484, 1, 0)


def test_dispatch_only_hour_has_no_action(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_hour_dispatch(
        db_path, 2026, 10, 9, 5, mode="Normal", power=0, power_max=0, written=False, manual=False
    )

    assert retrieve_day_actions(db_path, 2026, 10, 9) == {}
    assert retrieve_day_decisions(db_path, 2026, 10, 9)[0]["charge"] is None


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
    store_hour_data(db_path, the_day, hour, use_fee=0.02, return_fee=0.01, vat_percentage=21)


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


def test_learned_house_load_leaves_out_the_ev_charger(tmp_path, freezer):
    """The EV charger only charges on solar surplus, so it's no load the
    battery planning has to cover: it's taken out before learning."""
    freezer.move_to("2024-01-29 12:00:00")
    db_path = str(tmp_path / "test.db")
    with_ev = Day(year=2024, mon=1, day=22, valid=True)  # a Monday
    with_ev.hour[10].valid = True
    with_ev.hour[10].real_house_load = 4000  # 3200 W of it the EV charger
    with_ev.hour[10].real_ev_load = 3200
    store_hour_data(db_path, with_ev, 10, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    assert calculate_and_store_mean_data(db_path, pv_power=5000) is True

    result = retrieve_mean_data(db_path, month=1, wday=1)
    assert result[10].house_load == pytest.approx(800, abs=1)


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


def test_solar_regression_learns_against_roof_plus_extra_pv(tmp_path, freezer):
    """Regression: the forecast covers all panels, but the correction was
    learned against the roof alone -- teaching it to shrink every forecast
    to the roof's share (here ~0.5)."""
    freezer.move_to("2024-05-15 12:00:00")
    db_path = str(tmp_path / "test.db")
    for day, raw in ((10, 1400), (11, 1500), (12, 1600), (13, 1700), (14, 1800)):
        the_day = Day(year=2024, mon=5, day=day, valid=True)
        hour = the_day.hour[14]
        hour.valid = True
        hour.real_house_load = 500
        hour.estimated_solar_power_raw = raw
        hour.real_solar_power_roof = raw / 2  # half on the roof ...
        hour.real_extra_pv_power = raw / 2  # ... half on the garage
        store_hour_data(db_path, the_day, 14, use_fee=0.02, return_fee=0.01, vat_percentage=21)

    assert calculate_and_store_mean_data(db_path, pv_power=2000) is True

    result = retrieve_mean_data(db_path, month=5, wday=c_weekday_for(2024, 5, 14))
    assert result[14].solar_factor == pytest.approx(1.0, abs=0.05)


def test_day_forecast_is_the_first_one_made_that_day(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_forecasts(
        db_path,
        [
            # made the evening before (lead > hour): not this day's own
            ForecastRow(2026, 10, 9, 12, 15, 4000, 3900, 800),
            # made at 00:00 (lead == hour) and at 06:00 (lead 6)
            ForecastRow(2026, 10, 9, 12, 12, 3000, 2900, 800),
            ForecastRow(2026, 10, 9, 12, 6, 2500, 2450, 750),
            ForecastRow(2026, 10, 9, 3, 3, 0, 0, 900),
        ],
    )

    result = retrieve_day_forecast(db_path, 2026, 10, 9)

    assert result[12] == {"solar": 2900, "solar_raw": 3000, "house_load": 800, "lead": 12}
    assert result[3]["house_load"] == 900


def test_store_forecasts_prunes_old_rows(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_forecasts(db_path, [ForecastRow(2026, 1, 1, 12, 2, 1000, 1000, 500)])
    store_forecasts(db_path, [ForecastRow(2026, 10, 9, 12, 2, 1000, 1000, 500)])

    assert retrieve_day_forecast(db_path, 2026, 1, 1) == {}
    assert retrieve_day_forecast(db_path, 2026, 10, 9) != {}


def _spread_history(db_path, ratios, lead=8, forecast=2000.0, price=0.10):
    """One noon hour per day: forecast `forecast` Wh made `lead` hours ahead,
    real solar forecast * ratio (half roof, half extra PV)."""
    for i, ratio in enumerate(ratios):
        day = date(2026, 9, 1) + timedelta(days=i)
        the_day = Day(year=day.year, mon=day.month, day=day.day, valid=True)
        hour = the_day.hour[12]
        hour.valid = True
        hour.price = price
        hour.real_house_load = 500
        hour.real_solar_power_roof = forecast * ratio / 2
        hour.real_extra_pv_power = forecast * ratio / 2
        store_hour_data(db_path, the_day, 12, use_fee=0.02, return_fee=0.01, vat_percentage=21)
        store_forecasts(
            db_path,
            [ForecastRow(day.year, day.month, day.day, 12, lead, forecast, forecast, 500)],
        )


def test_learn_forecast_spread_per_lead_time(tmp_path, freezer):
    freezer.move_to("2026-10-01 12:00:00")
    db_path = str(tmp_path / "test.db")
    # 0.50 .. 1.50 around a median of 1.0, in pairs on consecutive days so
    # the recency weighting doesn't tilt them (recent days weigh more)
    ratios = [1.0] + [r for k in range(1, 11) for r in (1.0 - 0.05 * k, 1.0 + 0.05 * k)]
    _spread_history(db_path, ratios, lead=8)

    assert learn_forecast_spread(db_path, pv_power=10000, percentiles=[0.1, 0.5, 0.9]) is True

    spread = retrieve_forecast_spread(db_path)
    low, mid, high = spread[lead_bucket(8)]
    assert low < 0.75
    assert mid == pytest.approx(1.0, abs=0.08)
    assert high > 1.25
    assert lead_bucket(0) not in spread  # nothing logged that close


def test_learn_forecast_spread_needs_enough_hours(tmp_path, freezer):
    freezer.move_to("2026-10-01 12:00:00")
    db_path = str(tmp_path / "test.db")
    _spread_history(db_path, [1.0] * (MIN_SPREAD_SAMPLES - 1))

    assert learn_forecast_spread(db_path, pv_power=10000, percentiles=[0.5]) is False
    assert retrieve_forecast_spread(db_path) == {}


def test_learn_forecast_spread_skips_negative_price_hours(tmp_path, freezer):
    """Panels may be switched off at a negative price: real 0 isn't a miss."""
    freezer.move_to("2026-10-01 12:00:00")
    db_path = str(tmp_path / "test.db")
    _spread_history(db_path, [0.0] * 20, price=-0.05)

    assert learn_forecast_spread(db_path, pv_power=10000, percentiles=[0.5]) is False


def c_weekday_for(year, mon, day) -> int:
    return c_weekday(datetime(year, mon, day).date())


# --- 5-minute samples -----------------------------------------------------------


def test_five_min_samples_round_trip(tmp_path):
    db_path = str(tmp_path / "test.db")
    sample = FiveMin(
        real_solar_power_roof=1200,
        real_extra_pv_power=300,
        total_active_power=-400,
        battery_power=-900,
        real_house_load=200,
    )
    store_five_min_sample(db_path, 2026, 10, 7, 10, 3, sample)
    store_five_min_sample(db_path, 2026, 10, 7, 11, 0, FiveMin(real_house_load=500))

    samples = retrieve_day_five_min(db_path, 2026, 10, 7)

    assert set(samples) == {10, 11}
    restored = samples[10][3]
    assert restored.real_solar_power_roof == 1200
    assert restored.real_extra_pv_power == 300
    assert restored.total_active_power == -400
    assert restored.battery_power == -900
    assert restored.real_house_load == 200


def test_five_min_samples_only_for_the_requested_day(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_five_min_sample(db_path, 2026, 10, 6, 10, 0, FiveMin(real_house_load=1))
    store_five_min_sample(db_path, 2026, 10, 7, 10, 0, FiveMin(real_house_load=2))

    assert retrieve_day_five_min(db_path, 2026, 10, 7)[10][0].real_house_load == 2


def test_five_min_samples_older_than_three_days_are_pruned(tmp_path):
    db_path = str(tmp_path / "test.db")
    store_five_min_sample(db_path, 2026, 10, 1, 10, 0, FiveMin(real_house_load=1))
    store_five_min_sample(db_path, 2026, 10, 4, 10, 0, FiveMin(real_house_load=1))

    store_five_min_sample(db_path, 2026, 10, 7, 10, 0, FiveMin(real_house_load=1))

    assert retrieve_day_five_min(db_path, 2026, 10, 1) == {}
    assert retrieve_day_five_min(db_path, 2026, 10, 4) != {}


# --- period summaries (history tab) -------------------------------------------


def _insert_hour(
    db_path, day, hour, *, house=500, roof=0, extra=0, feed_in=500, price=0.1, charge=0, discharge=0
):
    with closing(sqlite3.connect(db_path)) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO hour_data (year, mon, day, hour, house_load, solar_power_roof,
                extra_pv_power, feed_in, price, use_fee, return_fee, vat_percentage,
                battery_charge_energy, battery_discharge_energy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.02, 0.02, 6, ?, ?)
            """,
            (
                day.year,
                day.month,
                day.day,
                hour,
                house,
                roof,
                extra,
                feed_in,
                price,
                charge,
                discharge,
            ),
        )
        conn.commit()


def test_period_summary_by_hour_has_all_24_hours_and_totals(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = date(2026, 10, 6)
    _insert_hour(db_path, day, 12, house=400, roof=3000, extra=1000, feed_in=-2600, charge=1000)
    _insert_hour(db_path, day, 20, house=800, feed_in=0, discharge=800)

    summary = retrieve_period_summary(db_path, day, day, "hour", 6.0)

    assert summary["group"] == "hour"
    assert [r["key"] for r in summary["rows"]] == [f"{h:02d}" for h in range(24)]
    noon = summary["rows"][12]
    assert (noon["solar_wh_roof"], noon["solar_wh_extra"], noon["solar_wh"]) == (3000, 1000, 4000)
    assert (noon["import_wh"], noon["export_wh"]) == (0, 2600)
    assert summary["rows"][3]["hours"] == 0  # nothing stored: shown as a gap
    totals = summary["totals"]
    assert totals["hours"] == 2
    assert totals["house_wh"] == 1200
    assert totals["battery_charge_wh"] == 1000
    assert totals["battery_discharge_wh"] == 800


def test_period_summary_by_day_includes_empty_days(tmp_path):
    db_path = str(tmp_path / "test.db")
    _insert_hour(db_path, date(2026, 10, 5), 10, house=500)
    _insert_hour(db_path, date(2026, 10, 5), 11, house=700)
    _insert_hour(db_path, date(2026, 10, 7), 10, house=300)

    summary = retrieve_period_summary(db_path, date(2026, 10, 5), date(2026, 10, 7), "day", 6.0)

    assert [(r["key"], r["hours"], r["house_wh"]) for r in summary["rows"]] == [
        ("2026-10-05", 2, 1200),
        ("2026-10-06", 0, 0),
        ("2026-10-07", 1, 300),
    ]


def test_period_summary_by_month_over_a_year(tmp_path):
    db_path = str(tmp_path / "test.db")
    _insert_hour(db_path, date(2026, 3, 10), 10, house=1000)
    _insert_hour(db_path, date(2026, 10, 1), 10, house=2000)

    summary = retrieve_period_summary(db_path, date(2026, 1, 1), date(2026, 12, 31), "month", 6.0)

    assert len(summary["rows"]) == 12
    assert summary["rows"][2] == summary["rows"][2] | {"key": "2026-03", "house_wh": 1000}
    assert summary["rows"][9]["house_wh"] == 2000
    assert summary["totals"]["house_wh"] == 3000


def test_period_summary_savings_match_the_day_savings(tmp_path):
    db_path = str(tmp_path / "test.db")
    day = date(2026, 10, 6)
    _insert_hour(db_path, day, 12, house=400, roof=3000, extra=1000, feed_in=-2600, charge=1000)

    summary = retrieve_period_summary(db_path, day, day, "hour", 6.0)
    savings = retrieve_day_savings(db_path, 2026, 10, 6, 6.0)

    noon = summary["rows"][12]
    assert noon["savings_solar_eur"] == pytest.approx(savings[12]["savings_solar_eur"], abs=0.01)
    assert noon["savings_battery_eur"] == pytest.approx(
        savings[12]["savings_battery_eur"], abs=0.01
    )


def test_period_summary_rejects_unknown_group(tmp_path):
    with pytest.raises(ValueError):
        retrieve_period_summary(
            str(tmp_path / "t.db"), date(2026, 1, 1), date(2026, 1, 1), "week", 6.0
        )
