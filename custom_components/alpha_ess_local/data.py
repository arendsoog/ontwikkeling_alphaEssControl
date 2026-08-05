"""
Data types – vertaling van Data.h
"""

from dataclasses import dataclass, field
from enum import IntEnum


class Earning(IntEnum):
    NO_EARNING = 0
    EARNING_ON_RETURN = 1  # Geld verdienen op teruglevering
    EARNING_ON_USE = 2  # Negatieve prijs: geld verdienen op afname


class Charge(IntEnum):
    CHARGING_ON_PV = 0  # Laden via PV
    NO_CHARGING = 1  # Niet laden
    NO_DISCHARGING = 2  # Niet ontladen
    CHARGING_ON_GRID = 3  # Laden via net
    CHARGING_DISCHARGE = 4  # Ontladen


MAX_FIVE_MINS = 12
MAX_QUARTERS = 4
MAX_HOURS = 24
MAX_WEEK_DAYS = 7

# SOC constanten (x10, dus 900 = 90.0%)
SOC_MAX = 900
SOC_MAX_CHARGE_PROVIDER = 200
SOC_MAX_BATTERY = 1000
SOC_MAX_CHARGE_ON_GRID = 1000
SOC_MIN = 104
SOC_MIN_DISCHARGE_AFTER = 200

# Wh; noise floor below which a measured solar sample is excluded from regression
MIN_EXPECTED_SOLAR_POWER = 150.0


@dataclass
class FiveMin:
    real_solar_power_roof: float = 0.0
    real_extra_pv_power: float = 0.0
    real_house_load: float = 0.0
    total_active_power: float = 0.0
    battery_power: float = 0.0  # negative = charge, positive = discharge (api.py convention)
    # Derived at sample time (see orchestrator._solar_to_battery/_grid_to_battery)
    # and stored here directly — not recomputed from battery_power later —
    # so a rehydrated/synthetic sample (restart mid-hour) can carry the
    # correct average without needing to fake a battery_power that would
    # reproduce it.
    solar_to_battery: float = 0.0
    grid_to_battery: float = 0.0


@dataclass
class ProviderOverride:
    valid: bool = False
    power_setting: int = 0


@dataclass
class Hour:
    valid: bool = False
    price: float = 0.0
    highest: bool = False
    lowest: bool = False
    earning_on_load_highest: bool = False
    cutoff_soc: int = SOC_MAX
    earning: Earning = Earning.NO_EARNING
    charge: Charge = Charge.CHARGING_ON_PV
    real_charge: Charge = Charge.CHARGING_ON_PV
    feed_in: bool = True
    estimated_start_soc: int = 0
    real_start_soc: int = 0
    estimated_solar_percentage_ned: float = 0.0
    estimated_solar_power: int = 0
    estimated_solar_power_raw: float = 0.0
    estimated_solar_power_roof_factor: float = -1.0
    estimated_solar_power_roof_offset: float = 0.0
    real_solar_power_roof: int = 0
    real_extra_pv_power: int = 0
    estimated_house_load: int = 0
    estimated_house_load_sigma: float = 0.0  # onzekerheid huislast (voor scenario's)
    real_house_load: int = 0
    total_active_power: int = 0
    # Wh (hourly-average W): battery charging power attributed to each
    # source this hour, via energy balance — solar surplus (solar minus
    # house load) is charged to solar first, any additional charging to
    # grid. Not a separately-metered value; the inverter doesn't report a
    # per-source charge split.
    real_solar_to_battery: int = 0
    real_grid_to_battery: int = 0
    estimated_result: float = 0.0
    real_result: float = 0.0
    five_min_count: int = 0
    provider_override: list = field(
        default_factory=lambda: [ProviderOverride() for _ in range(MAX_QUARTERS)]
    )
    five_min: list = field(default_factory=lambda: [FiveMin() for _ in range(MAX_FIVE_MINS)])


@dataclass
class Day:
    valid: bool = False
    year: int = 0
    mon: int = 0
    day: int = 0
    earning_on_return_all_day: bool = False
    index_highest: int = -1
    index_lowest: int = -1
    index_charge: int = -1
    index_discharge: int = -1
    charge_on_grid_used: bool = False
    discharge_used: bool = False
    hour: list = field(default_factory=lambda: [Hour() for _ in range(MAX_HOURS)])
