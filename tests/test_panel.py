"""Tests for the sidebar panel's websocket payloads (frontend/__init__.py)."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local import LIVE_OPTIONS, async_reload_entry
from custom_components.alpha_ess_local.const import DOMAIN
from custom_components.alpha_ess_local.data import Day, FiveMin
from custom_components.alpha_ess_local.frontend import (
    PANEL_COMPONENT,
    PANEL_URL_PATH,
    SETTABLE_OPTIONS,
    _is_our_panel,
    _ws_set_option,
    day_details,
    extra_pv_string_entities,
    source_list,
    today_power_payload,
)


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


# --- history day details ------------------------------------------------------


def test_day_details_labels_actions_and_stamps_samples(tmp_path):
    from custom_components.alpha_ess_local import storage

    db_path = str(tmp_path / "test.db")
    storage.store_hour_action(db_path, 2026, 10, 6, 4, 3, selection="optimum", opt_extra=0.62)
    storage.store_hour_dispatch(
        db_path,
        2026,
        10,
        6,
        4,
        mode="State of Charge control",
        power=5077,
        power_max=5077,
        written=True,
        manual=False,
    )
    storage.store_five_min_sample(
        db_path, 2026, 10, 6, 4, 1, FiveMin(real_solar_power_roof=0, real_house_load=1042)
    )

    details = day_details(db_path, date(2026, 10, 6))

    (decision,) = details["decisions"]
    assert decision["action"] == "Charge-grid"
    assert decision["written"] is True
    assert decision["manual"] is False
    (sample,) = details["samples"]
    assert sample["t"] == (_midnight() + timedelta(hours=4, minutes=5)).timestamp() * 1000
    assert sample["house"] == 1042


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


# --- price/solar source order ---------------------------------------------------


def test_source_list_in_order_with_the_active_one_marked():
    names = {"entsoe": "ENTSO-E", "frank_energie": "Frank Energie"}
    configured = {"entsoe": "sensor.entsoe", "frank_energie": "sensor.frank"}

    assert source_list(names, configured, "frank_energie", "entsoe") == [
        {"name": "Frank Energie", "active": False},
        {"name": "ENTSO-E", "active": True},
    ]


def test_source_list_skips_unconfigured_and_names_summed_locations():
    names = {"forecast_solar": "Forecast.Solar", "solcast": "Solcast"}
    configured = {"forecast_solar": ["Voorkant", "Achterkant"], "solcast": []}

    assert source_list(names, configured, "forecast_solar", "forecast_solar") == [
        {"name": "Forecast.Solar (Voorkant + Achterkant)", "active": True},
    ]


def test_source_list_single_location_shows_just_the_source():
    names = {"forecast_solar": "Forecast.Solar", "solcast": "Solcast"}
    configured = {"forecast_solar": ["Voorkant"], "solcast": ["Solcast Solar"]}

    assert [s["name"] for s in source_list(names, configured, "solcast", "")] == [
        "Solcast",
        "Forecast.Solar",
    ]


# --- extra PV strings -----------------------------------------------------------


def _sma_entry_with_strings(hass):
    """An SMA config entry with total power, strings A/B, and disabled C."""
    sma = MockConfigEntry(domain="sma")
    sma.add_to_hass(hass)
    registry = er.async_get(hass)

    def add(key, object_id, **kwargs):
        return registry.async_get_or_create(
            "sensor", "sma", f"123-{key}", config_entry=sma, suggested_object_id=object_id, **kwargs
        ).entity_id

    total = add("6100_0046C200_0", "pv_power")
    b = add("6380_40251E00_1", "pv_power_b")
    a = add("6380_40251E00_0", "pv_power_a")
    add("6380_40251E00_2", "pv_power_c", disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    return sma, total, a, b


async def test_extra_pv_strings_from_the_sma_integration(hass):
    sma, total, a, b = _sma_entry_with_strings(hass)

    assert extra_pv_string_entities(hass, {"extra_pv_power_entity": total}) == [a, b]
    assert extra_pv_string_entities(hass, {"extra_pv_power_entity": f"sma:{sma.entry_id}"}) == [
        a,
        b,
    ]


async def test_extra_pv_strings_picked_in_options_win(hass):
    _, total, _, _ = _sma_entry_with_strings(hass)
    options = {
        "extra_pv_power_entity": total,
        "extra_pv_string_entities": ["sensor.string_1", "sensor.string_2"],
    }

    assert extra_pv_string_entities(hass, options) == ["sensor.string_1", "sensor.string_2"]


async def test_extra_pv_strings_none_for_a_non_sma_source(hass):
    assert extra_pv_string_entities(hass, {"extra_pv_power_entity": "sensor.sma_ac_vermogen"}) == []
    assert extra_pv_string_entities(hass, {}) == []


# --- options update listener ---------------------------------------------------


def _loaded_entry(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(options=dict(options))
    return entry


def test_panel_toggles_are_all_live_options():
    """Every option the panel toggles must apply without a reload, or the
    reload blanks the panel right after the click."""
    assert set(SETTABLE_OPTIONS) <= LIVE_OPTIONS


async def test_live_option_change_skips_reload(hass):
    entry = _loaded_entry(hass, {"extra_pv_control_enabled": False, "pv_power": 9800})
    hass.config_entries.async_update_entry(
        entry, options={"extra_pv_control_enabled": True, "pv_power": 9800}
    )

    with patch.object(hass.config_entries, "async_reload") as reload:
        await async_reload_entry(hass, entry)

    reload.assert_not_called()
    assert entry.runtime_data.options["extra_pv_control_enabled"] is True


async def test_other_option_change_reloads(hass):
    entry = _loaded_entry(hass, {"extra_pv_control_enabled": False, "pv_power": 9800})
    hass.config_entries.async_update_entry(
        entry, options={"extra_pv_control_enabled": True, "pv_power": 10000}
    )

    with patch.object(hass.config_entries, "async_reload") as reload:
        await async_reload_entry(hass, entry)

    reload.assert_called_once_with(entry.entry_id)


# --- sidebar panel identity -----------------------------------------------------


def _panel(component_name, config=None):
    # Only the fields _is_our_panel reads -- frontend.Panel's constructor
    # changes between Home Assistant versions.
    return SimpleNamespace(component_name=component_name, config=config)


def test_panel_url_is_specific_enough_not_to_clash_with_a_dashboard():
    # A dashboard titled "AlphaESS" readily gets /alpha-ess or /dashboard-alphaess.
    assert PANEL_URL_PATH not in ("alpha-ess", "alphaess", "dashboard-alphaess")


def test_our_custom_panel_is_recognised():
    assert _is_our_panel(_panel("custom", {"_panel_custom": {"name": PANEL_COMPONENT}}))


def test_a_dashboard_on_the_same_url_is_not_ours():
    assert not _is_our_panel(_panel("lovelace", {"mode": "storage"}))


def test_another_custom_panel_on_the_same_url_is_not_ours():
    assert not _is_our_panel(_panel("custom", {"_panel_custom": {"name": "something-else"}}))
