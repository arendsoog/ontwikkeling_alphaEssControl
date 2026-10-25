"""Config flow for AlphaESSControl."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .api import (
    AlphaEssLocalApiClient,
    AlphaEssLocalApiClientAuthenticationError,
    AlphaEssLocalApiClientCommunicationError,
    AlphaEssLocalApiClientError,
)
from .const import (
    CONF_ALLOW_PROVIDER_CONTROL_HOURS,
    CONF_APPLY_VAT_ON_RETURN,
    CONF_CONTROL_ENABLED,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_EV_CHARGER_ENERGY_ENTITY,
    CONF_EV_CHARGER_POWER_ENTITY,
    CONF_EXCLUDED_LOAD_ENTITIES,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_EXTRA_PV_MODBUS_ADDRESS,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_MODBUS_OFF_VALUE,
    CONF_EXTRA_PV_MODBUS_ON_VALUE,
    CONF_EXTRA_PV_MODBUS_SLAVE,
    CONF_EXTRA_PV_PANEL_COUNT,
    CONF_EXTRA_PV_PANEL_WP,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_EXTRA_PV_STRING_ENTITIES,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_HOUSE_LOAD_POWER_ENTITY,
    CONF_INVERTER_NOMINAL_POWER,
    CONF_NETWORK_USE_FEE_LOW,
    CONF_NETWORK_USE_FEE_NORMAL,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_SOURCE_PRIMARY,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_PANEL_COUNT,
    CONF_PV_PANEL_WP,
    CONF_PV_POWER,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_SOURCE_PRIMARY,
    CONF_SOLCAST_ENTRIES,
    CONF_USABLE_BATTERY_CAPACITY,
    CONF_VAT_PERCENTAGE,
    DEFAULT_ALLOW_PROVIDER_CONTROL_HOURS,
    DEFAULT_APPLY_VAT_ON_RETURN,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_MODBUS_ADDRESS,
    DEFAULT_EXTRA_PV_MODBUS_OFF_VALUE,
    DEFAULT_EXTRA_PV_MODBUS_ON_VALUE,
    DEFAULT_EXTRA_PV_MODBUS_SLAVE,
    DEFAULT_NETWORK_USE_FEE,
    DEFAULT_PERSIST_DAILY_CHARGE_LIMIT,
    DEFAULT_PORT,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
)
from .prices import configured_price_entities, is_price_entity
from .solar import SOLAR_SOURCES, configured_solar_sources

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


def _power_selector(unit: str) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            mode=selector.NumberSelectorMode.BOX, min=0, unit_of_measurement=unit
        )
    )


def _count_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=0, step=1)
    )


def _house_load_candidates(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Auto-detect P1/smart-meter sources usable for real-time house load.

    Two shapes, both matched by their entities' unique_id suffix (stable
    across locales, unlike entity_id/friendly_name):
    - HomeWizard P1 (`homewizard` integration): a single `*_active_power_w`
      sensor already reports signed net power (positive=use, negative=feed)
      — usable directly, so the candidate value is that entity_id.
    - DSMR (`dsmr` integration): usage/delivery are two separate, always-
      positive sensors (`*_current_electricity_usage`/`..._delivery`) — the
      candidate value is the config entry ("dsmr:<entry_id>"), resolved to
      both sibling entities and netted (usage - delivery) at read time —
      see orchestrator.py's house-load reader.
    """
    registry = er.async_get(hass)
    candidates: list[tuple[str, str]] = []

    for entry in hass.config_entries.async_entries("homewizard"):
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if reg_entry.domain == "sensor" and reg_entry.unique_id.endswith("_active_power_w"):
                candidates.append((reg_entry.entity_id, entry.title))

    for entry in hass.config_entries.async_entries("dsmr"):
        reg_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        if any(e.unique_id.endswith("_current_electricity_usage") for e in reg_entries):
            candidates.append((f"dsmr:{entry.entry_id}", entry.title))

    return candidates


# DSMR's own "belgium_maximum_demand_current_month" sensor -- the highest
# monthly 15-minute-average peak, published only by Belgian "5B" meters
# (verified against homeassistant/components/dsmr/sensor.py). Exactly what
# CONF_PEAK_LOAD_THIS_MONTH_ENTITY wants (the capaciteitstarief floor), so
# it's worth auto-detecting like the other DSMR-derived fields above,
# instead of leaving that field a plain pick-any-sensor selector.
_DSMR_PEAK_LOAD_KEY = "belgium_maximum_demand_current_month"


