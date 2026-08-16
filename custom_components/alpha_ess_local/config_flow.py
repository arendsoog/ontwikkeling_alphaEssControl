"""Config flow for AlphaESS Local Control."""

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
    CONF_CONTROL_ENABLED,
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_EXTRA_PV_CONTROL_ENABLED,
    CONF_EXTRA_PV_MODBUS_ADDRESS,
    CONF_EXTRA_PV_MODBUS_HUB,
    CONF_EXTRA_PV_MODBUS_OFF_VALUE,
    CONF_EXTRA_PV_MODBUS_ON_VALUE,
    CONF_EXTRA_PV_MODBUS_SLAVE,
    CONF_EXTRA_PV_POWER_CAPACITY,
    CONF_EXTRA_PV_POWER_ENTITY,
    CONF_FORECAST_SOLAR_ENTRIES,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_HOUSE_LOAD_POWER_ENTITY,
    CONF_INVERTER_NOMINAL_POWER,
    CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
    CONF_PERSIST_DAILY_CHARGE_LIMIT,
    CONF_PROVIDER_RETURN_FEE,
    CONF_PROVIDER_USE_FEE,
    CONF_PV_POWER,
    CONF_SOLCAST_ENTRIES,
    CONF_USABLE_BATTERY_CAPACITY,
    CONF_VAT_PERCENTAGE,
    DEFAULT_ALLOW_PROVIDER_CONTROL_HOURS,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_CONTROL_ENABLED,
    DEFAULT_EXTRA_PV_MODBUS_ADDRESS,
    DEFAULT_EXTRA_PV_MODBUS_OFF_VALUE,
    DEFAULT_EXTRA_PV_MODBUS_ON_VALUE,
    DEFAULT_EXTRA_PV_MODBUS_SLAVE,
    DEFAULT_FORECAST_SOLAR_ENTRIES,
    DEFAULT_PERSIST_DAILY_CHARGE_LIMIT,
    DEFAULT_PORT,
    DEFAULT_PROVIDER_RETURN_FEE,
    DEFAULT_PROVIDER_USE_FEE,
    DEFAULT_SOLCAST_ENTRIES,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
)

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


def _config_entry_checklist(entries: list[config_entries.ConfigEntry]) -> selector.SelectSelector:
    """A checkbox list of config entries (e.g. one per roof plane/location)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=entry.entry_id, label=entry.title)
                for entry in entries
            ],
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _entities_with_attribute(
    hass: HomeAssistant,
    domain: str,
    attribute: str,
    *,
    exclude_unique_id_prefix: str | None = None,
) -> list[er.RegistryEntry]:
    """Sensor entities from a domain's config entries that currently expose `attribute`.

    Used to auto-detect the *specific* entity prices.py needs (e.g. ENTSO-e's
    "average price" sensor, which carries the prices_today attribute) out of
    every sensor that integration creates, rather than offering a raw pick-any
    -sensor EntitySelector where it's easy to select the wrong one.

    `exclude_unique_id_prefix` drops entities such as Frank Energie's gas-price
    sensors, which carry the same `prices` attribute as the electricity ones
    but use a stable "frank_energie.gas_*" unique_id regardless of locale.
    """
    registry = er.async_get(hass)
    return [
        reg_entry
        for entry in hass.config_entries.async_entries(domain)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg_entry.domain == "sensor"
        and (state := hass.states.get(reg_entry.entity_id)) is not None
        and attribute in state.attributes
        and not (
            exclude_unique_id_prefix and reg_entry.unique_id.startswith(exclude_unique_id_prefix)
        )
    ]


def _entity_checklist(entries: list[er.RegistryEntry]) -> selector.SelectSelector:
    """A single-pick list of candidate entities — same look as the config-entry checklists."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(
                    value=entry.entity_id,
                    label=entry.name or entry.original_name or entry.entity_id,
                )
                for entry in entries
            ],
            mode=selector.SelectSelectorMode.LIST,
        )
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


