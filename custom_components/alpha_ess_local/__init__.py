"""The AlphaESSControl integration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.event import async_track_time_change

from .api import AlphaEssLocalApiClient
from .const import DOMAIN
from .coordinator import (
    AlphaEssLocalDataUpdateCoordinator,
    AlphaEssLocalPricesCoordinator,
    AlphaEssLocalSolarCoordinator,
)
from .data import SOC_MAX, SOC_MAX_CHARGE_ON_GRID, SOC_MIN_DISCHARGE_AFTER, Charge
from .orchestrator import (
    AlphaEssLocalDispatchCoordinator,
    AlphaEssLocalRealDataCoordinator,
    AlphaEssLocalScheduleCoordinator,
    async_handle_daily_rollover,
    async_handle_hourly_rollover,
    migrate_legacy_db_if_needed,
)

PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

# Manual dispatch-override services — port of ReadConfigMinute's C/D/P/N
# minute-config commands. Applied to every loaded config entry (this
# integration targets a single inverter in practice, matching the
# original's single-instance control loop), stay active until explicitly
# cleared (not one-shot).
SERVICE_FORCE_CHARGE_GRID = "force_charge_grid"
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_FORCE_CHARGE_PV = "force_charge_pv"
SERVICE_FORCE_NO_CHARGING = "force_no_charging"
SERVICE_CLEAR_MANUAL_OVERRIDE = "clear_manual_override"

_MANUAL_OVERRIDES: dict[str, tuple[Charge, int]] = {
    SERVICE_FORCE_CHARGE_GRID: (Charge.CHARGING_ON_GRID, SOC_MAX_CHARGE_ON_GRID),
    SERVICE_FORCE_DISCHARGE: (Charge.CHARGING_DISCHARGE, SOC_MIN_DISCHARGE_AFTER),
    SERVICE_FORCE_CHARGE_PV: (Charge.CHARGING_ON_PV, SOC_MAX),
    SERVICE_FORCE_NO_CHARGING: (Charge.NO_CHARGING, SOC_MAX),
}


@dataclass
class AlphaEssLocalRuntimeData:
    """Runtime state shared across platforms for one config entry."""

    client: AlphaEssLocalApiClient
    modbus_coordinator: AlphaEssLocalDataUpdateCoordinator
    prices_coordinator: AlphaEssLocalPricesCoordinator
    solar_coordinator: AlphaEssLocalSolarCoordinator
    real_data_coordinator: AlphaEssLocalRealDataCoordinator
    schedule_coordinator: AlphaEssLocalScheduleCoordinator
    dispatch_coordinator: AlphaEssLocalDispatchCoordinator


type AlphaEssLocalConfigEntry = ConfigEntry[AlphaEssLocalRuntimeData]


def _async_register_dispatch_services(hass: HomeAssistant) -> None:
    """Register the manual dispatch-override services, once per HA instance."""
    if hass.services.has_service(DOMAIN, SERVICE_CLEAR_MANUAL_OVERRIDE):
        return

    async def _handle_override(call: ServiceCall) -> None:
        charge, cutoff_soc = _MANUAL_OVERRIDES[call.service]
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.runtime_data is not None:
                entry.runtime_data.dispatch_coordinator.set_manual_override(charge, cutoff_soc)

    async def _handle_clear(_call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.runtime_data is not None:
                entry.runtime_data.dispatch_coordinator.set_manual_override(None)

    for service in _MANUAL_OVERRIDES:
        hass.services.async_register(DOMAIN, service, _handle_override)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_MANUAL_OVERRIDE, _handle_clear)


async def async_setup_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """Set up AlphaESSControl from a config entry."""
    await hass.async_add_executor_job(migrate_legacy_db_if_needed, hass, entry)

    client = AlphaEssLocalApiClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
    )

    modbus_coordinator = AlphaEssLocalDataUpdateCoordinator(hass, entry, client)
    await modbus_coordinator.async_config_entry_first_refresh()

    prices_coordinator = AlphaEssLocalPricesCoordinator(hass, entry)
    await prices_coordinator.async_config_entry_first_refresh()

    solar_coordinator = AlphaEssLocalSolarCoordinator(hass, entry)
    await solar_coordinator.async_config_entry_first_refresh()

    real_data_coordinator = AlphaEssLocalRealDataCoordinator(hass, entry, modbus_coordinator)
    await real_data_coordinator.async_config_entry_first_refresh()

    schedule_coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )
    await schedule_coordinator.async_config_entry_first_refresh()

    dispatch_coordinator = AlphaEssLocalDispatchCoordinator(
        hass, entry, modbus_coordinator, schedule_coordinator
    )
    await dispatch_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = AlphaEssLocalRuntimeData(
        client=client,
        modbus_coordinator=modbus_coordinator,
        prices_coordinator=prices_coordinator,
        solar_coordinator=solar_coordinator,
        real_data_coordinator=real_data_coordinator,
        schedule_coordinator=schedule_coordinator,
        dispatch_coordinator=dispatch_coordinator,
    )

    _async_register_dispatch_services(hass)

    entry.async_on_unload(
        async_track_time_change(
            hass,
            partial(async_handle_hourly_rollover, schedule_coordinator),
            minute=0,
            second=0,
        )
    )
    entry.async_on_unload(
        async_track_time_change(
            hass,
            partial(
                async_handle_daily_rollover,
                hass,
                entry,
                solar_coordinator,
                schedule_coordinator,
            ),
            hour=0,
            minute=0,
            second=0,
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.async_close()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
