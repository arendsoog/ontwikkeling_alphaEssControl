"""Tests for the AlphaESSControl options flow."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local.config_flow import (
    OPTIONS_SECTIONS,
    SMA_PV_POWER_AGGREGATE_KEY,
    SMA_PV_POWER_STRING_KEY,
)
from custom_components.alpha_ess_local.const import (
    CONF_ALLOW_PROVIDER_CONTROL_HOURS,
    CONF_APPLY_VAT_ON_RETURN,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_EXTRA_PV_PANEL_COUNT,
    CONF_EXTRA_PV_PANEL_WP,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITIES,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_GRID_EXPORT_POWER_ENTITY,
    CONF_GRID_IMPORT_POWER_ENTITY,
    CONF_GRID_TARIFF_ENTITY,
    CONF_HOUSE_LOAD_POWER_ENTITY,
    CONF_NETWORK_USE_FEE_LOW,
    CONF_NETWORK_USE_FEE_NORMAL,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_SOURCE_PRIMARY,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_PANEL_COUNT,
    CONF_PV_PANEL_WP,
    CONF_PV_POWER,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_SOURCE_PRIMARY,
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


async def _open_section(hass: HomeAssistant, entry, section: str):
    """Open the options flow and pick `section` from its menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": section}
    )


async def _save_section(hass: HomeAssistant, entry, section: str, user_input: dict):
    """Open one options section, submit `user_input`, and wait for the update."""
    result = await _open_section(hass, entry, section)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


async def test_options_flow_opens_on_section_menu(hass: HomeAssistant, mock_api_client):
    entry = await _create_entry(hass, mock_api_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == list(OPTIONS_SECTIONS)


async def test_options_flow_shows_form_with_defaults(hass: HomeAssistant, mock_api_client):
    """On first run (no saved options), each section pre-fills the coded defaults."""
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "installation")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "installation"
    assert _default_for(result["data_schema"], CONF_PV_PANEL_WP) == 0
    assert _default_for(result["data_schema"], CONF_PV_PANEL_COUNT) == 0

    result = await _open_section(hass, entry, "prices")

    assert result["step_id"] == "prices"
    assert _default_for(result["data_schema"], CONF_APPLY_VAT_ON_RETURN) == (
        DEFAULT_APPLY_VAT_ON_RETURN
    )
    assert _default_for(result["data_schema"], CONF_NETWORK_USE_FEE_NORMAL) == (
        DEFAULT_NETWORK_USE_FEE
    )
    assert _default_for(result["data_schema"], CONF_NETWORK_USE_FEE_LOW) == (
        DEFAULT_NETWORK_USE_FEE
    )
    assert _default_for(result["data_schema"], CONF_PROVIDER_USE_FEE) == DEFAULT_PROVIDER_USE_FEE
    assert _suggested_value_for(result["data_schema"], CONF_PRICE_ENTITIES) == []


