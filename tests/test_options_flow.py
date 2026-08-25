"""Tests for the AlphaESSControl options flow."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local.config_flow import (
    SMA_PV_POWER_AGGREGATE_KEY,
    SMA_PV_POWER_STRING_KEY,
    _extra_pv_candidates,
    _house_load_candidates,
    _peak_load_candidates,
)
from custom_components.alpha_ess_local.const import (
    CONF_ALLOW_PROVIDER_CONTROL_HOURS,
    CONF_APPLY_VAT_ON_RETURN,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_EXTRA_PV_PANEL_COUNT,
    CONF_EXTRA_PV_PANEL_WP,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_HOUSE_LOAD_POWER_ENTITY,
    CONF_NETWORK_USE_FEE_LOW,
    CONF_NETWORK_USE_FEE_NORMAL,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_PANEL_COUNT,
    CONF_PV_PANEL_WP,
    CONF_PV_POWER,
    CONF_SOLCAST_ENTRIES,
    DEFAULT_APPLY_VAT_ON_RETURN,
    DEFAULT_NETWORK_USE_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DOMAIN,
)


def _default_for(schema, key):
    """Look up a data_schema Optional marker's default value by key name."""
    marker = next(k for k in schema.schema if k == key)
    return marker.default()


def _suggested_value_for(schema, key):
    """Look up a data_schema Optional marker's suggested_value by key name.

    Used for entity-selector fields, which pre-fill via `description=
    {"suggested_value": ...}` instead of `default=` (EntitySelector rejects
    a None default).
    """
    marker = next(k for k in schema.schema if k == key)
    return marker.description["suggested_value"]


def _register_price_entity(hass: HomeAssistant, domain: str, object_id: str, attribute: str) -> str:
    """Register a config entry + sensor entity + state exposing `attribute`.

    Mirrors what a real ENTSO-E/Frank Energie integration entry looks like,
    so `_entities_with_attribute` picks it up as a checklist candidate.
    """
    config_entry = MockConfigEntry(domain=domain)
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        domain,
        f"{domain}_{object_id}",
        config_entry=config_entry,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entry.entity_id, "0.25", {attribute: []})
    return entry.entity_id


async def _create_entry(hass: HomeAssistant, mock_api_client):
    """Create a config entry via the (mocked) config flow, without loading it.

    The options flow only needs the entry to exist in the registry, not be
    set up — avoiding a real Modbus connection attempt in these tests.
    """
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "192.168.1.50", "port": 502}
    )
    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_options_flow_shows_form_with_defaults(hass: HomeAssistant, mock_api_client):
    """On first run (no saved options), the form pre-fills the coded defaults."""
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _default_for(result["data_schema"], CONF_PV_PANEL_WP) == 0
    assert _default_for(result["data_schema"], CONF_APPLY_VAT_ON_RETURN) == (
        DEFAULT_APPLY_VAT_ON_RETURN
    )
    assert _default_for(result["data_schema"], CONF_NETWORK_USE_FEE_NORMAL) == (
        DEFAULT_NETWORK_USE_FEE
    )
    assert _default_for(result["data_schema"], CONF_NETWORK_USE_FEE_LOW) == (
        DEFAULT_NETWORK_USE_FEE
    )
    assert _default_for(result["data_schema"], CONF_PV_PANEL_COUNT) == 0
    assert _default_for(result["data_schema"], CONF_PROVIDER_USE_FEE) == DEFAULT_PROVIDER_USE_FEE
    assert _suggested_value_for(result["data_schema"], CONF_ENTSOE_PRICE_ENTITY) is None


