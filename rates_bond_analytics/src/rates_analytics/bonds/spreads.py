r"""Bond spreads: G-spread, I-spread, Z-spread and par-par asset-swap spread.

All prices per 100; spreads returned in decimal (x 1e4 for bp).

**G-spread** – yield over the government curve at the bond's maturity:
:math:`G = y_{bond} - y^{par}_{gov}(T)`, where :math:`y^{par}_{gov}(T)` is the yield of a
*par* bond with the same schedule priced on the government curve (an
interpolated benchmark yield that removes coupon effects).

**I-spread** – yield over the interpolated swap rate at the bond's maturity:
:math:`I = y^{1Y}_{bond} - S(T)` with :math:`S(T)` the par rate of a spot-starting
swap (fixed annual 30/360 vs EURIBOR 6M, €STR discounting) ending exactly on the
bond maturity, and :math:`y^{1Y}` the annual-equivalent bond yield (same
compounding as the swap fixed leg).

**Z-spread** – constant shift :math:`z` of the zero curve (continuous, ACT/365F)
that reprices the bond:

.. math:: P_{dirty} = \sum_k CF_k \frac{P(t_k)\,e^{-z t_k}}{P(t_s)\,e^{-z t_s}}

**ASW par-par** – the investor pays par (100) for the bond plus a swap in which
he pays the bond coupons and receives the floating index + :math:`A` on 100:

.. math::
    A = \frac{(100 - P_{dirty})\,P_d(t_s) + \sum_k c_k P_d(t_k) - \text{PV}_{float}}{\text{BPV}_{float}}

with :math:`\text{PV}_{float} = 100\sum_j F_j \tau_j P_d(p_j)` (EURIBOR 6M forwards on the
projection curve, or compounded €STR) and :math:`\text{BPV}_{float} = 100\sum_j \tau_j P_d(p_j)`;
everything discounted on €STR (CSA). In single curve this reduces to the
textbook :math:`A = (P_{swap\,curve} - P_{dirty}) / \text{BPV}`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from rates_analytics.bonds.analytics import equivalent_yield, yield_to_maturity
from rates_analytics.bonds.bond import FixedRateBond
from rates_analytics.conventions.market import EUR_ESTR_OIS, EUR_EURIBOR_6M_SWAP, SwapConvention
from rates_analytics.curves.shifted import SpreadedCurve
from rates_analytics.curves.yield_curve import YieldCurve
from rates_analytics.instruments.common import InstrumentError
from rates_analytics.instruments.irs import VanillaSwap
from rates_analytics.instruments.ois import OvernightIndexedSwap


def _dirty(bond: FixedRateBond, clean: float, settlement: date) -> float:
    if clean is None or not np.isfinite(clean) or clean <= 0:
        raise InstrumentError(f"{bond.isin}: invalid clean price {clean!r}")
    return clean + bond.accrued_interest(settlement)


# ----------------------------------------------------------------- Z-spread
def z_spread(bond: FixedRateBond, clean: float, settlement: date, curve: YieldCurve) -> float:
    """Constant zero-rate spread (continuous, ACT/365F) over ``curve`` that reprices the bond."""
    target = _dirty(bond, clean, settlement)

    def f(z: float) -> float:
        return bond.dirty_price_from_curve(SpreadedCurve(curve, z), settlement) - target

    lo, hi = -0.20, 0.50
    if f(lo) * f(hi) > 0:
        raise InstrumentError(f"{bond.isin}: no Z-spread in [-20 %, 50 %] for clean {clean}")
    return brentq(f, lo, hi, xtol=1e-14, rtol=4 * np.finfo(float).eps, maxiter=200)


# ----------------------------------------------------------------- G-spread
def par_yield_on_curve(bond: FixedRateBond, settlement: date, curve: YieldCurve) -> float:
    """Yield of a par bond with ``bond``'s schedule priced on ``curve`` (interpolated benchmark yield).

    The clean price on the curve is linear in the coupon :math:`c`:
    :math:`\\text{clean}(c) = c\\,(A - a) + R`, so the par coupon is :math:`c^* = (100 - R)/(A - a)`.
    """
    unit = FixedRateBond(bond.isin + "-PAR", 0.01, bond.maturity, bond.issue_date, bond.convention,
                         bond.first_coupon_date, bond.day_counter, bond.frequency)
    zero = FixedRateBond(bond.isin + "-ZC", 0.0, bond.maturity, bond.issue_date, bond.convention,
                         bond.first_coupon_date, bond.day_counter, bond.frequency)
    r = zero.clean_price_from_curve(curve, settlement)
    slope = (unit.clean_price_from_curve(curve, settlement) - r) / 0.01
    c_par = (100.0 - r) / slope
    par = FixedRateBond(bond.isin + "-PAR", c_par, bond.maturity, bond.issue_date, bond.convention,
                        bond.first_coupon_date, bond.day_counter, bond.frequency)
    return yield_to_maturity(par, 100.0, settlement)


def g_spread(bond: FixedRateBond, clean: float, settlement: date, gov_curve: YieldCurve,
             benchmark_yields: pd.Series | None = None) -> tuple[float, float]:
    """(G-spread, benchmark yield). With ``benchmark_yields`` (index = years to maturity,
    values = yields) the benchmark is linearly interpolated (Bloomberg style) instead."""
    y = yield_to_maturity(bond, clean, settlement)
    if benchmark_yields is not None:
        s = benchmark_yields.sort_index()
        t = (bond.maturity - settlement).days / 365.25
        bench = float(np.interp(t, s.index.to_numpy(dtype=float), s.to_numpy(dtype=float)))
    else:
        bench = par_yield_on_curve(bond, settlement, gov_curve)
    return y - bench, bench


# ----------------------------------------------------------------- I-spread
def swap_rate_to_maturity(bond: FixedRateBond, settlement: date, discount_curve: YieldCurve,
                          forward_curve: YieldCurve, convention: SwapConvention = EUR_EURIBOR_6M_SWAP) -> float:
    """Par rate of a swap from the bond settlement date to the bond maturity (broken-dated)."""
    return VanillaSwap(settlement, bond.maturity, convention=convention).fair_rate(discount_curve, forward_curve)


def i_spread(bond: FixedRateBond, clean: float, settlement: date, discount_curve: YieldCurve,
             forward_curve: YieldCurve, convention: SwapConvention = EUR_EURIBOR_6M_SWAP) -> tuple[float, float]:
    """(I-spread, swap rate): annual-equivalent bond yield minus the par swap rate at maturity."""
    y = yield_to_maturity(bond, clean, settlement)
    f = int(bond.convention.yield_frequency)
    fixed_f = int(convention.fixed_leg.frequency)
    swap = swap_rate_to_maturity(bond, settlement, discount_curve, forward_curve, convention)
    return equivalent_yield(y, f, fixed_f) - swap, swap


# --------------------------------------------------------------------- ASW
@dataclass(frozen=True)
class AssetSwapResult:
    """Par-par asset swap decomposition (per 100 nominal, PVs as of the curve date)."""

    spread: float
    index: str
    upfront: float            # 100 - dirty, paid at settlement
    pv_coupons: float
    pv_float: float
    bpv_float: float


def asw_par_par(bond: FixedRateBond, clean: float, settlement: date, discount_curve: YieldCurve,
                forward_curve: YieldCurve | None = None, index: str = "EURIBOR6M",
                convention: SwapConvention | None = None) -> AssetSwapResult:
    """Par-par asset-swap spread versus EURIBOR (``index='EURIBOR6M'``) or €STR (``index='ESTR'``)."""
    dirty = _dirty(bond, clean, settlement)
    cps = [cf for cf in bond.cashflows(settlement) if cf.kind == "coupon"]
    pv_cpn = float(sum(cf.amount * discount_curve.discount(cf.payment_date) for cf in cps))
    if index.upper() == "ESTR":
        swap: OvernightIndexedSwap | VanillaSwap = OvernightIndexedSwap(
            settlement, bond.maturity, notional=100.0, convention=convention or EUR_ESTR_OIS)
        fwd = forward_curve or discount_curve
    else:
        if forward_curve is None:
            raise InstrumentError("ASW versus an IBOR index needs its projection curve")
        swap = VanillaSwap(settlement, bond.maturity, notional=100.0, convention=convention or EUR_EURIBOR_6M_SWAP)
        fwd = forward_curve
    leg = swap.float_leg
    pv_float = swap.float_leg_pv(discount_curve, fwd)
    bpv = 100.0 * float(np.dot(leg.accruals, discount_curve.discount(leg.payment_dates)))
    upfront = 100.0 - dirty
    a = (upfront * discount_curve.discount(settlement) + pv_cpn - pv_float) / bpv
    return AssetSwapResult(a, index.upper(), upfront, pv_cpn, pv_float, bpv)


# ----------------------------------------------------------------- reports
@dataclass(frozen=True)
class SpreadAnalytics:
    """All spreads of one bond (decimals)."""

    isin: str
    name: str
    settlement: date
    clean: float
    ytm: float
    g_spread: float | None
    gov_benchmark_yield: float | None
    i_spread: float
    swap_rate: float
    z_spread_swap: float
    z_spread_ois: float
    z_spread_gov: float | None
    asw_euribor: float
    asw_estr: float

    def as_series(self, in_bp: bool = True) -> pd.Series:
        s = pd.Series(asdict(self))
        if in_bp:
            for k in ("g_spread", "i_spread", "z_spread_swap", "z_spread_ois", "z_spread_gov",
                      "asw_euribor", "asw_estr"):
                if s[k] is not None:
                    s[k] = s[k] * 1e4
        return s


def spread_analytics(bond: FixedRateBond, as_of: date, clean: float, curves, gov_curve: YieldCurve | None = None,
                     ibor: str = "EURIBOR6M") -> SpreadAnalytics:
    """G / I / Z / ASW spreads of ``bond`` on a :class:`~rates_analytics.curves.CurveSet` (+ government curve)."""
    settle = bond.settlement_date(as_of)
    disc, fwd = curves.discount_curve, curves.forward_curve(ibor)
    y = yield_to_maturity(bond, clean, settle)
    g, bench = (g_spread(bond, clean, settle, gov_curve) if gov_curve is not None else (None, None))
    i, sw = i_spread(bond, clean, settle, disc, fwd)
    return SpreadAnalytics(
        isin=bond.isin, name=str(bond), settlement=settle, clean=clean, ytm=y,
        g_spread=g, gov_benchmark_yield=bench, i_spread=i, swap_rate=sw,
        z_spread_swap=z_spread(bond, clean, settle, fwd),
        z_spread_ois=z_spread(bond, clean, settle, disc),
        z_spread_gov=z_spread(bond, clean, settle, gov_curve) if gov_curve is not None else None,
        asw_euribor=asw_par_par(bond, clean, settle, disc, fwd, "EURIBOR6M").spread,
        asw_estr=asw_par_par(bond, clean, settle, disc, None, "ESTR").spread,
    )


def spread_table(bonds: Sequence[FixedRateBond], clean_prices: Mapping[str, float], as_of: date, curves,
                 gov_curve: YieldCurve | None = None, min_maturity_years: float = 0.25) -> pd.DataFrame:
    """Spreads (bp) of every priced bond, sorted by maturity."""
    rows = []
    for b in sorted(bonds, key=lambda x: x.maturity):
        px = clean_prices.get(b.isin)
        if px is None or not np.isfinite(px) or b.residual_years(as_of) < min_maturity_years:
            continue
        s = spread_analytics(b, as_of, float(px), curves, gov_curve).as_series()
        s["maturity"] = b.maturity
        rows.append(s)
    return pd.DataFrame(rows).set_index("isin") if rows else pd.DataFrame()
