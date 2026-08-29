"""Tests for orchestrator.py.

`AlphaEssLocalScheduleCoordinator` tests mock `run_scheduler`
(orchestrator.py's bound reference to schedule.set_schedule) rather than
running the real DP — the DP itself is already covered by
test_schedule.py, so these tests focus on the *wiring* (right args, right
merged Day objects), not re-verifying the scheduler's decisions.
"""

import glob
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.alpha_ess_local.config_flow import SMA_PV_POWER_STRING_KEY
from custom_components.alpha_ess_local.const import (
    CONF_ALLOW_PROVIDER_CONTROL_HOURS,
    CONF_CONTROL_ENABLED,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_EXTRA_PV_MODBUS_ADDRESS,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_MODBUS_OFF_VALUE,
    CONF_EXTRA_PV_MODBUS_ON_VALUE,
    CONF_EXTRA_PV_MODBUS_SLAVE,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    CONF_USABLE_BATTERY_CAPACITY,
    DOMAIN,
)
from custom_components.alpha_ess_local.data import Charge, Day, Earning, FiveMin, Hour
from custom_components.alpha_ess_local.orchestrator import (
    MIN_SIGMA_WH,
    AlphaEssLocalDispatchCoordinator,
    AlphaEssLocalRealDataCoordinator,
    AlphaEssLocalScheduleCoordinator,
    _dsmr_net_power,
    _dsmr_tariff_indicator,
    _effective_use_fee,
    _entity_power,
    _extra_pv_power,
    _grid_to_battery,
    _house_load_power,
    _house_load_tariff,
    _number_entity_value,
    _sample_hour,
    _sma_pv_power,
    _solar_to_battery,
    _switch_entity_value,
    async_handle_daily_rollover,
    async_handle_hourly_rollover,
    get_db_path,
    hour_correction_from_mean,
    merge_day_sources,
    migrate_legacy_db_if_needed,
    watch_for_source_recovery,
)
from custom_components.alpha_ess_local.protocol import DispatchMode, DispatchParam
from custom_components.alpha_ess_local.storage import HourMean

# --- merge_day_sources -------------------------------------------------------


def _date():
    from datetime import date

    return date(2024, 1, 15)


def test_merge_day_sources_combines_price_and_solar_fields():
    price_day = Day(valid=True)
    price_day.hour[10].valid = True
    price_day.hour[10].price = 0.20
    price_day.hour[10].earning = Earning.EARNING_ON_RETURN

    solar_day = Day(valid=True)
    solar_day.hour[10].valid = True
    solar_day.hour[10].estimated_solar_power = 500
    solar_day.hour[10].estimated_solar_power_raw = 480.0

    merged = merge_day_sources(_date(), price_day, solar_day, None)

    assert merged.valid is True
    assert merged.hour[10].valid is True
    assert merged.hour[10].price == 0.20
    assert merged.hour[10].earning == Earning.EARNING_ON_RETURN
    assert merged.hour[10].estimated_solar_power == 500
    assert merged.hour[10].estimated_solar_power_raw == 480.0


def test_merge_day_sources_applies_house_load_with_floor():
    price_day = Day(valid=True)
    solar_day = Day(valid=False)
    house_load = {
        10: HourMean(house_load=50.0, house_load_sigma=10.0, solar_factor=-1.0, solar_offset=0.0)
    }

    merged = merge_day_sources(_date(), price_day, solar_day, house_load)

    # 50 Wh is below MIN_SIGMA_WH -> floored
    assert merged.hour[10].estimated_house_load == MIN_SIGMA_WH
    assert merged.hour[10].estimated_house_load_sigma == 10.0


def test_merge_day_sources_defaults_house_load_when_no_history():
    price_day = Day(valid=True)
    solar_day = Day(valid=False)

    merged = merge_day_sources(_date(), price_day, solar_day, None)

    for hour in merged.hour:
        assert hour.estimated_house_load == MIN_SIGMA_WH


def test_merge_day_sources_sets_year_mon_day_from_target_date():
    merged = merge_day_sources(_date(), Day(), Day(), None)
    assert (merged.year, merged.mon, merged.day) == (2024, 1, 15)


# --- hour_correction_from_mean -----------------------------------------------


def test_hour_correction_from_mean_extracts_factor_offset():
    mean = {
        5: HourMean(house_load=100.0, house_load_sigma=10.0, solar_factor=1.2, solar_offset=20.0)
    }
    assert hour_correction_from_mean(mean) == {5: (1.2, 20.0)}


def test_hour_correction_from_mean_none_passthrough():
    assert hour_correction_from_mean(None) is None
    assert hour_correction_from_mean({}) is None


# --- _solar_to_battery / _grid_to_battery / _sample_hour ---------------------


def test_solar_to_battery_pure_solar_surplus_charging():
    # 2000 W solar, 500 W house load -> 1500 W surplus; battery charging at
    # 1000 W (battery_power=-1000), fully within the surplus -> all solar.
    sample = FiveMin(
        real_solar_power_roof=2000.0,
        real_extra_pv_power=0.0,
        real_house_load=500.0,
        battery_power=-1000.0,
    )
    assert _solar_to_battery(sample) == 1000.0
    assert _grid_to_battery(sample) == 0.0


def test_grid_to_battery_no_solar_all_grid():
    # No solar at all, battery charging 800 W -> entirely from the grid.
    sample = FiveMin(
        real_solar_power_roof=0.0,
        real_extra_pv_power=0.0,
        real_house_load=300.0,
        battery_power=-800.0,
    )
    assert _solar_to_battery(sample) == 0.0
    assert _grid_to_battery(sample) == 800.0


def test_solar_and_grid_to_battery_mixed_when_charging_exceeds_surplus():
    # 1200 W solar, 900 W house load -> 300 W surplus; battery charging at
    # 500 W -> 300 W of it from solar surplus, the remaining 200 W from grid.
    sample = FiveMin(
        real_solar_power_roof=1200.0,
        real_extra_pv_power=0.0,
        real_house_load=900.0,
        battery_power=-500.0,
    )
    assert _solar_to_battery(sample) == 300.0
    assert _grid_to_battery(sample) == 200.0


def test_solar_to_battery_zero_when_discharging():
    # Discharging (battery_power positive) -> no charging at all, from either source.
    sample = FiveMin(
        real_solar_power_roof=1500.0,
        real_extra_pv_power=0.0,
        real_house_load=500.0,
        battery_power=600.0,
    )
    assert _solar_to_battery(sample) == 0.0
    assert _grid_to_battery(sample) == 0.0


def test_sample_hour_accumulates_solar_and_grid_to_battery_averages():
    hour = Hour()
    # Sample 1: 1200 W solar, total_active_power=200 -> real_house_load =
    # 1200 + 0 + 200 - 500 = 900; 300 W surplus, charging 500 W -> 300
    # solar / 200 grid.
    _sample_hour(hour, 1200.0, 0.0, 200.0, -500.0, 0.02, 0.01, 21.0)
    # Sample 2: no solar, total_active_power=800 -> real_house_load =
    # 0 + 0 + 800 - 800 = 0; no surplus, charging 800 W -> all grid.
    _sample_hour(hour, 0.0, 0.0, 800.0, -800.0, 0.02, 0.01, 21.0)

    assert hour.five_min_count == 2
    assert hour.real_solar_to_battery == round((300.0 + 0.0) / 2)
    assert hour.real_grid_to_battery == round((200.0 + 800.0) / 2)


def test_sample_hour_return_vat_percentage_overrides_export_side_real_result():
    hour = Hour()
    hour.price = 0.20
    # pv_roof=1000, total_active_power=-200 (exporting) -> real_house_load
    # = 1000 + 0 - 200 + 0 = 800 (valid, not negative); active_power ends
    # up negative -> real_result uses the mk_return_price branch.
    _sample_hour(hour, 1000.0, 0.0, -200.0, 0.0, 0.02, 0.01, 21.0, return_vat_percentage=0)

    # real_result = -active_power * mk_return_price(0.20, 0.01, 0) / 1000
    #             = 200 * 0.21 / 1000 = 0.042 (vs 0.0504 with the full 21%
    #             VAT applied, i.e. omitting return_vat_percentage).
    assert hour.real_result == pytest.approx(0.042)


# --- get_db_path -------------------------------------------------------------


def test_get_db_path_keyed_by_unique_id_not_entry_id(hass: HomeAssistant):
    # Removing and re-adding the integration against the same inverter gets
    # a fresh, random entry_id each time -- keying the db filename by the
    # stable host:port unique_id instead means history survives that.
    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.106:1502")
    entry.add_to_hass(hass)

    path = get_db_path(hass, entry)

    assert entry.entry_id not in path
    assert "192.168.1.106_1502" in path