async def test_options_flow_saves_and_updates_entry_options(hass: HomeAssistant, mock_api_client):
    """Submitting the form stores the values on the config entry's options."""
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    entry = await _create_entry(hass, mock_api_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PV_PANEL_WP: 365,
            CONF_PV_PANEL_COUNT: 20,
            CONF_ALLOW_PROVIDER_CONTROL_HOURS: ["6", "7", "8", "16"],
            CONF_ENTSOE_PRICE_ENTITY: "sensor.entsoe_average_electricity_price_today",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # the raw per-panel inputs are kept (so the form can be reopened and
    # edited), and the single total wattage other readers consume is
    # computed from them.
    assert entry.options[CONF_PV_PANEL_WP] == 365
    assert entry.options[CONF_PV_PANEL_COUNT] == 20
    assert entry.options[CONF_PV_POWER] == 7300
    assert entry.options[CONF_ALLOW_PROVIDER_CONTROL_HOURS] == ["6", "7", "8", "16"]
    assert (
        entry.options[CONF_ENTSOE_PRICE_ENTITY] == "sensor.entsoe_average_electricity_price_today"
    )
    # untouched fields are still present, filled with their coded defaults
    assert entry.options[CONF_PROVIDER_USE_FEE] == DEFAULT_PROVIDER_USE_FEE


async def test_options_flow_reopen_prefills_previously_saved_values(
    hass: HomeAssistant, mock_api_client
):
    """Reopening the options flow shows the previously saved values, not the coded defaults."""
    entry = await _create_entry(hass, mock_api_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PV_PANEL_WP: 365, CONF_PV_PANEL_COUNT: 20}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _default_for(result["data_schema"], CONF_PV_PANEL_WP) == 365
    assert _default_for(result["data_schema"], CONF_PV_PANEL_COUNT) == 20


async def test_options_flow_saves_vat_exemption_and_network_fee_fields(
    hass: HomeAssistant, mock_api_client
):
    entry = await _create_entry(hass, mock_api_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_APPLY_VAT_ON_RETURN: False,
            CONF_NETWORK_USE_FEE_NORMAL: 0.0564,
            CONF_NETWORK_USE_FEE_LOW: 0.0512,
        },
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_APPLY_VAT_ON_RETURN] is False
    assert entry.options[CONF_NETWORK_USE_FEE_NORMAL] == pytest.approx(0.0564)
    assert entry.options[CONF_NETWORK_USE_FEE_LOW] == pytest.approx(0.0512)


async def test_options_flow_computes_extra_pv_capacity_from_panel_fields(
    hass: HomeAssistant, mock_api_client
):
    """Extra-PV capacity is likewise computed from Wp-per-panel * panel count."""
    entry = await _create_entry(hass, mock_api_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_EXTRA_PV_PANEL_WP: 405, CONF_EXTRA_PV_PANEL_COUNT: 12},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EXTRA_PV_PANEL_WP] == 405
    assert entry.options[CONF_EXTRA_PV_PANEL_COUNT] == 12
    assert entry.options[CONF_EXTRA_PV_POWER_CAPACITY] == 4860


async def test_options_flow_omits_solar_checklists_when_no_source_integrations(
    hass: HomeAssistant, mock_api_client
):
    """No Forecast.Solar/Solcast entries -> those fields aren't shown at all."""
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    keys = list(result["data_schema"].schema)
    assert not any(key == CONF_FORECAST_SOLAR_ENTRIES for key in keys)
    assert not any(key == CONF_SOLCAST_ENTRIES for key in keys)
    assert "Forecast.Solar" in result["description_placeholders"]["solar_forecast_note"]
    assert "Solcast" in result["description_placeholders"]["solar_forecast_note"]


async def test_options_flow_shows_forecast_solar_checklist_with_real_entries(
    hass: HomeAssistant, mock_api_client
):
    """A configured Forecast.Solar entry appears as a checklist option, by its title."""
    forecast_solar_entry = MockConfigEntry(domain="forecast_solar", title="Achterkant")
    forecast_solar_entry.add_to_hass(hass)
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    marker = next(k for k in result["data_schema"].schema if k == CONF_FORECAST_SOLAR_ENTRIES)
    field_selector = result["data_schema"].schema[marker]
    options = field_selector.config["options"]
    assert options == [{"value": forecast_solar_entry.entry_id, "label": "Achterkant"}]
    # Forecast.Solar is now configured, so only the Solcast half of the note remains.
    assert "Forecast.Solar" not in result["description_placeholders"]["solar_forecast_note"]
    assert "Solcast" in result["description_placeholders"]["solar_forecast_note"]


async def test_options_flow_note_empty_when_both_sources_configured(
    hass: HomeAssistant, mock_api_client
):
    """No missing-source note once both Forecast.Solar and Solcast are configured."""
    MockConfigEntry(domain="forecast_solar", title="Achterkant").add_to_hass(hass)
    MockConfigEntry(domain="solcast_solar", title="Home").add_to_hass(hass)
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    _register_price_entity(hass, "frank_energie", "frank_energie_prices", "prices")
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["description_placeholders"]["solar_forecast_note"] == ""


