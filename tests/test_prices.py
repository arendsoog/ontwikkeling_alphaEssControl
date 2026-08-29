"""Tests for prices.py.

The ENTSO-e/Frank Energie attribute shapes used here are based on reading
those integrations' source (see the plan doc), not a live example — if real
entities don't match, these tests (and the parser) will need updating.
"""

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.alpha_ess_local.data import Day, Earning
from custom_components.alpha_ess_local.prices import (
    _parse_entsoe_prices,
    _parse_frank_energie_prices,
    apply_earning_classification,
    build_day,
    mk_return_price,
    mk_use_price,
    read_hour_prices,
)

ENTSOE_ENTITY_ID = "sensor.entsoe_average_electricity_price_today"
FRANK_ENERGIE_ENTITY_ID = "sensor.frank_energie_current_electricity_price"


def _local_iso(hour: int, *, on_date=None) -> str:
    """Build a local ISO datetime string for `hour` today (or `on_date`)."""
    base = dt_util.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    if on_date is not None:
        base = base.replace(year=on_date.year, month=on_date.month, day=on_date.day)
    return base.isoformat()


def test_mk_use_price_adds_fee_and_vat():
    assert mk_use_price(0.10, use_fee=0.02, vat_percentage=21) == 0.10 * 1.21 + 0.02


def test_mk_return_price_adds_fee_and_vat():
    assert mk_return_price(0.10, return_fee=0.02, vat_percentage=21) == 0.10 * 1.21 + 0.02


def test_parse_entsoe_prices_reads_time_and_price(hass: HomeAssistant):
    state = State(
        ENTSOE_ENTITY_ID,
        "0.15",
        {
            "unit_of_measurement": "EUR/kWh",
            "prices_today": [
                {"time": _local_iso(0), "price": 0.10},
                {"time": _local_iso(13), "price": 0.25},
            ],
        },
    )

    prices = _parse_entsoe_prices(state, "prices_today")

    assert prices == {0: 0.10, 13: 0.25}


def test_parse_entsoe_prices_accepts_native_datetime_objects(hass: HomeAssistant):
    """Same class of bug as Frank Energie's — see the sibling regression test."""
    time_dt = dt_util.now().replace(hour=13, minute=0, second=0, microsecond=0)
    state = State(
        ENTSOE_ENTITY_ID,
        "0.15",
        {
            "unit_of_measurement": "EUR/kWh",
            "prices_today": [{"time": time_dt, "price": 0.25}],
        },
    )

    prices = _parse_entsoe_prices(state, "prices_today")

    assert prices == {13: 0.25}


def test_parse_entsoe_prices_normalizes_mwh_to_kwh(hass: HomeAssistant):
    state = State(
        ENTSOE_ENTITY_ID,
        "150",
        {
            "unit_of_measurement": "EUR/MWh",
            "prices_today": [{"time": _local_iso(0), "price": 150.0}],
        },
    )

    prices = _parse_entsoe_prices(state, "prices_today")

    assert prices == {0: 0.15}


def test_parse_entsoe_prices_missing_attribute_returns_none(hass: HomeAssistant):
    state = State(ENTSOE_ENTITY_ID, "0.15", {})

    assert _parse_entsoe_prices(state, "prices_today") is None


def test_parse_entsoe_prices_malformed_entry_returns_none(hass: HomeAssistant):
    state = State(
        ENTSOE_ENTITY_ID,
        "0.15",
        {"prices_today": [{"time": _local_iso(0)}]},  # missing "price"
    )

    assert _parse_entsoe_prices(state, "prices_today") is None


def test_parse_frank_energie_prices_filters_by_date(hass: HomeAssistant):
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    state = State(
        FRANK_ENERGIE_ENTITY_ID,
        "0.20",
        {
            "prices": [
                {"from": _local_iso(10, on_date=today), "till": "", "price": 0.18},
                {"from": _local_iso(10, on_date=tomorrow), "till": "", "price": 0.99},
            ]
        },
    )

    prices = _parse_frank_energie_prices(state, today)

    assert prices == {10: 0.18}


def test_parse_frank_energie_prices_accepts_native_datetime_objects(hass: HomeAssistant):
    """Regression test: the real, live Frank Energie integration puts actual
    `datetime` objects into "from"/"till" (confirmed live), not ISO strings
    as originally assumed — `dt_util.parse_datetime` raises TypeError on
    those, silently breaking price lookups until this was caught live."""
    today = dt_util.now().date()
    from_dt = dt_util.now().replace(hour=10, minute=0, second=0, microsecond=0)
    state = State(
        FRANK_ENERGIE_ENTITY_ID,
        "0.20",
        {"prices": [{"from": from_dt, "till": from_dt + timedelta(hours=1), "price": 0.18}]},
    )

    prices = _parse_frank_energie_prices(state, today)

    assert prices == {10: 0.18}


def test_parse_frank_energie_prices_missing_attribute_returns_none(hass: HomeAssistant):
    state = State(FRANK_ENERGIE_ENTITY_ID, "0.20", {})

    assert _parse_frank_energie_prices(state, dt_util.now().date()) is None


