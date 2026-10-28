"""Tests for the smart (P1) meter's sensors: meter.py and the readers in
orchestrator.py."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local.meter import (
    GridMeter,
    configured_grid_meter,
    detected_meter_sensors,
    detected_peak_sensor,
    tariff_from_state,
)
from custom_components.alpha_ess_local.orchestrator import grid_meter_power, grid_meter_tariff


def _dsmr_meter(hass: HomeAssistant, *, with_delivery: bool = True) -> tuple[str, dict[str, str]]:
    """A DSMR config entry with its usage/delivery/tariff/peak sensors."""
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    suffixes = {
        "usage": "current_electricity_usage",
        "tariff": "electricity_active_tariff",
        "peak": "belgium_maximum_demand_current_month",
    }
    if with_delivery:
        suffixes["delivery"] = "current_electricity_delivery"
    sensors = {
        name: registry.async_get_or_create(
            "sensor", "dsmr", f"serial123_{suffix}", config_entry=config_entry
        ).entity_id
        for name, suffix in suffixes.items()
    }
    return config_entry.entry_id, sensors


# --- which sensors -------------------------------------------------------------


def test_configured_grid_meter_reads_the_chosen_sensors(hass: HomeAssistant):
    options = {
        "grid_import_power_entity": "sensor.import",
        "grid_export_power_entity": "sensor.export",
        "grid_tariff_entity": "sensor.tariff",
    }

    assert configured_grid_meter(hass, options) == GridMeter(
        "sensor.import", "sensor.export", "sensor.tariff"
    )
    assert configured_grid_meter(hass, {"grid_import_power_entity": "sensor.net"}) == (
        GridMeter("sensor.net")
    )


def test_configured_grid_meter_none_without_an_import_sensor(hass: HomeAssistant):
    assert configured_grid_meter(hass, {}) is None
    # Saved without a meter: the old source left behind isn't used any more.
    assert (
        configured_grid_meter(
            hass, {"grid_import_power_entity": None, "house_load_power_entity": "sensor.p1"}
        )
        is None
    )


def test_configured_grid_meter_reads_the_old_house_load_source(hass: HomeAssistant):
    entry_id, sensors = _dsmr_meter(hass)

    assert configured_grid_meter(hass, {"house_load_power_entity": "sensor.p1"}) == GridMeter(
        "sensor.p1"
    )
    assert configured_grid_meter(hass, {"house_load_power_entity": f"dsmr:{entry_id}"}) == (
        GridMeter(sensors["usage"], sensors["delivery"], sensors["tariff"])
    )


def test_configured_grid_meter_none_for_an_incomplete_old_dsmr_source(hass: HomeAssistant):
    """Without DSMR's delivery sensor the usage alone isn't the net power."""
    entry_id, _ = _dsmr_meter(hass, with_delivery=False)

    assert configured_grid_meter(hass, {"house_load_power_entity": f"dsmr:{entry_id}"}) is None


def test_detected_meter_sensors_finds_dsmr(hass: HomeAssistant):
    _, sensors = _dsmr_meter(hass)

    assert detected_meter_sensors(hass) == {
        "grid_import_power_entity": sensors["usage"],
        "grid_export_power_entity": sensors["delivery"],
        "grid_tariff_entity": sensors["tariff"],
        "peak_load_this_month_entity": sensors["peak"],
    }
    assert detected_peak_sensor(hass) == sensors["peak"]


def test_detected_meter_sensors_finds_homewizard(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="homewizard", title="P1 meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    net, tariff, peak = (
        registry.async_get_or_create(
            "sensor", "homewizard", f"abc123_{key}", config_entry=config_entry
        ).entity_id
        for key in ("active_power_w", "active_tariff", "monthly_power_peak_w")
    )
    registry.async_get_or_create(
        "sensor", "homewizard", "abc123_total_power_import_kwh", config_entry=config_entry
    )

    assert detected_meter_sensors(hass) == {
        "grid_import_power_entity": net,
        "grid_tariff_entity": tariff,
        "peak_load_this_month_entity": peak,
    }
    assert detected_peak_sensor(hass) == peak


def test_detected_meter_sensors_empty_when_nothing_installed(hass: HomeAssistant):
    assert detected_meter_sensors(hass) == {}
    assert detected_peak_sensor(hass) is None


# --- tariff ------------------------------------------------------------------


def test_tariff_from_state_words():
    assert tariff_from_state("normal", "BE") == "normal"
    assert tariff_from_state("Laag", "NL") == "low"
    assert tariff_from_state("off-peak", None) == "low"
    assert tariff_from_state("onbekend", "BE") is None


def test_tariff_from_state_numbers_differ_per_country():
    assert tariff_from_state("1", "BE") == "normal"
    assert tariff_from_state("2", "BE") == "low"
    assert tariff_from_state("1", "NL") == "low"
    assert tariff_from_state("0002", "NL") == "normal"
    assert tariff_from_state("3", "BE") is None


def test_grid_meter_tariff_reads_the_tariff_sensor(hass: HomeAssistant):
    hass.config.country = "BE"
    hass.states.async_set("sensor.tariff", "2")

    assert grid_meter_tariff(hass, GridMeter("sensor.import", None, "sensor.tariff")) == "low"
    hass.states.async_set("sensor.tariff", "unavailable")
    assert grid_meter_tariff(hass, GridMeter("sensor.import", None, "sensor.tariff")) is None
    assert grid_meter_tariff(hass, GridMeter("sensor.import")) is None
    assert grid_meter_tariff(hass, None) is None


# --- power -------------------------------------------------------------------


def test_grid_meter_power_import_minus_export(hass: HomeAssistant):
    hass.states.async_set("sensor.import", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.export", "0.1", {"unit_of_measurement": "kW"})

    assert grid_meter_power(hass, GridMeter("sensor.import", "sensor.export")) == 400.0


def test_grid_meter_power_net_sensor_alone(hass: HomeAssistant):
    hass.states.async_set("sensor.net", "-250", {"unit_of_measurement": "W"})

    assert grid_meter_power(hass, GridMeter("sensor.net")) == -250.0


def test_grid_meter_power_none_when_a_sensor_has_no_reading(hass: HomeAssistant):
    hass.states.async_set("sensor.import", "0.5", {"unit_of_measurement": "kW"})

    assert grid_meter_power(hass, GridMeter("sensor.import", "sensor.export")) is None
    assert grid_meter_power(hass, GridMeter("sensor.gone")) is None
    assert grid_meter_power(hass, None) is None