async def test_options_flow_solar_fields_ordered_between_frank_energie_and_extra_pv(
    hass: HomeAssistant, mock_api_client
):
    """Solar-forecast checklists sit right after frank_energie, before extra_pv."""
    MockConfigEntry(domain="forecast_solar", title="Achterkant").add_to_hass(hass)
    MockConfigEntry(domain="solcast_solar", title="Home").add_to_hass(hass)
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    _register_price_entity(hass, "frank_energie", "frank_energie_prices", "prices")
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    keys = [str(key) for key in result["data_schema"].schema]
    assert keys.index(CONF_FRANK_ENERGIE_PRICE_ENTITY) < keys.index(CONF_FORECAST_SOLAR_ENTRIES)
    assert keys.index(CONF_FORECAST_SOLAR_ENTRIES) < keys.index(CONF_SOLCAST_ENTRIES)
    assert keys.index(CONF_SOLCAST_ENTRIES) < keys.index(CONF_EXTRA_PV_POWER_ENTITY)


# --- house-load auto-detect (HomeWizard P1 / DSMR) --------------------------


def test_house_load_candidates_empty_when_nothing_installed(hass: HomeAssistant):
    assert _house_load_candidates(hass) == []


def test_house_load_candidates_detects_homewizard_active_power(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="homewizard", title="P1 meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        "homewizard",
        "abc123_active_power_w",
        config_entry=config_entry,
        suggested_object_id="p1_meter_active_power",
    )

    candidates = _house_load_candidates(hass)

    assert candidates == [(entry.entity_id, "P1 meter")]


def test_house_load_candidates_ignores_other_homewizard_sensors(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="homewizard", title="Socket")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "homewizard",
        "abc123_total_power_import_kwh",
        config_entry=config_entry,
    )

    assert _house_load_candidates(hass) == []


def test_house_load_candidates_detects_dsmr_pair(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "dsmr",
        "serial123_current_electricity_usage",
        config_entry=config_entry,
    )
    registry.async_get_or_create(
        "sensor",
        "dsmr",
        "serial123_current_electricity_delivery",
        config_entry=config_entry,
    )

    candidates = _house_load_candidates(hass)

    assert candidates == [(f"dsmr:{config_entry.entry_id}", "Slimme meter")]


async def test_options_flow_house_load_field_is_checklist_when_detected(
    hass: HomeAssistant, mock_api_client
):
    dsmr_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    dsmr_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_usage", config_entry=dsmr_entry
    )
    registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_delivery", config_entry=dsmr_entry
    )
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    marker = next(k for k in result["data_schema"].schema if k == CONF_HOUSE_LOAD_POWER_ENTITY)
    field_selector = result["data_schema"].schema[marker]
    options = field_selector.config["options"]
    assert options == [{"value": f"dsmr:{dsmr_entry.entry_id}", "label": "Slimme meter"}]


async def test_options_flow_house_load_field_falls_back_to_entity_selector(
    hass: HomeAssistant, mock_api_client
):
    """With nothing auto-detected, the field stays a manual pick-any-sensor selector."""
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    marker = next(k for k in result["data_schema"].schema if k == CONF_HOUSE_LOAD_POWER_ENTITY)
    field_selector = result["data_schema"].schema[marker]
    assert "options" not in field_selector.config


# --- peak-load-this-month auto-detect (DSMR Belgian 5B meters) --------------


def test_peak_load_candidates_empty_when_nothing_installed(hass: HomeAssistant):
    assert _peak_load_candidates(hass) == []