def _peak_load_candidates(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Auto-detect DSMR's own peak-load-this-month sensor (Belgian 5B meters)."""
    registry = er.async_get(hass)
    candidates: list[tuple[str, str]] = []
    for entry in hass.config_entries.async_entries("dsmr"):
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if reg_entry.domain == "sensor" and reg_entry.unique_id.endswith(
                f"_{_DSMR_PEAK_LOAD_KEY}"
            ):
                candidates.append((reg_entry.entity_id, entry.title))
    return candidates


# pysma's internal register keys for its PV-power sensors, verified directly
# from pysma/definitions/webconnect.py — pysma doesn't give these entities a
# readable unique_id suffix the way HomeWizard/DSMR do, so there's nothing
# more semantic to match on:
# - "6100_0046C200" ("pv_power"): the *total* PV power — disabled by default
#   in pysma, so often doesn't exist as a live entity at all.
# - "6380_40251E00" ("pv_power_a"/"_b"/"_c"): per-MPPT-string power —
#   enabled by default (a/b; c is off). Summed at read time when the total
#   isn't available — see orchestrator.py.
SMA_PV_POWER_AGGREGATE_KEY = "6100_0046C200"
SMA_PV_POWER_STRING_KEY = "6380_40251E00"


def _extra_pv_candidates(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Auto-detect a second/separate PV installation's power source.

    Currently only SMA Solar (`sma` integration) is recognized — prefers the
    total-power entity if enabled, otherwise falls back to summing the
    per-string entities (see SMA_PV_POWER_*_KEY above).
    """
    registry = er.async_get(hass)
    candidates: list[tuple[str, str]] = []

    for entry in hass.config_entries.async_entries("sma"):
        reg_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        aggregate = next(
            (e for e in reg_entries if e.unique_id.endswith(f"-{SMA_PV_POWER_AGGREGATE_KEY}_0")),
            None,
        )
        if aggregate:
            candidates.append((aggregate.entity_id, entry.title))
        elif any(f"-{SMA_PV_POWER_STRING_KEY}_" in e.unique_id for e in reg_entries):
            candidates.append((f"sma:{entry.entry_id}", entry.title))

    return candidates


def _value_label_checklist(candidates: list[tuple[str, str]]) -> selector.SelectSelector:
    """A single-pick list built from explicit (value, label) pairs."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=value, label=label) for value, label in candidates
            ],
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _missing_source_note(hass: HomeAssistant, missing: list[str]) -> str:
    """A short note appended to the options form listing sources with nothing to pick from."""
    if not missing:
        return ""
    sources = " / ".join(missing)
    if hass.config.language.startswith("nl"):
        return f"\n\nGeen {sources} integratie gevonden — installeer er één voor deze functie."
    return f"\n\nNo {sources} integration found — install one to enable this feature."


def _eur_per_kwh_selector(*, min_value: float | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        mode=selector.NumberSelectorMode.BOX, step="any", unit_of_measurement="EUR/kWh"
    )
    if min_value is not None:
        config["min"] = min_value
    return selector.NumberSelector(config)


# Each options-menu section builds its own (marker, selector) field list —
# an ordered list rather than a dict literal, so fields can be conditionally
# omitted while keeping their position. A builder returns its fields plus
# the names of source integrations with nothing to pick from, for the "none
# found" note on that section's form.
_SectionFields = tuple[list[tuple[Any, Any]], list[str]]


def _installation_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """Roof PV, battery and inverter physical specs."""
    return [
        (
            vol.Optional(CONF_PV_PANEL_WP, default=options.get(CONF_PV_PANEL_WP, 0)),
            _power_selector("Wp"),
        ),
        (
            vol.Optional(CONF_PV_PANEL_COUNT, default=options.get(CONF_PV_PANEL_COUNT, 0)),
            _count_selector(),
        ),
        (
            vol.Optional(
                CONF_USABLE_BATTERY_CAPACITY,
                default=options.get(CONF_USABLE_BATTERY_CAPACITY, 0),
            ),
            _power_selector("Wh"),
        ),
        (
            vol.Optional(
                CONF_INVERTER_NOMINAL_POWER,
                default=options.get(CONF_INVERTER_NOMINAL_POWER, 0),
            ),
            _power_selector("W"),
        ),
    ], []


def _price_entity_candidates(hass: HomeAssistant) -> list[str]:
    """Sensors carrying day-ahead prices prices.py can read (ENTSO-e,
    Frank Energie, Nord Pool, ...), for the price-entity picker.

    Frank Energie's gas-price sensors carry the same `prices` attribute as
    the electricity ones; they're left out by their stable unique_id.
    """
    registry = er.async_get(hass)
    candidates = []
    for state in hass.states.async_all("sensor"):
        if not is_price_entity(state):
            continue
        reg_entry = registry.async_get(state.entity_id)
        if reg_entry and reg_entry.unique_id.startswith("frank_energie.gas_"):
            continue
        candidates.append(state.entity_id)
    return candidates


def _ordered_entities_selector(include_entities: list[str]) -> selector.EntitySelector:
    """A list of entities picked one by one ("add entity"), in the order
    they're tried -- draggable on Home Assistant versions that can."""
    config: dict[str, Any] = {"domain": "sensor", "multiple": True}
    if include_entities:
        config["include_entities"] = include_entities
    try:
        return selector.EntitySelector({**config, "reorder": True})
    except vol.Invalid:
        return selector.EntitySelector(config)


def _prices_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """Supplier/network fees, VAT, and the price entities."""
    fields: list[tuple[Any, Any]] = [
        (
            vol.Optional(
                CONF_PROVIDER_USE_FEE,
                default=options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE),
            ),
            _eur_per_kwh_selector(),
        ),
        (
            vol.Optional(
                CONF_PROVIDER_RETURN_FEE,
                default=options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE),
            ),
            _eur_per_kwh_selector(),
        ),
        (
            vol.Optional(
                CONF_VAT_PERCENTAGE,
                default=options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX, min=0, max=100, unit_of_measurement="%"
                )
            ),
        ),
        (
            # Some contracts (e.g. Belgian Frank Energie's teruglevering
            # formula) are explicitly VAT-exempt on the feed-in/return price
            # while still taxing use normally -- on by default so every
            # existing installation keeps applying VAT to both sides.
            vol.Optional(
                CONF_APPLY_VAT_ON_RETURN,
                default=options.get(CONF_APPLY_VAT_ON_RETURN, DEFAULT_APPLY_VAT_ON_RETURN),
            ),
            selector.BooleanSelector(),
        ),
        (
            # Netbeheerder ("Afname") per-kWh network cost, day/night
            # differentiated -- separate from CONF_PROVIDER_USE_FEE (the
            # energy supplier's fee) and from the capaciteitstarief
            # (max_grid_load, billed on peak kW). Only ever applied where
            # the real tariff state is known (live DSMR sampling) -- see
            # orchestrator.py's _house_load_tariff.
            vol.Optional(
                CONF_NETWORK_USE_FEE_NORMAL,
                default=options.get(CONF_NETWORK_USE_FEE_NORMAL, DEFAULT_NETWORK_USE_FEE),
            ),
            _eur_per_kwh_selector(min_value=0),
        ),
        (
            vol.Optional(
                CONF_NETWORK_USE_FEE_LOW,
                default=options.get(CONF_NETWORK_USE_FEE_LOW, DEFAULT_NETWORK_USE_FEE),
            ),
            _eur_per_kwh_selector(min_value=0),
        ),
    ]

    # The price entities: picked one by one, tried in their order -- the
    # first with prices for a day is used, the next ones are backups. The
    # picker offers only sensors with readable prices (plus the ones already
    # chosen, should one be unavailable right now).
    configured = configured_price_entities(options)
    candidates = _price_entity_candidates(hass)
    fields.append(
        (
            vol.Optional(CONF_PRICE_ENTITIES, description={"suggested_value": configured}),
            _ordered_entities_selector(
                [*candidates, *(entity for entity in configured if entity not in candidates)]
            ),
        )
    )

    missing = [] if candidates or configured else ["ENTSO-E / Frank Energie / Nord Pool"]
    return fields, missing


