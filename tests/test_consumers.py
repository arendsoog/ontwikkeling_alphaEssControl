"""Tests for consumers.py: the consumers left out of the learned house load."""

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local import async_migrate_entry
from custom_components.alpha_ess_local.const import (
    DOMAIN,
    SUBENTRY_CONSUMER,
    SUBENTRY_EV_CHARGER,
)
from custom_components.alpha_ess_local.consumers import (
    consumer_options,
    consumers_from_options,
    ev_charger_cards,
    ev_charger_energy_entities,
    ev_charger_groups,
    ev_charger_power_entities,
    ev_chargers,
    ev_chargers_from_options,
    excluded_power_entities,
    other_consumers,
)


def test_lists_take_over_the_single_charger():
    """Saved before the lists: the one charger's sensors are read along."""
    options = {
        "ev_charger_power_entity": "sensor.ev_power",
        "ev_charger_energy_entity": "sensor.ev_energy",
        "ev_charger_power_entities": ["sensor.ev2_power", "sensor.ev_power"],
        "excluded_load_entities": ["sensor.heat_pump"],
    }

    assert ev_charger_power_entities(options) == ["sensor.ev_power", "sensor.ev2_power"]
    assert ev_charger_energy_entities(options) == ["sensor.ev_energy"]
    assert excluded_power_entities(options) == [
        "sensor.ev_power",
        "sensor.ev2_power",
        "sensor.heat_pump",
    ]
    assert excluded_power_entities({}) == []


def test_ev_charger_groups_per_device(hass: HomeAssistant):
    """Each charger (a device in HA) gets its own group, named after it;
    sensors without a device form one unnamed group."""
    config_entry = MockConfigEntry(domain="alfen")
    config_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    entities = {}
    for device_name in ("Oprit", "Garage"):
        device = devices.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={("alfen", device_name)},
            name=device_name,
        )
        for kind in ("power", "energy", "l1", "l2"):
            entities[(device_name, kind)] = registry.async_get_or_create(
                "sensor",
                "alfen",
                f"{device_name}_{kind}",
                config_entry=config_entry,
                device_id=device.id,
            ).entity_id
    options = {
        "ev_charger_power_entities": [
            entities[("Oprit", "power")],
            entities[("Garage", "power")],
            "sensor.modbus_laadpaal_vermogen",
        ],
        "ev_charger_energy_entities": [entities[("Garage", "energy")]],
        "ev_charger_phase_entities": [
            entities[("Oprit", "l1")],
            entities[("Oprit", "l2")],
            "sensor.modbus_laadpaal_vermogen_l1",
        ],
    }

    assert ev_charger_groups(hass, options) == [
        {
            "name": "Oprit",
            "power": [entities[("Oprit", "power")]],
            "energy": [],
            "phases": [entities[("Oprit", "l1")], entities[("Oprit", "l2")]],
        },
        {
            "name": "Garage",
            "power": [entities[("Garage", "power")]],
            "energy": [entities[("Garage", "energy")]],
            "phases": [],
        },
        {
            "name": None,
            "power": ["sensor.modbus_laadpaal_vermogen"],
            "energy": [],
            "phases": ["sensor.modbus_laadpaal_vermogen_l1"],
        },
    ]


# --- EV chargers as subentries ----------------------------------------------------


def _entry(hass: HomeAssistant, options: dict | None = None, **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"host": "192.168.1.50", "port": 502}, options=options or {}, **kwargs
    )
    entry.add_to_hass(hass)
    return entry


def _add_charger(hass: HomeAssistant, entry, title: str, data: dict) -> str:
    subentry = ConfigSubentry(
        data=data, subentry_type=SUBENTRY_EV_CHARGER, title=title, unique_id=None
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry.subentry_id


async def test_add_ev_charger(hass: HomeAssistant):
    entry = _entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_EV_CHARGER), context={"source": SOURCE_USER}
    )
    assert [str(key) for key in result["data_schema"].schema] == [
        "name",
        "ev_charger_power_entity",
        "ev_charger_energy_entity",
        "ev_charger_phase_entities",
    ]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Oprit",
            "ev_charger_power_entity": "sensor.alfen_vermogen",
            "ev_charger_energy_entity": "sensor.alfen_energie",
            "ev_charger_phase_entities": ["sensor.alfen_l1", "sensor.alfen_l2"],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ev_chargers(entry) == [
        {
            "name": "Oprit",
            "ev_charger_power_entity": "sensor.alfen_vermogen",
            "ev_charger_energy_entity": "sensor.alfen_energie",
            "ev_charger_phase_entities": ["sensor.alfen_l1", "sensor.alfen_l2"],
        }
    ]


async def test_change_ev_charger(hass: HomeAssistant):
    entry = _entry(hass)
    subentry_id = _add_charger(
        hass, entry, "EV-lader", {"ev_charger_power_entities": ["sensor.a", "sensor.b"]}
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_EV_CHARGER),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    marker = next(k for k in result["data_schema"].schema if k == "ev_charger_power_entity")
    assert marker.description["suggested_value"] == "sensor.a"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Garage", "ev_charger_power_entity": "sensor.b"}
    )

    assert result["reason"] == "reconfigure_successful"
    assert ev_chargers(entry) == [{"name": "Garage", "ev_charger_power_entity": "sensor.b"}]