def test_peak_load_candidates_detects_belgian_maximum_demand_sensor(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry_reg = registry.async_get_or_create(
        "sensor",
        "dsmr",
        "serial123_belgium_maximum_demand_current_month",
        config_entry=config_entry,
    )

    candidates = _peak_load_candidates(hass)

    assert candidates == [(entry_reg.entity_id, "Slimme meter")]


def test_peak_load_candidates_ignores_unrelated_dsmr_sensors(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_usage", config_entry=config_entry
    )

    assert _peak_load_candidates(hass) == []


async def test_options_flow_peak_load_field_stays_a_free_entity_picker(
    hass: HomeAssistant, mock_api_client
):
    """Unlike house-load/extra-PV, this field is never locked to a checklist --
    there's no "wrong" alternative to guard against, so it must always stay
    free to point at any sensor."""
    dsmr_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    dsmr_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "dsmr",
        "serial123_belgium_maximum_demand_current_month",
        config_entry=dsmr_entry,
    )
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    marker = next(k for k in result["data_schema"].schema if k == CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
    field_selector = result["data_schema"].schema[marker]
    assert "options" not in field_selector.config


async def test_options_flow_peak_load_field_suggests_detected_sensor(
    hass: HomeAssistant, mock_api_client
):
    """A detected Belgian 5B sensor pre-fills the field (by its own friendly
    name, not the DSMR hub's host:port title), but the user can still pick
    any other sensor -- see test_options_flow_peak_load_field_stays_a_free_entity_picker."""
    dsmr_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    dsmr_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry_reg = registry.async_get_or_create(
        "sensor",
        "dsmr",
        "serial123_belgium_maximum_demand_current_month",
        config_entry=dsmr_entry,
    )
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert (
        _suggested_value_for(result["data_schema"], CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
        == entry_reg.entity_id
    )


async def test_options_flow_peak_load_field_suggests_nothing_when_undetected(
    hass: HomeAssistant, mock_api_client
):
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _suggested_value_for(result["data_schema"], CONF_PEAK_LOAD_THIS_MONTH_ENTITY) is None


async def test_options_flow_peak_load_field_prefers_saved_value_over_detected_sensor(
    hass: HomeAssistant, mock_api_client
):
    """Once the user has picked their own sensor, reopening the form must not
    silently override it back to the auto-detected one."""
    dsmr_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    dsmr_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "dsmr",
        "serial123_belgium_maximum_demand_current_month",
        config_entry=dsmr_entry,
    )
    entry = await _create_entry(hass, mock_api_client)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "sensor.my_own_peak_sensor"}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert (
        _suggested_value_for(result["data_schema"], CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
        == "sensor.my_own_peak_sensor"
    )


# --- extra-PV auto-detect (SMA Solar) ----------------------------------------


def test_extra_pv_candidates_empty_when_nothing_installed(hass: HomeAssistant):
    assert _extra_pv_candidates(hass) == []


def test_extra_pv_candidates_prefers_aggregate_pv_power(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        "sma",
        f"{config_entry.entry_id}-{SMA_PV_POWER_AGGREGATE_KEY}_0",
        config_entry=config_entry,
    )

    candidates = _extra_pv_candidates(hass)

    assert candidates == [(entry.entity_id, "SMA Sunny Boy")]


def test_extra_pv_candidates_falls_back_to_string_sum_reference(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        "sma",
        f"{config_entry.entry_id}-{SMA_PV_POWER_STRING_KEY}_0",
        config_entry=config_entry,
    )
    registry.async_get_or_create(
        "sensor",
        "sma",
        f"{config_entry.entry_id}-{SMA_PV_POWER_STRING_KEY}_1",
        config_entry=config_entry,
    )

    candidates = _extra_pv_candidates(hass)

    assert candidates == [(f"sma:{config_entry.entry_id}", "SMA Sunny Boy")]


def test_extra_pv_candidates_ignores_unrelated_sma_sensors(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "sma", f"{config_entry.entry_id}-9999_deadbeef_0", config_entry=config_entry
    )

    assert _extra_pv_candidates(hass) == []


async def test_options_flow_extra_pv_field_is_checklist_when_detected(
    hass: HomeAssistant, mock_api_client
):
    sma_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    sma_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry_reg = registry.async_get_or_create(
        "sensor",
        "sma",
        f"{sma_entry.entry_id}-{SMA_PV_POWER_AGGREGATE_KEY}_0",
        config_entry=sma_entry,
    )
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    marker = next(k for k in result["data_schema"].schema if k == CONF_EXTRA_PV_POWER_ENTITY)
    field_selector = result["data_schema"].schema[marker]
    options = field_selector.config["options"]
    assert options == [{"value": entry_reg.entity_id, "label": "SMA Sunny Boy"}]