def test_get_db_path_falls_back_to_entry_id_without_unique_id(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id=None)
    entry.add_to_hass(hass)

    path = get_db_path(hass, entry)

    assert entry.entry_id in path


# --- migrate_legacy_db_if_needed ----------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_alpha_ess_local_db_files(hass: HomeAssistant):
    # hass.config.path() in this test harness resolves to a real, shared
    # testing_config/ directory on disk (not a fresh tmp dir per test), so
    # .db files these tests create/rename must be cleaned up or they'd leak
    # into (and break) other tests.
    yield
    for path in glob.glob(hass.config.path("alpha_ess_local_*.db")):
        os.remove(path)


def test_migrate_legacy_db_adopts_the_sole_orphaned_file(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.106:1502")
    entry.add_to_hass(hass)
    legacy_path = hass.config.path("alpha_ess_local_01SOMEOLDENTRYID.db")
    open(legacy_path, "w").close()

    migrate_legacy_db_if_needed(hass, entry)

    new_path = get_db_path(hass, entry)
    assert os.path.exists(new_path)
    assert not os.path.exists(legacy_path)


def test_migrate_legacy_db_does_nothing_without_an_orphan(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.106:1502")
    entry.add_to_hass(hass)

    migrate_legacy_db_if_needed(hass, entry)  # must not raise

    assert not os.path.exists(get_db_path(hass, entry))


def test_migrate_legacy_db_does_not_overwrite_an_existing_db(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.106:1502")
    entry.add_to_hass(hass)
    new_path = get_db_path(hass, entry)
    with open(new_path, "w") as f:
        f.write("already has data")
    legacy_path = hass.config.path("alpha_ess_local_01SOMEOLDENTRYID.db")
    open(legacy_path, "w").close()

    migrate_legacy_db_if_needed(hass, entry)

    with open(new_path) as f:
        assert f.read() == "already has data"
    assert os.path.exists(legacy_path)  # left alone, not consumed


def test_migrate_legacy_db_skips_ambiguous_multiple_orphans(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.106:1502")
    entry.add_to_hass(hass)
    orphan_1 = hass.config.path("alpha_ess_local_01AAAAAAAAAAAAAAAAAAAAAAAA.db")
    orphan_2 = hass.config.path("alpha_ess_local_01BBBBBBBBBBBBBBBBBBBBBBBB.db")
    open(orphan_1, "w").close()
    open(orphan_2, "w").close()

    migrate_legacy_db_if_needed(hass, entry)

    assert not os.path.exists(get_db_path(hass, entry))
    assert os.path.exists(orphan_1)
    assert os.path.exists(orphan_2)


def test_migrate_legacy_db_ignores_files_claimed_by_other_entries(hass: HomeAssistant):
    other_entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.107:1502")
    other_entry.add_to_hass(hass)
    other_path = get_db_path(hass, other_entry)
    open(other_path, "w").close()

    entry = MockConfigEntry(domain=DOMAIN, options={}, unique_id="192.168.1.106:1502")
    entry.add_to_hass(hass)

    migrate_legacy_db_if_needed(hass, entry)

    # other_entry's db is a real file matching the "alpha_ess_local_*.db"
    # glob, but it's claimed by a currently configured entry -- must not be
    # treated as an orphan and stolen.
    assert not os.path.exists(get_db_path(hass, entry))
    assert os.path.exists(other_path)


# --- AlphaEssLocalRealDataCoordinator ----------------------------------------


async def test_real_data_coordinator_accumulates_sample_without_storing(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 10:03:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock()
    ) as mock_store:
        data = await coordinator._async_update_data()

    assert data.day.hour[current_hour].valid is True
    assert data.day.hour[current_hour].five_min_count == 1
    mock_store.assert_not_called()


async def test_real_data_coordinator_sets_current_hour_price_from_prices_coordinator(
    hass: HomeAssistant, freezer
):
    # Regression test: Hour.price used to silently stay at its 0.0
    # dataclass default forever (nothing in this coordinator ever set it),
    # which meant storage.retrieve_day_savings' EUR figures only ever
    # reflected the small use_fee/return_fee component, not the actual
    # market price.
    freezer.move_to("2024-01-15 10:03:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    price_day = Day(valid=True)
    price_day.hour[current_hour].valid = True
    price_day.hour[current_hour].price = 0.185
    prices_coordinator = SimpleNamespace(data={"today": price_day})

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator
    )

    with patch("custom_components.alpha_ess_local.storage.store_hour_data", MagicMock()):
        data = await coordinator._async_update_data()

    assert data.day.hour[current_hour].price == 0.185


async def test_real_data_coordinator_leaves_price_at_default_when_prices_not_ready(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 10:03:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch("custom_components.alpha_ess_local.storage.store_hour_data", MagicMock()):
        data = await coordinator._async_update_data()

    assert data.day.hour[current_hour].price == 0.0


async def test_real_data_coordinator_stores_on_hour_boundary(hass: HomeAssistant, freezer):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()
        mock_store.assert_not_called()

        freezer.move_to("2024-01-15 10:08:00")
        await coordinator._async_update_data()
        mock_store.assert_not_called()

        freezer.move_to("2024-01-15 11:01:00")
        next_hour = dt_util.now().hour
        data = await coordinator._async_update_data()

    mock_store.assert_called_once()
    args = mock_store.call_args.args
    assert args[2] == first_hour  # the hour that just completed
    assert data.day.hour[next_hour].valid is True  # accumulation continues into the new hour


async def test_real_data_coordinator_uses_pv_total_energy_register_delta_when_available(
    hass: HomeAssistant, freezer
):
    # The inverter's own cumulative "Total Energy from PV" register is more
    # accurate than averaging our own coarse periodic power samples -- its
    # hour-boundary delta should override real_solar_power_roof.
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={
            "pv_power": 500,
            "battery_power": -200,
            "grid_power": 50,
            "pv_total_energy": 10.0,
        }
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()  # baseline captured: 10.0 kWh

        modbus_coordinator.data["pv_total_energy"] = 11.6  # +1.6 kWh over the hour
        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    previous_day = mock_store.call_args.args[1]
    # 1.6 kWh delta -> 1600 Wh, not the 500 W sample average.
    assert previous_day.hour[first_hour].real_solar_power_roof == 1600


async def test_real_data_coordinator_falls_back_to_sampled_average_without_pv_total_energy(
    hass: HomeAssistant, freezer
):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()

        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    previous_day = mock_store.call_args.args[1]
    assert previous_day.hour[first_hour].real_solar_power_roof == 1000


async def test_real_data_coordinator_ignores_negative_pv_total_energy_delta(
    hass: HomeAssistant, freezer
):
    """A register reset/rollover must not silently produce a nonsense negative Wh."""
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 500, "battery_power": -200, "grid_power": 50, "pv_total_energy": 10.0}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()

        modbus_coordinator.data["pv_total_energy"] = 9.0  # reset/rollover
        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    previous_day = mock_store.call_args.args[1]
    assert previous_day.hour[first_hour].real_solar_power_roof == 500  # sample average, unaffected


async def test_real_data_coordinator_uses_battery_energy_register_deltas_when_available(
    hass: HomeAssistant, freezer
):
    # Same pattern as pv_total_energy -- the inverter's own cumulative
    # charge/discharge counters override the sample-averaged battery_power
    # split, and unlike that split they're gross (not netted).
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={
            "pv_power": 500,
            "battery_power": -200,
            "grid_power": 50,
            "battery_total_energy_charge": 10.0,
            "battery_total_energy_discharge": 5.0,
        }
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()  # baselines captured: 10.0 / 5.0 kWh

        modbus_coordinator.data["battery_total_energy_charge"] = 11.6  # +1.6 kWh charged
        modbus_coordinator.data["battery_total_energy_discharge"] = 5.3  # +0.3 kWh discharged
        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    previous_day = mock_store.call_args.args[1]
    assert previous_day.hour[first_hour].real_battery_charge_energy == 1600
    assert previous_day.hour[first_hour].real_battery_discharge_energy == 300


async def test_real_data_coordinator_falls_back_to_zero_battery_energy_without_registers(
    hass: HomeAssistant, freezer
):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()

        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    previous_day = mock_store.call_args.args[1]
    assert previous_day.hour[first_hour].real_battery_charge_energy == 0
    assert previous_day.hour[first_hour].real_battery_discharge_energy == 0


async def test_real_data_coordinator_ignores_negative_battery_energy_deltas(
    hass: HomeAssistant, freezer
):
    """A register reset/rollover must not silently produce a nonsense negative Wh."""
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={
            "pv_power": 500,
            "battery_power": -200,
            "grid_power": 50,
            "battery_total_energy_charge": 10.0,
            "battery_total_energy_discharge": 5.0,
        }
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()

        modbus_coordinator.data["battery_total_energy_charge"] = 9.0  # reset/rollover
        modbus_coordinator.data["battery_total_energy_discharge"] = 4.0  # reset/rollover
        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    previous_day = mock_store.call_args.args[1]
    assert previous_day.hour[first_hour].real_battery_charge_energy == 0
    assert previous_day.hour[first_hour].real_battery_discharge_energy == 0


async def test_real_data_coordinator_stores_hour_with_network_fee_blended_by_tariff(
    hass: HomeAssistant, freezer
):
    # A DSMR house-load source whose active-tariff indicator reads "low" for
    # the whole hour -- the persisted use_fee for that hour should be
    # provider_use_fee + network_use_fee_low, not provider_use_fee alone.
    dsmr_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    dsmr_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    usage = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_usage", config_entry=dsmr_entry
    )
    delivery = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_delivery", config_entry=dsmr_entry
    )
    tariff = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_electricity_active_tariff", config_entry=dsmr_entry
    )
    hass.states.async_set(usage.entity_id, "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set(delivery.entity_id, "0.0", {"unit_of_measurement": "kW"})
    hass.states.async_set(tariff.entity_id, "low")

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            "house_load_power_entity": f"dsmr:{dsmr_entry.entry_id}",
            "provider_use_fee": 0.02,
            "network_use_fee_low": 0.05,
            "network_use_fee_normal": 0.03,
        },
    )
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.store_hour_data", MagicMock(return_value=True)
    ) as mock_store:
        freezer.move_to("2024-01-15 10:03:00")
        await coordinator._async_update_data()

        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    mock_store.assert_called_once()
    use_fee_arg = mock_store.call_args.args[3]
    assert use_fee_arg == pytest.approx(0.02 + 0.05)