async def test_consumer_options_and_cards_from_the_chargers(hass: HomeAssistant):
    entry = _entry(hass, {"excluded_load_entities": ["sensor.warmtepomp"]})
    _add_charger(
        hass,
        entry,
        "Oprit",
        {
            "ev_charger_power_entity": "sensor.oprit_power",
            "ev_charger_energy_entity": "sensor.oprit_energy",
            "ev_charger_phase_entities": ["sensor.oprit_l1"],
        },
    )
    _add_charger(hass, entry, "Garage", {"ev_charger_power_entity": "sensor.garage_power"})

    options = consumer_options(entry)

    assert ev_charger_power_entities(options) == ["sensor.oprit_power", "sensor.garage_power"]
    assert ev_charger_energy_entities(options) == ["sensor.oprit_energy"]
    assert excluded_power_entities(options) == [
        "sensor.oprit_power",
        "sensor.garage_power",
        "sensor.warmtepomp",
    ]
    assert ev_charger_cards(hass, entry) == [
        {
            "name": "Oprit",
            "power": ["sensor.oprit_power"],
            "energy": ["sensor.oprit_energy"],
            "phases": ["sensor.oprit_l1"],
        },
        {"name": "Garage", "power": ["sensor.garage_power"], "energy": [], "phases": []},
    ]


def test_ev_chargers_from_options_one_per_device(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="alfen")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("alfen", "x")}, name="Alfen"
    )
    power = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor", "alfen", "power", config_entry=config_entry, device_id=device.id
        )
        .entity_id
    )

    chargers, rest = ev_chargers_from_options(
        hass,
        {
            "ev_charger_power_entities": [power, "sensor.modbus_power"],
            "ev_charger_phase_entities": ["sensor.modbus_l1"],
            "excluded_load_entities": ["sensor.warmtepomp"],
        },
        "EV-lader",
    )

    assert chargers == [
        ("Alfen", {"ev_charger_power_entity": power}),
        (
            "EV-lader",
            {
                "ev_charger_power_entity": "sensor.modbus_power",
                "ev_charger_phase_entities": ["sensor.modbus_l1"],
            },
        ),
    ]
    assert rest == {"excluded_load_entities": ["sensor.warmtepomp"]}


async def test_migrate_moves_the_chargers_into_subentries(hass: HomeAssistant):
    hass.config.language = "nl"
    entry = _entry(
        hass,
        {
            "ev_charger_power_entity": "sensor.laadpaal_vermogen",
            "ev_charger_energy_entity": "sensor.laadpaal_energie",
            "pv_power": 9800,
        },
        minor_version=2,
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 4
    assert entry.options == {"pv_power": 9800}
    assert ev_chargers(entry) == [
        {
            "name": "EV-lader",
            "ev_charger_power_entity": "sensor.laadpaal_vermogen",
            "ev_charger_energy_entity": "sensor.laadpaal_energie",
        }
    ]


# --- other consumers as subentries ------------------------------------------------


async def test_add_consumer(hass: HomeAssistant):
    entry = _entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CONSUMER), context={"source": SOURCE_USER}
    )
    assert [str(key) for key in result["data_schema"].schema] == [
        "name",
        "consumer_power_entity",
        "consumer_energy_entity",
    ]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Warmtepomp",
            "consumer_power_entity": "sensor.warmtepomp_vermogen",
            "consumer_energy_entity": "sensor.warmtepomp_energie",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert other_consumers(entry) == [
        {
            "name": "Warmtepomp",
            "consumer_power_entity": "sensor.warmtepomp_vermogen",
            "consumer_energy_entity": "sensor.warmtepomp_energie",
        }
    ]


async def test_consumer_options_reads_the_consumers(hass: HomeAssistant):
    entry = _entry(hass)
    for title, power in (("Warmtepomp", "sensor.warmtepomp"), ("Boiler", "sensor.boiler")):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={"consumer_power_entity": power},
                subentry_type=SUBENTRY_CONSUMER,
                title=title,
                unique_id=None,
            ),
        )
    _add_charger(hass, entry, "Oprit", {"ev_charger_power_entity": "sensor.oprit_power"})

    assert excluded_power_entities(consumer_options(entry)) == [
        "sensor.oprit_power",
        "sensor.warmtepomp",
        "sensor.boiler",
    ]


def test_consumers_from_options_named_after_their_sensor(hass: HomeAssistant):
    hass.states.async_set("sensor.warmtepomp", "500", {"friendly_name": "Warmtepomp vermogen"})

    consumers, rest = consumers_from_options(
        hass, {"excluded_load_entities": ["sensor.warmtepomp", "sensor.gone"], "pv_power": 1}
    )

    assert consumers == [
        ("Warmtepomp vermogen", {"consumer_power_entity": "sensor.warmtepomp"}),
        ("sensor.gone", {"consumer_power_entity": "sensor.gone"}),
    ]
    assert rest == {"pv_power": 1}


async def test_migrate_moves_the_consumers_into_subentries(hass: HomeAssistant):
    entry = _entry(
        hass, {"excluded_load_entities": ["sensor.warmtepomp"], "pv_power": 9800}, minor_version=3
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 4
    assert entry.options == {"pv_power": 9800}
    assert other_consumers(entry) == [
        {"name": "sensor.warmtepomp", "consumer_power_entity": "sensor.warmtepomp"}
    ]
