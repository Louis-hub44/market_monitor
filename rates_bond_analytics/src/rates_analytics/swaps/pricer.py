r"""SWPM-style swap valuation: NPV, accrued, par rate / spread, PV01, DV01, bucketed risk.

Works for :class:`~rates_analytics.instruments.VanillaSwap` (EURIBOR, dual curve) and
:class:`~rates_analytics.instruments.OvernightIndexedSwap` (€STR), spot, forward or
seasoned (past fixings from a :class:`~rates_analytics.instruments.fixings.Fixings`).

Sign convention: every amount is seen from the **holder** (payer swap = pay fixed,
receive floating). Definitions:

* NPV (dirty) :math:`= \text{PV}_{float} - \text{PV}_{fixed}` for a payer;
* accrued: interest accrued on the current periods up to the curve date;
  clean NPV = NPV - accrued;
* par rate :math:`K^* = \text{PV}_{float} / A`, par spread :math:`s^* = s - \text{NPV}_{payer} / \text{BPV}_{float}`;
* PV01 :math:`= A \times 1\text{bp}` (value of 1bp on the fixed rate);
* DV01: value change for +1bp on every market quote (sum of buckets, or full
  re-bootstrap with ``rebuild_parallel=True``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from rates_analytics.curves.market_curves import CurveSet
from rates_analytics.instruments.fixings import Fixings
from rates_analytics.instruments.irs import VanillaSwap
from rates_analytics.instruments.ois import OvernightIndexedSwap
from rates_analytics.swaps.risk import BucketedRisk, bucketed_risk, parallel_dv01

Swap = VanillaSwap | OvernightIndexedSwap


def curves_for(swap: Swap, curves: CurveSet):
    """(discount curve, projection curve) of a swap in a curve set."""
    disc = curves.discount_curve
    if isinstance(swap, OvernightIndexedSwap):
        return disc, curves.forward_curve(swap.convention.index.name)
    return disc, curves.forward_curve(swap.convention.index.name)


def swap_npv(swap: Swap, curves: CurveSet, fixings=None) -> float:
    disc, fwd = curves_for(swap, curves)
    return swap.npv(disc, fwd, fixings)


@dataclass
class LegReport:
    """One leg: PV and accrued (holder-signed) plus its cash-flow table."""

    name: str
    pv: float
    accrued: float
    cashflows: pd.DataFrame


@dataclass
class SwapValuation:
    """SWPM screen."""

    as_of: date
    notional: float
    pay_fixed: bool
    npv: float
    accrued: float
    par_rate: float
    par_spread: float
    pv01: float
    fixed_leg: LegReport
    float_leg: LegReport
    dv01: float | None = None
    bucketed: BucketedRisk | None = None

    @property
    def clean_npv(self) -> float:
        return self.npv - self.accrued

    def summary(self) -> pd.Series:
        return pd.Series({
            "as_of": self.as_of, "notional": self.notional, "side": "payer" if self.pay_fixed else "receiver",
            "npv": self.npv, "accrued": self.accrued, "clean_npv": self.clean_npv,
            "fixed_leg_pv": self.fixed_leg.pv, "float_leg_pv": self.float_leg.pv,
            "par_rate": self.par_rate, "par_spread_bp": self.par_spread * 1e4, "pv01": self.pv01,
            "dv01": self.dv01,
        })


def _fixed_table(swap: Swap, disc, sign: float) -> tuple[pd.DataFrame, float]:
    ref = disc.reference_date
    rows, accrued = [], 0.0
    dc = swap.convention.fixed_leg.day_counter
    for p in swap.fixed_leg.periods:
        if p.payment_date <= ref:
            continue
        amount = sign * swap.notional * swap.fixed_rate * p.year_fraction
        df = disc.discount(p.payment_date)
        rows.append({"accrual_start": p.start, "accrual_end": p.end, "payment_date": p.payment_date,
                     "year_fraction": p.year_fraction, "rate": swap.fixed_rate, "status": "fixed",
                     "amount": amount, "discount": df, "pv": amount * df})
        if p.start <= ref < p.end:
            accrued += sign * swap.notional * swap.fixed_rate * dc.year_fraction(p.start, ref)
    return pd.DataFrame(rows), accrued


def _float_table(swap: Swap, disc, fwd, fixings, sign: float) -> tuple[pd.DataFrame, float]:
    ref = disc.reference_date
    periods = swap.float_leg.periods
    rows, accrued = [], 0.0
    if isinstance(swap, VanillaSwap):
        rates, known = swap.float_rates(fwd, fixings)
        dc = swap.convention.float_leg.day_counter
        for i, p in enumerate(periods):
            if p.payment_date <= ref:
                continue
            amount = sign * swap.notional * (rates[i] + swap.spread) * p.year_fraction
            df = disc.discount(p.payment_date)
            rows.append({"accrual_start": p.start, "accrual_end": p.end, "payment_date": p.payment_date,
                         "fixing_date": p.fixing_date, "year_fraction": p.year_fraction, "rate": rates[i],
                         "spread": swap.spread, "status": "fixed" if known[i] else "projected",
                         "amount": amount, "discount": df, "pv": amount * df})
            if p.start <= ref < p.end:
                accrued += sign * swap.notional * (rates[i] + swap.spread) * dc.year_fraction(p.start, ref)
    else:
        growth, to_date = swap.compounded_growth(fwd, fixings)
        dc = swap.convention.float_leg.day_counter
        for i, p in enumerate(periods):
            if p.payment_date <= ref:
                continue
            status = "fixed" if p.end <= ref else ("partially fixed" if p.start < ref else "projected")
            amount = sign * swap.notional * (growth[i] + swap.spread * p.year_fraction)
            df = disc.discount(p.payment_date)
            rows.append({"accrual_start": p.start, "accrual_end": p.end, "payment_date": p.payment_date,
                         "year_fraction": p.year_fraction, "rate": growth[i] / p.year_fraction,
                         "spread": swap.spread, "status": status, "amount": amount, "discount": df,
                         "pv": amount * df})
            if p.start <= ref < p.end:
                accrued += sign * swap.notional * (to_date[i] + swap.spread * dc.year_fraction(p.start, ref))
    return pd.DataFrame(rows), accrued


def value_swap(swap: Swap, curves: CurveSet, fixings=None) -> SwapValuation:
    """NPV, accrued, par rate / spread, PV01 and cash-flow tables (no curve risk)."""
    fx = Fixings.coerce(fixings) if fixings is not None else None
    disc, fwd = curves_for(swap, curves)
    payer = 1.0 if swap.pay_fixed else -1.0
    fixed_tab, fixed_acc = _fixed_table(swap, disc, -payer)
    float_tab, float_acc = _float_table(swap, disc, fwd, fx, payer)
    npv = swap.npv(disc, fwd, fx)
    pv_fixed = float(fixed_tab["pv"].sum()) if len(fixed_tab) else 0.0
    pv_float = float(float_tab["pv"].sum()) if len(float_tab) else 0.0
    if not np.isclose(pv_fixed + pv_float, npv, rtol=0, atol=1e-6 * max(1.0, swap.notional)):
        raise AssertionError("Leg tables do not add up to the NPV")  # pragma: no cover - internal check
    return SwapValuation(
        as_of=curves.as_of, notional=swap.notional, pay_fixed=swap.pay_fixed, npv=npv,
        accrued=fixed_acc + float_acc, par_rate=swap.fair_rate(disc, fwd, fx),
        par_spread=swap.fair_spread(disc, fwd, fx), pv01=swap.annuity(disc) * 1e-4,
        fixed_leg=LegReport("fixed", pv_fixed, fixed_acc, fixed_tab),
        float_leg=LegReport("float", pv_float, float_acc, float_tab),
    )


def swpm(swap: Swap, curves: CurveSet, fixings=None, risk: bool = True, method: str = "jacobian",
         rebuild_parallel: bool = False, bump: float = 1e-4) -> SwapValuation:
    """Full SWPM screen: valuation + DV01 + bucketed risk per market quote."""
    val = value_swap(swap, curves, fixings)
    if not risk:
        return val
    fx = Fixings.coerce(fixings) if fixings is not None else None

    def pricer(cs: CurveSet) -> float:
        return swap_npv(swap, cs, fx)

    val.bucketed = bucketed_risk(curves, pricer, bump=bump, method=method)
    val.dv01 = parallel_dv01(curves, pricer, bump) if rebuild_parallel else val.bucketed.total
    return val