async def test_real_data_coordinator_passes_return_vat_percentage_to_retrieve_day_savings(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 10:03:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={"vat_percentage": 21, "apply_vat_on_return": False},
    )
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.storage.retrieve_day_savings",
        MagicMock(return_value=[]),
    ) as mock_retrieve:
        await coordinator._async_update_data()

    mock_retrieve.assert_called_once()
    args = mock_retrieve.call_args.args
    assert args[4] == 21  # default_vat_percentage, unaffected
    assert args[5] == 0.0  # return_vat_percentage=0 -- apply_vat_on_return is False


async def test_real_data_coordinator_rehydrates_todays_hours_on_startup(
    hass: HomeAssistant, freezer
):
    # Simulates a Home Assistant restart mid-day: a fresh coordinator (no
    # in-memory `_today` yet) whose first refresh happens at 14:03, with
    # hours 8 and 9 already persisted from before the restart. Those hours
    # must reappear in the running "today" total, not be lost.
    freezer.move_to("2024-01-15 14:03:00")
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.retrieve_day_hours",
        MagicMock(
            return_value={8: (400.0, 100.0, 20.0, 60.0, 15.0), 9: (350.0, 90.0, 10.0, 40.0, 5.0)}
        ),
    ):
        data = await coordinator._async_update_data()

    assert data.day.hour[8].valid is True
    assert data.day.hour[8].real_house_load == 400.0
    assert data.day.hour[8].real_solar_power_roof == 100.0
    assert data.day.hour[8].real_extra_pv_power == 20.0
    assert data.day.hour[8].real_solar_to_battery == 60.0
    assert data.day.hour[8].real_grid_to_battery == 15.0
    assert data.day.hour[9].valid is True
    assert data.day.hour[9].real_house_load == 350.0


async def test_real_data_coordinator_rehydrates_current_hour_progress_on_startup(
    hass: HomeAssistant, freezer
):
    # A restart mid-hour: 3 samples already landed for the current hour
    # (14:00) before the restart, persisted as a running average. That
    # average/count must survive, or a restart would silently drop whatever
    # 5-minute samples had already been gathered this hour.
    freezer.move_to("2024-01-15 14:23:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    def _retrieve_hour_progress(_db_path, _year, _month, _day, hour):
        # Only the current hour (14) has an in-progress row -- every earlier
        # hour genuinely has nothing (matches real behavior; a blanket
        # return_value here would make every earlier hour look like an
        # orphaned-but-complete hour to the new recovery loop).
        if hour == current_hour:
            return (500.0, 120.0, 30.0, 400.0, 3, 80.0, 20.0, 50.0, 5.0, 2.0)
        return None

    with (
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_day_hours",
            MagicMock(return_value={}),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_hour_progress",
            MagicMock(side_effect=_retrieve_hour_progress),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_progress",
            MagicMock(),
        ),
    ):
        data = await coordinator._async_update_data()

    hour = data.day.hour[current_hour]
    assert hour.valid is True
    # rehydrated count (3) plus this update's own fresh sample.
    assert hour.five_min_count == 4
    # Rehydrated samples carry the persisted solar/grid-to-battery average
    # directly (see orchestrator.py's rehydration block) — the hour-level
    # real_solar_to_battery/real_grid_to_battery is then recomputed fresh
    # over all 4 samples (rehydrated + this update's own), so it's not
    # asserted here as a fixed value.
    for sample in hour.five_min[:3]:
        assert sample.real_house_load == 500.0
        assert sample.real_solar_power_roof == 120.0
        assert sample.real_extra_pv_power == 30.0
        assert sample.total_active_power == 400.0
        assert sample.solar_to_battery == 80.0
        assert sample.grid_to_battery == 20.0
    # The pv_total_energy hour-start baseline also survives the restart --
    # without this, the register-delta fix (see _sample_hour) would fall
    # back to the sample-averaged value for this hour too.
    assert coordinator._pv_total_energy_at_hour_start == 50.0
    # Same for the battery charge/discharge baselines.
    assert coordinator._battery_charge_energy_at_hour_start == 5.0
    assert coordinator._battery_discharge_energy_at_hour_start == 2.0


async def test_real_data_coordinator_recovers_orphaned_earlier_hour_progress(
    hass: HomeAssistant, freezer
):
    # A restart landed exactly between an earlier hour finishing (a
    # full-count hour_progress row) and the next update cycle that would
    # normally have migrated it into hour_data -- that row is now
    # "orphaned": absent from hour_data, but its hour is no longer
    # now.hour either, so the plain current-hour rehydration above would
    # silently drop it. Anchored late in the day (UTC) and derived from
    # the actual resolved local hour, rather than a hardcoded hour, so
    # this doesn't depend on the test environment's timezone offset.
    freezer.move_to("2024-01-15 23:23:00")
    current_hour = dt_util.now().hour
    assert current_hour >= 5, "test anchor time too close to local midnight for this offset"
    orphan_hour = current_hour - 4
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    def _retrieve_hour_progress(_db_path, _year, _month, _day, hour):
        if hour == orphan_hour:
            return (2484.0, 0.0, 3659.0, 2484.0, 12, 1428.0, 42.0, None, None, None)
        return None

    with (
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_day_hours",
            MagicMock(return_value={}),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_hour_progress",
            MagicMock(side_effect=_retrieve_hour_progress),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_progress",
            MagicMock(),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_data",
            MagicMock(return_value=True),
        ) as mock_store_hour_data,
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.delete_hour_progress",
            MagicMock(),
        ) as mock_delete_hour_progress,
    ):
        data = await coordinator._async_update_data()

    # Recovered into the live Day, so today's totals include it again.
    recovered_hour = data.day.hour[orphan_hour]
    assert recovered_hour.valid is True
    assert recovered_hour.real_house_load == 2484.0
    assert recovered_hour.real_extra_pv_power == 3659.0
    assert recovered_hour.real_solar_to_battery == 1428.0
    assert recovered_hour.real_grid_to_battery == 42.0

    # And migrated into permanent storage exactly once, same as a normal
    # hour-boundary transition would have done.
    mock_store_hour_data.assert_called_once()
    assert mock_store_hour_data.call_args.args[2] == orphan_hour
    mock_delete_hour_progress.assert_any_call(ANY, 2024, 1, 15, orphan_hour)

    # The genuinely-current hour is untouched by the recovery loop.
    assert data.day.hour[current_hour].valid is True