async def test_options_flow_saves_and_updates_entry_options(hass: HomeAssistant, mock_api_client):
    """Submitting a section stores its values on the config entry's options."""
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    entry = await _create_entry(hass, mock_api_client)

    result = await _save_section(
        hass, entry, "installation", {CONF_PV_PANEL_WP: 365, CONF_PV_PANEL_COUNT: 20}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await _save_section(
        hass,
        entry,
        "prices",
        {CONF_PRICE_ENTITIES: ["sensor.entsoe_average_electricity_price_today"]},
    )
    await _save_section(
        hass, entry, "control", {CONF_ALLOW_PROVIDER_CONTROL_HOURS: ["6", "7", "8", "16"]}
    )

    # the raw per-panel inputs are kept (so the form can be reopened and
    # edited), and the single total wattage other readers consume is
    # computed from them -- and survive saving the later sections.
    assert entry.options[CONF_PV_PANEL_WP] == 365
    assert entry.options[CONF_PV_PANEL_COUNT] == 20
    assert entry.options[CONF_PV_POWER] == 7300
    assert entry.options[CONF_ALLOW_PROVIDER_CONTROL_HOURS] == ["6", "7", "8", "16"]
    assert entry.options[CONF_PRICE_ENTITIES] == ["sensor.entsoe_average_electricity_price_today"]
    # untouched fields of a saved section are filled with their coded defaults
    assert entry.options[CONF_PROVIDER_USE_FEE] == DEFAULT_PROVIDER_USE_FEE


async def test_options_flow_clearing_an_optional_field_removes_it(
    hass: HomeAssistant, mock_api_client
):
    """A cleared entity field (absent from user_input) must not keep its old value."""
    entry = await _create_entry(hass, mock_api_client)
    await _save_section(
        hass, entry, "meter", {CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "sensor.my_own_peak_sensor"}
    )

    await _save_section(hass, entry, "meter", {})

    assert CONF_PEAK_LOAD_THIS_MONTH_ENTITY not in entry.options


async def test_options_flow_reopen_prefills_previously_saved_values(
    hass: HomeAssistant, mock_api_client
):
    """Reopening the options flow shows the previously saved values, not the coded defaults."""
    entry = await _create_entry(hass, mock_api_client)
    result = await _open_section(hass, entry, "installation")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PV_PANEL_WP: 365, CONF_PV_PANEL_COUNT: 20}
    )
    await hass.async_block_till_done()

    result = await _open_section(hass, entry, "installation")

    assert _default_for(result["data_schema"], CONF_PV_PANEL_WP) == 365
    assert _default_for(result["data_schema"], CONF_PV_PANEL_COUNT) == 20


async def test_options_flow_saves_vat_exemption_and_network_fee_fields(
    hass: HomeAssistant, mock_api_client
):
    entry = await _create_entry(hass, mock_api_client)
    result = await _open_section(hass, entry, "prices")

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
    result = await _open_section(hass, entry, "extra_pv")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "measurement": {},
            "installation": {CONF_EXTRA_PV_PANEL_WP: 405, CONF_EXTRA_PV_PANEL_COUNT: 12},
            "control": {},
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EXTRA_PV_PANEL_WP] == 405
    assert entry.options[CONF_EXTRA_PV_PANEL_COUNT] == 12
    assert entry.options[CONF_EXTRA_PV_POWER_CAPACITY] == 4860


async def test_options_flow_solar_note_when_no_forecast_integration(
    hass: HomeAssistant, mock_api_client
):
    """No Forecast.Solar/Solcast locations: the note says so."""
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "solar_forecast")

    assert "Forecast.Solar" in result["description_placeholders"]["missing_source_note"]
    assert "Solcast" in result["description_placeholders"]["missing_source_note"]


def _register_location(hass: HomeAssistant, domain: str, title: str) -> tuple[str, str]:
    """A solar forecast location (config entry) with a couple of sensors;
    returns its entry id and its "energy production today" sensor."""
    config_entry = MockConfigEntry(domain=domain, title=title)
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", domain, f"{config_entry.entry_id}_power_production_now", config_entry=config_entry
    )
    today = registry.async_get_or_create(
        "sensor",
        domain,
        f"{config_entry.entry_id}_energy_production_today",
        config_entry=config_entry,
    )
    return config_entry.entry_id, today.entity_id


async def test_options_flow_solar_offers_one_sensor_per_location(
    hass: HomeAssistant, mock_api_client
):
    """Each location is offered by its "energy production today" sensor."""
    _, back = _register_location(hass, "forecast_solar", "Achterkant")
    _, home = _register_location(hass, "solcast_solar", "Home")
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "solar_forecast")

    marker = next(k for k in result["data_schema"].schema if k == CONF_SOLAR_FORECAST_ENTITIES)
    config = result["data_schema"].schema[marker].config
    assert config["include_entities"] == [back, home]
    assert config["multiple"] is True
    assert config["reorder"] is True
    assert result["description_placeholders"]["missing_source_note"] == ""


async def test_options_flow_note_empty_when_both_sources_configured(
    hass: HomeAssistant, mock_api_client
):
    """No missing-source note once both Forecast.Solar and Solcast are configured."""
    _register_location(hass, "forecast_solar", "Achterkant")
    _register_location(hass, "solcast_solar", "Home")
    _register_price_entity(hass, "entsoe", "entsoe_average_electricity_price_today", "prices_today")
    _register_price_entity(hass, "frank_energie", "frank_energie_prices", "prices")
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "solar_forecast")
    assert result["description_placeholders"]["missing_source_note"] == ""

    result = await _open_section(hass, entry, "prices")
    assert result["description_placeholders"]["missing_source_note"] == ""


