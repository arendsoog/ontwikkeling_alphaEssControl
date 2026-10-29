"""The AlphaESSControl integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change

from .api import AlphaEssLocalApiClient, AlphaEssLocalApiClientError
from .const import (
    CONF_CONTROL_ENABLED,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    DOMAIN,
    SUBENTRY_CONSUMER,
    SUBENTRY_EV_CHARGER,
    SUBENTRY_PV_INSTALLATION,
)
from .consumers import consumers_from_options, ev_chargers_from_options
from .coordinator import (
    AlphaEssLocalDataUpdateCoordinator,
    AlphaEssLocalPricesCoordinator,
    AlphaEssLocalSolarCoordinator,
)
from .data import SOC_MAX, SOC_MAX_CHARGE_ON_GRID, SOC_MIN_DISCHARGE_AFTER, Charge
from .extra_pv import installation_from_options
from .frontend import async_register_panel, async_unregister_panel
from .orchestrator import (
    AlphaEssLocalDispatchCoordinator,
    AlphaEssLocalRealDataCoordinator,
    AlphaEssLocalScheduleCoordinator,
    async_handle_daily_rollover,
    async_handle_hourly_rollover,
    migrate_legacy_db_if_needed,
    watch_for_source_recovery,
)

PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

# Options the coordinators read from entry.options on every cycle, so a
# change to only these takes effect without a reload. Reloading would drop
# all entities for several seconds -- and blank the panel that toggled them.
LIVE_OPTIONS = frozenset(
    {CONF_CONTROL_ENABLED, CONF_EXTRA_PV_CONTROL_ENABLED, CONF_PERSIST_DAILY_CHARGE_LIMIT}
)

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
    # The options and subentries this entry was (re)loaded with, so the
    # update listener can tell a live-read option change from one that
    # needs a reload.
    options: dict[str, Any] = field(default_factory=dict)
    subentries: dict[str, Any] = field(default_factory=dict)


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


async def _async_register_device(
    hass: HomeAssistant, entry: AlphaEssLocalConfigEntry, client: AlphaEssLocalApiClient
) -> None:
    """Create the device up front with its serial number, read once from the
    inverter -- it doesn't change, so it isn't polled. A failed read only
    leaves the serial number out."""
    try:
        serial_number = await client.async_get_serial_number() or None
    except AlphaEssLocalApiClientError:
        serial_number = None
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="AlphaESS",
        manufacturer="AlphaESS",
        serial_number=serial_number,
    )


async def async_setup_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """Set up AlphaESSControl from a config entry."""
    await hass.async_add_executor_job(migrate_legacy_db_if_needed, hass, entry)

    client = AlphaEssLocalApiClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
    )

    modbus_coordinator = AlphaEssLocalDataUpdateCoordinator(hass, entry, client)
    await modbus_coordinator.async_config_entry_first_refresh()
    await _async_register_device(hass, entry, client)

    prices_coordinator = AlphaEssLocalPricesCoordinator(hass, entry)
    await prices_coordinator.async_config_entry_first_refresh()

    solar_coordinator = AlphaEssLocalSolarCoordinator(hass, entry)
    await solar_coordinator.async_config_entry_first_refresh()

    real_data_coordinator = AlphaEssLocalRealDataCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator
    )
    await real_data_coordinator.async_config_entry_first_refresh()

    schedule_coordinator = AlphaEssLocalScheduleCoordinator(
        hass, entry, modbus_coordinator, prices_coordinator, solar_coordinator
    )
    await schedule_coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(watch_for_source_recovery(prices_coordinator, schedule_coordinator))
    entry.async_on_unload(watch_for_source_recovery(solar_coordinator, schedule_coordinator))

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
        options=dict(entry.options),
        subentries=_subentries_snapshot(entry),
    )

    _async_register_dispatch_services(hass)
    await async_register_panel(hass)

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


async def async_migrate_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """1.1 -> 1.2: the PV installation in the options becomes a subentry.
    1.2 -> 1.3: so does each EV charger; 1.3 -> 1.4: and each other consumer."""
    if entry.version > 1:
        return False
    dutch = hass.config.language.startswith("nl")
    if entry.minor_version < 2:
        installation, options = installation_from_options(hass, entry.options)
        if installation is not None:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=installation,
                    subentry_type=SUBENTRY_PV_INSTALLATION,
                    title="PV-installatie" if dutch else "PV installation",
                    unique_id=None,
                ),
            )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    if entry.minor_version < 3:
        chargers, options = ev_chargers_from_options(
            hass, entry.options, "EV-lader" if dutch else "EV charger"
        )
        for title, data in chargers:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=data, subentry_type=SUBENTRY_EV_CHARGER, title=title, unique_id=None
                ),
            )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=3)
    if entry.minor_version < 4:
        consumers, options = consumers_from_options(hass, entry.options)
        for title, data in consumers:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=data, subentry_type=SUBENTRY_CONSUMER, title=title, unique_id=None
                ),
            )
        hass.config_entries.async_update_entry(entry, options=options, minor_version=4)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> None:
    """Remove the sidebar panel once the last entry is deleted.

    Done here rather than on unload: every options change reloads the
    entry, and removing the panel then would yank it out from under an
    open panel page (e.g. right after toggling an option from it).
    """
    if not any(
        other.entry_id != entry.entry_id for other in hass.config_entries.async_entries(DOMAIN)
    ):
        async_unregister_panel(hass)


def _subentries_snapshot(entry: AlphaEssLocalConfigEntry) -> dict[str, Any]:
    return {
        subentry_id: (subentry.title, dict(subentry.data))
        for subentry_id, subentry in entry.subentries.items()
    }


async def async_reload_entry(hass: HomeAssistant, entry: AlphaEssLocalConfigEntry) -> None:
    """Reload the config entry when options or subentries (PV installations)
    change -- unless only live options did."""
    previous = entry.runtime_data.options
    changed = {
        key
        for key in previous.keys() | entry.options.keys()
        if previous.get(key) != entry.options.get(key)
    }
    if changed <= LIVE_OPTIONS and entry.runtime_data.subentries == _subentries_snapshot(entry):
        entry.runtime_data.options = dict(entry.options)
        return
    await hass.config_entries.async_reload(entry.entry_id)
