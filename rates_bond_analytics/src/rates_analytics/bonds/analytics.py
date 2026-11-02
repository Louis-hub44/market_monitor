r"""YAS-style bond analytics: price / yield, durations, convexity, DV01, key-rate durations.

**Yield conventions** (``YieldConvention``)

* ``STREET`` (ICMA, Bloomberg "Street convention"): cash flows discounted on their
  *theoretical* coupon dates,
  :math:`P = \sum_k CF_k (1 + y/F)^{-w_k}`, :math:`w_k = F\sum_{j\le k}\tau_j`;
* ``TRUE`` (Bloomberg "True yield", QuantLib): same chain of ICMA fractions but up
  to the *actual payment dates* (weekend / holiday shifts lengthen the periods).

**Yield risk** (with :math:`t_k = w_k/F`, :math:`v = 1 + y/F`, :math:`PV_k = CF_k v^{-w_k}`):

.. math::
    D^{mac} = \frac{\sum_k t_k PV_k}{P}, \quad
    D^{mod} = \frac{D^{mac}}{1 + y/F}, \quad
    C = \frac{1}{P}\frac{\partial^2 P}{\partial y^2} = \frac{\sum_k t_k (t_k + 1/F) PV_k}{P\,(1+y/F)^2}, \quad
    DV01 = D^{mod}\, P_{dirty} \times 10^{-4}

(prices per 100, :math:`P` dirty). Bloomberg's "Risk" is :math:`D^{mod} P_{dirty}/100`.

**Curve risk**: the bond is repriced on the curve (optionally shifted by its
Z-spread) under parallel and key-rate *tent* shocks of the zero curve:

.. math::
    D^{eff} = \frac{P_- - P_+}{2 P_0 \Delta}, \quad
    C^{eff} = \frac{P_- + P_+ - 2P_0}{P_0 \Delta^2}, \quad
    KRD_k = \frac{P_-^{(k)} - P_+^{(k)}}{2 P_0 \Delta}, \quad \sum_k KRD_k \approx D^{eff}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from rates_analytics.bonds.bond import FixedRateBond
from rates_analytics.conventions.dateutils import add_months
from rates_analytics.curves.shifted import SpreadedCurve, key_rate_shift, parallel_shift
from rates_analytics.curves.yield_curve import YieldCurve
from rates_analytics.instruments.common import InstrumentError

DEFAULT_KEY_TENORS = (0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0)


class YieldConvention(str, Enum):
    STREET = "street"
    TRUE = "true"


# ------------------------------------------------------------------ yields
def _exponents(bond: FixedRateBond, settlement: date, convention: YieldConvention
               ) -> tuple[np.ndarray, np.ndarray, int]:
    """(cash flow amounts, exponents w_k in yield periods, yield frequency F)."""
    convention = YieldConvention(convention)
    if convention is YieldConvention.STREET:
        return bond._yield_exponents(settlement)
    fy = int(bond.convention.yield_frequency)
    dc = bond.day_counter
    amounts, expos = [], []
    last, cum = settlement, 0.0
    for cf in bond.cashflows(settlement):
        pay = cf.payment_date
        if cf.accrual is not None:
            rs, re_ = cf.accrual.ref_start, cf.accrual.ref_end
        else:  # redemption: QuantLib fakes a reference period ending on the payment date
            rs, re_ = (add_months(pay, -12) if last == settlement else last), pay
        if pay > last:
            cum += dc.year_fraction(last, pay, rs, re_)
            last = pay
        amounts.append(cf.amount)
        expos.append(cum)
    return np.array(amounts), fy * np.array(expos), fy


def dirty_price(bond: FixedRateBond, ytm: float, settlement: date,
                convention: YieldConvention = YieldConvention.STREET) -> float:
    """Dirty price per 100 from a yield."""
    a, w, f = _exponents(bond, settlement, convention)
    base = 1.0 + ytm / f
    if base <= 0:
        raise InstrumentError(f"{bond.isin}: yield {ytm:.4%} not admissible")
    return float(np.dot(a, base ** (-w)))


def clean_price(bond: FixedRateBond, ytm: float, settlement: date,
                convention: YieldConvention = YieldConvention.STREET) -> float:
    return dirty_price(bond, ytm, settlement, convention) - bond.accrued_interest(settlement)


def yield_to_maturity(bond: FixedRateBond, clean: float, settlement: date,
                      convention: YieldConvention = YieldConvention.STREET) -> float:
    """Yield from a clean price (Brent, 1e-14)."""
    if not np.isfinite(clean) or clean <= 0:
        raise InstrumentError(f"{bond.isin}: invalid clean price {clean!r}")
    a, w, f = _exponents(bond, settlement, convention)
    target = clean + bond.accrued_interest(settlement)

    def g(y: float) -> float:
        return float(np.dot(a, (1.0 + y / f) ** (-w))) - target

    lo, hi = -0.5 * f / 2, 1.0
    while g(hi) > 0 and hi < 64:
        hi *= 2
    if g(lo) * g(hi) > 0:
        raise InstrumentError(f"{bond.isin}: no yield for clean price {clean}")
    return brentq(g, lo, hi, xtol=1e-15, rtol=4 * np.finfo(float).eps, maxiter=300)


def equivalent_yield(y: float, from_frequency: int, to_frequency: int) -> float:
    """Convert a compounded yield between frequencies, e.g. BTP semi-annual -> annual equivalent."""
    if from_frequency <= 0 or to_frequency <= 0:
        raise InstrumentError("Frequencies must be positive")
    return to_frequency * ((1.0 + y / from_frequency) ** (from_frequency / to_frequency) - 1.0)


def yield_risk(bond: FixedRateBond, ytm: float, settlement: date,
               convention: YieldConvention = YieldConvention.STREET) -> dict[str, float]:
    """Dirty price, Macaulay / modified duration, convexity and DV01 (per 100) at ``ytm``."""
    a, w, f = _exponents(bond, settlement, convention)
    v = 1.0 + ytm / f
    pv = a * v ** (-w)
    p = float(pv.sum())
    t = w / f
    mac = float(np.dot(t, pv)) / p
    mod = mac / v
    conv = float(np.dot(t * (t + 1.0 / f), pv)) / (p * v * v)
    return {"dirty": p, "macaulay": mac, "modified": mod, "convexity": conv, "dv01": mod * p * 1e-4}


@dataclass(frozen=True)
class YieldAnalytics:
    """YAS "yield" block for one bond and one settlement date (prices per 100)."""

    isin: str
    name: str
    settlement: date
    previous_coupon: date
    next_coupon: date
    accrued_days: int
    accrued: float
    clean: float
    dirty: float
    ytm: float
    true_yield: float
    yield_frequency: int
    annual_equivalent_yield: float
    current_yield: float
    macaulay: float
    modified: float
    convexity: float
    dv01: float
    risk: float

    def as_series(self) -> pd.Series:
        return pd.Series(asdict(self))


def yield_analytics(bond: FixedRateBond, settlement: date, clean: float | None = None,
                    ytm: float | None = None, convention: YieldConvention = YieldConvention.STREET
                    ) -> YieldAnalytics:
    """YAS calculator: give exactly one of ``clean`` (price -> yield) or ``ytm`` (yield -> price)."""
    if (clean is None) == (ytm is None):
        raise InstrumentError("Give exactly one of clean price or yield")
    if clean is None:
        assert ytm is not None
        clean = clean_price(bond, ytm, settlement, convention)
    else:
        ytm = yield_to_maturity(bond, clean, settlement, convention)
    street = ytm if YieldConvention(convention) is YieldConvention.STREET else \
        yield_to_maturity(bond, clean, settlement, YieldConvention.STREET)
    true_y = ytm if YieldConvention(convention) is YieldConvention.TRUE else \
        yield_to_maturity(bond, clean, settlement, YieldConvention.TRUE)
    risk = yield_risk(bond, street, settlement, YieldConvention.STREET)
    period = bond.current_period(settlement)
    f = int(bond.convention.yield_frequency)
    ai = bond.accrued_interest(settlement)
    return YieldAnalytics(
        isin=bond.isin, name=str(bond), settlement=settlement,
        previous_coupon=period.start, next_coupon=period.end,
        accrued_days=(settlement - period.start).days, accrued=ai, clean=clean, dirty=clean + ai,
        ytm=street, true_yield=true_y, yield_frequency=f,
        annual_equivalent_yield=equivalent_yield(street, f, 1),
        current_yield=bond.coupon * bond.redemption / clean,
        macaulay=risk["macaulay"], modified=risk["modified"], convexity=risk["convexity"],
        dv01=risk["dv01"], risk=risk["modified"] * (clean + ai) / 100.0,
    )


# ------------------------------------------------------------- curve risk
@dataclass
class CurveRisk:
    """Curve-based risk of a bond (bumps of the zero curve, ``bump`` in decimal)."""

    model_dirty: float
    spread: float
    effective_duration: float
    effective_convexity: float
    krd: pd.Series
    bump: float

    @property
    def krd_sum(self) -> float:
        return float(self.krd.sum())

    def krd_dv01(self, nominal: float = 1_000_000.0) -> pd.Series:
        """Key-rate DV01 in currency for ``nominal`` (price change for a 1bp tent shock)."""
        return self.krd * self.model_dirty * 1e-4 * nominal / 100.0


def curve_risk(bond: FixedRateBond, curve: YieldCurve, settlement: date,
               key_tenors: Sequence[float] = DEFAULT_KEY_TENORS, bump: float = 1e-4,
               spread: float = 0.0) -> CurveRisk:
    """Effective duration / convexity and key-rate durations on ``curve`` (+ constant ``spread``)."""
    if bump <= 0:
        raise InstrumentError("bump must be positive")
    keys = sorted(float(k) for k in key_tenors)
    base = SpreadedCurve(curve, spread) if spread else curve
    p0 = bond.dirty_price_from_curve(base, settlement)
    up = bond.dirty_price_from_curve(parallel_shift(base, bump), settlement)
    dn = bond.dirty_price_from_curve(parallel_shift(base, -bump), settlement)
    krd = {}
    for k, t in enumerate(keys):
        pu = bond.dirty_price_from_curve(key_rate_shift(base, keys, k, bump), settlement)
        pd_ = bond.dirty_price_from_curve(key_rate_shift(base, keys, k, -bump), settlement)
        krd[t] = (pd_ - pu) / (2 * p0 * bump)
    return CurveRisk(model_dirty=p0, spread=spread, effective_duration=(dn - up) / (2 * p0 * bump),
                     effective_convexity=(dn + up - 2 * p0) / (p0 * bump * bump),
                     krd=pd.Series(krd, name="krd"), bump=bump)


# ------------------------------------------------------------------- YAS
@dataclass
class YASReport:
    """Full YAS-like report: yield block, settlement amounts, cash flows, curve risk."""

    analytics: YieldAnalytics
    nominal: float
    cashflows: pd.DataFrame
    curve: CurveRisk | None = None
    extra: dict = field(default_factory=dict)

    @property
    def amounts(self) -> dict[str, float]:
        """Settlement amounts for the nominal (principal, accrued, total)."""
        a, n = self.analytics, self.nominal / 100.0
        return {"principal": a.clean * n, "accrued": a.accrued * n, "total": a.dirty * n,
                "dv01": a.dv01 * n}

    def summary(self) -> pd.Series:
        s = self.analytics.as_series()
        for k, v in self.amounts.items():
            s[f"amount_{k}"] = v
        if self.curve is not None:
            s["model_dirty"] = self.curve.model_dirty
            s["effective_duration"] = self.curve.effective_duration
            s["effective_convexity"] = self.curve.effective_convexity
            s["krd_sum"] = self.curve.krd_sum
        return s


def cashflow_table(bond: FixedRateBond, settlement: date, ytm: float, nominal: float = 100.0,
                   curve: YieldCurve | None = None) -> pd.DataFrame:
    """Future cash flows with yield-based PV (street) and, if given, curve DF / PV."""
    _a, w, f = _exponents(bond, settlement, YieldConvention.STREET)
    cfs = bond.cashflows(settlement)
    # street exponents are per coupon period (redemption on the last one)
    rows = []
    for i, cf in enumerate(cfs):
        wk = w[-1] if cf.kind == "redemption" else w[i]
        row = {"payment_date": cf.payment_date, "type": cf.kind,
               "accrual_start": cf.accrual.start if cf.accrual else None,
               "accrual_end": cf.accrual.end if cf.accrual else None,
               "amount": cf.amount * nominal / 100.0, "time": wk / f,
               "pv_yield": cf.amount * nominal / 100.0 * (1 + ytm / f) ** (-wk)}
        if curve is not None:
            df = curve.discount(cf.payment_date) / curve.discount(settlement)
            row["df_curve"] = df
            row["pv_curve"] = row["amount"] * df
        rows.append(row)
    return pd.DataFrame(rows)


def yas(bond: FixedRateBond, as_of: date, clean: float | None = None, ytm: float | None = None,
        curve: YieldCurve | None = None, nominal: float = 1_000_000.0,
        key_tenors: Sequence[float] = DEFAULT_KEY_TENORS, spread: float | str = 0.0) -> YASReport:
    """YAS screen for ``bond`` traded on ``as_of`` (settlement T+2): give a clean price or a yield.

    ``spread="z"`` shifts ``curve`` by the bond's Z-spread before computing the curve risk, so that
    the model price equals the market price (market-consistent effective duration and KRDs).
    """
    if nominal <= 0:
        raise InstrumentError("nominal must be positive")
    settle = bond.settlement_date(as_of)
    ya = yield_analytics(bond, settle, clean=clean, ytm=ytm)
    extra: dict = {}
    if isinstance(spread, str):
        if spread.lower() != "z":
            raise InstrumentError(f"Unknown spread mode {spread!r} (use a number or 'z')")
        if curve is None:
            raise InstrumentError("spread='z' needs a curve")
        from rates_analytics.bonds.spreads import z_spread  # local import: spreads depends on analytics

        spread = z_spread(bond, ya.clean, settle, curve)
        extra["z_spread"] = spread
    risk = curve_risk(bond, curve, settle, key_tenors, spread=float(spread)) if curve is not None else None
    priced_on = SpreadedCurve(curve, float(spread)) if (curve is not None and spread) else curve
    return YASReport(ya, nominal, cashflow_table(bond, settle, ya.ytm, nominal, priced_on), risk, extra)


def analytics_table(bonds: Sequence[FixedRateBond], clean_prices: Mapping[str, float], as_of: date
                    ) -> pd.DataFrame:
    """One YAS line per priced bond (sorted by maturity) – the universe view of the UI."""
    rows = []
    for b in sorted(bonds, key=lambda x: x.maturity):
        px = clean_prices.get(b.isin)
        settle = b.settlement_date(as_of)
        if px is None or not np.isfinite(px) or settle >= b.maturity:
            continue
        a = yield_analytics(b, settle, clean=float(px))
        rows.append({"isin": b.isin, "name": str(b), "maturity": b.maturity, "coupon": b.coupon,
                     "clean": a.clean, "accrued": a.accrued, "ytm": a.ytm, "macaulay": a.macaulay,
                     "modified": a.modified, "convexity": a.convexity, "dv01": a.dv01})
    return pd.DataFrame(rows).set_index("isin") if rows else pd.DataFrame()
