"""Constants for the AlphaESS Local Control integration."""

from datetime import timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "alpha_ess_local"
DEFAULT_NAME = "AlphaESS"
DEFAULT_PORT = 502
SCAN_INTERVAL = timedelta(seconds=30)

# Options flow: device physical specs (config.ini [General])
CONF_PV_POWER = "pv_power"
CONF_USABLE_BATTERY_CAPACITY = "usable_battery_capacity"
CONF_INVERTER_NOMINAL_POWER = "inverter_nominal_power"

# Options flow: pricing (config.ini [General])
CONF_PROVIDER_USE_FEE = "provider_use_fee"
CONF_PROVIDER_RETURN_FEE = "provider_return_fee"
CONF_VAT_PERCENTAGE = "vat_percentage"
DEFAULT_PROVIDER_USE_FEE = 0.01815
DEFAULT_PROVIDER_RETURN_FEE = 0.01815
DEFAULT_VAT_PERCENTAGE = 21

# Options flow: price source entities (config.ini [General])
# Each references an existing sensor entity from another HA integration
# (e.g. the ENTSO-e / Frank Energie HACS integrations) that already provides
# that data — including any country/zone setup, done once in that
# integration rather than duplicated here. No raw tokens are stored.
CONF_ENTSOE_PRICE_ENTITY = "entsoe_price_entity"
CONF_FRANK_ENERGIE_PRICE_ENTITY = "frank_energie_price_entity"

# Options flow: solar forecast source config entries (config.ini [General])
# Unlike the price sources above, these reference *config entries* (not
# entities) — Forecast.Solar and Solcast both expose their full hourly
# forecast only via HA's "energy platform" cross-integration hook
# (async_get_solar_forecast(hass, config_entry_id)), not an entity attribute.
# Each is a *list* of entry IDs (checkbox multi-select): a roof with more
# than one orientation (e.g. east + west) needs one Forecast.Solar entry per
# plane, and those planes' forecasts are summed together — see solar.py.
CONF_FORECAST_SOLAR_ENTRIES = "forecast_solar_entries"
CONF_SOLCAST_ENTRIES = "solcast_entries"
DEFAULT_FORECAST_SOLAR_ENTRIES: list[str] = []
DEFAULT_SOLCAST_ENTRIES: list[str] = []

# Options flow: optional local sensors (config.ini [General])
# "extra" = a second, separately-metered PV installation besides the main
# roof array (config.ini's ShellyPMPVPower/ShellyPMNetworkName) — not
# necessarily garage-located, just wherever the extra array/meter is.
CONF_EXTRA_PV_POWER_ENTITY = "extra_pv_power_entity"
CONF_EXTRA_PV_POWER_CAPACITY = "extra_pv_power_capacity"

# Options flow: extra-PV price-based on/off control. A Modbus write to a
# *different* device (e.g. a second SMA inverter) via HA's core `modbus`
# integration's `write_register` service — not the same Modbus connection
# api.py uses for the AlphaESS inverter itself. The register layout isn't
# the same for every SMA installation, so hub/slave/address/values are all
# user-configurable rather than hardcoded. CONF_EXTRA_PV_CONTROL_ENABLED is
# a dedicated feature toggle (default off) — separate from
# CONF_CONTROL_ENABLED below — so the hub/register config can be entered
# once and paused/resumed without clearing it. Even when enabled, an actual
# Modbus write still only happens when CONF_CONTROL_ENABLED (the master
# real-write switch) is also on; otherwise this only logs its decision.
CONF_EXTRA_PV_CONTROL_ENABLED = "extra_pv_control_enabled"
CONF_EXTRA_PV_MODBUS_HUB = "extra_pv_modbus_hub"
CONF_EXTRA_PV_MODBUS_SLAVE = "extra_pv_modbus_slave"
CONF_EXTRA_PV_MODBUS_ADDRESS = "extra_pv_modbus_address"
CONF_EXTRA_PV_MODBUS_ON_VALUE = "extra_pv_modbus_on_value"
CONF_EXTRA_PV_MODBUS_OFF_VALUE = "extra_pv_modbus_off_value"
DEFAULT_EXTRA_PV_CONTROL_ENABLED = False
DEFAULT_EXTRA_PV_MODBUS_SLAVE = 1
DEFAULT_EXTRA_PV_MODBUS_ADDRESS = 0
DEFAULT_EXTRA_PV_MODBUS_ON_VALUE = 100
DEFAULT_EXTRA_PV_MODBUS_OFF_VALUE = 0

CONF_HOUSE_LOAD_POWER_ENTITY = "house_load_power_entity"

# Options flow: scheduler tuning (config.ini [Daily])
CONF_ALLOW_PROVIDER_CONTROL_HOURS = "allow_provider_control_hours"
DEFAULT_ALLOW_PROVIDER_CONTROL_HOURS: list[str] = []
# Minimum daily profit (EUR) the optimized schedule must clear over the
# baseline before it's used instead of falling back — not an options-flow
# field: a live-adjustable `number` entity (number.py), same "Bediening"
# pattern as the SOC bounds. Stored there in whole cents (0-100 slider);
# DEFAULT_DAILY_MIN_PROFIT stays in EUR since that's the unit
# ScheduleConfig/schedule.py actually consume.
DEFAULT_DAILY_MIN_PROFIT = 0.40

# Scheduler SOC bounds. Previously hardcoded (data.py's
# SOC_MAX/SOC_MAX_CHARGE_ON_GRID/SOC_MIN_DISCHARGE_AFTER) — the user wants
# to tune these themselves, live: a lower ceiling on positive-price hours
# (battery lifespan), a higher one on negative-price hours (take full
# advantage — "maximale netontlasting"), and the floor the scheduler won't
# discharge the battery below when it deliberately chooses to discharge
# (also lifespan). Not options-flow fields — each is its own live-adjustable
# `NumberEntity` directly on this integration's device (number.py), the
# same "Bediening" pattern other integrations (e.g. Alfen Wallbox) use for
# device settings, rather than a value tucked away in a settings dialog.
# DEFAULT_* seed each entity's initial value and are what orchestrator.py
# falls back to if the entity is ever genuinely unavailable.
DEFAULT_MAX_SOC_POSITIVE_PRICE = 90.0
DEFAULT_MAX_SOC_NEGATIVE_PRICE = 100.0
DEFAULT_MIN_SOC_DISCHARGE = 20.0

# Options flow: dispatch control (Phase 7b)
# CONF_CONTROL_ENABLED is the master switch for real writes to the inverter
# (async_set_dispatch_param/async_set_max_feed_into_grid) — default off, so
# the dispatch coordinator only ever computes and logs its decision until
# the user explicitly flips this on themselves.
CONF_CONTROL_ENABLED = "control_enabled"
DEFAULT_CONTROL_ENABLED = False
# Whether the once-per-day grid-charge/discharge budget survives a Home
# Assistant restart (persisted to the SQLite DB) or resets like the
# original C program's plain in-memory state.
CONF_PERSIST_DAILY_CHARGE_LIMIT = "persist_daily_charge_limit"
DEFAULT_PERSIST_DAILY_CHARGE_LIMIT = True