def _location_sensor(hass: HomeAssistant, entry_id: str) -> str | None:
    """The sensor that stands for a solar forecast location (config entry)
    in the options: its "energy production today", else its first sensor."""
    sensors = [
        reg_entry
        for reg_entry in er.async_entries_for_config_entry(er.async_get(hass), entry_id)
        if reg_entry.domain == "sensor" and not reg_entry.disabled_by
    ]
    preferred = next(
        (
            reg_entry
            for reg_entry in sensors
            if reg_entry.unique_id.endswith(("energy_production_today", "forecast_today"))
        ),
        sensors[0] if sensors else None,
    )
    return preferred.entity_id if preferred else None


def _solar_forecast_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """Solar forecast sources.

    Forecast.Solar/Solcast expose their full hourly forecast only via HA's
    "energy platform" hook (async_get_solar_forecast), keyed by config
    entry -- so each location is picked by one of its sensors, standing for
    its config entry. Picked one by one and dragged in order like the price
    entities; the locations of one integration are summed (e.g. one
    Forecast.Solar entry per roof plane), see solar.py.
    """
    candidates = [
        entity_id
        for domain in SOLAR_SOURCES
        for entry in hass.config_entries.async_entries(domain)
        if (entity_id := _location_sensor(hass, entry.entry_id))
    ]
    if CONF_SOLAR_FORECAST_ENTITIES in options:
        configured = list(options[CONF_SOLAR_FORECAST_ENTITIES] or [])
    else:
        configured = [
            entity_id
            for _, entry_ids in configured_solar_sources(hass, options)
            for entry_id in entry_ids
            if (entity_id := _location_sensor(hass, entry_id))
        ]

    fields: list[tuple[Any, Any]] = [
        (
            vol.Optional(CONF_SOLAR_FORECAST_ENTITIES, description={"suggested_value": configured}),
            _ordered_entities_selector(
                [*candidates, *(entity for entity in configured if entity not in candidates)]
            ),
        )
    ]
    missing = [] if candidates or configured else ["Forecast.Solar / Solcast"]
    return fields, missing


