"""Tests for the schedule sensors in sensor.py.

The modbus/price/solar sensor families are exercised indirectly through
other tests (coordinators, config flow); this file covers the new
schedule-observability and extra/total-PV-power sensors specifically, since
they're the first sensors this integration has direct tests for.
"""

from types import SimpleNamespace

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
    AlphaEssLocalDispatchModeSensor,
    AlphaEssLocalExtraPvPowerSensor,
    AlphaEssLocalGridToBatteryTodaySensor,
    AlphaEssLocalHouseLoadTodaySensor,
    AlphaEssLocalSolarEnergyTodaySensor,
    AlphaEssLocalSolarToBatteryTodaySensor,
    AlphaEssLocalTotalPvPowerSensor,
    _current_hour_action,
    _next_hour_action,
    _remaining_solar_surplus_today,
)


def _fake_coordinator(data=None):
    return SimpleNamespace(data=data, config_entry=SimpleNamespace(entry_id="test-entry"))


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
    }
    assert _description("schedule_current_hour_action").value_fn is _current_hour_action
    assert _description("schedule_next_hour_action").value_fn is _next_hour_action
    assert (
        _description("schedule_remaining_solar_surplus_today").value_fn
        is _remaining_solar_surplus_today
    )


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
