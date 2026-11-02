r"""Stable integration API for the other modules of the terminal.

Everything here returns plain Python / pandas objects (no engine classes leak
out unless asked), so Valuation Screener, Pricing Lab, PTF Event Radar,
Portfolio Intelligence and the CVA Calculator can plug in without knowing the
internals::

    from rates_analytics import api

    snap = api.market_snapshot(date(2026, 10, 6))          # provider from config.yaml
    df = api.discount_factors(snap.curves, [1.0, 5.0, 10.0])  # €STR discounting
    grid = api.curve_export(snap.curves)                    # CVA Calculator: DF + forwards grid
    payload = api.curves_to_dict(snap.curves)               # JSON-able nodes, rebuilt with curves_from_dict
    table = api.bond_table(snap, snap.bonds)                # Portfolio Intelligence: YAS + spreads per line
    risk = api.swap_risk(snap, "IRS", start, maturity, 0.025, 10e6, pay_fixed=True)

Signatures in this module are the contract: they only change with a major version.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from rates_analytics.bonds import FixedRateBond, bonds_from_static, spread_table
from rates_analytics.bonds.analytics import analytics_table
from rates_analytics.config import Settings, load_settings
from rates_analytics.curves import (
    CurveSet,
    InterpolatedCurve,
    SovereignCurveFit,
    YieldCurve,
    build_eur_curves,
    sovereign_curve_from_market,
)
from rates_analytics.curves.estr import ESTR_CURVE_INSTRUMENTS, ESTR_FIXING_ID
from rates_analytics.curves.ibor import EURIBOR6M_CURVE_INSTRUMENTS
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.proxy import (
    ECB_AAA_NSS_IDS,
    ProxyCurveConfig,
    ProxyReport,
    benchmark_curve_from_market,
    complete_with_proxy,
    term_structure,
)
from rates_analytics.curves.sovereign import resolve_universe
from rates_analytics.data import MarketDataService, build_market_data_service
from rates_analytics.exceptions import RatesAnalyticsError
from rates_analytics.instruments import Fixings, OvernightIndexedSwap, VanillaSwap, fixings_from_market
from rates_analytics.network import NetworkSettings
from rates_analytics.swaps import swpm

API_VERSION = "1.0"
DEFAULT_SOVEREIGNS = ("BUND", "OAT", "BTP")


@dataclass
class MarketSnapshot:
    """Curves, sovereign fits, bonds, prices and fixings for one valuation date."""

    as_of: date
    curves: CurveSet  # set right after construction by market_snapshot
    sovereigns: dict[str, SovereignCurveFit] = field(default_factory=dict)
    bonds: dict[str, FixedRateBond] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    fixings: dict[str, Fixings] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    service: MarketDataService | None = None
    proxy: ProxyReport = field(default_factory=ProxyReport)
    sources: dict[str, str] = field(default_factory=dict)       # what each block was built from
    data_errors: dict[str, str] = field(default_factory=dict)   # id -> provider error (SSL diagnostics)

    def gov_curve(self, name: str = "BUND") -> YieldCurve:
        try:
            return self.sovereigns[name].curve
        except KeyError as exc:
            raise RatesAnalyticsError(f"No sovereign curve {name}; available: {sorted(self.sovereigns)}") from exc


def market_snapshot(as_of: date, settings: Settings | None = None, interpolation: str = "log_linear",
                    sovereigns: Sequence[str] = DEFAULT_SOVEREIGNS, fixings_years: int = 5,
                    network: NetworkSettings | None = None) -> MarketSnapshot:
    """Build everything for ``as_of`` from the configured data provider (``config.yaml`` / ``.env``).

    * swap curves from market quotes, or **proxy** curves from public data when the swap
      quotes are missing (free / FMP modes, see :mod:`rates_analytics.curves.proxy`);
    * sovereign curves from the bond universe, or from generic benchmark yields;
    * ``network`` overrides the SSL policy of ``settings`` (corporate proxy).

    A missing sovereign curve only adds a warning: the swap curves are always required.
    """
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a datetime.date")
    settings = settings or load_settings()
    svc = build_market_data_service(settings, network=network)
    snap = MarketSnapshot(as_of, None, service=svc)  # type: ignore[arg-type]

    def quotes(ids: Sequence[str]) -> dict[str, float]:
        q = svc.quotes(ids, as_of)
        snap.data_errors.update(svc.last_report.errors)
        return q["value"].dropna().to_dict()

    ids = [i.market_id for i in (*ESTR_CURVE_INSTRUMENTS, *EURIBOR6M_CURVE_INSTRUMENTS)]
    market = quotes(ids)
    cfg = ProxyCurveConfig.from_mapping(settings.proxy_curves)
    inputs: dict[str, float] = {}
    n_swaps = sum(1 for k in market if ".OIS." in k or ".SWAP." in k)
    if n_swaps < 6 and cfg.enabled:
        inputs = quotes([ESTR_FIXING_ID, *ECB_AAA_NSS_IDS.values()])
    try:
        full, snap.proxy = complete_with_proxy(as_of, market, inputs, cfg)
    except CurveError as exc:
        raise CurveError(f"Courbes de swap impossibles à construire : {exc}. "
                         f"Erreurs de données : {_first_errors(snap.data_errors)}") from exc
    snap.curves = build_eur_curves(as_of, full, interpolation=interpolation)
    snap.sources["swap curves"] = "proxy" if snap.proxy.used else "market"

    for name in sovereigns:
        try:
            isins, kw = resolve_universe(settings, name, svc)
            snap.sovereigns[name] = sovereign_curve_from_market(svc, as_of, isins, name=name, **kw)
            bonds, _ = bonds_from_static(svc.bond_static(isins))
            snap.bonds.update({b.isin: b for b in bonds})
            snap.prices.update(svc.bond_prices(isins, as_of)["mid"].dropna().to_dict())
            snap.sources[name] = "bonds"
            continue
        except Exception as exc:  # no bond-level data: try the generic benchmark yields
            reason = str(exc)
        country = settings.benchmark_curves.get(name)
        if not country:
            snap.warnings.append(f"{name}: {reason}")
            continue
        try:
            fit, bonds, prices = benchmark_curve_from_market(svc, as_of, name, country)
            snap.sovereigns[name] = fit
            snap.bonds.update({b.isin: b for b in bonds})
            snap.prices.update(prices)
            snap.sources[name] = "benchmarks"
        except Exception as exc:
            snap.data_errors.update(svc.last_report.errors)
            snap.warnings.append(f"{name}: {reason} ; benchmarks : {exc}")

    start = as_of - timedelta(days=365 * fixings_years)
    for mid, name in (("EUR.ESTR.FIX", "ESTR"), ("EUR.EURIBOR6M.FIX", "EURIBOR6M")):
        try:
            snap.fixings[name] = fixings_from_market(svc, mid, start, as_of, name)
        except Exception as exc:
            snap.warnings.append(f"fixings {name}: {exc}")
    if not snap.fixings.get("EURIBOR6M") and snap.fixings.get("ESTR"):
        basis6m = float(term_structure(cfg.euribor6m_basis_bp)(0.5))
        snap.fixings["EURIBOR6M"] = Fixings({d: v + basis6m for d, v in snap.fixings["ESTR"].items()}, "EURIBOR6M")
        snap.sources["EURIBOR6M fixings"] = "proxy (€STR + basis 6M)"
    return snap


def _first_errors(errors: Mapping[str, str], n: int = 2) -> str:
    return " | ".join(f"{k}: {v}" for k, v in list(errors.items())[:n]) or "aucune"


# ------------------------------------------------------------------ curves
def _curve(curves: CurveSet, index: str) -> YieldCurve:
    return curves.forward_curve(index)


def discount_factors(curves: CurveSet, points: Iterable[date | float], index: str = "ESTR") -> pd.Series:
    """Discount factors on dates or year fractions (ACT/365F from ``as_of``)."""
    pts = list(points)
    c = _curve(curves, index)
    return pd.Series([float(c.discount(p)) for p in pts], index=pts, name=f"df_{index.lower()}")


def curve_export(curves: CurveSet, horizon_years: float = 50.0, step_months: int = 1,
                 forward_tenor_months: int = 6) -> pd.DataFrame:
    """Monthly grid with DF, zero rates (cont., ACT/365F) and simple forwards (ACT/360) of every curve.

    Column names: ``t``, ``date``, ``df_<idx>``, ``zero_<idx>``, ``fwd_<idx>`` with ``idx`` in
    ``estr`` and the forwarding indices (``euribor6m`` …). This is the input of the CVA exposure engine.
    """
    if horizon_years <= 0 or step_months <= 0:
        raise ValueError("horizon_years and step_months must be positive")
    names = [curves.overnight_index, *curves.forwards]
    n = int(horizon_years * 12 // step_months)
    dates = [pd.Timestamp(curves.as_of) + pd.DateOffset(months=k * step_months) for k in range(1, n + 1)]
    out: dict[str, Any] = {"date": [d.date() for d in dates]}
    out["t"] = [(d - curves.as_of).days / 365.0 for d in out["date"]]
    for name in names:
        c, key = _curve(curves, name), name.lower()
        ends = [(pd.Timestamp(d) + pd.DateOffset(months=forward_tenor_months)).date() for d in out["date"]]
        out[f"df_{key}"] = [float(c.discount(d)) for d in out["date"]]
        out[f"zero_{key}"] = [float(c.zero_rate(d)) for d in out["date"]]
        out[f"fwd_{key}"] = [float(c.forward_rate(d, e)) for d, e in zip(out["date"], ends)]
    return pd.DataFrame(out)


def curves_to_dict(curves: CurveSet) -> dict[str, Any]:
    """JSON-serialisable snapshot of the curve nodes (dates ISO, discount factors)."""
    def one(c: YieldCurve) -> dict[str, Any]:
        if not isinstance(c, InterpolatedCurve):
            raise RatesAnalyticsError("Only bootstrapped (interpolated) curves can be exported")
        return {"dates": [d.isoformat() for d in c.dates], "discounts": [float(x) for x in c.node_discounts],
                "interpolation": c.interpolation.value}

    return {"api_version": API_VERSION, "as_of": curves.as_of.isoformat(), "overnight_index": curves.overnight_index,
            "curves": {curves.overnight_index: one(curves.discount_curve),
                       **{k: one(v.curve) for k, v in curves.forwards.items()}}}


def curves_from_dict(payload: Mapping[str, Any]) -> dict[str, InterpolatedCurve]:
    """Rebuild the curves exported by :func:`curves_to_dict` (same interpolation, bit-identical nodes)."""
    as_of = date.fromisoformat(payload["as_of"])
    return {name: InterpolatedCurve(as_of, [date.fromisoformat(d) for d in spec["dates"]], spec["discounts"],
                                    interpolation=spec["interpolation"], name=name)
            for name, spec in payload["curves"].items()}


# ------------------------------------------------------------------- bonds
def bond_table(snap: MarketSnapshot, bonds: Mapping[str, FixedRateBond] | Sequence[FixedRateBond],
               prices: Mapping[str, float] | None = None, benchmark: str = "BUND") -> pd.DataFrame:
    """YAS analytics + G / I / Z / ASW spreads per bond (one row per ISIN, index = ISIN)."""
    items = list(bonds.values()) if isinstance(bonds, Mapping) else list(bonds)
    px = dict(prices or snap.prices)
    priced = [b for b in items if b.isin in px and np.isfinite(px[b.isin])]
    if not priced:
        return pd.DataFrame()
    yas_tab = analytics_table(priced, px, snap.as_of)
    gov = snap.gov_curve(benchmark) if benchmark in snap.sovereigns else None
    spreads = spread_table(priced, px, snap.as_of, snap.curves, gov)
    if "isin" in spreads.columns:
        spreads = spreads.set_index("isin")
    extra = [c for c in spreads.columns if c not in yas_tab.columns]
    return yas_tab.join(spreads[extra])


# -------------------------------------------------------------------- swaps
def make_swap(kind: str, start: date, maturity: date, fixed_rate: float, notional: float, pay_fixed: bool = True,
              spread: float = 0.0) -> VanillaSwap | OvernightIndexedSwap:
    """``kind`` = ``"IRS"`` (vs EURIBOR 6M) or ``"OIS"`` (vs €STR)."""
    kind = kind.upper()
    if kind == "IRS":
        return VanillaSwap(start, maturity, fixed_rate, notional=notional, pay_fixed=pay_fixed, spread=spread)
    if kind == "OIS":
        return OvernightIndexedSwap(start, maturity, fixed_rate, notional=notional, pay_fixed=pay_fixed, spread=spread)
    raise ValueError(f"Unknown swap kind {kind!r} (IRS or OIS)")


def swap_risk(snap: MarketSnapshot, kind: str, start: date, maturity: date, fixed_rate: float, notional: float,
              pay_fixed: bool = True, spread: float = 0.0) -> dict[str, Any]:
    """NPV, par rate, DV01 and bucketed DV01 per market quote, as plain numbers / DataFrame."""
    swap = make_swap(kind, start, maturity, fixed_rate, notional, pay_fixed, spread)
    fx = snap.fixings.get("EURIBOR6M" if kind.upper() == "IRS" else "ESTR")
    v = swpm(swap, snap.curves, fx)
    return {"npv": v.npv, "clean_npv": v.clean_npv, "accrued": v.accrued, "par_rate": v.par_rate,
            "dv01": v.dv01, "pv01": v.pv01, "buckets": v.bucketed.table if v.bucketed is not None else None}
