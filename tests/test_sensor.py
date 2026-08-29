"""Tests for the schedule sensors in sensor.py.

The modbus/price/solar sensor families are exercised indirectly through
other tests (coordinators, config flow); this file covers the new
schedule-observability and extra/total-PV-power sensors specifically, since
they're the first sensors this integration has direct tests for.
"""

from types import SimpleNamespace

import pytest
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.alpha_ess_local.data import Charge, Day
from custom_components.alpha_ess_local.dispatch import DispatchDecision
from custom_components.alpha_ess_local.orchestrator import RealPowerData
from custom_components.alpha_ess_local.protocol import DispatchMode, DispatchParam
from custom_components.alpha_ess_local.sensor import (
    MODBUS_SENSOR_DESCRIPTIONS,
    PRICE_SENSOR_DESCRIPTIONS,
    SCHEDULE_SENSOR_DESCRIPTIONS,
    SOLAR_SENSOR_DESCRIPTIONS,
    AlphaEssLocalDaySensor,
    AlphaEssLocalDispatchModeSensor,
    AlphaEssLocalExtraPvPowerSensor,
    AlphaEssLocalGridToBatteryTodaySensor,
    AlphaEssLocalHouseLoadTodaySensor,
    AlphaEssLocalSolarEnergyTodaySensor,
    AlphaEssLocalSolarToBatteryTodaySensor,
    AlphaEssLocalTotalPvPowerSensor,
    AlphaEssLocalYesterdaySavingsSensor,
    _current_hour_action,
    _day_plan_attributes,
    _day_plan_hour_count,
    _hour_plan_entries,
    _next_hour_action,
    _remaining_solar_surplus_today,
)


def _fake_coordinator(data=None):
    return SimpleNamespace(
        data=data, config_entry=SimpleNamespace(entry_id="test-entry", options={})
    )


def _description(key: str):
    return next(d for d in SCHEDULE_SENSOR_DESCRIPTIONS if d.key == key)


def test_current_hour_action_returns_charging_message():
    day = Day(valid=True)
    hour = day.hour[dt_util.now().hour]
    hour.valid = True
    hour.charge = Charge.CHARGING_DISCHARGE

    assert _current_hour_action(day, {}) == "Discharge"


def test_current_hour_action_none_when_day_invalid():
    assert _current_hour_action(Day(valid=False), {}) is None


def test_current_hour_action_none_when_hour_invalid():
    day = Day(valid=True)
    assert _current_hour_action(day, {}) is None


def test_next_hour_action_returns_charging_message():
    day = Day(valid=True)
    next_hour_index = (dt_util.now().hour + 1) % 24
    hour = day.hour[next_hour_index]
    hour.valid = True
    hour.charge = Charge.CHARGING_ON_GRID

    assert _next_hour_action(day, {}) == "Charge-grid"


def test_next_hour_action_none_when_day_invalid():
    assert _next_hour_action(Day(valid=False), {}) is None


def test_schedule_sensor_descriptions_cover_current_and_next_hour():
    keys = {d.key for d in SCHEDULE_SENSOR_DESCRIPTIONS}
    assert keys == {
        "schedule_current_hour_action",
        "schedule_next_hour_action",
        "schedule_remaining_solar_surplus_today",
        "schedule_today_plan",
    }
    assert _description("schedule_current_hour_action").value_fn is _current_hour_action
    assert _description("schedule_next_hour_action").value_fn is _next_hour_action
    assert (
        _description("schedule_remaining_solar_surplus_today").value_fn
        is _remaining_solar_surplus_today
    )
    assert _description("schedule_today_plan").value_fn is _day_plan_hour_count
    assert _description("schedule_today_plan").attributes_fn is _day_plan_attributes


# --- _remaining_solar_surplus_today -------------------------------------------


def test_remaining_solar_surplus_today_sums_surplus_from_current_hour(freezer):
    # Frozen well clear of the day boundary so hour+2 stays a valid index —
    # real "now" flakes near midnight (see the 23:00 failure this replaced).
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    day = Day(valid=True)
    day.hour[current_hour - 1].valid = True  # before "now" -> excluded
    day.hour[current_hour - 1].estimated_solar_power = 5000
    day.hour[current_hour - 1].estimated_house_load = 0
    day.hour[current_hour].valid = True
    day.hour[current_hour].estimated_solar_power = 800
    day.hour[current_hour].estimated_house_load = 300  # surplus 500
    day.hour[current_hour + 1].valid = True
    day.hour[current_hour + 1].estimated_solar_power = 200
    day.hour[current_hour + 1].estimated_house_load = 600  # deficit -> clamped to 0
    day.hour[current_hour + 2].valid = True
    day.hour[current_hour + 2].estimated_solar_power = 1000
    day.hour[current_hour + 2].estimated_house_load = 400  # surplus 600

    assert _remaining_solar_surplus_today(day, {}) == 1100  # 500 + 0 + 600