async def test_options_flow_price_entities_offers_only_price_sensors(
    hass: HomeAssistant, mock_api_client
):
    """The picker lists the sensors with readable prices (not Frank Energie's
    gas prices, nor other sensors), plus an already chosen one that's gone."""
    frank = _register_price_entity(hass, "frank_energie", "frank_energie_prices", "prices")
    nordpool = _register_price_entity(hass, "nordpool", "nordpool_kwh_nl_eur", "raw_today")
    config_entry = MockConfigEntry(domain="frank_energie")
    config_entry.add_to_hass(hass)
    gas = er.async_get(hass).async_get_or_create(
        "sensor", "frank_energie", "frank_energie.gas_market", config_entry=config_entry
    )
    hass.states.async_set(gas.entity_id, "1.1", {"prices": []})
    hass.states.async_set("sensor.temperature", "20", {})
    entry = await _create_entry(hass, mock_api_client)
    hass.config_entries.async_update_entry(
        entry, options={CONF_PRICE_ENTITIES: [nordpool, "sensor.gone"]}
    )

    result = await _open_section(hass, entry, "prices")

    marker = next(k for k in result["data_schema"].schema if k == CONF_PRICE_ENTITIES)
    config = result["data_schema"].schema[marker].config
    assert sorted(config["include_entities"]) == sorted([frank, nordpool, "sensor.gone"])
    assert config["multiple"] is True
    assert config["reorder"] is True
    assert _suggested_value_for(result["data_schema"], CONF_PRICE_ENTITIES) == [
        nordpool,
        "sensor.gone",
    ]
    assert result["description_placeholders"]["missing_source_note"] == ""


async def test_options_flow_prices_prefills_and_replaces_the_old_pair(
    hass: HomeAssistant, mock_api_client
):
    """Saved before the list existed: the form shows the old pair in its old
    order, and saving stores the list and drops the old keys."""
    entry = await _create_entry(hass, mock_api_client)
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_ENTSOE_PRICE_ENTITY: "sensor.entsoe",
            CONF_FRANK_ENERGIE_PRICE_ENTITY: "sensor.frank",
            CONF_PRICE_SOURCE_PRIMARY: "frank_energie",
        },
    )

    result = await _open_section(hass, entry, "prices")
    assert _suggested_value_for(result["data_schema"], CONF_PRICE_ENTITIES) == [
        "sensor.frank",
        "sensor.entsoe",
    ]

    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRICE_ENTITIES: ["sensor.frank", "sensor.entsoe"]}
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_PRICE_ENTITIES] == ["sensor.frank", "sensor.entsoe"]
    for key in (
        CONF_ENTSOE_PRICE_ENTITY,
        CONF_FRANK_ENERGIE_PRICE_ENTITY,
        CONF_PRICE_SOURCE_PRIMARY,
    ):
        assert key not in entry.options


async def test_options_flow_prices_note_when_no_price_sensor_exists(
    hass: HomeAssistant, mock_api_client
):
    """Each section's note only covers its own sources -- no solar names on Prices."""
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "prices")

    note = result["description_placeholders"]["missing_source_note"]
    assert "ENTSO-E" in note
    assert "Solcast" not in note