async def test_real_data_coordinator_recovers_yesterdays_orphaned_last_hour(
    hass: HomeAssistant, freezer
):
    # A restart landed exactly at the day boundary -- yesterday's hour 23
    # finished (a full-count hour_progress row) but the coordinator never
    # got a cycle to promote it into hour_data before "today" became a new
    # day. The current-day-only recovery loop (see the test above) can't
    # reach it, since it's no longer "today" by the time this runs -- this
    # is the dedicated yesterday-hour-23 check's job instead.
    freezer.move_to("2024-01-15 00:03:00")
    today_local = dt_util.now().date()
    yesterday_local = today_local - timedelta(days=1)
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    def _retrieve_hour_progress(_db_path, year, month, day, hour):
        if (year, month, day, hour) == (
            yesterday_local.year,
            yesterday_local.month,
            yesterday_local.day,
            23,
        ):
            return (2484.0, 0.0, 3659.0, 2484.0, 12, 1428.0, 42.0, None, None, None)
        return None

    with (
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_day_hours",
            MagicMock(return_value={}),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_hour_progress",
            MagicMock(side_effect=_retrieve_hour_progress),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_progress",
            MagicMock(),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_data",
            MagicMock(return_value=True),
        ) as mock_store_hour_data,
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.delete_hour_progress",
            MagicMock(),
        ) as mock_delete_hour_progress,
    ):
        await coordinator._async_update_data()

    # Stored under *yesterday's* date, hour 23 -- not folded into today.
    mock_store_hour_data.assert_called_once()
    stored_day = mock_store_hour_data.call_args.args[1]
    stored_hour = mock_store_hour_data.call_args.args[2]
    assert (stored_day.year, stored_day.mon, stored_day.day) == (
        yesterday_local.year,
        yesterday_local.month,
        yesterday_local.day,
    )
    assert stored_hour == 23
    assert stored_day.hour[23].real_house_load == 2484.0
    assert stored_day.hour[23].real_extra_pv_power == 3659.0
    assert stored_day.hour[23].real_solar_to_battery == 1428.0
    assert stored_day.hour[23].real_grid_to_battery == 42.0

    mock_delete_hour_progress.assert_any_call(
        ANY, yesterday_local.year, yesterday_local.month, yesterday_local.day, 23
    )


async def test_real_data_coordinator_skips_yesterday_recovery_when_already_stored(
    hass: HomeAssistant, freezer
):
    # Yesterday's hour 23 is already present in hour_data (the normal path
    # worked fine) -- must not re-store or re-touch it.
    freezer.move_to("2024-01-15 00:03:00")
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    def _retrieve_day_hours(_db_path, _year, _month, _day):
        return {23: (2000.0, 0.0, 0.0, 0.0, 0.0)}

    with (
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_day_hours",
            MagicMock(side_effect=_retrieve_day_hours),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_hour_progress",
            MagicMock(return_value=None),
        ) as mock_retrieve_progress,
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_progress",
            MagicMock(),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_data",
            MagicMock(return_value=True),
        ) as mock_store_hour_data,
    ):
        await coordinator._async_update_data()

    # Since retrieve_day_hours already reports hour 23 present, the
    # yesterday-hour-23 orphan check must skip straight past it -- never
    # even asking retrieve_hour_progress for hour 23, let alone storing.
    assert all(call.args[-1] != 23 for call in mock_retrieve_progress.call_args_list)
    mock_store_hour_data.assert_not_called()


async def test_real_data_coordinator_persists_progress_after_each_sample(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:03:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.store_hour_progress",
        MagicMock(),
    ) as mock_store_progress:
        data = await coordinator._async_update_data()

    mock_store_progress.assert_called_once()
    args = mock_store_progress.call_args.args
    assert args[4] == current_hour
    assert args[9] == data.day.hour[current_hour].five_min_count
    assert args[10] == data.day.hour[current_hour].real_solar_to_battery
    assert args[11] == data.day.hour[current_hour].real_grid_to_battery


async def test_real_data_coordinator_persists_new_hours_own_pv_total_energy_baseline(
    hass: HomeAssistant, freezer
):
    """On an hour transition, the progress row persisted for the *new* hour
    must carry that hour's own fresh baseline, not the just-completed
    hour's -- store_hour_progress runs after the baseline reset, not
    before it (see _async_update_data's comment on the ordering)."""
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 500, "battery_power": -200, "grid_power": 50, "pv_total_energy": 10.0}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    freezer.move_to("2024-01-15 10:03:00")
    await coordinator._async_update_data()  # baseline for hour 10 captured: 10.0 kWh

    modbus_coordinator.data["pv_total_energy"] = 11.6  # new hour's fresh reading
    freezer.move_to("2024-01-15 11:01:00")
    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.store_hour_progress",
        MagicMock(),
    ) as mock_store_progress:
        await coordinator._async_update_data()

    mock_store_progress.assert_called_once()
    # Positional arg 12 is pv_total_energy_at_hour_start -- must be hour
    # 11's own fresh baseline (11.6), not hour 10's stale one (10.0).
    assert mock_store_progress.call_args.args[12] == 11.6


async def test_real_data_coordinator_deletes_progress_when_hour_completes(
    hass: HomeAssistant, freezer
):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": -200, "grid_power": 50}
    )
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with (
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.store_hour_data",
            MagicMock(return_value=True),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.delete_hour_progress",
            MagicMock(),
        ) as mock_delete_progress,
    ):
        freezer.move_to("2024-01-15 10:03:00")
        first_hour = dt_util.now().hour
        await coordinator._async_update_data()
        mock_delete_progress.assert_not_called()

        freezer.move_to("2024-01-15 11:01:00")
        await coordinator._async_update_data()

    mock_delete_progress.assert_called_once_with(ANY, 2024, 1, 15, first_hour)


async def test_real_data_coordinator_rehydration_empty_on_fresh_day(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 00:03:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(data={"pv_power": 0, "battery_power": 0, "grid_power": 0})
    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )

    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.retrieve_day_hours",
        MagicMock(return_value={}),
    ) as mock_retrieve:
        data = await coordinator._async_update_data()

    # called for both today (rehydration) and yesterday (day-boundary
    # orphaned-last-hour recovery -- see the yesterday-hour-23 check in
    # _async_update_data). Dates derived from dt_util.now() rather than
    # hardcoded, since the test hass fixture's timezone can shift the frozen
    # UTC timestamp above onto a different local calendar date.
    today_local = dt_util.now().date()
    yesterday_local = today_local - timedelta(days=1)
    assert mock_retrieve.call_count == 2
    mock_retrieve.assert_any_call(ANY, today_local.year, today_local.month, today_local.day)
    mock_retrieve.assert_any_call(
        ANY, yesterday_local.year, yesterday_local.month, yesterday_local.day
    )
    # nothing to rehydrate -> only the hour actually sampled just now is valid.
    assert all(h.valid == (i == current_hour) for i, h in enumerate(data.day.hour))


async def test_real_data_coordinator_ignores_negative_house_load(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 10:03:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    # pv + battery + grid sums deeply negative -> CalculatePower's guard should skip it
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 0, "battery_power": 0, "grid_power": -5000}
    )

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )
    data = await coordinator._async_update_data()

    assert data.day.hour[current_hour].five_min_count == 0


async def test_real_data_coordinator_uses_house_load_entity_when_configured(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 10:03:00")
    current_hour = dt_util.now().hour
    hass.states.async_set("sensor.p1_power", "123")
    entry = MockConfigEntry(domain=DOMAIN, options={"house_load_power_entity": "sensor.p1_power"})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 0, "battery_power": 0, "grid_power": 999}
    )

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )
    data = await coordinator._async_update_data()

    # total_active_power should come from the P1 entity (123), not the
    # inverter's own grid meter (999).
    assert data.day.hour[current_hour].total_active_power == 123


async def test_real_data_coordinator_extra_pv_power_none_when_unconfigured(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 10:03:00")
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": 0, "grid_power": 50}
    )

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )
    data = await coordinator._async_update_data()

    assert data.extra_pv_power is None


