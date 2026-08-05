"""DataUpdateCoordinators for AlphaESS Local Control."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import storage
from .api import (
    AlphaEssLocalApiClient,
    AlphaEssLocalApiClientAuthenticationError,
    AlphaEssLocalApiClientError,
)
from .const import (
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_SOLCAST_ENTRIES,
    CONF_VAT_PERCENTAGE,
    DEFAULT_FORECAST_SOLAR_ENTRIES,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_SOLCAST_ENTRIES,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
    SCAN_INTERVAL,
)
from .data import Day
from .orchestrator import get_db_path, hour_correction_from_mean
from .prices import build_day as build_price_day
from .solar import build_day as build_solar_day

DAY_DATA_SCAN_INTERVAL = timedelta(minutes=30)

# Short, bounded backoff for a specific case: our first refresh raced a
# *source* integration's own startup (ENTSO-e/Frank Energie/Forecast.Solar/
# Solcast can take several seconds to load, since they fetch real data
# during their own async_setup_entry — confirmed live: Frank Energie's own
# setup took ~14s here) and lost, coming back with "nothing found" even
# though a source *is* configured. update_interval (30 min) is far too slow
# to recover from a one-off startup race — this retries a few times, quickly,
# then gives up and falls back to the normal interval.
_STARTUP_RETRY_DELAYS_SECONDS = (10, 20, 30)


class _StartupRaceRetryHelper:
    """See `_STARTUP_RETRY_DELAYS_SECONDS` above for the case this covers."""

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        self._coordinator = coordinator
        self._attempt = 0
        self._unsub: callable | None = None

    def note_result(self, *, configured: bool, found_data: bool) -> None:
        """Call after building this refresh's result.

        `configured`: whether the user actually set up a source at all (no
        retry if not — that's a normal, permanent state, not a race).
        `found_data`: whether this refresh actually found usable data.
        """
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

        if found_data or not configured:
            self._attempt = 0
            return

        if self._attempt >= len(_STARTUP_RETRY_DELAYS_SECONDS):
            return

        delay = _STARTUP_RETRY_DELAYS_SECONDS[self._attempt]
        self._attempt += 1

        async def _retry(_now) -> None:
            await self._coordinator.async_request_refresh()

        self._unsub = async_call_later(self._coordinator.hass, delay, _retry)


class AlphaEssLocalDataUpdateCoordinator(DataUpdateCoordinator):
    """Polls the AlphaESS device on a fixed interval."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AlphaEssLocalApiClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            return await self.client.async_get_data()
        except AlphaEssLocalApiClientAuthenticationError as exception:
            raise UpdateFailed(exception) from exception
        except AlphaEssLocalApiClientError as exception:
            raise UpdateFailed(exception) from exception


class AlphaEssLocalPricesCoordinator(DataUpdateCoordinator[dict[str, Day]]):
    """Derives today's/tomorrow's prices from other HA integrations' entities.

    No network I/O of our own — just reads already-cached state from the
    entities configured via entsoe_price_entity/frank_energie_price_entity,
    so a 30-minute interval is cheap and just keeps day-rollover / newly
    published day-ahead prices picked up promptly.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_prices",
            update_interval=DAY_DATA_SCAN_INTERVAL,
        )
        self._retry_helper = _StartupRaceRetryHelper(self)

    async def _async_update_data(self) -> dict[str, Day]:
        """Rebuild today's and tomorrow's prices from the configured entities."""
        options = self.config_entry.options
        entsoe_entity_id = options.get(CONF_ENTSOE_PRICE_ENTITY)
        frank_energie_entity_id = options.get(CONF_FRANK_ENERGIE_PRICE_ENTITY)
        use_fee = options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE)
        return_fee = options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE)
        vat_percentage = options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE)

        today = dt_util.now().date()
        tomorrow = today + timedelta(days=1)

        today_day = build_price_day(
            self.hass,
            today,
            entsoe_entity_id,
            frank_energie_entity_id,
            use_fee,
            return_fee,
            vat_percentage,
        )
        tomorrow_day = build_price_day(
            self.hass,
            tomorrow,
            entsoe_entity_id,
            frank_energie_entity_id,
            use_fee,
            return_fee,
            vat_percentage,
        )

        self._retry_helper.note_result(
            configured=bool(entsoe_entity_id or frank_energie_entity_id),
            found_data=today_day.valid,
        )

        return {"today": today_day, "tomorrow": tomorrow_day}


class AlphaEssLocalSolarCoordinator(DataUpdateCoordinator[dict[str, Day]]):
    """Derives today's/tomorrow's solar forecast from other HA integrations.

    No network I/O of our own — reads Forecast.Solar's/Solcast's already
    computed forecast via HA's "energy platform" hook, so a 30-minute
    interval is cheap and just keeps things current.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_solar",
            update_interval=DAY_DATA_SCAN_INTERVAL,
        )
        self._retry_helper = _StartupRaceRetryHelper(self)

    async def _async_update_data(self) -> dict[str, Day]:
        """Rebuild today's and tomorrow's solar forecast from the configured entries."""
        options = self.config_entry.options
        forecast_solar_entry_ids = options.get(
            CONF_FORECAST_SOLAR_ENTRIES, DEFAULT_FORECAST_SOLAR_ENTRIES
        )
        solcast_entry_ids = options.get(CONF_SOLCAST_ENTRIES, DEFAULT_SOLCAST_ENTRIES)

        today = dt_util.now().date()
        tomorrow = today + timedelta(days=1)

        db_path = get_db_path(self.hass, self.config_entry)
        mean = await self.hass.async_add_executor_job(
            storage.retrieve_mean_data, db_path, today.month, storage.c_weekday(today)
        )
        hour_correction = hour_correction_from_mean(mean)

        today_day = await build_solar_day(
            self.hass, today, forecast_solar_entry_ids, solcast_entry_ids, hour_correction
        )
        tomorrow_day = await build_solar_day(
            self.hass, tomorrow, forecast_solar_entry_ids, solcast_entry_ids, hour_correction
        )

        self._retry_helper.note_result(
            configured=bool(forecast_solar_entry_ids or solcast_entry_ids),
            found_data=today_day.valid,
        )

        return {"today": today_day, "tomorrow": tomorrow_day}