async def test_options_flow_solar_prefills_and_replaces_the_old_lists(
    hass: HomeAssistant, mock_api_client
):
    """Saved before the list existed: the form shows the old locations in
    the old order, and saving stores the list and drops the old keys."""
    back_id, back = _register_location(hass, "forecast_solar", "Achterkant")
    front_id, front = _register_location(hass, "forecast_solar", "Voorkant")
    home_id, home = _register_location(hass, "solcast_solar", "Home")
    entry = await _create_entry(hass, mock_api_client)
    hass.config_entries.async_update_entry(
        entry,
        options={
            CONF_FORECAST_SOLAR_ENTRIES: [back_id, front_id],
            CONF_SOLCAST_ENTRIES: [home_id],
            CONF_SOLAR_SOURCE_PRIMARY: "solcast",
        },
    )

    result = await _open_section(hass, entry, "solar_forecast")
    assert _suggested_value_for(result["data_schema"], CONF_SOLAR_FORECAST_ENTITIES) == [
        home,
        back,
        front,
    ]

    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SOLAR_FORECAST_ENTITIES: [back, front, home]}
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_SOLAR_FORECAST_ENTITIES] == [back, front, home]
    for key in (CONF_FORECAST_SOLAR_ENTRIES, CONF_SOLCAST_ENTRIES, CONF_SOLAR_SOURCE_PRIMARY):
        assert key not in entry.options


async def test_options_flow_solar_note_shows_the_summed_locations(
    hass: HomeAssistant, mock_api_client
):
    """The form states how the list is read: one service's locations stay
    summed together wherever they are in the list."""
    _, back = _register_location(hass, "forecast_solar", "Achterkant")
    _, front = _register_location(hass, "forecast_solar", "Voorkant")
    _, home = _register_location(hass, "solcast_solar", "Home")
    entry = await _create_entry(hass, mock_api_client)
    hass.config_entries.async_update_entry(
        entry, options={CONF_SOLAR_FORECAST_ENTITIES: [back, home, front]}
    )

    result = await _open_section(hass, entry, "solar_forecast")

    assert result["description_placeholders"]["sources_note"] == (
        "\n\n**In use now:** 1. Forecast.Solar: Achterkant + Voorkant (summed)"
        " · 2. Solcast: Home (backup)"
    )

    result = await _open_section(hass, entry, "prices")
    assert result["description_placeholders"]["sources_note"] == ""


# --- smart meter --------------------------------------------------------------


def _register_dsmr(hass: HomeAssistant) -> tuple[str, dict[str, str]]:
    """A DSMR config entry with its usage/delivery/tariff sensors."""
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    return config_entry.entry_id, {
        name: registry.async_get_or_create(
            "sensor", "dsmr", f"serial123_{suffix}", config_entry=config_entry
        ).entity_id
        for name, suffix in (
            ("usage", "current_electricity_usage"),
            ("delivery", "current_electricity_delivery"),
            ("tariff", "electricity_active_tariff"),
        )
    }


async def test_options_flow_meter_fields_are_free_entity_pickers(
    hass: HomeAssistant, mock_api_client
):
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "meter")

    assert [str(key) for key in result["data_schema"].schema] == [
        CONF_GRID_IMPORT_POWER_ENTITY,
        CONF_GRID_EXPORT_POWER_ENTITY,
        CONF_GRID_TARIFF_ENTITY,
        CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    ]
    for marker, field_selector in result["data_schema"].schema.items():
        assert "options" not in field_selector.config, marker
        assert _suggested_value_for(result["data_schema"], str(marker)) is None


async def test_options_flow_meter_prefills_a_detected_dsmr_meter(
    hass: HomeAssistant, mock_api_client
):
    _, sensors = _register_dsmr(hass)
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "meter")

    schema = result["data_schema"]
    assert _suggested_value_for(schema, CONF_GRID_IMPORT_POWER_ENTITY) == sensors["usage"]
    assert _suggested_value_for(schema, CONF_GRID_EXPORT_POWER_ENTITY) == sensors["delivery"]
    assert _suggested_value_for(schema, CONF_GRID_TARIFF_ENTITY) == sensors["tariff"]


