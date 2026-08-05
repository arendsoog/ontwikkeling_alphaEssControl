"""Tests for AlphaEssLocalPricesCoordinator and AlphaEssLocalSolarCoordinator."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alpha_ess_local.const import (
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    DOMAIN,
)
from custom_components.alpha_ess_local.coordinator import (
    _STARTUP_RETRY_DELAYS_SECONDS,
    AlphaEssLocalPricesCoordinator,
    AlphaEssLocalSolarCoordinator,
    _StartupRaceRetryHelper,
)
from custom_components.alpha_ess_local.data import Day

ENTSOE_ENTITY_ID = "sensor.entsoe_average_electricity_price_today"


async def test_prices_coordinator_builds_today_and_tomorrow(hass: HomeAssistant):
    hass.states.async_set(
        ENTSOE_ENTITY_ID,
        "0.15",
        {
            "prices_today": [
                {
                    "time": dt_util.now()
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .isoformat(),
                    "price": 0.10,
                }
            ]
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_ENTSOE_PRICE_ENTITY: ENTSOE_ENTITY_ID},
    )
    entry.add_to_hass(hass)

    coordinator = AlphaEssLocalPricesCoordinator(hass, entry)
    data = await coordinator._async_update_data()

    assert set(data.keys()) == {"today", "tomorrow"}
    assert data["today"].valid is True
    assert data["today"].hour[0].price == 0.10


async def test_prices_coordinator_handles_no_configured_entities(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    coordinator = AlphaEssLocalPricesCoordinator(hass, entry)
    data = await coordinator._async_update_data()

    assert data["today"].valid is False
    assert data["tomorrow"].valid is False


async def test_solar_coordinator_builds_today_and_tomorrow(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_FORECAST_SOLAR_ENTRIES: ["forecast-solar-entry-id"]},
    )
    entry.add_to_hass(hass)

    fake_day = Day(valid=True)
    fake_day.hour[0].valid = True
    fake_day.hour[0].estimated_solar_power = 111

    with patch(
        "custom_components.alpha_ess_local.coordinator.build_solar_day",
        AsyncMock(return_value=fake_day),
    ) as mock_build_solar_day:
        coordinator = AlphaEssLocalSolarCoordinator(hass, entry)
        data = await coordinator._async_update_data()

    assert set(data.keys()) == {"today", "tomorrow"}
    assert data["today"].hour[0].estimated_solar_power == 111
    assert mock_build_solar_day.await_count == 2


async def test_solar_coordinator_handles_no_configured_entries(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.add_to_hass(hass)

    coordinator = AlphaEssLocalSolarCoordinator(hass, entry)
    data = await coordinator._async_update_data()

    assert data["today"].valid is False
    assert data["tomorrow"].valid is False


# --- _StartupRaceRetryHelper --------------------------------------------------


class _FakeCoordinator:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.async_request_refresh = AsyncMock()


async def test_startup_retry_helper_schedules_retry_when_configured_but_empty(
    hass: HomeAssistant,
):
    coordinator = _FakeCoordinator(hass)
    helper = _StartupRaceRetryHelper(coordinator)

    helper.note_result(configured=True, found_data=False)
    coordinator.async_request_refresh.assert_not_awaited()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=_STARTUP_RETRY_DELAYS_SECONDS[0] + 1)
    )
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_awaited_once()


async def test_startup_retry_helper_no_retry_when_not_configured(hass: HomeAssistant):
    coordinator = _FakeCoordinator(hass)
    helper = _StartupRaceRetryHelper(coordinator)

    helper.note_result(configured=False, found_data=False)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_not_awaited()


async def test_startup_retry_helper_no_retry_when_data_found(hass: HomeAssistant):
    coordinator = _FakeCoordinator(hass)
    helper = _StartupRaceRetryHelper(coordinator)

    helper.note_result(configured=True, found_data=True)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_not_awaited()


async def test_startup_retry_helper_stops_after_max_attempts(hass: HomeAssistant):
    coordinator = _FakeCoordinator(hass)
    helper = _StartupRaceRetryHelper(coordinator)

    now = dt_util.utcnow()
    for _ in _STARTUP_RETRY_DELAYS_SECONDS:
        helper.note_result(configured=True, found_data=False)
        now += timedelta(seconds=max(_STARTUP_RETRY_DELAYS_SECONDS) + 1)
        async_fire_time_changed(hass, now)
        await hass.async_block_till_done()

    assert coordinator.async_request_refresh.await_count == len(_STARTUP_RETRY_DELAYS_SECONDS)

    # One more still-empty result beyond the configured attempts schedules nothing further.
    helper.note_result(configured=True, found_data=False)
    now += timedelta(seconds=max(_STARTUP_RETRY_DELAYS_SECONDS) + 1)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done()

    assert coordinator.async_request_refresh.await_count == len(_STARTUP_RETRY_DELAYS_SECONDS)


async def test_startup_retry_helper_resets_attempt_counter_after_success(hass: HomeAssistant):
    coordinator = _FakeCoordinator(hass)
    helper = _StartupRaceRetryHelper(coordinator)

    helper.note_result(configured=True, found_data=False)
    helper.note_result(configured=True, found_data=True)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    coordinator.async_request_refresh.assert_not_awaited()
    assert helper._attempt == 0


async def test_prices_coordinator_schedules_retry_on_startup_race(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_ENTSOE_PRICE_ENTITY: ENTSOE_ENTITY_ID},
    )
    entry.add_to_hass(hass)

    coordinator = AlphaEssLocalPricesCoordinator(hass, entry)
    with patch.object(coordinator, "async_request_refresh", AsyncMock()) as mock_refresh:
        data = await coordinator._async_update_data()
        assert data["today"].valid is False

        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=_STARTUP_RETRY_DELAYS_SECONDS[0] + 1)
        )
        await hass.async_block_till_done()

    mock_refresh.assert_awaited_once()


async def test_solar_coordinator_schedules_retry_on_startup_race(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_FORECAST_SOLAR_ENTRIES: ["forecast-solar-entry-id"]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.alpha_ess_local.coordinator.build_solar_day",
        AsyncMock(return_value=Day(valid=False)),
    ):
        coordinator = AlphaEssLocalSolarCoordinator(hass, entry)
        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as mock_refresh:
            data = await coordinator._async_update_data()
            assert data["today"].valid is False

            async_fire_time_changed(
                hass, dt_util.utcnow() + timedelta(seconds=_STARTUP_RETRY_DELAYS_SECONDS[0] + 1)
            )
            await hass.async_block_till_done()

    mock_refresh.assert_awaited_once()
