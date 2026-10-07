"""Tests for the sidebar panel's websocket payloads (frontend/__init__.py)."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import Unauthorized
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local.const import DOMAIN
from custom_components.alpha_ess_local.data import Day, FiveMin
from custom_components.alpha_ess_local.frontend import _ws_set_option, today_power_payload


def _day() -> Day:
    return Day(valid=True, year=2026, mon=10, day=6)


def _midnight():
    return dt_util.start_of_local_day(date(2026, 10, 6))


def test_today_power_payload_none_for_invalid_day():
    assert today_power_payload(Day(), dt_util.now()) is None


def test_today_power_payload_stamps_samples_five_minutes_apart():
    day = _day()
    hour = day.hour[10]
    hour.valid = True
    hour.five_min_count = 2
    hour.five_min[0] = FiveMin(real_solar_power_roof=1000, total_active_power=-200)
    hour.five_min[1] = FiveMin(real_solar_power_roof=1200, battery_power=-500)

    payload = today_power_payload(day, _midnight() + timedelta(hours=12))

    ten = (_midnight() + timedelta(hours=10)).timestamp() * 1000
    assert [s["t"] for s in payload["samples"]] == [ten, ten + 5 * 60 * 1000]
    assert payload["samples"][0]["pv_roof"] == 1000
    assert payload["samples"][0]["grid"] == -200
    assert payload["samples"][1]["battery"] == -500


def test_today_power_payload_skips_invalid_hours_and_unsampled_slots():
    day = _day()
    day.hour[9].valid = False
    day.hour[10].valid = True
    day.hour[10].five_min_count = 1

    payload = today_power_payload(day, _midnight() + timedelta(hours=12))

    assert len(payload["samples"]) == 1
    assert [h["hour"] for h in payload["hours"]] == [10]


def test_today_power_payload_weights_the_in_progress_hour():
    day = _day()
    done = day.hour[10]
    done.valid = True
    done.five_min_count = 12
    done.real_solar_power_roof = 2000
    done.real_extra_pv_power = 400
    current = day.hour[11]
    current.valid = True
    current.five_min_count = 3  # a quarter of the hour sampled so far
    current.real_solar_power_roof = 1000
    current.real_extra_pv_power = 800

    payload = today_power_payload(day, _midnight() + timedelta(hours=11, minutes=16))

    assert payload["solar_roof_wh"] == pytest.approx(2000 + 250)
    assert payload["extra_pv_wh"] == pytest.approx(400 + 200)


def test_today_power_payload_shows_hour_averages_for_hours_without_samples():
    """A completed hour restored without its 5-minute samples still shows
    PV and house as its hour averages; grid and battery stay unknown."""
    day = _day()
    hour = day.hour[8]
    hour.valid = True
    hour.real_solar_power_roof = 600
    hour.real_extra_pv_power = 100
    hour.real_house_load = 450

    payload = today_power_payload(day, _midnight() + timedelta(hours=12))

    eight = (_midnight() + timedelta(hours=8)).timestamp() * 1000
    assert [s["t"] for s in payload["samples"]] == [eight, eight + 55 * 60 * 1000]
    first = payload["samples"][0]
    assert (first["pv_roof"], first["extra_pv"], first["house"]) == (600, 100, 450)
    assert first["grid"] is None
    assert first["battery"] is None


# --- set_option websocket command --------------------------------------------


def _ws_connection(is_admin: bool = True):
    connection = MagicMock()
    connection.user.is_admin = is_admin
    return connection


def test_set_option_updates_entry_options(hass):
    entry = MockConfigEntry(domain=DOMAIN, options={"control_enabled": False, "pv_power": 9800})
    entry.add_to_hass(hass)
    connection = _ws_connection()

    with patch.object(hass.config_entries, "async_loaded_entries", return_value=[entry]):
        _ws_set_option(
            hass, connection, {"id": 1, "type": "x", "key": "control_enabled", "value": True}
        )

    assert entry.options == {"control_enabled": True, "pv_power": 9800}
    connection.send_result.assert_called_once_with(1, {"control_enabled": True})


def test_set_option_errors_when_not_loaded(hass):
    connection = _ws_connection()

    _ws_set_option(
        hass, connection, {"id": 1, "type": "x", "key": "control_enabled", "value": True}
    )

    connection.send_error.assert_called_once()
    connection.send_result.assert_not_called()


def test_set_option_requires_admin(hass):
    connection = _ws_connection(is_admin=False)

    with pytest.raises(Unauthorized):
        _ws_set_option(
            hass, connection, {"id": 1, "type": "x", "key": "control_enabled", "value": True}
        )

    connection.send_result.assert_not_called()