def _extra_pv_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """A second/separate PV installation and its Modbus price control."""
    extra_pv_candidates = _extra_pv_candidates(hass)
    return [
        (
            vol.Optional(
                CONF_EXTRA_PV_POWER_ENTITY,
                description={"suggested_value": options.get(CONF_EXTRA_PV_POWER_ENTITY)},
            ),
            # Same tick-if-detected pattern as house-load: prefers an
            # auto-detected source (currently SMA Solar), falls back to a
            # manual pick-any-sensor selector when nothing is detected.
            _value_label_checklist(extra_pv_candidates)
            if extra_pv_candidates
            else selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_STRING_ENTITIES,
                description={"suggested_value": options.get(CONF_EXTRA_PV_STRING_ENTITIES)},
            ),
            selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
            ),
        ),
        (
            vol.Optional(CONF_EXTRA_PV_PANEL_WP, default=options.get(CONF_EXTRA_PV_PANEL_WP, 0)),
            _power_selector("Wp"),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_PANEL_COUNT, default=options.get(CONF_EXTRA_PV_PANEL_COUNT, 0)
            ),
            _count_selector(),
        ),
        (
            # Dedicated feature toggle for the extra-PV price control below —
            # separate from CONF_CONTROL_ENABLED, so the hub/register config
            # can be entered once and paused/resumed without clearing it.
            # Defaults off.
            vol.Optional(
                CONF_EXTRA_PV_CONTROL_ENABLED,
                default=options.get(
                    CONF_EXTRA_PV_CONTROL_ENABLED, DEFAULT_EXTRA_PV_CONTROL_ENABLED
                ),
            ),
            selector.BooleanSelector(),
        ),
        (
            # Register layout isn't the same for every SMA installation, so
            # all of hub/slave/address/values are configurable rather than
            # hardcoded — see dispatch.decide_extra_pv_state.
            vol.Optional(
                CONF_EXTRA_PV_MODBUS_HUB,
                description={"suggested_value": options.get(CONF_EXTRA_PV_MODBUS_HUB)},
            ),
            selector.TextSelector(),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_MODBUS_SLAVE,
                default=options.get(CONF_EXTRA_PV_MODBUS_SLAVE, DEFAULT_EXTRA_PV_MODBUS_SLAVE),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=0, step=1)
            ),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_MODBUS_ADDRESS,
                default=options.get(CONF_EXTRA_PV_MODBUS_ADDRESS, DEFAULT_EXTRA_PV_MODBUS_ADDRESS),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=0, step=1)
            ),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_MODBUS_ON_VALUE,
                default=options.get(
                    CONF_EXTRA_PV_MODBUS_ON_VALUE, DEFAULT_EXTRA_PV_MODBUS_ON_VALUE
                ),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step=1)
            ),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_MODBUS_OFF_VALUE,
                default=options.get(
                    CONF_EXTRA_PV_MODBUS_OFF_VALUE, DEFAULT_EXTRA_PV_MODBUS_OFF_VALUE
                ),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step=1)
            ),
        ),
    ], []


