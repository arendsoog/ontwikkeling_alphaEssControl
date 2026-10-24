"""Day-ahead price handling for AlphaESSControl.

Port of Prices.c. Unlike the original, prices are read from existing HA
entities (e.g. the ENTSO-e / Frank Energie / Nord Pool integrations) rather
than fetched via our own HTTP calls to those services. The options flow's
price_entities is an ordered list: the first entity with prices for a day is
used, the next ones are backups.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTSOE_PRICE_ENTITY,
    CONF_FRANK_ENERGIE_PRICE_ENTITY,
    CONF_PRICE_ENTITIES,
    CONF_PRICE_SOURCE_PRIMARY,
    DEFAULT_PRICE_SOURCE_PRIMARY,
    LOGGER,
    PRICE_SOURCE_FRANK_ENERGIE,
)
from .data import Day, Earning

_ENTSOE_MWH_UNITS = {"eur/mwh", "€/mwh"}

# The attributes a price entity can carry its day-ahead prices in: ENTSO-e
# (prices_today/prices_tomorrow), Frank Energie (prices), Nord Pool
# (raw_today/raw_tomorrow). Used to tell price entities from other sensors.
PRICE_LIST_ATTRIBUTES = ("prices_today", "prices", "raw_today")


def configured_price_entities(options: Mapping[str, Any]) -> list[str]:
    """The price entities in the order they're tried.

    Installations set up before price_entities existed still have one
    ENTSO-e and one Frank Energie entity plus which of the two goes first;
    those are read as that same ordered list until the prices are saved
    again.
    """
    if CONF_PRICE_ENTITIES in options:
        return [entity for entity in options[CONF_PRICE_ENTITIES] or [] if entity]
    legacy = [options.get(CONF_ENTSOE_PRICE_ENTITY), options.get(CONF_FRANK_ENERGIE_PRICE_ENTITY)]
    primary = options.get(CONF_PRICE_SOURCE_PRIMARY, DEFAULT_PRICE_SOURCE_PRIMARY)
    if primary == PRICE_SOURCE_FRANK_ENERGIE:
        legacy.reverse()
    return [entity for entity in legacy if entity]


def is_price_entity(state: State) -> bool:
    """Whether a sensor carries day-ahead prices this module can read."""
    return any(isinstance(state.attributes.get(name), list) for name in PRICE_LIST_ATTRIBUTES)


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


def _parse_nordpool_prices(state: State, target_date: date) -> dict[int, float] | None:
    """Parse Nord Pool's raw_today/raw_tomorrow attributes for a calendar date.

    Each entry is {"start": <datetime>, "end": <datetime>, "value": <float>},
    in the entity's unit -- converted from cents or per MWh to EUR/kWh.
    """
    unit = str(state.attributes.get("unit_of_measurement") or "").strip().lower()
    scale = 1.0
    if state.attributes.get("price_in_cents") or unit.startswith(("c/", "ct/", "cent")):
        scale = 0.01
    elif unit.endswith("/mwh"):
        scale = 0.001

    prices: dict[int, list[float]] = {}
    for attribute in ("raw_today", "raw_tomorrow"):
        for entry in state.attributes.get(attribute) or []:
            try:
                start = _as_datetime(entry["start"])
                value = entry["value"]
                if value is None:
                    continue
                price = float(value) * scale
            except (KeyError, TypeError, ValueError) as exception:
                LOGGER.warning(
                    "Nord Pool entity %s: could not parse price entry %r (%s)",
                    state.entity_id,
                    entry,
                    exception,
                )
                return None
            if start is None:
                continue
            local_start = dt_util.as_local(start)
            if local_start.date() == target_date:
                prices.setdefault(local_start.hour, []).append(price)

    return _hourly_means(prices) or None


def parse_entity_prices(state: State, target_date: date) -> dict[int, float] | None:
    """One price entity's prices for `target_date`, whichever shape it uses."""
    attributes = state.attributes
    if "prices_today" in attributes:
        attribute = "prices_today" if target_date == dt_util.now().date() else "prices_tomorrow"
        return _parse_entsoe_prices(state, attribute)
    if "prices" in attributes:
        return _parse_frank_energie_prices(state, target_date)
    if "raw_today" in attributes:
        return _parse_nordpool_prices(state, target_date)
    LOGGER.debug("Entity %s carries no prices this integration can read", state.entity_id)
    return None


def read_hour_prices(
    hass: HomeAssistant, entity_ids: list[str], target_date: date
) -> dict[int, float] | None:
    """Port of ReadPrices: the first entity's prices, falling back to the next."""
    return read_hour_prices_with_source(hass, entity_ids, target_date)[0]


def read_hour_prices_with_source(
    hass: HomeAssistant, entity_ids: list[str], target_date: date
) -> tuple[dict[int, float] | None, str]:
    """The prices of the first entity in `entity_ids` that has them.

    Also returns that entity's id ("" if none had any).
    """
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        prices = parse_entity_prices(state, target_date) if state else None
        if prices:
            LOGGER.debug("Found prices for %s (using %s)", target_date, entity_id)
            return prices, entity_id
        LOGGER.debug("No prices for %s from %s", target_date, entity_id)

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
    entity_ids: list[str],
    use_fee: float,
    return_fee: float,
    vat_percentage: float,
    return_vat_percentage: float | None = None,
) -> Day:
    """Build a Day of prices for target_date from the configured price entities."""
    day = Day(year=target_date.year, mon=target_date.month, day=target_date.day)

    prices, day.source = read_hour_prices_with_source(hass, entity_ids, target_date)
    if not prices:
        return day

    for hour_index, price in prices.items():
        day.hour[hour_index].valid = True
        day.hour[hour_index].price = price

    apply_earning_classification(day, use_fee, return_fee, vat_percentage, return_vat_percentage)
    day.valid = True
    return day
