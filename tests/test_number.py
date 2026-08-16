"""Tests for number.py.

These are the live-adjustable scheduler-setting sliders (SOC bounds, the
minimum daily profit threshold, and the max grid-charge load) that live
directly on the device (see orchestrator._number_entity_value for the read
side).
"""

from unittest.mock import MagicMock

from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.alpha_ess_local.const import (
    DEFAULT_DAILY_MIN_PROFIT,
    DEFAULT_MAX_GRID_LOAD,
    DEFAULT_MAX_SOC_NEGATIVE_PRICE,
    DEFAULT_MAX_SOC_POSITIVE_PRICE,
    DEFAULT_MIN_SOC_DISCHARGE,
    DOMAIN,
)
from custom_components.alpha_ess_local.number import (
    NUMBER_DESCRIPTIONS,
    AlphaEssLocalSchedulerNumber,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, entry_id="test-entry")


def _description(key: str):
    return next(d for d in NUMBER_DESCRIPTIONS if d.key == key)


def test_number_descriptions_cover_all_five_scheduler_settings():
    keys = {d.key for d in NUMBER_DESCRIPTIONS}
    assert keys == {
        "max_soc_positive_price",
        "max_soc_negative_price",
        "min_soc_discharge",
        "daily_min_profit",
        "max_grid_load",
    }


def test_soc_and_profit_number_descriptions_are_0_to_100_sliders_with_step_1():
    for key in (
        "max_soc_positive_price",
        "max_soc_negative_price",
        "min_soc_discharge",
        "daily_min_profit",
    ):
        description = _description(key)
        assert description.native_min_value == 0
        assert description.native_max_value == 100
        assert description.native_step == 1


def test_soc_number_descriptions_use_percent_unit():
    for key in ("max_soc_positive_price", "max_soc_negative_price", "min_soc_discharge"):
        assert _description(key).native_unit_of_measurement == PERCENTAGE


def test_daily_min_profit_description_uses_cents_unit():
    assert _description("daily_min_profit").native_unit_of_measurement == "ct"


def test_max_grid_load_description_is_5_to_10_kw_slider_with_half_kw_step():
    description = _description("max_grid_load")
    assert description.native_min_value == 5
    assert description.native_max_value == 10
    assert description.native_step == 0.5
    assert description.native_unit_of_measurement == UnitOfPower.KILO_WATT


def test_number_descriptions_default_values_match_const_defaults():
    assert _description("max_soc_positive_price").default_value == DEFAULT_MAX_SOC_POSITIVE_PRICE
    assert _description("max_soc_negative_price").default_value == DEFAULT_MAX_SOC_NEGATIVE_PRICE
    assert _description("min_soc_discharge").default_value == DEFAULT_MIN_SOC_DISCHARGE
    # DEFAULT_DAILY_MIN_PROFIT is in EUR (0.40); this entity's own scale is
    # whole cents (40).
    assert _description("daily_min_profit").default_value == round(DEFAULT_DAILY_MIN_PROFIT * 100)
    assert _description("max_grid_load").default_value == DEFAULT_MAX_GRID_LOAD


def test_soc_number_initial_value_matches_description_default():
    description = _description("max_soc_positive_price")
    number = AlphaEssLocalSchedulerNumber(_entry(), description)

    assert number.native_value == description.default_value


def test_soc_number_unique_id_includes_entry_id_and_key():
    entry = _entry()
    description = _description("min_soc_discharge")
    number = AlphaEssLocalSchedulerNumber(entry, description)

    assert number.unique_id == f"{entry.entry_id}_min_soc_discharge"


def test_soc_number_device_info_matches_entry(hass: HomeAssistant):
    entry = _entry()
    description = _description("max_soc_positive_price")
    number = AlphaEssLocalSchedulerNumber(entry, description)

    assert number.device_info["identifiers"] == {(DOMAIN, entry.entry_id)}


async def test_soc_number_set_native_value_updates_state(hass: HomeAssistant):
    # `async_write_ha_state` is stubbed out here: it requires the entity to
    # have gone through full entity-platform registration (translation-key
    # unit-of-measurement lookups need `platform_data`), which is HA's own
    # plumbing, not this method's logic — what's under test is that
    # `async_set_native_value` updates the value and asks HA to write state.
    entry = _entry()
    entry.add_to_hass(hass)
    description = _description("max_soc_negative_price")
    number = AlphaEssLocalSchedulerNumber(entry, description)
    number.hass = hass
    number.entity_id = "number.test_max_soc_negative_price"
    number.async_write_ha_state = MagicMock()

    await number.async_set_native_value(97.5)

    assert number.native_value == 97.5
    number.async_write_ha_state.assert_called_once()


async def test_soc_number_restores_last_value_after_restart(hass: HomeAssistant):
    entry = _entry()
    entry.add_to_hass(hass)
    description = _description("max_soc_positive_price")
    entity_id = "number.test_max_soc_positive_price"

    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(entity_id, "82.5"),
                {
                    "native_max_value": 100.0,
                    "native_min_value": 0.0,
                    "native_step": 0.1,
                    "native_unit_of_measurement": PERCENTAGE,
                    "native_value": 82.5,
                },
            )
        ],
    )

    number = AlphaEssLocalSchedulerNumber(entry, description)
    number.hass = hass
    number.entity_id = entity_id
    await number.async_added_to_hass()

    assert number.native_value == 82.5