def _ev_charger_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """Consumption left out of the learned house load: the EV charger (also
    shown in the panel) and any other big, irregular consumers."""
    return [
        (
            vol.Optional(
                CONF_EV_CHARGER_POWER_ENTITY,
                description={"suggested_value": options.get(CONF_EV_CHARGER_POWER_ENTITY)},
            ),
            selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power")
            ),
        ),
        (
            vol.Optional(
                CONF_EV_CHARGER_ENERGY_ENTITY,
                description={"suggested_value": options.get(CONF_EV_CHARGER_ENERGY_ENTITY)},
            ),
            selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="energy")
            ),
        ),
        (
            vol.Optional(
                CONF_EXCLUDED_LOAD_ENTITIES,
                description={"suggested_value": options.get(CONF_EXCLUDED_LOAD_ENTITIES)},
            ),
            selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
            ),
        ),
    ], []


def _meter_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """Smart-meter (P1/DSMR) sources: live house load and monthly peak."""
    house_load_candidates = _house_load_candidates(hass)
    peak_load_candidates = _peak_load_candidates(hass)
    return [
        (
            vol.Optional(
                CONF_HOUSE_LOAD_POWER_ENTITY,
                description={"suggested_value": options.get(CONF_HOUSE_LOAD_POWER_ENTITY)},
            ),
            # Tick a detected P1/smart-meter source (HomeWizard P1, DSMR) when
            # any exist; otherwise fall back to a manual pick-any-sensor
            # selector — unlike the price/solar sources, "no match" here
            # just means an undetected meter brand, not a missing integration,
            # so this field is never simply omitted.
            _value_label_checklist(house_load_candidates)
            if house_load_candidates
            else selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        ),
        (
            vol.Optional(
                CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
                # Pre-fills with the previously saved value if there is one;
                # otherwise with DSMR's own "belgium_maximum_demand_current_
                # month" sensor when detected (Belgian 5B meters). Always a
                # plain pick-any-sensor selector (not a locked checklist like
                # house-load/extra-PV) -- unlike those, this field has
                # no "wrong" alternative to guard against, so it should
                # always be free to point at any sensor, auto-detected or
                # not, shown by its own friendly name rather than the DSMR
                # hub's host:port title.
                description={
                    "suggested_value": options.get(CONF_PEAK_LOAD_THIS_MONTH_ENTITY)
                    or (peak_load_candidates[0][0] if peak_load_candidates else None)
                },
            ),
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        ),
    ], []


def _control_fields(hass: HomeAssistant, options: Mapping[str, Any]) -> _SectionFields:
    """Scheduler/dispatch behaviour, including the master control switch."""
    return [
        (
            vol.Optional(
                CONF_ALLOW_PROVIDER_CONTROL_HOURS,
                default=options.get(
                    CONF_ALLOW_PROVIDER_CONTROL_HOURS, DEFAULT_ALLOW_PROVIDER_CONTROL_HOURS
                ),
            ),
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[str(hour) for hour in range(24)],
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        ),
        (
            vol.Optional(
                CONF_PERSIST_DAILY_CHARGE_LIMIT,
                default=options.get(
                    CONF_PERSIST_DAILY_CHARGE_LIMIT, DEFAULT_PERSIST_DAILY_CHARGE_LIMIT
                ),
            ),
            selector.BooleanSelector(),
        ),
        (
            # Master switch for real writes to the inverter — off means the
            # dispatch coordinator only ever computes and logs its decision.
            # Deliberately last in the form and defaulting off.
            vol.Optional(
                CONF_CONTROL_ENABLED,
                default=options.get(CONF_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED),
            ),
            selector.BooleanSelector(),
        ),
    ], []