async def test_real_data_coordinator_extra_pv_power_reports_configured_reading(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 10:03:00")
    hass.states.async_set("sensor.sma_pv_power", "321", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(domain=DOMAIN, options={"extra_pv_power_entity": "sensor.sma_pv_power"})
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(
        data={"pv_power": 1000, "battery_power": 0, "grid_power": 50}
    )

    coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, SimpleNamespace(data=None)
    )
    data = await coordinator._async_update_data()

    assert data.extra_pv_power == 321.0


# --- AlphaEssLocalScheduleCoordinator ----------------------------------------


async def test_schedule_coordinator_calls_scheduler_with_current_soc_and_hour(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    price_day = Day(valid=True)
    solar_day = Day(valid=True)
    prices_coordinator = SimpleNamespace(data={"today": price_day, "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": solar_day, "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        data = await coordinator._async_update_data()

    assert set(data.keys()) == {"today", "tomorrow"}
    args = mock_run_scheduler.call_args.args
    assert args[0] == 550  # 55.0% -> native 0-1000 scale
    assert args[1] == current_hour
    assert args[2] is data["today"]
    assert args[3] is data["tomorrow"]


async def test_schedule_coordinator_converts_soc_bound_options_to_native_scale(
    hass: HomeAssistant, freezer
):
    # SOC bounds and daily_min_profit live on this integration's own
    # `number` entities (number.py — device-side sliders, read via the
    # entity registry by unique_id). SOC bounds are plain 0-100 percent;
    # ScheduleConfig needs data.py's 0.1%-unit scale (e.g. 900 = 90.0%).
    # daily_min_profit is whole cents on its own entity; ScheduleConfig
    # needs EUR.
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    hass.states.async_set(_register_soc_number(hass, entry, "max_soc_positive_price"), "85.0")
    hass.states.async_set(_register_soc_number(hass, entry, "max_soc_negative_price"), "97.5")
    hass.states.async_set(_register_soc_number(hass, entry, "min_soc_discharge"), "25.0")
    hass.states.async_set(_register_soc_number(hass, entry, "daily_min_profit"), "65")
    hass.states.async_set(_register_soc_number(hass, entry, "max_grid_load"), "7.5")

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    price_day = Day(valid=True)
    solar_day = Day(valid=True)
    prices_coordinator = SimpleNamespace(data={"today": price_day, "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": solar_day, "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        await coordinator._async_update_data()

    config = mock_run_scheduler.call_args.args[4]
    assert config.max_soc_positive_price == 850
    assert config.max_soc_negative_price == 975
    assert config.min_soc_discharge == 250
    assert config.daily_min_profit == pytest.approx(0.65)
    # kW slider (7.5) -> Wh, schedule.py's own scale (1 hour of kW == kWh == 1000 Wh).
    assert config.max_grid_load == pytest.approx(7500.0)
    # No switch.py entity registered here -- falls back to its default (on).
    assert config.discharge_enabled is True


async def test_schedule_coordinator_reads_discharge_enabled_from_live_switch(
    hass: HomeAssistant, freezer
):
    # discharge_enabled lives on this integration's own `switch` entity
    # (switch.py -- the device-side toggle gating the scheduler's once-per-day
    # deliberate CHARGING_DISCHARGE action), read the same way the SOC-bound
    # `number` entities are.
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    hass.states.async_set(_register_switch(hass, entry, "discharge_enabled"), "off")

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        await coordinator._async_update_data()

    config = mock_run_scheduler.call_args.args[4]
    assert config.discharge_enabled is False


async def test_schedule_coordinator_resolves_return_vat_percentage_from_apply_vat_on_return(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(
        domain=DOMAIN, options={"vat_percentage": 21, "apply_vat_on_return": False}
    )
    entry.add_to_hass(hass)
    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        await coordinator._async_update_data()

    config = mock_run_scheduler.call_args.args[4]
    assert config.vat_percentage == 21
    assert config.return_vat_percentage == 0.0


async def test_schedule_coordinator_uses_peak_load_sensor_when_higher_than_slider(
    hass: HomeAssistant, freezer
):
    # Belgian capaciteitstarief bills on the month's single highest 15-min
    # grid-import peak -- once house load alone has already pushed that
    # peak above the max_grid_load slider this month, charging up to that
    # already-paid-for level costs nothing extra, so the higher of the two
    # (slider vs. this optional external sensor) becomes the effective cap.
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "sensor.piekbelasting_deze_maand"}
    )
    entry.add_to_hass(hass)
    hass.states.async_set(_register_soc_number(hass, entry, "max_grid_load"), "6.0")
    hass.states.async_set("sensor.piekbelasting_deze_maand", "8.5", {"unit_of_measurement": "kW"})

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        await coordinator._async_update_data()

    config = mock_run_scheduler.call_args.args[4]
    # 8.5 kW sensor > 6.0 kW slider -> the sensor's value (in Wh) wins.
    assert config.max_grid_load == pytest.approx(8500.0)


async def test_schedule_coordinator_ignores_peak_load_sensor_when_lower_than_slider(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_PEAK_LOAD_THIS_MONTH_ENTITY: "sensor.piekbelasting_deze_maand"}
    )
    entry.add_to_hass(hass)
    hass.states.async_set(_register_soc_number(hass, entry, "max_grid_load"), "6.0")
    hass.states.async_set("sensor.piekbelasting_deze_maand", "3.0", {"unit_of_measurement": "kW"})

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        await coordinator._async_update_data()

    config = mock_run_scheduler.call_args.args[4]
    # 3.0 kW sensor < 6.0 kW slider -> the slider still applies.
    assert config.max_grid_load == pytest.approx(6000.0)


async def test_schedule_coordinator_ignores_unconfigured_peak_load_sensor(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)
    hass.states.async_set(_register_soc_number(hass, entry, "max_grid_load"), "6.0")

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()
        ) as mock_run_scheduler,
    ):
        await coordinator._async_update_data()

    config = mock_run_scheduler.call_args.args[4]
    assert config.max_grid_load == pytest.approx(6000.0)


async def test_schedule_coordinator_overlays_persisted_dispatch_daily_state(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PERSIST_DAILY_CHARGE_LIMIT: True})
    entry.add_to_hass(hass)

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_dispatch_daily_state",
            MagicMock(return_value=(True, False, 6, -1)),
        ),
        patch("custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()),
    ):
        data = await coordinator._async_update_data()

    assert data["today"].charge_on_grid_used is True
    assert data["today"].discharge_used is False
    assert data["today"].index_charge == 6
    assert data["today"].index_discharge == -1


async def test_schedule_coordinator_ignores_persisted_state_when_option_disabled(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_PERSIST_DAILY_CHARGE_LIMIT: False})
    entry.add_to_hass(hass)

    modbus_coordinator = SimpleNamespace(data={"battery_soc": 55.0})
    prices_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})
    solar_coordinator = SimpleNamespace(data={"today": Day(valid=True), "tomorrow": Day()})

    coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )

    with (
        patch(
            "custom_components.alpha_ess_local.storage.retrieve_mean_data",
            MagicMock(return_value=None),
        ),
        patch(
            "custom_components.alpha_ess_local.orchestrator.storage.retrieve_dispatch_daily_state",
            MagicMock(return_value=(True, False, 6, -1)),
        ) as mock_retrieve_daily_state,
        patch("custom_components.alpha_ess_local.orchestrator.run_scheduler", MagicMock()),
    ):
        data = await coordinator._async_update_data()

    mock_retrieve_daily_state.assert_not_called()
    assert data["today"].charge_on_grid_used is False
    assert data["today"].index_charge == -1


# --- AlphaEssLocalDispatchCoordinator -----------------------------------------


def _fake_client(cur_dispatch_param, cur_feed_in_percentage=0):
    return SimpleNamespace(
        async_get_dispatch_param=AsyncMock(return_value=cur_dispatch_param),
        async_get_max_feed_into_grid=AsyncMock(return_value=cur_feed_in_percentage),
        async_set_dispatch_param=AsyncMock(),
        async_set_max_feed_into_grid=AsyncMock(),
    )


def _fake_modbus_coordinator(client, battery_soc=50.0):
    return SimpleNamespace(data={"battery_soc": battery_soc}, client=client)


def _schedule_day_with_hour(hour_index, **hour_overrides) -> Day:
    day = Day(valid=True)
    hour = day.hour[hour_index]
    hour.valid = True
    hour.cutoff_soc = hour_overrides.pop("cutoff_soc", 900)
    hour.charge = hour_overrides.pop("charge", Charge.CHARGING_ON_PV)
    hour.earning = hour_overrides.pop("earning", Earning.NO_EARNING)
    for key, value in hour_overrides.items():
        setattr(hour, key, value)
    return day


async def test_dispatch_coordinator_control_disabled_never_writes(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000})
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour)
    schedule_coordinator = SimpleNamespace(data={"today": day})

    # Hardware currently reports something different from what will be
    # decided, so this isn't skipped as "already set".
    cur_param = DispatchParam(
        mode=DispatchMode.NO_BATTERY_CHARGE,
        started=True,
        power=0,
        cutoff_soc=0,
        duration=3600,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    decision = await coordinator._async_update_data()

    assert decision.param.mode == DispatchMode.NORMAL
    client.async_set_dispatch_param.assert_not_called()
    client.async_set_max_feed_into_grid.assert_not_called()


async def test_dispatch_coordinator_control_enabled_writes_when_changed(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_USABLE_BATTERY_CAPACITY: 10000, CONF_CONTROL_ENABLED: True},
    )
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour)
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.NO_BATTERY_CHARGE,
        started=True,
        power=0,
        cutoff_soc=0,
        duration=3600,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    decision = await coordinator._async_update_data()

    client.async_set_max_feed_into_grid.assert_awaited_once_with(decision.target_feed_in_percentage)
    client.async_set_dispatch_param.assert_awaited_once_with(decision.param)