def test_remaining_solar_surplus_today_skips_invalid_hours(freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    day = Day(valid=True)
    day.hour[current_hour].valid = True
    day.hour[current_hour].estimated_solar_power = 800
    day.hour[current_hour].estimated_house_load = 300
    # hour+1 stays invalid (defaults) -> excluded, not counted as 0 surplus

    assert _remaining_solar_surplus_today(day, {}) == 500


def test_remaining_solar_surplus_today_none_when_day_invalid():
    assert _remaining_solar_surplus_today(Day(valid=False), {}) is None


# --- schedule_today_plan (_hour_plan_entries / _day_plan_hour_count / _day_plan_attributes) --


def test_hour_plan_entries_covers_only_valid_hours_in_order():
    day = Day(valid=True)
    day.hour[5].valid = True
    day.hour[5].price = 0.15
    day.hour[5].estimated_solar_power = 0
    day.hour[5].estimated_house_load = 300
    day.hour[5].charge = Charge.NO_DISCHARGING
    day.hour[9].valid = True
    day.hour[9].price = 0.05
    day.hour[9].estimated_solar_power = 1200
    day.hour[9].estimated_house_load = 400
    day.hour[9].charge = Charge.CHARGING_ON_GRID
    # hour 6-8 stay invalid -> excluded entirely, not padded with zeros

    entries = _hour_plan_entries(day)

    assert entries == [
        {
            "hour": 5,
            "price": 0.15,
            "solar_forecast_w": 0,
            "house_load_w": 300,
            "action": "No-discharging",
        },
        {
            "hour": 9,
            "price": 0.05,
            "solar_forecast_w": 1200,
            "house_load_w": 400,
            "action": "Charge-grid",
        },
    ]


def test_day_plan_hour_count_matches_number_of_valid_hours():
    day = Day(valid=True)
    for hour in day.hour[:4]:
        hour.valid = True

    assert _day_plan_hour_count(day, {}) == 4


def test_day_plan_hour_count_none_when_day_invalid():
    assert _day_plan_hour_count(Day(valid=False), {}) is None


def test_day_plan_attributes_wraps_hour_plan_entries_under_hours_key():
    day = Day(valid=True)
    day.hour[0].valid = True

    attributes = _day_plan_attributes(day, {})

    assert attributes == {"hours": _hour_plan_entries(day)}


def test_day_plan_attributes_none_when_day_invalid():
    assert _day_plan_attributes(Day(valid=False), {}) is None


def test_day_sensor_extra_state_attributes_uses_attributes_fn():
    description = _description("schedule_today_plan")
    day = Day(valid=True)
    day.hour[0].valid = True
    coordinator = _fake_coordinator({"today": day})

    sensor = AlphaEssLocalDaySensor(coordinator, description)

    assert sensor.extra_state_attributes == {"hours": _hour_plan_entries(day)}


def test_day_sensor_extra_state_attributes_none_without_attributes_fn():
    description = _description("schedule_current_hour_action")
    coordinator = _fake_coordinator({"today": Day(valid=True)})

    sensor = AlphaEssLocalDaySensor(coordinator, description)

    assert sensor.extra_state_attributes is None


# --- AlphaEssLocalExtraPvPowerSensor / AlphaEssLocalTotalPvPowerSensor -------


def test_extra_pv_power_sensor_returns_configured_reading():
    coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=321.0))
    sensor = AlphaEssLocalExtraPvPowerSensor(coordinator)

    assert sensor.native_value == 321.0


def test_extra_pv_power_sensor_none_when_unconfigured():
    coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=None))
    sensor = AlphaEssLocalExtraPvPowerSensor(coordinator)

    assert sensor.native_value is None


def test_extra_pv_power_sensor_none_when_coordinator_has_no_data_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalExtraPvPowerSensor(coordinator)

    assert sensor.native_value is None


