r"""Multi-asset comparison: rebased histories and period statistics.

Base 100 is only meaningful for price-like series. Rates and spreads are compared on
their **cumulative change in bp** from the same origin, and vol indices on their change
in points; each convention gets its own panel:

* ``pct`` : :math:`100 \, L_t / L_{t_b}`
* ``bp``  : :math:`k (L_t - L_{t_b})` (:math:`k = 100` for yields in %, 1 for spreads in bp)
* ``abs`` : :math:`L_t - L_{t_b}`

The origin :math:`t_b` is the first date on which **every** selected series is available,
so that all lines start together.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from market_monitor.analytics.performance import change, daily_changes
from market_monitor.referential import ChangeUnit, Instrument

PERIODS = ("1M", "3M", "6M", "YTD", "1A", "3A", "5A")
PERIOD_DAYS = {"1M": 31, "3M": 92, "6M": 183, "1A": 365, "3A": 3 * 365 + 1, "5A": 5 * 365 + 1}
ANNUALISATION = 252
GROUP_ORDER = (ChangeUnit.PCT, ChangeUnit.BP, ChangeUnit.ABS)
LATE_START_TOLERANCE_DAYS = 7


def period_start(period: str, as_of: date) -> date:
    """First date of a display period ending at ``as_of`` (YTD = 31 Dec of last year)."""
    if period == "YTD":
        return date(as_of.year - 1, 12, 31)
    if period not in PERIOD_DAYS:
        raise ValueError(f"unknown period {period!r} (allowed: {PERIODS})")
    return as_of - timedelta(days=PERIOD_DAYS[period])


def normalise(series: pd.Series, unit: ChangeUnit, bp_factor: float | None,
              base_value: float, base: float = 100.0) -> pd.Series:
    """Rebase a level series on ``base_value`` under the instrument convention."""
    if unit is ChangeUnit.PCT:
        if not base_value > 0:
            raise ValueError("base 100 requires a positive base value")
        return base * series / base_value
    if unit is ChangeUnit.BP:
        return (bp_factor or 1.0) * (series - base_value)
    return series - base_value


def max_drawdown(series: pd.Series) -> float:
    """Worst peak-to-trough fall, in % (non-positive)."""
    if series.empty:
        return math.nan
    return float(100.0 * (series / series.cummax() - 1.0).min())


@dataclass
class ComparisonResult:
    """Rebased series (columns = ids), grouping by convention and period statistics."""

    series: pd.DataFrame
    groups: dict[ChangeUnit, list[str]]
    base_date: pd.Timestamp | None
    stats: pd.DataFrame
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)


def compare(
    levels: pd.DataFrame, instruments: Sequence[Instrument], start: date, end: date
) -> ComparisonResult:
    """Rebase every instrument on the common origin within ``[start, end]``."""
    available = _available(levels, instruments, start, end)
    errors = {i.id: "no data over the period" for i in instruments if i.id not in available}
    if not available:
        return ComparisonResult(pd.DataFrame(), {}, None, pd.DataFrame(), errors)
    base_date = max(s.index[0] for s in available.values())
    rebased: dict[str, pd.Series] = {}
    rows: dict[str, dict[str, float]] = {}
    warnings: dict[str, str] = {}
    for inst in (i for i in instruments if i.id in available):
        full = available[inst.id]
        s = full[full.index >= base_date]
        rebased[inst.id] = normalise(s, inst.change, inst.bp_factor, float(s.iloc[0]))
        rows[inst.id] = period_stats(s, inst)
        if (full.index[0] - pd.Timestamp(start)).days > LATE_START_TOLERANCE_DAYS:
            warnings[inst.id] = f"history starts on {full.index[0]:%Y-%m-%d}"
    groups = {u: [i for i in rebased if _unit(instruments, i) is u] for u in GROUP_ORDER}
    return ComparisonResult(
        series=pd.DataFrame(rebased),
        groups={u: ids for u, ids in groups.items() if ids},
        base_date=base_date,
        stats=pd.DataFrame.from_dict(rows, orient="index"),
        errors=errors,
        warnings=warnings,
    )


def _available(
    levels: pd.DataFrame, instruments: Sequence[Instrument], start: date, end: date
) -> dict[str, pd.Series]:
    window = levels.loc[pd.Timestamp(start): pd.Timestamp(end)]
    out = {}
    for inst in instruments:
        if inst.id in window.columns and window[inst.id].notna().any():
            out[inst.id] = window[inst.id].dropna()
    return out


def _unit(instruments: Sequence[Instrument], instrument_id: str) -> ChangeUnit:
    return next(i.change for i in instruments if i.id == instrument_id)


def period_stats(series: pd.Series, inst: Instrument) -> dict[str, float]:
    """Start / end levels, change, annualised volatility, max drawdown (prices), range."""
    first, last = float(series.iloc[0]), float(series.iloc[-1])
    steps = daily_changes(series, inst.change, inst.bp_factor).dropna()
    vol = float(steps.std(ddof=1)) * math.sqrt(ANNUALISATION) if len(steps) >= 10 else math.nan
    return {
        "start": first,
        "end": last,
        "change": change(last, first, inst.change, inst.bp_factor),
        "ann_vol": vol,
        "max_drawdown": max_drawdown(series) if inst.change is ChangeUnit.PCT else math.nan,
        "low": float(series.min()),
        "high": float(series.max()),
    }
