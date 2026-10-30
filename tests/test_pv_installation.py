"""Tests for the PV installations: their subentry flow, how the rest reads
them (extra_pv.py), and the move from the options into a subentry."""

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local import async_migrate_entry
from custom_components.alpha_ess_local.const import DOMAIN, SUBENTRY_PV_INSTALLATION
from custom_components.alpha_ess_local.extra_pv import (
    SMA_PV_POWER_AGGREGATE_KEY,
    SMA_PV_POWER_STRING_KEY,
    extra_pv_options,
    extra_pv_switches,
    installation_from_options,
    pv_installations,
)


def _entry(hass: HomeAssistant, options: dict | None = None, **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"host": "192.168.1.50", "port": 502}, options=options or {}, **kwargs
    )
    entry.add_to_hass(hass)
    return entry


def _add_installation(hass: HomeAssistant, entry, title: str, data: dict) -> str:
    subentry = ConfigSubentry(
        data=data, subentry_type=SUBENTRY_PV_INSTALLATION, title=title, unique_id=None
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry.subentry_id


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


def _block(schema, name):
    """A form block's (section's) inner schema."""
    marker = next(k for k in schema.schema if k == name)
    return schema.schema[marker].schema


def _suggested(schema, key):
    marker = next(k for k in schema.schema if k == key)
    return marker.description["suggested_value"]


# --- the subentry flow ---------------------------------------------------------


async def test_add_installation_form(hass: HomeAssistant):
    """A name, and three blocks -- the switch's collapsed; a detected SMA
    pre-fills the power with its total."""
    total, _ = _sma_sensors(hass, f"{SMA_PV_POWER_AGGREGATE_KEY}_0", f"{SMA_PV_POWER_STRING_KEY}_0")
    entry = _entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PV_INSTALLATION), context={"source": SOURCE_USER}
    )

    schema = result["data_schema"]
    assert [str(key) for key in schema.schema] == [
        "name",
        "icon",
        "measurement",
        "installation",
        "control",
    ]
    assert [str(key) for key in _block(schema, "measurement").schema] == [
        "extra_pv_power_entity",
        "extra_pv_energy_entity",
        "extra_pv_string_entities",
    ]
    assert [str(key) for key in _block(schema, "control").schema] == ["extra_pv_switch_entity"]
    control = next(k for k in schema.schema if k == "control")
    assert schema.schema[control].options["collapsed"] is True
    assert _suggested(_block(schema, "measurement"), "extra_pv_power_entity") == total


async def test_add_installation_not_prefilled_with_sma_strings(hass: HomeAssistant):
    """Without the SMA's total its strings aren't the installation's power."""
    _sma_sensors(hass, f"{SMA_PV_POWER_STRING_KEY}_0", f"{SMA_PV_POWER_STRING_KEY}_1")
    entry = _entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PV_INSTALLATION), context={"source": SOURCE_USER}
    )

    measurement = _block(result["data_schema"], "measurement")
    assert _suggested(measurement, "extra_pv_power_entity") is None


async def test_add_installation_creates_a_subentry(hass: HomeAssistant):
    entry = _entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PV_INSTALLATION), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Garage",
            "measurement": {
                "extra_pv_power_entity": "sensor.sma_ac",
                "extra_pv_energy_entity": "sensor.sma_total_yield",
            },
            "installation": {"extra_pv_panel_wp": 305, "extra_pv_panel_count": 31},
            "control": {"extra_pv_switch_entity": "switch.sma_productie"},
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert pv_installations(entry) == [
        {
            "name": "Garage",
            "extra_pv_power_entity": "sensor.sma_ac",
            "extra_pv_energy_entity": "sensor.sma_total_yield",
            "extra_pv_panel_wp": 305,
            "extra_pv_panel_count": 31,
            "extra_pv_switch_entity": "switch.sma_productie",
        }
    ]


async def test_change_installation(hass: HomeAssistant):
    entry = _entry(hass)
    subentry_id = _add_installation(
        hass, entry, "PV-installatie", {"extra_pv_power_entities": ["sensor.sma_ac", "sensor.b"]}
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PV_INSTALLATION),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["data_schema"]({"measurement": {}, "installation": {}, "control": {}})[
        "name"
    ] == ("PV-installatie")
    measurement = _block(result["data_schema"], "measurement")
    assert _suggested(measurement, "extra_pv_power_entity") == "sensor.sma_ac"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Garage",
            "measurement": {"extra_pv_power_entity": "sensor.micro"},
            "installation": {},
            "control": {},
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert pv_installations(entry) == [
        {
            "name": "Garage",
            "extra_pv_power_entity": "sensor.micro",
            "extra_pv_panel_wp": 0,
            "extra_pv_panel_count": 0,
        }
    ]


