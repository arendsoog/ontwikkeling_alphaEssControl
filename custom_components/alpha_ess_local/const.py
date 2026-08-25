"""Constants for the AlphaESSControl integration."""

from datetime import timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "alpha_ess_local"
DEFAULT_NAME = "AlphaESS"
DEFAULT_PORT = 502
SCAN_INTERVAL = timedelta(seconds=30)

# Options flow: device physical specs (config.ini [General])
# CONF_PV_POWER is not itself a form field -- it's computed from
# CONF_PV_PANEL_WP * CONF_PV_PANEL_COUNT each time the options form is
# submitted (config_flow.py's async_step_init) and stored alongside them, so
# every other reader (e.g. orchestrator.py's solar-regression calibration)
# keeps consuming a single total wattage. Entering Wp-per-panel and panel
# count separately is much harder to fat-finger than one raw watt total.
CONF_PV_POWER = "pv_power"
CONF_PV_PANEL_WP = "pv_panel_wp"
CONF_PV_PANEL_COUNT = "pv_panel_count"
CONF_USABLE_BATTERY_CAPACITY = "usable_battery_capacity"
CONF_INVERTER_NOMINAL_POWER = "inverter_nominal_power"

# Options flow: pricing (config.ini [General])
CONF_PROVIDER_USE_FEE = "provider_use_fee"
CONF_PROVIDER_RETURN_FEE = "provider_return_fee"
CONF_VAT_PERCENTAGE = "vat_percentage"
DEFAULT_PROVIDER_USE_FEE = 0.01815
DEFAULT_PROVIDER_RETURN_FEE = 0.01815
DEFAULT_VAT_PERCENTAGE = 21
# Some contracts (e.g. Belgian Frank Energie's teruglevering formula) are
# explicitly VAT-exempt on the feed-in/return price while still taxing use
# normally -- off by default so every existing installation keeps applying
# VAT to both sides, matching prior behavior.
CONF_APPLY_VAT_ON_RETURN = "apply_vat_on_return"
DEFAULT_APPLY_VAT_ON_RETURN = True
# Netbeheerder ("Afname") per-kWh network cost -- separate from
# CONF_PROVIDER_USE_FEE (the energy *supplier's* fee) and from
# CONF_PEAK_LOAD_THIS_MONTH_ENTITY/max_grid_load (the capaciteitstarief,
# billed on peak kW, not per kWh). Day/night differentiated since Belgian
# digital-meter net tariffs commonly are; only ever applied where the real
# tariff state is known (live DSMR sampling), never to the forward-looking
# DP schedule -- see orchestrator.py's _dsmr_tariff_indicator.
CONF_NETWORK_USE_FEE_NORMAL = "network_use_fee_normal"
CONF_NETWORK_USE_FEE_LOW = "network_use_fee_low"
DEFAULT_NETWORK_USE_FEE = 0.0

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
# Same Wp-per-panel * panel-count split as CONF_PV_POWER above -- see its
# comment.
CONF_EXTRA_PV_POWER_CAPACITY = "extra_pv_power_capacity"
CONF_EXTRA_PV_PANEL_WP = "extra_pv_panel_wp"
CONF_EXTRA_PV_PANEL_COUNT = "extra_pv_panel_count"

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

# Max grid load (kW) during the scheduler's own deliberate grid-charge
# (CHARGING_ON_GRID) — caps house load + charge power together, not just the
# charge power alone, so the DP won't push the total grid import peak above
# this. Aimed at Belgian "capaciteitstarief"/capaciteitskost, which is billed
# on your highest 15-minute grid-import peak per month — keeping that peak
# low is the whole point of this slider, independent of any price-arbitrage
# reasoning elsewhere in the scheduler. Same "Bediening" live-slider pattern
# as the SOC bounds above (number.py); default matches schedule.py's existing
# hardcoded CHARGE_LIMIT (10 kW) so a caller that doesn't pass it, or an
# installation that hasn't touched the slider yet, keeps prior behavior.
DEFAULT_MAX_GRID_LOAD = 10.0

# Optional entity pointing at an external "peak load this month" sensor
# (e.g. a P1/DSMR-derived template sensor tracking the month's highest
# 15-min-average grid-import power). The capaciteitstarief bills on that one
# monthly peak regardless of how many times it's reached — so once a peak
# higher than max_grid_load has already occurred this month (from house load
# alone, which this integration can't control), charging up to that
# already-paid-for level costs nothing extra. Treated as a floor, not a
# ceiling: the *effective* cap used each cycle is
# max(max_grid_load slider, this sensor's current value) — never lower than
# the slider. Optional; if unset or unavailable, the slider alone applies
# (today's behavior).
CONF_PEAK_LOAD_THIS_MONTH_ENTITY = "peak_load_this_month_entity"

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
