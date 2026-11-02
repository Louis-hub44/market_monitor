"""UI service layer: builds the market state and the tables the dashboard shows.

Pure Python (no Streamlit): every function here only calls the engine, so it is
unit-tested and reusable by other front-ends of the terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from rates_analytics import api
from rates_analytics.bonds import FixedRateBond, spread_table, yas
from rates_analytics.config import Settings, load_settings
from rates_analytics.curves import (
    CurveSet,
    SovereignCurveFit,
    zero_spreads,
)
from rates_analytics.curves.proxy import ProxyReport
from rates_analytics.data import MarketDataService
from rates_analytics.instruments import Fixings, OvernightIndexedSwap, VanillaSwap
from rates_analytics.network import NetworkSettings
from rates_analytics.scenarios import (
    CurveEnvironment,
    CurveScenario,
    Position,
    apply_scenario,
    par_rate_table,
    scenario_pnl,
)
from rates_analytics.swaps import SwapValuation, swpm

SOVEREIGNS = ("BUND", "OAT", "BTP")
CURVE_TENORS = ("1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y", "50Y")


@dataclass
class MarketState:
    """Everything the dashboard needs for one valuation date."""

    as_of: date
    settings: Settings
    service: MarketDataService
    curves: CurveSet
    sovereigns: dict[str, SovereignCurveFit] = field(default_factory=dict)
    bonds: dict[str, FixedRateBond] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    fixings: dict[str, Fixings] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    proxy: ProxyReport = field(default_factory=ProxyReport)
    sources: dict[str, str] = field(default_factory=dict)
    data_errors: dict[str, str] = field(default_factory=dict)

    @property
    def gov_curves(self):
        return {k: v.curve for k, v in self.sovereigns.items()}

    def environment(self) -> CurveEnvironment:
        return CurveEnvironment(self.curves, self.gov_curves)


FALLBACKS = {"static": [], "free": ["public", "fmp"], "fmp": ["free", "public"], "public": ["free"]}


def load_market_state(as_of: date, provider: str = "static", interpolation: str = "log_linear",
                      settings: Settings | None = None, network: NetworkSettings | None = None) -> MarketState:
    """Curves, sovereign fits, bond universe, prices and fixings for ``as_of``.

    ``network`` is the SSL policy chosen on the start screen (corporate proxy).
    """
    settings = settings or load_settings(overrides={"provider": provider, "fallback": FALLBACKS.get(provider, [])})
    snap = api.market_snapshot(as_of, settings, interpolation=interpolation, sovereigns=SOVEREIGNS,
                               network=network)
    return MarketState(as_of, settings, snap.service, snap.curves, snap.sovereigns, snap.bonds, snap.prices,
                       snap.fixings, snap.warnings, snap.proxy, snap.sources, snap.data_errors)


# ------------------------------------------------------------------ curves
def curve_grid(curves: CurveSet, horizon_years: int = 50, step_days: int = 30) -> pd.DataFrame:
    """Zero rates and 6M forwards of €STR and EURIBOR 6M on a monthly grid."""
    rows = []
    disc, e6 = curves.discount_curve, curves.forward_curve("EURIBOR6M")
    for k in range(1, horizon_years * 365 // step_days):
        d = curves.as_of + timedelta(days=k * step_days)
        e = d + timedelta(days=182)
        rows.append({"date": d, "years": (d - curves.as_of).days / 365.0,
                     "estr_zero": disc.zero_rate(d), "e6m_zero": e6.zero_rate(d),
                     "estr_fwd6m": disc.forward_rate(d, e), "e6m_fwd6m": e6.forward_rate(d, e)})
    return pd.DataFrame(rows)


def par_rates(curves: CurveSet, tenors=CURVE_TENORS) -> pd.DataFrame:
    """Par OIS and IRS rates, basis (GC-style table)."""
    b = curves.basis_spreads(tenors).set_index("tenor")
    disc = curves.discount_curve
    b["estr_zero"] = [disc.zero_rate(float(t[:-1])) for t in b.index]
    return b


def sovereign_table(state: MarketState) -> pd.DataFrame:
    fits = state.sovereigns
    rows = {n: {**f.par_yields((2, 5, 10, 30)).rename(lambda t: f"par_{int(t)}Y").to_dict(),
                "rmse_bp": f.rmse_bp, "n_used": f.n_used, "n_bonds": len(f.bonds)} for n, f in fits.items()}
    return pd.DataFrame(rows).T


def sovereign_spreads(state: MarketState) -> pd.DataFrame:
    if "BUND" not in state.sovereigns:
        return pd.DataFrame()
    return zero_spreads(state.sovereigns, "BUND", (2, 5, 10, 20, 30))


# --------------------------------------------------------------------- bonds
def bond_choices(state: MarketState, issuer: str | None = None) -> list[str]:
    return sorted((i for i, b in state.bonds.items() if issuer is None or b.issuer.upper().startswith(issuer)),
                  key=lambda i: state.bonds[i].maturity)


def bond_report(state: MarketState, isin: str, clean: float | None = None, nominal: float = 1e6,
                reference: str = "ESTR"):
    """YAS report (KRD on the chosen curve shifted by the Z-spread) + spread analytics."""
    from rates_analytics.bonds import spread_analytics

    b = state.bonds[isin]
    px = clean if clean is not None else state.prices[isin]
    env = state.environment()
    report = yas(b, state.as_of, clean=px, curve=env.curve(reference), nominal=nominal, spread="z")
    gov = state.sovereigns.get("BUND")
    spreads = spread_analytics(b, state.as_of, px, state.curves, gov.curve if gov else None)
    return report, spreads


def universe_spreads(state: MarketState, issuer: str) -> pd.DataFrame:
    bonds = [state.bonds[i] for i in bond_choices(state, issuer)]
    gov = state.sovereigns.get("BUND")
    return spread_table(bonds, state.prices, state.as_of, state.curves, gov.curve if gov else None)


# ---------------------------------------------------------------------- swaps
def make_swap(state: MarketState, kind: str, start: date, maturity: date, fixed_rate: float, notional: float,
              pay_fixed: bool, spread: float = 0.0):
    cls = OvernightIndexedSwap if kind == "OIS" else VanillaSwap
    return cls(start, maturity, fixed_rate, notional=notional, pay_fixed=pay_fixed, spread=spread)


def price_swap(state: MarketState, swap, risk: bool = True) -> SwapValuation:
    fx = state.fixings["ESTR" if isinstance(swap, OvernightIndexedSwap) else "EURIBOR6M"]
    return swpm(swap, state.curves, fx, risk=risk)


def par_rate_for(state: MarketState, kind: str, start: date, maturity: date) -> float:
    swap = make_swap(state, kind, start, maturity, 0.0, 1.0, True)
    fx = state.fixings["ESTR" if kind == "OIS" else "EURIBOR6M"]
    disc = state.curves.discount_curve
    fwd = disc if kind == "OIS" else state.curves.forward_curve("EURIBOR6M")
    return swap.fair_rate(disc, fwd, fx)


# ------------------------------------------------------------------ scenarios
def default_book(state: MarketState) -> list[Position]:
    as_of = state.as_of
    book = [Position("Pay IRS 10Y", VanillaSwap.from_tenor(as_of, "10Y", 0.027, notional=20e6)),
            Position("Rec OIS 2Y", OvernightIndexedSwap.from_tenor(as_of, "2Y", 0.0205, notional=50e6,
                                                                   pay_fixed=False))]
    for name, issuer, nominal in (("BTP", "IT", 10e6), ("BUND", "DE", -10e6)):
        choices = [i for i in bond_choices(state, issuer) if state.bonds[i].residual_years(as_of) > 7]
        if choices and name in state.sovereigns:
            isin = choices[0]
            book.append(Position(f"{'Long' if nominal > 0 else 'Short'} {state.bonds[isin]}", state.bonds[isin],
                                 nominal=nominal, clean_price=state.prices[isin], curve=name))
    return book


def run_scenarios(state: MarketState, book: list[Position], scenarios: list[CurveScenario], mode: str,
                  targets=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    pnl = scenario_pnl(book, state.environment(), scenarios, mode, targets)
    shocked = {s.name: apply_scenario(state.curves, s, mode, targets) for s in scenarios}
    return pnl, par_rate_table(state.curves, shocked)


def scenario_curve_grid(state: MarketState, scenario: CurveScenario, mode: str, targets=None) -> pd.DataFrame:
    base = curve_grid(state.curves, step_days=91)
    shocked = curve_grid(apply_scenario(state.curves, scenario, mode, targets), step_days=91)
    base["e6m_zero_scen"] = shocked["e6m_zero"].to_numpy()
    base["estr_zero_scen"] = shocked["estr_zero"].to_numpy()
    base["shock_bp"] = np.asarray(scenario(base["years"].to_numpy())) * 1e4
    return base