def test_read_hour_prices_prefers_entsoe(hass: HomeAssistant):
    hass.states.async_set(
        ENTSOE_ENTITY_ID,
        "0.15",
        {"prices_today": [{"time": _local_iso(0), "price": 0.10}]},
    )
    hass.states.async_set(
        FRANK_ENERGIE_ENTITY_ID,
        "0.20",
        {"prices": [{"from": _local_iso(0), "price": 0.99}]},
    )

    prices = read_hour_prices(
        hass, ENTSOE_ENTITY_ID, FRANK_ENERGIE_ENTITY_ID, dt_util.now().date(), "prices_today"
    )

    assert prices == {0: 0.10}


def test_read_hour_prices_falls_back_to_frank_energie(hass: HomeAssistant):
    hass.states.async_set(ENTSOE_ENTITY_ID, "unknown", {})  # no prices_today attribute
    hass.states.async_set(
        FRANK_ENERGIE_ENTITY_ID,
        "0.20",
        {"prices": [{"from": _local_iso(5), "price": 0.30}]},
    )

    prices = read_hour_prices(
        hass, ENTSOE_ENTITY_ID, FRANK_ENERGIE_ENTITY_ID, dt_util.now().date(), "prices_today"
    )

    assert prices == {5: 0.30}


def test_read_hour_prices_returns_none_when_both_missing(hass: HomeAssistant):
    prices = read_hour_prices(hass, None, None, dt_util.now().date(), "prices_today")

    assert prices is None


def test_apply_earning_classification_marks_highest_and_lowest():
    day = Day(valid=True)
    day.hour[5].valid = True
    day.hour[5].price = -0.05  # negative price -> earning on use
    day.hour[10].valid = True
    day.hour[10].price = 0.30  # highest
    day.hour[15].valid = True
    day.hour[15].price = 0.05  # lowest positive

    apply_earning_classification(day, use_fee=0.02, return_fee=0.02, vat_percentage=21)

    assert day.hour[5].earning == Earning.EARNING_ON_USE
    assert day.hour[10].earning == Earning.EARNING_ON_RETURN
    assert day.index_highest == 10
    assert day.hour[10].highest is True
    assert day.index_lowest == 5
    assert day.hour[5].lowest is True


def test_apply_earning_classification_ignores_invalid_hours():
    day = Day(valid=True)
    day.hour[3].valid = True
    day.hour[3].price = 0.10

    apply_earning_classification(day, use_fee=0.02, return_fee=0.02, vat_percentage=21)

    assert day.index_lowest == 3
    assert day.index_highest == 3
    for index, hour in enumerate(day.hour):
        if index != 3:
            assert hour.earning == Earning.NO_EARNING
            assert hour.highest is False
            assert hour.lowest is False


def test_apply_earning_classification_return_vat_percentage_overrides_return_side():
    # price=0.10, return_fee=-0.12: with the full 21% VAT on the return side
    # (baseline/no override), profit_on_return = 0.10*1.21 - 0.12 = 0.001 >
    # 0 -> EARNING_ON_RETURN.
    day = Day(valid=True)
    day.hour[5].valid = True
    day.hour[5].price = 0.10
    apply_earning_classification(day, use_fee=0.0, return_fee=-0.12, vat_percentage=21)
    assert day.hour[5].earning == Earning.EARNING_ON_RETURN

    # Same numbers, but the return side is VAT-exempt (return_vat_percentage
    # =0): profit_on_return = 0.10 - 0.12 = -0.02 <= 0, and cost_on_use
    # (still normally VAT'd) stays positive -- neither branch fires, so
    # earning stays at its NO_EARNING default instead of flipping to
    # EARNING_ON_RETURN.
    day2 = Day(valid=True)
    day2.hour[5].valid = True
    day2.hour[5].price = 0.10
    apply_earning_classification(
        day2, use_fee=0.0, return_fee=-0.12, vat_percentage=21, return_vat_percentage=0
    )
    assert day2.hour[5].earning == Earning.NO_EARNING


def test_build_day_populates_and_classifies(hass: HomeAssistant):
    today = dt_util.now().date()
    hass.states.async_set(
        ENTSOE_ENTITY_ID,
        "0.15",
        {
            "prices_today": [
                {"time": _local_iso(0), "price": 0.10},
                {"time": _local_iso(1), "price": 0.20},
            ]
        },
    )

    day = build_day(hass, today, ENTSOE_ENTITY_ID, None, 0.02, 0.02, 21)

    assert day.valid is True
    assert day.hour[0].valid is True
    assert day.hour[0].price == 0.10
    assert day.index_lowest == 0
    assert day.index_highest == 1


def test_build_day_stays_invalid_when_no_source_configured(hass: HomeAssistant):
    day = build_day(hass, dt_util.now().date(), None, None, 0.02, 0.02, 21)

    assert day.valid is False


def test_build_day_threads_return_vat_percentage_through(hass: HomeAssistant):
    today = dt_util.now().date()
    hass.states.async_set(
        ENTSOE_ENTITY_ID,
        "0.10",
        {"prices_today": [{"time": _local_iso(5), "price": 0.10}]},
    )

    day = build_day(hass, today, ENTSOE_ENTITY_ID, None, 0.0, -0.12, 21, return_vat_percentage=0)

    assert day.hour[5].earning == Earning.NO_EARNING


def test_parse_entsoe_prices_none_state_handled_by_caller(hass: HomeAssistant):
    # read_hour_prices should not crash if hass.states.get() returns None
    # (entity not configured / never created).
    mock_hass = MagicMock()
    mock_hass.states.get.return_value = None

    prices = read_hour_prices(
        mock_hass, ENTSOE_ENTITY_ID, None, dt_util.now().date(), "prices_today"
    )

    assert prices is None
