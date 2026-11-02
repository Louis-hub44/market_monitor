"""Curve templates: which market quote (logical id) feeds which bootstrap helper."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import pandas as pd

from rates_analytics.curves.helpers import RateHelper
from rates_analytics.curves.interpolation import CurveError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurveInstrument:
    """One line of a curve template.

    Attributes:
        market_id: logical id of the quote (``EUR.EURIBOR6M.SWAP.10Y``).
        kind: ``deposit`` | ``fra`` | ``ois`` | ``swap``.
        tenor: ``ON``, ``6M``, ``10Y``... or ``6X12`` for FRAs.
    """

    market_id: str
    kind: str
    tenor: str


def as_quote_mapping(quotes) -> dict[str, float]:
    """Accept a dict, a ``Series`` or a service quote ``DataFrame`` (``value`` column); drop NaN."""
    if isinstance(quotes, pd.DataFrame):
        quotes = quotes["value"]
    if isinstance(quotes, pd.Series):
        quotes = quotes.to_dict()
    return {k: float(v) for k, v in dict(quotes).items() if v is not None and pd.notna(v)}


def make_helpers(
    instruments: Sequence[CurveInstrument],
    quotes: Mapping[str, float] | pd.Series | pd.DataFrame,
    factory: Callable[[CurveInstrument, float], RateHelper],
) -> tuple[list[RateHelper], dict[str, str]]:
    """Build helpers for the available quotes.

    Returns ``(helpers, skipped)`` where ``skipped`` maps ids to the reason
    (missing quote, or pillar already taken by a shorter instrument).
    """
    q = as_quote_mapping(quotes)
    helpers: list[RateHelper] = []
    skipped: dict[str, str] = {}
    for ins in instruments:
        if ins.market_id not in q:
            skipped[ins.market_id] = "no quote"
            continue
        helpers.append(factory(ins, q[ins.market_id]))
    seen: dict[date, str] = {}
    unique: list[RateHelper] = []
    for h in sorted(helpers, key=lambda x: x.pillar_date):
        key = h.market_id or h.label
        if h.pillar_date in seen:
            skipped[key] = f"same pillar as {seen[h.pillar_date]}"
            continue
        seen[h.pillar_date] = key
        unique.append(h)
    return unique, skipped


def check_enough(helpers: Sequence[RateHelper], minimum: int, name: str) -> None:
    if len(helpers) < minimum:
        raise CurveError(f"{name}: only {len(helpers)} instruments available (min {minimum})")
