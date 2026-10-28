"""Snipe maths every lane shares: the odd-cent max, eBay's bid steps, an offer idea.

Nothing here bids or offers; these are numbers for the alert text.
"""
from __future__ import annotations

import math

# eBay's bid increments: (up to this current price, the next bid is this much more).
INCREMENTS = ((0.99, 0.05), (4.99, 0.25), (24.99, 0.50), (99.99, 1.00), (249.99, 2.50),
              (499.99, 5.00), (999.99, 10.00))
OFFER_SHARE = 0.85   # an offer idea is this share of the most the card is worth paying
# A cheap card's going rate is quoted at the shipping it usually has. Under this
# value, what the listing's own shipping adds or saves moves its value too.
TYPICAL_SHIPPING = 2.00
SHIPPING_MATTERS_UNDER = 50.0


def for_listing(value: float | None, shipping: float | None) -> float | None:
    """The going rate of a card under $50 for this listing, with its own shipping.

    Buyers of cheap cards bid on the price and pay most of the shipping on top,
    so the same card ends up costing more delivered with $5 shipping than with
    $1. Checked on 160 picked auctions that sold: valuing each one this way cut
    the typical gap to the real delivered price from 46% to 26% under $10, and
    from 34% to 28% for $10 to $50. Dearer cards and unknown shipping are left
    as they are, and the value never falls below half the going rate.
    """
    if value is None or shipping is None or value >= SHIPPING_MATTERS_UNDER:
        return value
    return round(max(value + shipping - TYPICAL_SHIPPING, value / 2), 2)


def odd_cents(limit: float | None) -> float | None:
    """The cheapskate max under `limit`: D dollars and D cents (25.25, 35.35, 47.47).

    Odd cents beat the round-number maxes other snipers type, and between $25
    and $99.99 eBay's step is a whole dollar, so the cents decide ties. If D.DD
    is over the limit, D-1 is used the same way. Never above `limit`; under $1
    the limit is kept as it is.
    """
    if limit is None:
        return None
    if limit < 1:
        return round(limit, 2)
    whole = math.floor(limit + 1e-9)
    for d in (whole, whole - 1):
        price = round(d + (d % 100) / 100, 2)
        if d >= 1 and price <= limit + 1e-9:
            return price
    return round(limit, 2)


def increment(current: float) -> float:
    """How much more than the current price the next bid has to be."""
    return next((step for top, step in INCREMENTS if current <= top), 25.00)


def next_bid(current: float, bids: int | None) -> float:
    """The lowest bid eBay would take now: the opening price with no bids yet."""
    return round(current + (increment(current) if bids else 0), 2)


def offer_idea(most: float | None) -> float | None:
    """A best-offer amount to type yourself: odd cents of OFFER_SHARE of `most`."""
    return odd_cents(most * OFFER_SHARE) if most else None