async def test_dispatch_coordinator_throttles_charging_on_grid_by_effective_max_grid_load(
    hass: HomeAssistant, freezer
):
    # The live "Max grid load" slider (number.py) must also throttle the
    # actual ~20s dispatch power command for CHARGING_ON_GRID, not just the
    # hourly plan's total Wh -- same capaciteitstarief reasoning as
    # schedule.py's planning-time cap.
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000})
    entry.add_to_hass(hass)
    hass.states.async_set(_register_soc_number(hass, entry, "max_grid_load"), "2.0")

    day = _schedule_day_with_hour(
        current_hour,
        charge=Charge.CHARGING_ON_GRID,
        earning=Earning.EARNING_ON_RETURN,
        cutoff_soc=900,
        estimated_house_load=300,
    )
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.NO_BATTERY_CHARGE,
        started=True,
        power=0,
        cutoff_soc=0,
        duration=3600,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client, battery_soc=50.0)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    decision = await coordinator._async_update_data()

    assert decision.param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    # 2.0 kW slider -> 2000 Wh, minus 300 Wh estimated house load = 1700 W,
    # well under the ~9460 W the battery could otherwise take.
    assert decision.param.power == 1700


async def test_dispatch_coordinator_skips_write_when_unchanged(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_USABLE_BATTERY_CAPACITY: 10000, CONF_CONTROL_ENABLED: True},
    )
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour)
    schedule_coordinator = SimpleNamespace(data={"today": day})

    # Already matches what will be decided: NORMAL, power=9460, cutoff=0,
    # pv_on=True, feed-in 100% (hour.feed_in defaults to True).
    cur_param = DispatchParam(
        mode=DispatchMode.NORMAL,
        started=True,
        power=9460,
        cutoff_soc=0,
        duration=3600,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=100)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    await coordinator._async_update_data()  # first call: hour_start -> always writes
    client.async_set_dispatch_param.reset_mock()
    client.async_set_max_feed_into_grid.reset_mock()

    await coordinator._async_update_data()  # second call, same hour, unchanged -> skip

    client.async_set_dispatch_param.assert_not_called()
    client.async_set_max_feed_into_grid.assert_not_called()


async def test_dispatch_coordinator_provider_override_detected_skips_own_write(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            CONF_ALLOW_PROVIDER_CONTROL_HOURS: [str(current_hour)],
        },
    )
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour)  # index_charge stays -1 by default
    schedule_coordinator = SimpleNamespace(data={"today": day})

    # Hardware reports an active, externally-set State-of-Charge-Control
    # with nonzero (discharging) power we never set ourselves.
    cur_param = DispatchParam(
        mode=DispatchMode.STATE_OF_CHARGE_CONTROL,
        started=True,
        power=-3000,
        cutoff_soc=200,
        duration=1800,
        para7=255,
        pv_on=False,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    await coordinator._async_update_data()

    # Our own decision is never written...
    client.async_set_max_feed_into_grid.assert_not_called()
    # ...but since the provider is discharging (power<0) and control is
    # enabled, PV gets turned on by re-writing the read-back param.
    client.async_set_dispatch_param.assert_awaited_once()
    written = client.async_set_dispatch_param.call_args.args[0]
    assert written.pv_on is True
    assert written.power == -3000
    assert day.hour[current_hour].provider_override[0].valid is True
    assert day.hour[current_hour].provider_override[0].power_setting == -3000


async def test_dispatch_coordinator_provider_charging_makes_no_extra_write(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            CONF_ALLOW_PROVIDER_CONTROL_HOURS: [str(current_hour)],
        },
    )
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour)
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.STATE_OF_CHARGE_CONTROL,
        started=True,
        power=3000,  # positive = provider charging
        cutoff_soc=800,
        duration=1800,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    await coordinator._async_update_data()

    client.async_set_dispatch_param.assert_not_called()
    client.async_set_max_feed_into_grid.assert_not_called()


async def test_dispatch_coordinator_persists_charge_on_grid_used_on_transition_only(
    hass: HomeAssistant, freezer
):
    # schedule.py's DP now maintains charge_on_grid_used itself (including
    # multi-hour rate-capped sessions -- see schedule.py's
    # _calculate_best_schedule); the dispatch coordinator's only remaining
    # job is noticing the False->True transition and persisting it, once.
    freezer.move_to("2024-01-15 06:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000})
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour, charge=Charge.CHARGING_ON_GRID)
    day.charge_on_grid_used = True  # as if schedule.py's DP just completed the session
    day.index_charge = current_hour
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.DEFAULT,
        started=False,
        power=0,
        cutoff_soc=0,
        duration=0,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.store_dispatch_daily_state",
        MagicMock(),
    ) as mock_store:
        await coordinator._async_update_data()
        mock_store.assert_called_once()

        mock_store.reset_mock()
        await coordinator._async_update_data()  # still True, no new transition -> no new persist
        mock_store.assert_not_called()


async def test_dispatch_coordinator_does_not_persist_mid_session(hass: HomeAssistant, freezer):
    # A multi-hour grid-charge session that schedule.py's DP hasn't marked
    # complete yet (charge_on_grid_used still False, per
    # _evaluate_hour_action's rate-capped continuation) must not be persisted
    # as "used" -- there's no False->True transition to react to.
    freezer.move_to("2024-01-15 06:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000})
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour, charge=Charge.CHARGING_ON_GRID)
    day.charge_on_grid_used = False  # session still open
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.DEFAULT,
        started=False,
        power=0,
        cutoff_soc=0,
        duration=0,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.store_dispatch_daily_state",
        MagicMock(),
    ) as mock_store:
        await coordinator._async_update_data()

    mock_store.assert_not_called()


async def test_dispatch_coordinator_does_not_persist_when_option_disabled(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 06:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_USABLE_BATTERY_CAPACITY: 10000, CONF_PERSIST_DAILY_CHARGE_LIMIT: False},
    )
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour, charge=Charge.CHARGING_ON_GRID)
    day.charge_on_grid_used = True
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.DEFAULT,
        started=False,
        power=0,
        cutoff_soc=0,
        duration=0,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    with patch(
        "custom_components.alpha_ess_local.orchestrator.storage.store_dispatch_daily_state",
        MagicMock(),
    ) as mock_store:
        await coordinator._async_update_data()

    mock_store.assert_not_called()


async def test_dispatch_coordinator_uses_safe_default_when_schedule_not_ready(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000})
    entry.add_to_hass(hass)

    schedule_coordinator = SimpleNamespace(data=None)

    cur_param = DispatchParam(
        mode=DispatchMode.DEFAULT,
        started=False,
        power=0,
        cutoff_soc=0,
        duration=0,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    decision = await coordinator._async_update_data()

    # SAFE_DEFAULT_HOUR: CHARGING_ON_PV, cutoff_soc=SOC_MAX(900), NO_EARNING
    # -> below_cutoff at 50% SOC -> NORMAL.
    assert decision.param.mode == DispatchMode.NORMAL


async def test_dispatch_coordinator_manual_override_replaces_schedule_choice(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000})
    entry.add_to_hass(hass)

    day = _schedule_day_with_hour(current_hour, charge=Charge.NO_CHARGING, cutoff_soc=900)
    schedule_coordinator = SimpleNamespace(data={"today": day})

    cur_param = DispatchParam(
        mode=DispatchMode.DEFAULT,
        started=False,
        power=0,
        cutoff_soc=0,
        duration=0,
        para7=255,
        pv_on=True,
    )
    client = _fake_client(cur_param, cur_feed_in_percentage=0)
    modbus_coordinator = _fake_modbus_coordinator(client)

    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    coordinator.set_manual_override(Charge.CHARGING_ON_GRID, 1000)

    decision = await coordinator._async_update_data()

    # CHARGING_ON_GRID, cutoff==SOC_MAX_BATTERY(1000) -> always below cutoff,
    # NO_EARNING -> STATE_OF_CHARGE_CONTROL.
    assert decision.param.mode == DispatchMode.STATE_OF_CHARGE_CONTROL
    # The override must not leak into the scheduler's own Day.
    assert day.hour[current_hour].charge == Charge.NO_CHARGING

    coordinator.set_manual_override(None)
    decision = await coordinator._async_update_data()

    # Back to the schedule's own choice: NO_CHARGING, NO_EARNING -> NO_BATTERY_CHARGE.
    assert decision.param.mode == DispatchMode.NO_BATTERY_CHARGE


# --- AlphaEssLocalDispatchCoordinator: extra-PV Modbus control ---------------

_EXTRA_PV_OPTIONS = {
    CONF_EXTRA_PV_CONTROL_ENABLED: True,
    CONF_EXTRA_PV_MODBUS_HUB: "sma_tripower",
    CONF_EXTRA_PV_MODBUS_SLAVE: 3,
    CONF_EXTRA_PV_MODBUS_ADDRESS: 40016,
    CONF_EXTRA_PV_MODBUS_ON_VALUE: 100,
    CONF_EXTRA_PV_MODBUS_OFF_VALUE: 0,
}