def test_total_pv_power_sensor_sums_alphaess_and_extra_pv():
    real_data_coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=300.0))
    modbus_coordinator = SimpleNamespace(data={"pv_power": 1000})
    sensor = AlphaEssLocalTotalPvPowerSensor(real_data_coordinator, modbus_coordinator)

    assert sensor.native_value == 1300


def test_total_pv_power_sensor_equals_pv_power_when_extra_pv_unconfigured():
    real_data_coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=None))
    modbus_coordinator = SimpleNamespace(data={"pv_power": 1000})
    sensor = AlphaEssLocalTotalPvPowerSensor(real_data_coordinator, modbus_coordinator)

    assert sensor.native_value == 1000


def test_total_pv_power_sensor_none_when_modbus_data_missing():
    real_data_coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=None))
    modbus_coordinator = SimpleNamespace(data=None)
    sensor = AlphaEssLocalTotalPvPowerSensor(real_data_coordinator, modbus_coordinator)

    assert sensor.native_value is None


# --- AlphaEssLocalHouseLoadTodaySensor ---------------------------------------


def test_house_load_today_sensor_sums_completed_hours():
    day = Day(valid=True)
    day.hour[0].valid = True
    day.hour[0].real_house_load = 400  # Wh for that hour
    day.hour[1].valid = True
    day.hour[1].real_house_load = 350
    # hour 2 never sampled -> valid=False, excluded even though real_house_load defaults to 0
    coordinator = _fake_coordinator(RealPowerData(day=day, extra_pv_power=None))
    sensor = AlphaEssLocalHouseLoadTodaySensor(coordinator)

    assert sensor.native_value == 0.75  # (400 + 350) / 1000 kWh


def test_house_load_today_sensor_none_when_day_invalid():
    coordinator = _fake_coordinator(RealPowerData(day=Day(valid=False), extra_pv_power=None))
    sensor = AlphaEssLocalHouseLoadTodaySensor(coordinator)

    assert sensor.native_value is None


def test_house_load_today_sensor_none_when_coordinator_has_no_data_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalHouseLoadTodaySensor(coordinator)

    assert sensor.native_value is None


def test_house_load_today_sensor_zero_at_start_of_day():
    day = Day(valid=True)  # no hours sampled yet
    coordinator = _fake_coordinator(RealPowerData(day=day, extra_pv_power=None))
    sensor = AlphaEssLocalHouseLoadTodaySensor(coordinator)

    assert sensor.native_value == 0.0


# --- AlphaEssLocalSolarEnergyTodaySensor -------------------------------------


def test_solar_energy_today_sensor_sums_roof_and_extra_pv():
    day = Day(valid=True)
    day.hour[0].valid = True
    day.hour[0].real_solar_power_roof = 400  # Wh for that hour
    day.hour[0].real_extra_pv_power = 100
    day.hour[1].valid = True
    day.hour[1].real_solar_power_roof = 250
    # hour 2 never sampled -> valid=False, excluded
    coordinator = _fake_coordinator(RealPowerData(day=day, extra_pv_power=None))
    sensor = AlphaEssLocalSolarEnergyTodaySensor(coordinator)

    assert sensor.native_value == 0.75  # (400 + 100 + 250) / 1000 kWh


def test_solar_energy_today_sensor_none_when_day_invalid():
    coordinator = _fake_coordinator(RealPowerData(day=Day(valid=False), extra_pv_power=None))
    sensor = AlphaEssLocalSolarEnergyTodaySensor(coordinator)

    assert sensor.native_value is None


def test_solar_energy_today_sensor_none_when_coordinator_has_no_data_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalSolarEnergyTodaySensor(coordinator)

    assert sensor.native_value is None


def test_solar_energy_today_sensor_zero_at_start_of_day():
    day = Day(valid=True)  # no hours sampled yet
    coordinator = _fake_coordinator(RealPowerData(day=day, extra_pv_power=None))
    sensor = AlphaEssLocalSolarEnergyTodaySensor(coordinator)

    assert sensor.native_value == 0.0


# --- AlphaEssLocalSolarToBatteryTodaySensor / AlphaEssLocalGridToBatteryTodaySensor --