# --- read together -------------------------------------------------------------


async def test_extra_pv_options_fold_the_installations_together(hass: HomeAssistant):
    entry = _entry(hass, {"extra_pv_control_enabled": True})
    _add_installation(
        hass,
        entry,
        "Garage",
        {
            "extra_pv_power_entity": "sensor.sma_ac",
            "extra_pv_energy_entity": "sensor.sma_total_yield",
            "extra_pv_string_entities": ["sensor.sma_dc_1"],
            "extra_pv_panel_wp": 305,
            "extra_pv_panel_count": 31,
            "extra_pv_switch_entity": "switch.sma_productie",
        },
    )
    _add_installation(
        hass,
        entry,
        "Schuur",
        {
            "extra_pv_power_entities": ["sensor.micro_1", "sensor.micro_2"],
            "extra_pv_panel_wp": 400,
            "extra_pv_panel_count": 4,
        },
    )

    options = extra_pv_options(hass, entry)

    assert options["extra_pv_control_enabled"] is True
    assert options["extra_pv_power_entities"] == [
        "sensor.sma_ac",
        "sensor.micro_1",
        "sensor.micro_2",
    ]
    assert options["extra_pv_energy_entities"] == ["sensor.sma_total_yield"]
    assert options["extra_pv_string_entities"] == ["sensor.sma_dc_1"]
    assert options["extra_pv_power_capacity"] == 305 * 31 + 400 * 4
    assert extra_pv_switches(options) == ["switch.sma_productie"]


async def test_extra_pv_options_without_installations_are_the_options(hass: HomeAssistant):
    entry = _entry(hass, {"extra_pv_switch_entity": "switch.old"})

    assert extra_pv_options(hass, entry) == {"extra_pv_switch_entity": "switch.old"}
    assert extra_pv_switches(extra_pv_options(hass, entry)) == ["switch.old"]


# --- from the options into a subentry ------------------------------------------


def test_installation_from_options_without_one(hass: HomeAssistant):
    assert installation_from_options(hass, {"pv_power": 9800}) == (None, {"pv_power": 9800})


def test_installation_from_options_resolves_the_old_sma_source(hass: HomeAssistant):
    (total,) = _sma_sensors(hass, f"{SMA_PV_POWER_AGGREGATE_KEY}_0")
    hub = {"extra_pv_modbus_hub": "sma_tripower", "extra_pv_modbus_slave": 3}

    installation, rest = installation_from_options(
        hass,
        {
            "extra_pv_power_entity": total,
            "extra_pv_panel_wp": 305,
            "extra_pv_panel_count": 31,
            "extra_pv_power_capacity": 9455,
            "extra_pv_control_enabled": True,
            **hub,
        },
    )

    assert installation == {
        "extra_pv_power_entity": total,
        "extra_pv_string_entities": [],
        "extra_pv_panel_wp": 305,
        "extra_pv_panel_count": 31,
    }
    assert rest == {"extra_pv_control_enabled": True, **hub}


async def test_migrate_moves_the_installation_into_a_subentry(hass: HomeAssistant):
    hass.config.language = "nl"
    entry = _entry(
        hass,
        {
            "pv_power": 9800,
            "extra_pv_power_entities": ["sensor.sma_ac"],
            "extra_pv_switch_entity": "switch.sma_productie",
            "extra_pv_panel_wp": 305,
            "extra_pv_panel_count": 31,
            "extra_pv_power_capacity": 9455,
        },
        minor_version=1,
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 4
    assert entry.options == {"pv_power": 9800}
    assert pv_installations(entry) == [
        {
            "name": "PV-installatie",
            "extra_pv_power_entity": "sensor.sma_ac",
            "extra_pv_string_entities": [],
            "extra_pv_panel_wp": 305,
            "extra_pv_panel_count": 31,
            "extra_pv_switch_entity": "switch.sma_productie",
        }
    ]


async def test_migrate_without_an_installation(hass: HomeAssistant):
    entry = _entry(hass, {"pv_power": 9800}, minor_version=1)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 4
    assert entry.subentries == {}