async def test_extra_pv_not_configured_never_calls_modbus_service(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_USABLE_BATTERY_CAPACITY: 10000, CONF_CONTROL_ENABLED: True}
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = _schedule_day_with_hour(current_hour, earning=Earning.EARNING_ON_USE)
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.DEFAULT,
            started=False,
            power=0,
            cutoff_soc=0,
            duration=0,
            para7=255,
            pv_on=True,
        )
    )
    modbus_coordinator = _fake_modbus_coordinator(client)
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()

    assert calls == []


async def test_extra_pv_control_disabled_logs_but_never_writes(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_USABLE_BATTERY_CAPACITY: 10000, **_EXTRA_PV_OPTIONS},  # control_enabled off
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = _schedule_day_with_hour(current_hour, earning=Earning.EARNING_ON_USE)
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.DEFAULT,
            started=False,
            power=0,
            cutoff_soc=0,
            duration=0,
            para7=255,
            pv_on=True,
        )
    )
    modbus_coordinator = _fake_modbus_coordinator(client)
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()

    assert calls == []


async def test_extra_pv_feature_toggle_off_never_calls_modbus_service(hass: HomeAssistant, freezer):
    # Hub/register fully configured and the master control_enabled is on,
    # but the extra-PV feature's own dedicated toggle is off -> still a
    # no-op. This is the whole point of having a separate toggle: the
    # hub/register config can sit there configured-but-paused.
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            **{**_EXTRA_PV_OPTIONS, CONF_EXTRA_PV_CONTROL_ENABLED: False},
        },
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = _schedule_day_with_hour(current_hour, earning=Earning.EARNING_ON_USE)
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.DEFAULT,
            started=False,
            power=0,
            cutoff_soc=0,
            duration=0,
            para7=255,
            pv_on=True,
        )
    )
    modbus_coordinator = _fake_modbus_coordinator(client)
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()

    assert calls == []


async def test_extra_pv_control_enabled_writes_off_on_negative_price_when_battery_full(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            **_EXTRA_PV_OPTIONS,
        },
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = _schedule_day_with_hour(current_hour, earning=Earning.EARNING_ON_USE, cutoff_soc=900)
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.DEFAULT,
            started=False,
            power=0,
            cutoff_soc=0,
            duration=0,
            para7=255,
            pv_on=True,
        )
    )
    # 95% >= this hour's 90% cutoff -> battery "full" -> curtail, matching
    # decide_extra_pv_state's deliberate deviation from the original C
    # (which curtailed unconditionally).
    modbus_coordinator = _fake_modbus_coordinator(client, battery_soc=95.0)
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()

    assert len(calls) == 1
    assert calls[0].data == {"hub": "sma_tripower", "slave": 3, "address": 40016, "value": 0}


async def test_extra_pv_stays_on_on_negative_price_when_battery_not_full(
    hass: HomeAssistant, freezer
):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            **_EXTRA_PV_OPTIONS,
        },
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = _schedule_day_with_hour(current_hour, earning=Earning.EARNING_ON_USE, cutoff_soc=900)
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.DEFAULT,
            started=False,
            power=0,
            cutoff_soc=0,
            duration=0,
            para7=255,
            pv_on=True,
        )
    )
    # 50% < this hour's 90% cutoff -> battery still has room -> stays on
    # despite the negative price, so it can charge for free instead of
    # being curtailed for no reason.
    modbus_coordinator = _fake_modbus_coordinator(client, battery_soc=50.0)
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()

    assert len(calls) == 1
    assert calls[0].data == {"hub": "sma_tripower", "slave": 3, "address": 40016, "value": 100}


async def test_extra_pv_writes_on_when_no_earning(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            **_EXTRA_PV_OPTIONS,
        },
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = _schedule_day_with_hour(current_hour, earning=Earning.NO_EARNING)
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.DEFAULT,
            started=False,
            power=0,
            cutoff_soc=0,
            duration=0,
            para7=255,
            pv_on=True,
        )
    )
    modbus_coordinator = _fake_modbus_coordinator(client)
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()

    assert len(calls) == 1
    assert calls[0].data == {"hub": "sma_tripower", "slave": 3, "address": 40016, "value": 100}


async def test_extra_pv_skips_write_when_state_unchanged(hass: HomeAssistant, freezer):
    freezer.move_to("2024-01-15 14:00:00")
    current_hour = dt_util.now().hour
    next_hour = (current_hour + 1) % 24
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_USABLE_BATTERY_CAPACITY: 10000,
            CONF_CONTROL_ENABLED: True,
            **_EXTRA_PV_OPTIONS,
        },
    )
    entry.add_to_hass(hass)
    calls = async_mock_service(hass, "modbus", "write_register")

    day = Day(valid=True)
    for h in (current_hour, next_hour):
        day.hour[h].valid = True
        day.hour[h].earning = Earning.EARNING_ON_USE
        day.hour[h].cutoff_soc = 900
        day.hour[h].charge = Charge.CHARGING_ON_PV
    schedule_coordinator = SimpleNamespace(data={"today": day})
    client = _fake_client(
        DispatchParam(
            mode=DispatchMode.NO_BATTERY_CHARGE,
            started=True,
            power=0,
            cutoff_soc=0,
            duration=3600,
            para7=255,
            pv_on=False,
        ),
        cur_feed_in_percentage=0,
    )
    modbus_coordinator = _fake_modbus_coordinator(client, battery_soc=95.0)  # above cutoff -> full
    coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )

    await coordinator._async_update_data()  # first hour: battery full -> writes OFF
    assert len(calls) == 1

    freezer.move_to("2024-01-15 14:05:00")  # same hour, same earning -> no new write
    await coordinator._async_update_data()

    assert len(calls) == 1


# --- rollover handlers --------------------------------------------------------


async def test_async_handle_hourly_rollover_requests_schedule_refresh():
    schedule_coordinator = SimpleNamespace(async_request_refresh=AsyncMock())

    await async_handle_hourly_rollover(schedule_coordinator, None)

    schedule_coordinator.async_request_refresh.assert_awaited_once()


async def test_async_handle_daily_rollover_recomputes_means_and_refreshes(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={"pv_power": 5000})
    entry.add_to_hass(hass)
    solar_coordinator = SimpleNamespace(async_request_refresh=AsyncMock())
    schedule_coordinator = SimpleNamespace(async_request_refresh=AsyncMock())

    with patch(
        "custom_components.alpha_ess_local.storage.calculate_and_store_mean_data",
        MagicMock(return_value=True),
    ) as mock_calc:
        await async_handle_daily_rollover(
            hass, entry, solar_coordinator, schedule_coordinator, None
        )

    mock_calc.assert_called_once()
    assert mock_calc.call_args.args[1] == 5000
    solar_coordinator.async_request_refresh.assert_awaited_once()
    schedule_coordinator.async_request_refresh.assert_awaited_once()


class _FakeListenerCoordinator:
    """Minimal stand-in for a DataUpdateCoordinator's listener wiring.

    `watch_for_source_recovery` only touches `.data`, `.hass`, and
    `.async_add_listener`, so a real `DataUpdateCoordinator` is unnecessary
    (and, on current HA, requires a config entry to construct without
    tripping a ContextVar usage warning).
    """

    def __init__(self, hass: HomeAssistant, data: dict) -> None:
        self.hass = hass
        self.data = data
        self._listeners: list = []

    def async_add_listener(self, callback):
        self._listeners.append(callback)

        def _remove() -> None:
            self._listeners.remove(callback)

        return _remove

    def fire(self) -> None:
        for callback in list(self._listeners):
            callback()


async def test_watch_for_source_recovery_refreshes_schedule_on_valid_transition(
    hass: HomeAssistant,
):
    source_coordinator = _FakeListenerCoordinator(hass, {"today": Day(valid=False)})
    schedule_coordinator = SimpleNamespace(async_request_refresh=AsyncMock())

    unsub = watch_for_source_recovery(source_coordinator, schedule_coordinator)

    # Still invalid -> a regular refresh must not trigger a recompute.
    source_coordinator.fire()
    await hass.async_block_till_done()
    schedule_coordinator.async_request_refresh.assert_not_awaited()

    # False -> True transition -> triggers exactly once.
    source_coordinator.data = {"today": Day(valid=True)}
    source_coordinator.fire()
    await hass.async_block_till_done()
    schedule_coordinator.async_request_refresh.assert_awaited_once()

    # Further refreshes while still valid must not trigger again.
    source_coordinator.fire()
    await hass.async_block_till_done()
    schedule_coordinator.async_request_refresh.assert_awaited_once()

    unsub()


