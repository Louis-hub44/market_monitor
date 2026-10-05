"""Harmonised level history for a set of instruments.

Pipeline: referential -> symbol map -> MarketDataService (provider fallback + cache)
-> per-source unit scaling -> derived instruments -> sanity checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import pandas as pd

from market_monitor.data.models import DateLike
from market_monitor.data.quality import assemble_frame, empty_series
from market_monitor.data.service import MarketDataService
from market_monitor.referential import Instrument, Referential

DERIVED_SOURCE = "derived"


@dataclass
class UniverseHistory:
    """Levels in referential units, one column per requested instrument id.

    Attributes:
        levels: DatetimeIndex x ids, float64, NaN where missing.
        sources: id -> provider name (``"derived"`` for computed instruments).
        proxies: id -> description, when the serving source is a proxy.
        errors / warnings: id -> message (same semantics as the data layer).
    """

    levels: pd.DataFrame
    sources: dict[str, str] = field(default_factory=dict)
    proxies: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)


def load_universe_history(
    service: MarketDataService,
    referential: Referential,
    ids: Iterable[str],
    start: DateLike,
    end: DateLike,
) -> UniverseHistory:
    """Fetch, harmonise and derive the levels of ``ids`` between ``start`` and ``end``."""
    ids = tuple(dict.fromkeys(ids))
    raw_ids = referential.raw_dependencies(ids)
    fetched = service.get_history(referential.symbol_map(raw_ids), start, end)
    out = UniverseHistory(levels=pd.DataFrame())
    raw_levels: dict[str, pd.Series] = {}
    for raw_id in raw_ids:
        raw_levels[raw_id] = _scaled(referential.get(raw_id), fetched.data[raw_id],
                                     fetched.sources.get(raw_id), out)
        _copy_diagnostics(raw_id, fetched.errors, fetched.warnings, out)
    series: dict[str, pd.Series] = {}
    for instrument_id in ids:
        inst = referential.get(instrument_id)
        series[instrument_id] = (
            _derive(inst, raw_levels, out) if inst.is_derived else raw_levels[instrument_id]
        )
        _sanity_check(inst, series[instrument_id], out)
    out.levels = assemble_frame(series, ids)
    _drop_unrequested(out, set(ids))
    return out


def _scaled(inst: Instrument, series: pd.Series, source: str | None, out: UniverseHistory) -> pd.Series:
    """Apply the serving provider's ``scale`` and record proxies."""
    clean = series.dropna()
    if source is None or clean.empty:
        return clean
    spec = inst.tickers[source]
    out.sources[inst.id] = source
    if spec.proxy:
        out.proxies[inst.id] = spec.proxy
    return clean * spec.scale


def _copy_diagnostics(
    instrument_id: str, errors: Mapping[str, str], warnings: Mapping[str, str], out: UniverseHistory
) -> None:
    if instrument_id in errors:
        out.errors[instrument_id] = errors[instrument_id]
    if instrument_id in warnings:
        out.warnings[instrument_id] = warnings[instrument_id]


def _derive(inst: Instrument, raw_levels: Mapping[str, pd.Series], out: UniverseHistory) -> pd.Series:
    r"""Linear combination on common dates: :math:`L = m \sum_i w_i L_i`."""
    assert inst.derived is not None
    missing = [leg for leg, _ in inst.derived.legs if raw_levels[leg].empty]
    if missing:
        out.errors[inst.id] = f"missing leg(s): {', '.join(missing)}"
        return empty_series()
    legs = pd.concat({leg: raw_levels[leg] * w for leg, w in inst.derived.legs}, axis=1, join="inner")
    if legs.empty:
        out.errors[inst.id] = "legs have no common date"
        return empty_series()
    out.sources[inst.id] = DERIVED_SOURCE
    degraded = [leg for leg, _ in inst.derived.legs if leg in out.warnings or leg in out.proxies]
    if degraded:
        out.warnings[inst.id] = f"degraded leg(s): {', '.join(degraded)}"
    return legs.sum(axis=1) * inst.derived.multiplier


def _sanity_check(inst: Instrument, series: pd.Series, out: UniverseHistory) -> None:
    """Flag levels outside the plausible range of the quote type (wrong ``scale``?)."""
    if series.empty:
        return
    low, high = inst.sanity_bounds
    last = float(series.iloc[-1])
    if not low <= last <= high:
        message = f"implausible level {last:g} for a {inst.quote} quote (check ticker scale)"
        previous = out.warnings.get(inst.id)
        out.warnings[inst.id] = f"{previous}; {message}" if previous else message


def _drop_unrequested(out: UniverseHistory, keep: set[str]) -> None:
    """Legs fetched only to build derived instruments are not reported."""
    for mapping in (out.sources, out.proxies, out.errors, out.warnings):
        for key in [k for k in mapping if k not in keep]:
            del mapping[key]
