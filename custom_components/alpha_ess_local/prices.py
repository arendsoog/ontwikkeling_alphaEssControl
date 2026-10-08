"""Day-ahead price handling for AlphaESSControl.

Port of Prices.c. Unlike the original, prices are read from existing HA
entities (the ENTSO-e / Frank Energie integrations, configured via the
options flow's entsoe_price_entity/frank_energie_price_entity) rather than
fetched via our own HTTP calls to those services.
"""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_PRICE_SOURCE_PRIMARY,
    LOGGER,
    PRICE_SOURCE_ENTSOE,
    PRICE_SOURCE_FRANK_ENERGIE,
)
from .data import Day, Earning

_ENTSOE_MWH_UNITS = {"eur/mwh", "€/mwh"}

# Display names, in the default order of preference.
PRICE_SOURCES = {PRICE_SOURCE_ENTSOE: "ENTSO-E", PRICE_SOURCE_FRANK_ENERGIE: "Frank Energie"}


def _as_datetime(value: object) -> datetime | None:
    """Accept either an ISO string or an already-parsed datetime.

    HA integrations aren't consistent here — some (ENTSO-e) serialize
    timestamps as ISO strings, others (Frank Energie, confirmed live) put
    real `datetime` objects straight into the attribute — `parse_datetime`
    only accepts the former and raises on the latter.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


def mk_use_price(price: float, use_fee: float, vat_percentage: float) -> float:
    """Port of MkUsePrice: raw market price -> price paid per kWh used."""
    return price * (1 + vat_percentage / 100) + use_fee


def mk_return_price(price: float, return_fee: float, vat_percentage: float) -> float:
    """Port of MkReturnPrice: raw market price -> price earned per kWh returned."""
    return price * (1 + vat_percentage / 100) + return_fee


def _entsoe_price_to_eur_per_kwh(price: float, unit: str | None) -> float:
    """Normalize an ENTSO-e price to EUR/kWh (that integration may report MWh)."""
    if unit and unit.strip().lower() in _ENTSOE_MWH_UNITS:
        return price / 1000
    return price


def _parse_entsoe_prices(state: State, attribute: str) -> dict[int, float] | None:
    """Parse ENTSO-e's prices_today/prices_tomorrow attribute.

    Each entry is expected as {"time": <ISO datetime, local tz>, "price": <float>}.
    Returns {hour_0_23: price_eur_per_kwh}, or None if the attribute is
    missing or doesn't match the expected shape (logged either way).
    """
    entries = state.attributes.get(attribute)
    if not entries:
        LOGGER.debug("ENTSO-e entity %s has no '%s' attribute", state.entity_id, attribute)
        return None

    unit = state.attributes.get("unit_of_measurement")
    prices: dict[int, list[float]] = {}
    for entry in entries:
        try:
            timestamp = _as_datetime(entry["time"])
            price = float(entry["price"])
        except (KeyError, TypeError, ValueError) as exception:
            LOGGER.warning(
                "ENTSO-e entity %s: could not parse price entry %r (%s)",
                state.entity_id,
                entry,
                exception,
            )
            return None
        if timestamp is None:
            LOGGER.warning(
                "ENTSO-e entity %s: could not parse timestamp in %r", state.entity_id, entry
            )
            return None
        prices.setdefault(dt_util.as_local(timestamp).hour, []).append(
            _entsoe_price_to_eur_per_kwh(price, unit)
        )

    return _hourly_means(prices)


def _parse_frank_energie_prices(state: State, target_date: date) -> dict[int, float] | None:
    """Parse Frank Energie's 'prices' attribute for a specific calendar date.

    Each entry is expected as {"from": <ISO>, "till": <ISO>, "price": <float,
    EUR/kWh>}. Treated as a raw market price, same as ENTSO-e (see plan doc:
    this integration's exact price variant — raw vs all-in — could not be
    confirmed from source alone and needs live verification).
    """
    entries = state.attributes.get("prices")
    if not entries:
        LOGGER.debug("Frank Energie entity %s has no 'prices' attribute", state.entity_id)
        return None

    prices: dict[int, list[float]] = {}
    for entry in entries:
        try:
            start = _as_datetime(entry["from"])
            price = float(entry["price"])
        except (KeyError, TypeError, ValueError) as exception:
            LOGGER.warning(
                "Frank Energie entity %s: could not parse price entry %r (%s)",
                state.entity_id,
                entry,
                exception,
            )
            return None
        if start is None:
            LOGGER.warning(
                "Frank Energie entity %s: could not parse timestamp in %r", state.entity_id, entry
            )
            return None
        local_start = dt_util.as_local(start)
        if local_start.date() != target_date:
            continue
        prices.setdefault(local_start.hour, []).append(price)

    return _hourly_means(prices) or None


def _hourly_means(prices: dict[int, list[float]]) -> dict[int, float]:
    """One price per hour: the mean of that hour's entries.

    Since the day-ahead market moved to 15-minute products (Oct 2025),
    sources deliver four prices per hour (Frank Energie defaults to PT15M).
    Storing them by hour used to keep only the last quarter (xx:45); the
    scheduler plans per hour, so it needs the hour's average instead. An
    hourly feed (one entry per hour) is unaffected.
    """
    return {hour: sum(values) / len(values) for hour, values in prices.items()}


def read_hour_prices(
    hass: HomeAssistant,
    entsoe_entity_id: str | None,
    frank_energie_entity_id: str | None,
    target_date: date,
    entsoe_attribute: str,
) -> dict[int, float] | None:
    """Port of ReadPrices: try ENTSO-e, fall back to Frank Energie."""
    return read_hour_prices_with_source(
        hass, entsoe_entity_id, frank_energie_entity_id, target_date, entsoe_attribute
    )[0]


def read_hour_prices_with_source(
    hass: HomeAssistant,
    entsoe_entity_id: str | None,
    frank_energie_entity_id: str | None,
    target_date: date,
    entsoe_attribute: str,
    primary: str = DEFAULT_PRICE_SOURCE_PRIMARY,
) -> tuple[dict[int, float] | None, str]:
    """The prices from `primary` if it has them, else from the other source.

    Also returns which source they came from ("" if neither had any).
    """

    def entsoe() -> dict[int, float] | None:
        state = hass.states.get(entsoe_entity_id) if entsoe_entity_id else None
        return _parse_entsoe_prices(state, entsoe_attribute) if state else None

    def frank_energie() -> dict[int, float] | None:
        state = hass.states.get(frank_energie_entity_id) if frank_energie_entity_id else None
        return _parse_frank_energie_prices(state, target_date) if state else None

    readers = {PRICE_SOURCE_ENTSOE: entsoe, PRICE_SOURCE_FRANK_ENERGIE: frank_energie}
    configured = {
        PRICE_SOURCE_ENTSOE: entsoe_entity_id,
        PRICE_SOURCE_FRANK_ENERGIE: frank_energie_entity_id,
    }
    for source in sorted(readers, key=lambda s: s != primary):
        if not configured[source]:
            continue
        prices = readers[source]()
        if prices:
            LOGGER.debug("Found prices for %s (using %s)", target_date, PRICE_SOURCES[source])
            return prices, source
        LOGGER.debug("No %s prices for %s", PRICE_SOURCES[source], target_date)

    return None, ""


def apply_earning_classification(
    day: Day,
    use_fee: float,
    return_fee: float,
    vat_percentage: float,
    return_vat_percentage: float | None = None,
) -> None:
    """Port of ReadPricesAfterMath: classify earning per hour, find highest/lowest.

    `return_vat_percentage` overrides `vat_percentage` for the return-price
    side only (e.g. a BTW-exempt teruglevering formula) -- defaults to
    `vat_percentage` (apply VAT to both sides, prior behavior) when omitted.
    """
    effective_return_vat = (
        return_vat_percentage if return_vat_percentage is not None else vat_percentage
    )
    for hour in day.hour:
        if not hour.valid:
            continue

        profit_on_return = mk_return_price(hour.price, return_fee, effective_return_vat)
        cost_on_use = mk_use_price(hour.price, use_fee, vat_percentage)

        if profit_on_return > 0 and cost_on_use < 0:
            if profit_on_return >= -cost_on_use:
                cost_on_use = 0
            else:
                profit_on_return = 0

        if profit_on_return > 0 or (profit_on_return == 0 and cost_on_use >= 0):
            hour.earning = Earning.EARNING_ON_RETURN
        if cost_on_use < 0:
            hour.earning = Earning.EARNING_ON_USE

    valid_hours = [(index, hour) for index, hour in enumerate(day.hour) if hour.valid]
    if not valid_hours:
        return

    day.earning_on_return_all_day = all(
        hour.earning == Earning.EARNING_ON_RETURN for _, hour in valid_hours
    )

    lowest_index, lowest_hour = min(valid_hours, key=lambda item: item[1].price)
    highest_index, highest_hour = max(valid_hours, key=lambda item: item[1].price)
    day.index_lowest = lowest_index
    day.index_highest = highest_index
    lowest_hour.lowest = True
    highest_hour.highest = True


def build_day(
    hass: HomeAssistant,
    target_date: date,
    entsoe_entity_id: str | None,
    frank_energie_entity_id: str | None,
    use_fee: float,
    return_fee: float,
    vat_percentage: float,
    return_vat_percentage: float | None = None,
    primary: str = DEFAULT_PRICE_SOURCE_PRIMARY,
) -> Day:
    """Build a Day of prices for target_date from the configured source entities."""
    day = Day(year=target_date.year, mon=target_date.month, day=target_date.day)

    entsoe_attribute = "prices_today" if target_date == dt_util.now().date() else "prices_tomorrow"
    prices, day.source = read_hour_prices_with_source(
        hass, entsoe_entity_id, frank_energie_entity_id, target_date, entsoe_attribute, primary
    )
    if not prices:
        return day

    for hour_index, price in prices.items():
        day.hour[hour_index].valid = True
        day.hour[hour_index].price = price

    apply_earning_classification(day, use_fee, return_fee, vat_percentage, return_vat_percentage)
    day.valid = True
    return day