def test_solar_to_battery_today_sensor_sums_completed_hours():
    day = Day(valid=True)
    day.hour[0].valid = True
    day.hour[0].real_solar_to_battery = 300  # Wh for that hour
    day.hour[1].valid = True
    day.hour[1].real_solar_to_battery = 200
    # hour 2 never sampled -> valid=False, excluded
    coordinator = _fake_coordinator(RealPowerData(day=day, extra_pv_power=None))
    sensor = AlphaEssLocalSolarToBatteryTodaySensor(coordinator)

    assert sensor.native_value == 0.5  # (300 + 200) / 1000 kWh


def test_solar_to_battery_today_sensor_none_when_day_invalid():
    coordinator = _fake_coordinator(RealPowerData(day=Day(valid=False), extra_pv_power=None))
    sensor = AlphaEssLocalSolarToBatteryTodaySensor(coordinator)

    assert sensor.native_value is None


def test_solar_to_battery_today_sensor_none_when_coordinator_has_no_data_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalSolarToBatteryTodaySensor(coordinator)

    assert sensor.native_value is None


def test_grid_to_battery_today_sensor_sums_completed_hours():
    day = Day(valid=True)
    day.hour[0].valid = True
    day.hour[0].real_grid_to_battery = 150  # Wh for that hour
    day.hour[1].valid = True
    day.hour[1].real_grid_to_battery = 100
    # hour 2 never sampled -> valid=False, excluded
    coordinator = _fake_coordinator(RealPowerData(day=day, extra_pv_power=None))
    sensor = AlphaEssLocalGridToBatteryTodaySensor(coordinator)

    assert sensor.native_value == 0.25  # (150 + 100) / 1000 kWh


def test_grid_to_battery_today_sensor_none_when_day_invalid():
    coordinator = _fake_coordinator(RealPowerData(day=Day(valid=False), extra_pv_power=None))
    sensor = AlphaEssLocalGridToBatteryTodaySensor(coordinator)

    assert sensor.native_value is None


def test_grid_to_battery_today_sensor_none_when_coordinator_has_no_data_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalGridToBatteryTodaySensor(coordinator)

    assert sensor.native_value is None


# --- AlphaEssLocalYesterdaySavingsSensor ---------------------------------------


def _savings_hour(
    hour,
    solar_wh,
    savings_solar_eur,
    savings_battery_eur,
    solar_wh_roof=None,
    solar_wh_extra=0,
    battery_charge_wh=0,
    battery_discharge_wh=0,
):
    return {
        "hour": hour,
        "solar_wh": solar_wh,
        "solar_wh_roof": solar_wh - solar_wh_extra if solar_wh_roof is None else solar_wh_roof,
        "solar_wh_extra": solar_wh_extra,
        "battery_charge_wh": battery_charge_wh,
        "battery_discharge_wh": battery_discharge_wh,
        "savings_solar_eur": savings_solar_eur,
        "savings_battery_eur": savings_battery_eur,
    }


def test_yesterday_savings_sensor_sums_solar_and_battery_savings():
    hours = [
        _savings_hour(10, 600, 0.15, 0.10),
        _savings_hour(11, 800, 0.20, -0.05),  # battery lost money this hour
    ]
    coordinator = _fake_coordinator(
        RealPowerData(day=Day(), extra_pv_power=None, yesterday_savings=hours)
    )
    sensor = AlphaEssLocalYesterdaySavingsSensor(coordinator)

    assert sensor.native_value == pytest.approx(0.40)  # (0.15+0.10) + (0.20-0.05)


def test_yesterday_savings_sensor_none_when_no_hours_stored():
    coordinator = _fake_coordinator(
        RealPowerData(day=Day(), extra_pv_power=None, yesterday_savings=[])
    )
    sensor = AlphaEssLocalYesterdaySavingsSensor(coordinator)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_yesterday_savings_sensor_none_when_coordinator_has_no_data_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalYesterdaySavingsSensor(coordinator)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_yesterday_savings_sensor_attributes_include_hours_and_totals():
    hours = [
        _savings_hour(
            10,
            600,
            0.15,
            0.10,
            solar_wh_roof=250,
            solar_wh_extra=350,
            battery_charge_wh=900,
            battery_discharge_wh=100,
        ),
        _savings_hour(
            11,
            800,
            0.20,
            -0.05,
            solar_wh_roof=800,
            solar_wh_extra=0,
            battery_charge_wh=300,
            battery_discharge_wh=700,
        ),
    ]
    coordinator = _fake_coordinator(
        RealPowerData(day=Day(), extra_pv_power=None, yesterday_savings=hours)
    )
    sensor = AlphaEssLocalYesterdaySavingsSensor(coordinator)

    attributes = sensor.extra_state_attributes

    assert attributes["hours"] == hours
    assert attributes["solar_wh_total"] == 1400
    assert attributes["solar_wh_roof_total"] == 1050
    assert attributes["solar_wh_extra_total"] == 350
    assert attributes["battery_charge_wh_total"] == 1200
    assert attributes["battery_discharge_wh_total"] == 800
    assert attributes["savings_solar_eur_total"] == pytest.approx(0.35)
    assert attributes["savings_battery_eur_total"] == pytest.approx(0.05)