async def test_options_flow_meter_prefills_and_replaces_the_old_dsmr_source(
    hass: HomeAssistant, mock_api_client
):
    """Saved the old way (one DSMR source): the form shows its sensors, and
    saving stores them apart and drops the old key."""
    dsmr_id, sensors = _register_dsmr(hass)
    entry = await _create_entry(hass, mock_api_client)
    hass.config_entries.async_update_entry(
        entry, options={CONF_HOUSE_LOAD_POWER_ENTITY: f"dsmr:{dsmr_id}"}
    )

    result = await _open_section(hass, entry, "meter")
    assert (
        _suggested_value_for(result["data_schema"], CONF_GRID_EXPORT_POWER_ENTITY)
        == (sensors["delivery"])
    )

    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_GRID_IMPORT_POWER_ENTITY: sensors["usage"],
            CONF_GRID_EXPORT_POWER_ENTITY: sensors["delivery"],
            CONF_GRID_TARIFF_ENTITY: sensors["tariff"],
        },
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_GRID_IMPORT_POWER_ENTITY] == sensors["usage"]
    assert entry.options[CONF_GRID_EXPORT_POWER_ENTITY] == sensors["delivery"]
    assert entry.options[CONF_GRID_TARIFF_ENTITY] == sensors["tariff"]
    assert CONF_HOUSE_LOAD_POWER_ENTITY not in entry.options


async def test_options_flow_meter_saved_empty_isnt_prefilled_again(
    hass: HomeAssistant, mock_api_client
):
    """Cleared on purpose: reopening doesn't bring the detected meter back."""
    _register_dsmr(hass)
    entry = await _create_entry(hass, mock_api_client)
    await _save_section(hass, entry, "meter", {})
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_GRID_IMPORT_POWER_ENTITY: None}
    )

    result = await _open_section(hass, entry, "meter")

    assert _suggested_value_for(result["data_schema"], CONF_GRID_IMPORT_POWER_ENTITY) is None


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

    result = await _open_section(hass, entry, "meter")

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

    result = await _open_section(hass, entry, "meter")

    assert (
        _suggested_value_for(result["data_schema"], CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
        == entry_reg.entity_id
    )


async def test_options_flow_peak_load_field_suggests_nothing_when_undetected(
    hass: HomeAssistant, mock_api_client
):
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "meter")

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
    result = await _open_section(hass, entry, "meter")
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "sensor.my_own_peak_sensor"}
    )
    await hass.async_block_till_done()

    result = await _open_section(hass, entry, "meter")

    assert (
        _suggested_value_for(result["data_schema"], CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
        == "sensor.my_own_peak_sensor"
    )


# --- extra energy source -------------------------------------------------------


def _block(schema, name):
    """A form block's (section's) inner schema."""
    marker = next(k for k in schema.schema if k == name)
    return schema.schema[marker].schema


def _sma_sensors(hass: HomeAssistant, *keys: str) -> list[str]:
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    return [
        registry.async_get_or_create(
            "sensor", "sma", f"{config_entry.entry_id}-{key}", config_entry=config_entry
        ).entity_id
        for key in keys
    ]


async def test_options_flow_extra_pv_in_three_blocks(hass: HomeAssistant, mock_api_client):
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "extra_pv")

    schema = result["data_schema"]
    assert [str(key) for key in schema.schema] == ["measurement", "installation", "control"]
    assert [str(key) for key in _block(schema, "measurement").schema] == [
        "extra_pv_power_entities",
        "extra_pv_string_entities",
    ]
    assert [str(key) for key in _block(schema, "control").schema] == [
        "extra_pv_control_enabled",
        "extra_pv_switch_entity",
    ]
    control = next(k for k in schema.schema if k == "control")
    assert schema.schema[control].options["collapsed"] is True


async def test_options_flow_extra_pv_prefills_a_detected_sma(hass: HomeAssistant, mock_api_client):
    """The SMA integration's total PV power, else its strings."""
    strings = _sma_sensors(hass, f"{SMA_PV_POWER_STRING_KEY}_0", f"{SMA_PV_POWER_STRING_KEY}_1")
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "extra_pv")

    measurement = _block(result["data_schema"], "measurement")
    assert _suggested_value_for(measurement, CONF_EXTRA_PV_POWER_ENTITIES) == strings