# Options menu sections, in menu order: step_id -> field builder.
OPTIONS_SECTIONS = {
    "installation": _installation_fields,
    "prices": _prices_fields,
    "solar_forecast": _solar_forecast_fields,
    "meter": _meter_fields,
    "extra_pv": _extra_pv_fields,
    "ev_charger": _ev_charger_fields,
    "control": _control_fields,
}

# Options a section's newer field replaced: dropped when that section is
# saved, so they aren't read as a fallback any more.
_SUPERSEDED_KEYS = {
    "prices": {
        CONF_ENTSOE_PRICE_ENTITY,
        CONF_FRANK_ENERGIE_PRICE_ENTITY,
        CONF_PRICE_SOURCE_PRIMARY,
    },
    "solar_forecast": {
        CONF_FORECAST_SOLAR_ENTRIES,
        CONF_SOLCAST_ENTRIES,
        CONF_SOLAR_SOURCE_PRIMARY,
    },
}


def _section_schema(
    hass: HomeAssistant, section: str, options: Mapping[str, Any]
) -> tuple[vol.Schema, list[str]]:
    """Build one options section's form schema, pre-filled with the current values."""
    fields, missing = OPTIONS_SECTIONS[section](hass, options)
    return vol.Schema(dict(fields)), missing


class AlphaEssLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AlphaESSControl."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AlphaEssLocalOptionsFlow:
        """Get the options flow for this handler."""
        return AlphaEssLocalOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step, entered from the "Add integration" UI."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}")
            self._abort_if_unique_id_configured()

            try:
                await self._async_test_credentials(
                    host=user_input[CONF_HOST],
                    port=user_input[CONF_PORT],
                )
            except AlphaEssLocalApiClientAuthenticationError:
                errors["base"] = "invalid_auth"
            except AlphaEssLocalApiClientCommunicationError:
                errors["base"] = "cannot_connect"
            except AlphaEssLocalApiClientError:
                LOGGER.exception("Unexpected error validating AlphaESS connection")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_HOST],
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def _async_test_credentials(self, host: str, port: int) -> None:
        """Validate that the device is reachable at host:port."""
        client = AlphaEssLocalApiClient(host=host, port=port)
        await client.async_test_connection()


class AlphaEssLocalOptionsFlow(config_entries.OptionsFlow):
    """Handle the options flow for AlphaESSControl.

    Covers device physical specs, pricing, API tokens, optional local
    sensors, and scheduler tuning (see const.py) — settings consumed by
    later phases (prices, solar, scheduler), not by the Modbus driver.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the section menu."""
        return self.async_show_menu(step_id="init", menu_options=list(OPTIONS_SECTIONS))

    async def async_step_installation(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("installation", user_input)

    async def async_step_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("prices", user_input)

    async def async_step_solar_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("solar_forecast", user_input)

    async def async_step_extra_pv(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("extra_pv", user_input)

    async def async_step_ev_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("ev_charger", user_input)

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("meter", user_input)

    async def async_step_control(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self._async_section("control", user_input)

    def _async_section(
        self, section: str, user_input: dict[str, Any] | None
    ) -> config_entries.ConfigFlowResult:
        """Show one section's form, or save it on submit.

        Saving replaces only this section's fields and keeps every other
        section's stored options. The section's keys are dropped first, so
        a cleared optional field (absent from user_input) is really cleared
        rather than falling back to its old value.
        """
        schema, missing = _section_schema(self.hass, section, self.config_entry.options)

        if user_input is not None:
            section_keys = {str(marker) for marker in schema.schema}
            section_keys |= _SUPERSEDED_KEYS.get(section, set())
            data = {
                key: value
                for key, value in self.config_entry.options.items()
                if key not in section_keys
            }
            data.update(user_input)
            data[CONF_PV_POWER] = data.get(CONF_PV_PANEL_WP, 0) * data.get(CONF_PV_PANEL_COUNT, 0)
            data[CONF_EXTRA_PV_POWER_CAPACITY] = data.get(CONF_EXTRA_PV_PANEL_WP, 0) * data.get(
                CONF_EXTRA_PV_PANEL_COUNT, 0
            )
            return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id=section,
            data_schema=schema,
            description_placeholders={
                "missing_source_note": _missing_source_note(self.hass, missing)
            },
        )