# --- Diagnostic grouping ------------------------------------------------------
#
# Only sensors that are genuinely computed by us over time (the scheduler's
# decisions, and the three combined/summed "today" totals) are grouped under
# the device page's "Diagnostic" section. Sensors that just relay a single
# value from elsewhere — even with a small fee/VAT or unit conversion applied
# — stay as regular sensors: the raw AlphaESS modbus readings, prices, solar
# forecast, and extra PV power.


def test_modbus_sensors_are_not_diagnostic():
    for description in MODBUS_SENSOR_DESCRIPTIONS:
        assert description.entity_category is None, description.key


def test_price_sensors_are_not_diagnostic():
    for description in PRICE_SENSOR_DESCRIPTIONS:
        assert description.entity_category is None, description.key


def test_solar_forecast_sensors_are_not_diagnostic():
    for description in SOLAR_SENSOR_DESCRIPTIONS:
        assert description.entity_category is None, description.key


def test_schedule_sensors_are_diagnostic():
    for description in SCHEDULE_SENSOR_DESCRIPTIONS:
        assert description.entity_category is EntityCategory.DIAGNOSTIC, description.key


def test_extra_pv_power_sensor_is_not_diagnostic():
    coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=None))

    assert AlphaEssLocalExtraPvPowerSensor(coordinator).entity_category is None


def test_total_pv_power_sensor_is_diagnostic():
    real_data_coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=None))
    modbus_coordinator = SimpleNamespace(data={"pv_power": 0})

    assert (
        AlphaEssLocalTotalPvPowerSensor(real_data_coordinator, modbus_coordinator).entity_category
        is EntityCategory.DIAGNOSTIC
    )


def test_house_load_and_solar_energy_today_sensors_are_diagnostic():
    coordinator = _fake_coordinator(RealPowerData(day=Day(), extra_pv_power=None))

    assert (
        AlphaEssLocalHouseLoadTodaySensor(coordinator).entity_category is EntityCategory.DIAGNOSTIC
    )
    assert (
        AlphaEssLocalSolarEnergyTodaySensor(coordinator).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert (
        AlphaEssLocalSolarToBatteryTodaySensor(coordinator).entity_category
        is EntityCategory.DIAGNOSTIC
    )
    assert (
        AlphaEssLocalGridToBatteryTodaySensor(coordinator).entity_category
        is EntityCategory.DIAGNOSTIC
    )


# --- AlphaEssLocalDispatchModeSensor -------------------------------------------


def _fake_dispatch_param(mode: DispatchMode) -> DispatchParam:
    return DispatchParam(
        mode=mode, started=True, power=0, cutoff_soc=0, duration=3600, para7=255, pv_on=True
    )


def test_dispatch_mode_sensor_returns_friendly_label():
    decision = DispatchDecision(
        param=_fake_dispatch_param(DispatchMode.STATE_OF_CHARGE_CONTROL),
        target_feed_in_percentage=0,
    )
    coordinator = _fake_coordinator(decision)
    sensor = AlphaEssLocalDispatchModeSensor(coordinator)

    assert sensor.native_value == "State of Charge control"


def test_dispatch_mode_sensor_none_when_no_decision_yet():
    coordinator = _fake_coordinator(None)
    sensor = AlphaEssLocalDispatchModeSensor(coordinator)

    assert sensor.native_value is None


def test_dispatch_mode_sensor_is_diagnostic():
    decision = DispatchDecision(
        param=_fake_dispatch_param(DispatchMode.NORMAL), target_feed_in_percentage=100
    )
    coordinator = _fake_coordinator(decision)

    assert AlphaEssLocalDispatchModeSensor(coordinator).entity_category is EntityCategory.DIAGNOSTIC