async def test_options_flow_extra_pv_takes_over_the_old_settings(
    hass: HomeAssistant, mock_api_client
):
    """The old single source pre-fills the power list; picking a switch
    drops the old Modbus hub settings (kept as long as there's none)."""
    (total,) = _sma_sensors(hass, f"{SMA_PV_POWER_AGGREGATE_KEY}_0")
    entry = await _create_entry(hass, mock_api_client)
    hub = {
        "extra_pv_modbus_hub": "sma_tripower",
        "extra_pv_modbus_slave": 3,
        "extra_pv_modbus_address": 40016,
    }
    hass.config_entries.async_update_entry(
        entry, options={CONF_EXTRA_PV_POWER_ENTITY: total, **hub}
    )

    result = await _open_section(hass, entry, "extra_pv")
    measurement = _block(result["data_schema"], "measurement")
    assert _suggested_value_for(measurement, CONF_EXTRA_PV_POWER_ENTITIES) == [total]

    await _save_section(
        hass,
        entry,
        "extra_pv",
        {
            "measurement": {CONF_EXTRA_PV_POWER_ENTITIES: [total]},
            "installation": {},
            "control": {},
        },
    )
    assert entry.options[CONF_EXTRA_PV_POWER_ENTITIES] == [total]
    assert CONF_EXTRA_PV_POWER_ENTITY not in entry.options
    assert entry.options["extra_pv_modbus_hub"] == "sma_tripower"

    await _save_section(
        hass,
        entry,
        "extra_pv",
        {
            "measurement": {CONF_EXTRA_PV_POWER_ENTITIES: [total]},
            "installation": {},
            "control": {"extra_pv_switch_entity": "switch.sma_productie"},
        },
    )
    assert entry.options["extra_pv_switch_entity"] == "switch.sma_productie"
    for key in hub:
        assert key not in entry.options


async def test_options_flow_ev_charger_section_saves_the_lists(
    hass: HomeAssistant, mock_api_client
):
    """EV chargers' power, energy and phase-current sensors, and other
    consumers' power sensors -- any number of each."""
    entry = await _create_entry(hass, mock_api_client)

    result = await _open_section(hass, entry, "ev_charger")
    assert [str(key) for key in result["data_schema"].schema] == [
        "ev_charger_power_entities",
        "ev_charger_energy_entities",
        "ev_charger_phase_entities",
        "excluded_load_entities",
    ]

    await _save_section(
        hass,
        entry,
        "ev_charger",
        {
            "ev_charger_power_entities": ["sensor.laadpaal_vermogen"],
            "ev_charger_energy_entities": ["sensor.laadpaal_energie_totaal"],
            "ev_charger_phase_entities": ["sensor.laadpaal_vermogen_l1"],
            "excluded_load_entities": ["sensor.warmtepomp_vermogen"],
        },
    )

    assert entry.options["ev_charger_power_entities"] == ["sensor.laadpaal_vermogen"]
    assert entry.options["ev_charger_energy_entities"] == ["sensor.laadpaal_energie_totaal"]
    assert entry.options["ev_charger_phase_entities"] == ["sensor.laadpaal_vermogen_l1"]
    assert entry.options["excluded_load_entities"] == ["sensor.warmtepomp_vermogen"]


async def test_options_flow_ev_charger_section_takes_over_the_single_charger(
    hass: HomeAssistant, mock_api_client
):
    """Saved before the lists: the one charger's sensors pre-fill the lists,
    and saving drops the old keys."""
    entry = await _create_entry(hass, mock_api_client)
    hass.config_entries.async_update_entry(
        entry,
        options={
            "ev_charger_power_entity": "sensor.laadpaal_vermogen",
            "ev_charger_energy_entity": "sensor.laadpaal_energie_totaal",
        },
    )

    result = await _open_section(hass, entry, "ev_charger")
    schema = result["data_schema"]
    assert _suggested_value_for(schema, "ev_charger_power_entities") == ["sensor.laadpaal_vermogen"]
    assert _suggested_value_for(schema, "ev_charger_energy_entities") == [
        "sensor.laadpaal_energie_totaal"
    ]

    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "ev_charger_power_entities": ["sensor.laadpaal_vermogen"],
            "ev_charger_energy_entities": ["sensor.laadpaal_energie_totaal"],
        },
    )
    await hass.async_block_till_done()

    assert "ev_charger_power_entity" not in entry.options
    assert "ev_charger_energy_entity" not in entry.options
    assert entry.options["ev_charger_power_entities"] == ["sensor.laadpaal_vermogen"]
