"""Tests for solar.py.

Forecast.Solar / Solcast integrations aren't installed in this devcontainer,
so `get_solar_forecast`'s cross-integration "energy platform" call is
exercised with a mocked `async_get_integration`/platform module — the
`wh_hours` shape itself is HA's own documented, typed contract
(homeassistant.components.energy.types.SolarForecastType), not guesswork.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alpha_ess_local.solar import (
    _wh_hours_to_day_hours,
    build_day,
    get_combined_solar_forecast,
    get_solar_forecast,
    read_solar_forecast,
)

FORECAST_SOLAR_DOMAIN = "forecast_solar"
SOLCAST_DOMAIN = "solcast_solar"


def _local_iso(hour: int) -> str:
    return dt_util.now().replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_wh_hours_to_day_hours_buckets_and_sums_same_hour():
    today = dt_util.now().date()
    base = dt_util.now().replace(hour=10, minute=0, second=0, microsecond=0)
    wh_hours = {
        base.isoformat(): 100.0,
        base.replace(minute=30).isoformat(): 50.0,
        base.replace(hour=11).isoformat(): 200.0,
    }

    result = _wh_hours_to_day_hours(wh_hours, today)

    assert result == {10: 150.0, 11: 200.0}


def test_wh_hours_to_day_hours_filters_other_dates():
    today = dt_util.now().date()
    tomorrow = dt_util.now().replace(hour=5, minute=0, second=0, microsecond=0) + timedelta(days=1)
    wh_hours = {tomorrow.isoformat(): 42.0}

    result = _wh_hours_to_day_hours(wh_hours, today)

    assert result is None


def test_wh_hours_to_day_hours_skips_unparseable_timestamps():
    today = dt_util.now().date()
    wh_hours = {"not-a-timestamp": 10.0}

    result = _wh_hours_to_day_hours(wh_hours, today)

    assert result is None


async def test_get_solar_forecast_returns_none_for_unknown_entry(hass: HomeAssistant):
    result = await get_solar_forecast(hass, "does-not-exist")

    assert result is None


async def test_get_solar_forecast_calls_energy_platform_hook(hass: HomeAssistant):
    entry = MockConfigEntry(domain=FORECAST_SOLAR_DOMAIN)
    entry.add_to_hass(hass)

    fake_platform = SimpleNamespace(
        async_get_solar_forecast=AsyncMock(return_value={"wh_hours": {"x": 1.0}})
    )
    fake_integration = SimpleNamespace(async_get_platform=AsyncMock(return_value=fake_platform))

    with patch(
        "custom_components.alpha_ess_local.solar.async_get_integration",
        AsyncMock(return_value=fake_integration),
    ):
        result = await get_solar_forecast(hass, entry.entry_id)

    assert result == {"x": 1.0}
    fake_platform.async_get_solar_forecast.assert_awaited_once_with(hass, entry.entry_id)


async def test_get_solar_forecast_handles_missing_hook(hass: HomeAssistant):
    entry = MockConfigEntry(domain=FORECAST_SOLAR_DOMAIN)
    entry.add_to_hass(hass)

    fake_platform = SimpleNamespace()  # no async_get_solar_forecast attribute
    fake_integration = SimpleNamespace(async_get_platform=AsyncMock(return_value=fake_platform))

    with patch(
        "custom_components.alpha_ess_local.solar.async_get_integration",
        AsyncMock(return_value=fake_integration),
    ):
        result = await get_solar_forecast(hass, entry.entry_id)

    assert result is None


async def test_get_combined_solar_forecast_sums_multiple_planes(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, config_entry_id):
        if config_entry_id == "achterkant":
            return {_local_iso(10): 100.0}
        if config_entry_id == "voorkant":
            return {_local_iso(10): 60.0, _local_iso(11): 40.0}
        return None

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        hours = await get_combined_solar_forecast(hass, ["achterkant", "voorkant"], today)

    assert hours == {10: 160.0, 11: 40.0}


async def test_get_combined_solar_forecast_returns_none_when_all_entries_empty(
    hass: HomeAssistant,
):
    async def fake_get_solar_forecast(_hass, _config_entry_id):
        return None

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        hours = await get_combined_solar_forecast(
            hass, ["achterkant", "voorkant"], dt_util.now().date()
        )

    assert hours is None


async def test_read_solar_forecast_prefers_forecast_solar(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, config_entry_id):
        if config_entry_id == "forecast-solar-entry":
            return {_local_iso(0): 123.0}
        return {_local_iso(0): 999.0}

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        hours = await read_solar_forecast(hass, ["forecast-solar-entry"], ["solcast-entry"], today)

    assert hours == {0: 123.0}


async def test_read_solar_forecast_falls_back_to_solcast(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, config_entry_id):
        if config_entry_id == "solcast-entry":
            return {_local_iso(6): 55.0}
        return None

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        hours = await read_solar_forecast(hass, ["forecast-solar-entry"], ["solcast-entry"], today)

    assert hours == {6: 55.0}


async def test_read_solar_forecast_returns_none_when_both_missing(hass: HomeAssistant):
    hours = await read_solar_forecast(hass, [], [], dt_util.now().date())

    assert hours is None


async def test_build_day_populates_estimated_solar_power(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, _config_entry_id):
        return {_local_iso(9): 321.0}

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        day = await build_day(hass, today, ["forecast-solar-entry"], [])

    assert day.valid is True
    assert day.hour[9].valid is True
    assert day.hour[9].estimated_solar_power == 321


async def test_build_day_stays_invalid_when_no_source_configured(hass: HomeAssistant):
    day = await build_day(hass, dt_util.now().date(), [], [])

    assert day.valid is False


async def test_build_day_without_correction_leaves_raw_and_corrected_equal(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, _config_entry_id):
        return {_local_iso(9): 321.0}

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        day = await build_day(hass, today, ["forecast-solar-entry"], [])

    assert day.hour[9].estimated_solar_power_raw == 321.0
    assert day.hour[9].estimated_solar_power == 321


async def test_build_day_applies_reliable_correction(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, _config_entry_id):
        return {_local_iso(9): 200.0}

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        day = await build_day(
            hass, today, ["forecast-solar-entry"], [], hour_correction={9: (1.5, 10.0)}
        )

    assert day.hour[9].estimated_solar_power_raw == 200.0
    assert day.hour[9].estimated_solar_power == round(200.0 * 1.5 + 10.0)


async def test_build_day_skips_unreliable_correction(hass: HomeAssistant):
    today = dt_util.now().date()

    async def fake_get_solar_forecast(_hass, _config_entry_id):
        return {_local_iso(9): 200.0}

    with patch(
        "custom_components.alpha_ess_local.solar.get_solar_forecast",
        fake_get_solar_forecast,
    ):
        day = await build_day(
            hass, today, ["forecast-solar-entry"], [], hour_correction={9: (-1.0, 0.0)}
        )

    assert day.hour[9].estimated_solar_power == 200
