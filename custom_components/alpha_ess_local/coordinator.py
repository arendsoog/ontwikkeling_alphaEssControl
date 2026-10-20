"""DataUpdateCoordinators for AlphaESSControl."""

from __future__ import annotations

from datetime import date, datetime, timedelta

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
    CONF_APPLY_VAT_ON_RETURN,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_PRICE_SOURCE_PRIMARY,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_SOLAR_SOURCE_PRIMARY,
    CONF_SOLCAST_ENTRIES,
    CONF_VAT_PERCENTAGE,
    DEFAULT_APPLY_VAT_ON_RETURN,
    DEFAULT_FORECAST_SOLAR_ENTRIES,
    DEFAULT_PRICE_SOURCE_PRIMARY,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_SOLAR_SOURCE_PRIMARY,
    DEFAULT_SOLCAST_ENTRIES,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
    SCAN_INTERVAL,
)
from .data import Day
from .orchestrator import get_db_path, hour_correction_from_mean
from .prices import apply_earning_classification
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
_STARTUP_RETRY_DELAYS_SECONDS = (10, 20, 30, 60, 120, 300)


class _StartupRaceRetryHelper:
    """See `_STARTUP_RETRY_DELAYS_SECONDS` above for the case this covers."""

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        self._coordinator = coordinator
        self._attempt = 0
        self._unsub: callable | None = None
        self._missing_logged = False

    def note_result(self, *, configured: bool, found_data: bool, what: str = "data") -> None:
        """Call after building this refresh's result.

        `configured`: whether the user actually set up a source at all (no
        retry if not — that's a normal, permanent state, not a race).
        `found_data`: whether this refresh actually found usable data.
        """
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

        if found_data or not configured:
            if self._missing_logged and found_data:
                LOGGER.info("%s for today available again", what)
            self._missing_logged = False
            self._attempt = 0
            return

        if not self._missing_logged:
            # Logged once, so it shows in Home Assistant's own log.
            LOGGER.error("No %s for today from any configured source", what)
            self._missing_logged = True

        if self._attempt >= len(_STARTUP_RETRY_DELAYS_SECONDS):
            return

        delay = _STARTUP_RETRY_DELAYS_SECONDS[self._attempt]
        self._attempt += 1

        async def _retry(_now) -> None:
            await self._coordinator.async_request_refresh()

        self._unsub = async_call_later(self._coordinator.hass, delay, _retry)


def keep_last_known(new: Day, previous: Day | None, target: date) -> Day:
    """`new`, or -- when it has no data -- the last known Day for the same
    date (marked `kept`), so a source dropping out doesn't wipe a day's
    prices/forecast the planner was using."""
    if new.valid:
        if previous is not None and previous.kept:
            LOGGER.info("Data for %s available again (%s)", target, new.source or "?")
        return new
    if previous is None or not previous.valid:
        return new
    if (previous.year, previous.mon, previous.day) != (target.year, target.month, target.day):
        return new
    if not previous.kept:
        LOGGER.error(
            "No data from any source for %s -- keeping the last known (%s)",
            target,
            previous.source or "?",
        )
    previous.kept = True
    return previous


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
        # When the inverter last answered a poll (None: not yet) -- one
        # failed poll in between is no outage (see orchestrator._inverter_down).
        self.last_success_at: datetime | None = None

    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            data = await self.client.async_get_data()
            self.last_success_at = dt_util.utcnow()
            return data
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
        # True while today is priced from yesterday's stored hours.
        self._using_yesterday = False

    async def _async_update_data(self) -> dict[str, Day]:
        """Rebuild today's and tomorrow's prices from the configured entities."""
        options = self.config_entry.options
        entsoe_entity_id = options.get(CONF_ENTSOE_PRICE_ENTITY)
        frank_energie_entity_id = options.get(CONF_FRANK_ENERGIE_PRICE_ENTITY)
        use_fee = options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE)
        return_fee = options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE)
        vat_percentage = options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE)
        return_vat_percentage = (
            vat_percentage
            if options.get(CONF_APPLY_VAT_ON_RETURN, DEFAULT_APPLY_VAT_ON_RETURN)
            else 0.0
        )

        primary = options.get(CONF_PRICE_SOURCE_PRIMARY, DEFAULT_PRICE_SOURCE_PRIMARY)

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
            return_vat_percentage,
            primary,
        )
        tomorrow_day = build_price_day(
            self.hass,
            tomorrow,
            entsoe_entity_id,
            frank_energie_entity_id,
            use_fee,
            return_fee,
            vat_percentage,
            return_vat_percentage,
            primary,
        )

        self._retry_helper.note_result(
            configured=bool(entsoe_entity_id or frank_energie_entity_id),
            found_data=today_day.valid,
            what="prices",
        )

        previous = self.data or {}
        today_day = keep_last_known(today_day, previous.get("today"), today)
        if not today_day.valid:
            # Nothing from the sources and nothing read earlier today (e.g.
            # right after a restart): yesterday's stored prices, hour by
            # hour, rather than a plan without prices.
            today_day = await self._yesterdays_prices(
                today, use_fee, return_fee, vat_percentage, return_vat_percentage
            )
        return {
            "today": today_day,
            "tomorrow": keep_last_known(tomorrow_day, previous.get("tomorrow"), tomorrow),
        }

    async def _yesterdays_prices(
        self,
        today: date,
        use_fee: float,
        return_fee: float,
        vat_percentage: float,
        return_vat_percentage: float,
    ) -> Day:
        """Today priced like yesterday, from the integration's own stored
        hours (marked `kept`, so the panel shows the sources in red)."""
        day = Day(year=today.year, mon=today.month, day=today.day)
        yesterday = today - timedelta(days=1)
        prices = await self.hass.async_add_executor_job(
            storage.retrieve_day_prices,
            get_db_path(self.hass, self.config_entry),
            yesterday.year,
            yesterday.month,
            yesterday.day,
        )
        if not prices:
            return day
        for hour_index, price in prices.items():
            day.hour[hour_index].valid = True
            day.hour[hour_index].price = price
        apply_earning_classification(
            day, use_fee, return_fee, vat_percentage, return_vat_percentage
        )
        day.valid = True
        day.kept = True
        day.source = "yesterday"
        if not self._using_yesterday:
            LOGGER.error("No prices for %s -- planning with yesterday's prices meanwhile", today)
        self._using_yesterday = True
        return day


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

        primary = options.get(CONF_SOLAR_SOURCE_PRIMARY, DEFAULT_SOLAR_SOURCE_PRIMARY)
        today_day = await build_solar_day(
            self.hass, today, forecast_solar_entry_ids, solcast_entry_ids, hour_correction, primary
        )
        tomorrow_day = await build_solar_day(
            self.hass,
            tomorrow,
            forecast_solar_entry_ids,
            solcast_entry_ids,
            hour_correction,
            primary,
        )

        self._retry_helper.note_result(
            configured=bool(forecast_solar_entry_ids or solcast_entry_ids),
            found_data=today_day.valid,
            what="solar forecast",
        )

        previous = self.data or {}
        return {
            "today": keep_last_known(today_day, previous.get("today"), today),
            "tomorrow": keep_last_known(tomorrow_day, previous.get("tomorrow"), tomorrow),
        }