def _options_schema(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> tuple[vol.Schema, list[str]]:
    """Build the options form schema, pre-filled with the current values.

    Every price/forecast source checklist is left out entirely when there's
    nothing to pick from — an empty checklist is just confusing. Returns the
    schema plus the list of source names with nothing found, for the "none
    found" notice built by `AlphaEssLocalOptionsFlow.async_step_init`.
    """
    entsoe_entities = _entities_with_attribute(hass, "entsoe", "prices_today")
    frank_energie_entities = _entities_with_attribute(
        hass, "frank_energie", "prices", exclude_unique_id_prefix="frank_energie.gas_"
    )
    forecast_solar_entries = hass.config_entries.async_entries("forecast_solar")
    solcast_entries = hass.config_entries.async_entries("solcast_solar")
    house_load_candidates = _house_load_candidates(hass)
    extra_pv_candidates = _extra_pv_candidates(hass)

    missing = [
        name
        for name, found in (
            ("ENTSO-E", entsoe_entities),
            ("Frank Energie", frank_energie_entities),
            ("Forecast.Solar", forecast_solar_entries),
            ("Solcast", solcast_entries),
        )
        if not found
    ]

    # Built as an ordered list of (marker, selector) pairs, not a dict
    # literal, so the checklists can be spliced in at their natural position
    # in the form (ENTSO-E/Frank Energie/Forecast.Solar/Solcast, in that
    # order) while still being conditionally omitted.
    fields: list[tuple[Any, Any]] = [
        (vol.Optional(CONF_PV_POWER, default=options.get(CONF_PV_POWER, 0)), _power_selector("W")),
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
        (
            vol.Optional(
                CONF_PROVIDER_USE_FEE,
                default=options.get(CONF_PROVIDER_USE_FEE, DEFAULT_PROVIDER_USE_FEE),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    step="any",
                    unit_of_measurement="EUR/kWh",
                )
            ),
        ),
        (
            vol.Optional(
                CONF_PROVIDER_RETURN_FEE,
                default=options.get(CONF_PROVIDER_RETURN_FEE, DEFAULT_PROVIDER_RETURN_FEE),
            ),
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    step="any",
                    unit_of_measurement="EUR/kWh",
                )
            ),
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
    ]

    # Price/forecast source fields are auto-populated checklists (entity- or
    # config-entry-based, per source — see _entities_with_attribute /
    # _config_entry_checklist), not a raw pick-any-sensor EntitySelector:
    # this way there's nothing to misconfigure, and the field simply doesn't
    # appear when the corresponding integration isn't installed — see the
    # `missing` note built above.
    if entsoe_entities:
        fields.append(
            (
                vol.Optional(
                    CONF_ENTSOE_PRICE_ENTITY,
                    description={"suggested_value": options.get(CONF_ENTSOE_PRICE_ENTITY)},
                ),
                _entity_checklist(entsoe_entities),
            )
        )
    if frank_energie_entities:
        fields.append(
            (
                vol.Optional(
                    CONF_FRANK_ENERGIE_PRICE_ENTITY,
                    description={"suggested_value": options.get(CONF_FRANK_ENERGIE_PRICE_ENTITY)},
                ),
                _entity_checklist(frank_energie_entities),
            )
        )

    # Solar forecast fields are a checklist of config entries (not
    # entities) — Forecast.Solar/Solcast only expose their full hourly
    # forecast via HA's "energy platform" hook (async_get_solar_forecast),
    # keyed by config_entry_id. Any number of entries can be ticked (e.g.
    # one Forecast.Solar entry per roof plane/location); their forecasts
    # get summed together in solar.py. Omitted entirely when there are no
    # entries to pick from — see async_step_init for the "none found" note.
    if forecast_solar_entries:
        fields.append(
            (
                vol.Optional(
                    CONF_FORECAST_SOLAR_ENTRIES,
                    default=options.get(
                        CONF_FORECAST_SOLAR_ENTRIES, DEFAULT_FORECAST_SOLAR_ENTRIES
                    ),
                ),
                _config_entry_checklist(forecast_solar_entries),
            )
        )
    if solcast_entries:
        fields.append(
            (
                vol.Optional(
                    CONF_SOLCAST_ENTRIES,
                    default=options.get(CONF_SOLCAST_ENTRIES, DEFAULT_SOLCAST_ENTRIES),
                ),
                _config_entry_checklist(solcast_entries),
            )
        )

    fields += [
        (
            vol.Optional(
                CONF_EXTRA_PV_POWER_ENTITY,
                description={"suggested_value": options.get(CONF_EXTRA_PV_POWER_ENTITY)},
            ),
            # Same tick-if-detected pattern as house-load above: prefers an
            # auto-detected source (currently SMA Solar), falls back to a
            # manual pick-any-sensor selector when nothing is detected.
            _value_label_checklist(extra_pv_candidates)
            if extra_pv_candidates
            else selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        ),
        (
            vol.Optional(
                CONF_EXTRA_PV_POWER_CAPACITY,
                default=options.get(CONF_EXTRA_PV_POWER_CAPACITY, 0),
            ),
            _power_selector("Wp"),
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
        (
            vol.Optional(
                CONF_HOUSE_LOAD_POWER_ENTITY,
                description={"suggested_value": options.get(CONF_HOUSE_LOAD_POWER_ENTITY)},
            ),
            # Tick a detected P1/smart-meter source (HomeWizard P1, DSMR) when
            # any exist; otherwise fall back to a manual pick-any-sensor
            # selector — unlike the price/solar sources above, "no match" here
            # just means an undetected meter brand, not a missing integration,
            # so this field is never simply omitted.
            _value_label_checklist(house_load_candidates)
            if house_load_candidates
            else selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        ),
        (
            vol.Optional(
                CONF_PEAK_LOAD_THIS_MONTH_ENTITY,
                description={"suggested_value": options.get(CONF_PEAK_LOAD_THIS_MONTH_ENTITY)},
            ),
            # Points at your own "peak load this month" sensor (e.g. a
            # P1/DSMR-derived template sensor) -- no known integration to
            # auto-detect candidates from, so always a plain entity picker.
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        ),
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
    ]

    return vol.Schema(dict(fields)), missing


class AlphaEssLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AlphaESS Local Control."""

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
    """Handle the options flow for AlphaESS Local Control.

    Covers device physical specs, pricing, API tokens, optional local
    sensors, and scheduler tuning (see const.py) — settings consumed by
    later phases (prices, solar, scheduler), not by the Modbus driver.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the options step."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema, missing = _options_schema(self.hass, self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "solar_forecast_note": _missing_source_note(self.hass, missing)
            },
        )