async def test_watch_for_source_recovery_unsub_stops_further_refreshes(hass: HomeAssistant):
    source_coordinator = _FakeListenerCoordinator(hass, {"today": Day(valid=False)})
    schedule_coordinator = SimpleNamespace(async_request_refresh=AsyncMock())

    unsub = watch_for_source_recovery(source_coordinator, schedule_coordinator)
    unsub()

    source_coordinator.data = {"today": Day(valid=True)}
    source_coordinator.fire()
    await hass.async_block_till_done()

    schedule_coordinator.async_request_refresh.assert_not_awaited()


# --- house-load resolution (HomeWizard P1 / DSMR) ----------------------------


def test_entity_power_converts_kilowatt_to_watt(hass: HomeAssistant):
    hass.states.async_set("sensor.dsmr_usage", "1.5", {"unit_of_measurement": "kW"})
    assert _entity_power(hass, "sensor.dsmr_usage") == 1500.0


def test_entity_power_leaves_watts_unchanged(hass: HomeAssistant):
    hass.states.async_set("sensor.p1_power", "250", {"unit_of_measurement": "W"})
    assert _entity_power(hass, "sensor.p1_power") == 250.0


def test_entity_power_treats_sma_s32_nan_as_zero(hass: HomeAssistant):
    # SMA's Modbus profile reports 0x80000000 (-2147483648) for a signed
    # 32-bit register with no measurement -- e.g. an AC-power register while
    # the inverter is asleep at night. HA's core `modbus` integration passes
    # that raw value straight through rather than reporting unavailable.
    hass.states.async_set("sensor.sma_ac_vermogen", "-2147483648", {"unit_of_measurement": "W"})
    assert _entity_power(hass, "sensor.sma_ac_vermogen") == 0.0


# --- _number_entity_value ------------------------------------------------------


def _register_soc_number(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Register a fake number.py-style entity for `entry`/`key` and return its entity_id."""
    entity_entry = er.async_get(hass).async_get_or_create(
        "number", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
    )
    return entity_entry.entity_id


def test_number_entity_value_reads_live_value(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register_soc_number(hass, entry, "max_soc_positive_price")
    hass.states.async_set(entity_id, "85.0")

    assert _number_entity_value(hass, entry, "max_soc_positive_price", 90.0) == 85.0


def test_number_entity_value_falls_back_when_entity_not_registered_yet(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    assert _number_entity_value(hass, entry, "max_soc_positive_price", 90.0) == 90.0


def test_number_entity_value_falls_back_when_unavailable(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register_soc_number(hass, entry, "max_soc_positive_price")
    hass.states.async_set(entity_id, "unavailable")

    assert _number_entity_value(hass, entry, "max_soc_positive_price", 90.0) == 90.0


def test_number_entity_value_falls_back_on_non_numeric_state(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register_soc_number(hass, entry, "max_soc_positive_price")
    hass.states.async_set(entity_id, "not-a-number")

    assert _number_entity_value(hass, entry, "max_soc_positive_price", 90.0) == 90.0


# --- _switch_entity_value ------------------------------------------------------


def _register_switch(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Register a fake switch.py-style entity for `entry`/`key` and return its entity_id."""
    entity_entry = er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry
    )
    return entity_entry.entity_id


def test_switch_entity_value_reads_live_value(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register_switch(hass, entry, "discharge_enabled")
    hass.states.async_set(entity_id, "off")

    assert _switch_entity_value(hass, entry, "discharge_enabled", True) is False


def test_switch_entity_value_falls_back_when_entity_not_registered_yet(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    assert _switch_entity_value(hass, entry, "discharge_enabled", True) is True


def test_switch_entity_value_falls_back_when_unavailable(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register_switch(hass, entry, "discharge_enabled")
    hass.states.async_set(entity_id, "unavailable")

    assert _switch_entity_value(hass, entry, "discharge_enabled", True) is True


def test_dsmr_net_power_subtracts_delivery_from_usage(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    usage = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_usage", config_entry=config_entry
    )
    delivery = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_delivery", config_entry=config_entry
    )
    hass.states.async_set(usage.entity_id, "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set(delivery.entity_id, "0.1", {"unit_of_measurement": "kW"})

    assert _dsmr_net_power(hass, config_entry.entry_id) == 400.0  # 500W use - 100W feed-in


def test_dsmr_net_power_none_when_siblings_missing(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)

    assert _dsmr_net_power(hass, config_entry.entry_id) is None


def test_house_load_power_resolves_plain_entity_id(hass: HomeAssistant):
    hass.states.async_set("sensor.p1_power", "250", {"unit_of_measurement": "W"})
    assert _house_load_power(hass, "sensor.p1_power") == 250.0


def test_house_load_power_resolves_dsmr_reference(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    usage = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_usage", config_entry=config_entry
    )
    delivery = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_current_electricity_delivery", config_entry=config_entry
    )
    hass.states.async_set(usage.entity_id, "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set(delivery.entity_id, "0.0", {"unit_of_measurement": "kW"})

    assert _house_load_power(hass, f"dsmr:{config_entry.entry_id}") == 500.0


def test_house_load_power_none_when_unset():
    assert _house_load_power(None, None) is None


def test_dsmr_tariff_indicator_reads_active_tariff_state(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    tariff = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_electricity_active_tariff", config_entry=config_entry
    )
    hass.states.async_set(tariff.entity_id, "low")

    assert _dsmr_tariff_indicator(hass, config_entry.entry_id) == "low"


def test_dsmr_tariff_indicator_none_when_unavailable(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    tariff = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_electricity_active_tariff", config_entry=config_entry
    )
    hass.states.async_set(tariff.entity_id, "unavailable")

    assert _dsmr_tariff_indicator(hass, config_entry.entry_id) is None


def test_dsmr_tariff_indicator_none_when_sibling_missing(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)

    assert _dsmr_tariff_indicator(hass, config_entry.entry_id) is None


def test_house_load_tariff_resolves_dsmr_reference(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="dsmr", title="Slimme meter")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    tariff = registry.async_get_or_create(
        "sensor", "dsmr", "serial123_electricity_active_tariff", config_entry=config_entry
    )
    hass.states.async_set(tariff.entity_id, "normal")

    assert _house_load_tariff(hass, f"dsmr:{config_entry.entry_id}") == "normal"


def test_house_load_tariff_none_for_plain_entity_id(hass: HomeAssistant):
    # HomeWizard P1/plain-entity house-load sources have no known
    # tariff-indicator convention here -- explicitly out of scope.
    assert _house_load_tariff(hass, "sensor.p1_power") is None


def test_house_load_tariff_none_when_unset():
    assert _house_load_tariff(None, None) is None


def test_effective_use_fee_adds_normal_network_fee_for_normal_tariff():
    assert _effective_use_fee(0.02, "normal", 0.05, 0.03) == pytest.approx(0.07)


def test_effective_use_fee_adds_low_network_fee_for_low_tariff():
    assert _effective_use_fee(0.02, "low", 0.05, 0.03) == pytest.approx(0.05)


def test_effective_use_fee_adds_nothing_when_tariff_unknown():
    assert _effective_use_fee(0.02, None, 0.05, 0.03) == pytest.approx(0.02)


# --- extra-PV resolution (SMA Solar) -----------------------------------------


def test_sma_pv_power_sums_string_entities(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    string_a = registry.async_get_or_create(
        "sensor",
        "sma",
        f"{config_entry.entry_id}-{SMA_PV_POWER_STRING_KEY}_0",
        config_entry=config_entry,
    )
    string_b = registry.async_get_or_create(
        "sensor",
        "sma",
        f"{config_entry.entry_id}-{SMA_PV_POWER_STRING_KEY}_1",
        config_entry=config_entry,
    )
    hass.states.async_set(string_a.entity_id, "300", {"unit_of_measurement": "W"})
    hass.states.async_set(string_b.entity_id, "450", {"unit_of_measurement": "W"})

    assert _sma_pv_power(hass, config_entry.entry_id) == 750.0


def test_sma_pv_power_none_when_no_strings(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)

    assert _sma_pv_power(hass, config_entry.entry_id) is None


def test_extra_pv_power_resolves_plain_entity_id(hass: HomeAssistant):
    hass.states.async_set("sensor.pv_power", "500", {"unit_of_measurement": "W"})
    assert _extra_pv_power(hass, "sensor.pv_power") == 500.0


def test_extra_pv_power_resolves_sma_reference(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain="sma", title="SMA Sunny Boy")
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    string_a = registry.async_get_or_create(
        "sensor",
        "sma",
        f"{config_entry.entry_id}-{SMA_PV_POWER_STRING_KEY}_0",
        config_entry=config_entry,
    )
    hass.states.async_set(string_a.entity_id, "300", {"unit_of_measurement": "W"})

    assert _extra_pv_power(hass, f"sma:{config_entry.entry_id}") == 300.0


def test_extra_pv_power_none_when_unset():
    assert _extra_pv_power(None, None) is None
