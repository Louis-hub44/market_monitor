r"""Fixed-rate government bond (Bund, OAT, BTP...): cash flows, accrued, prices, yield.

Conventions (euro-area government bonds):

* coupons accrue on **unadjusted** coupon dates, ACT/ACT ICMA; a regular
  coupon is exactly :math:`100\,c/F`; irregular first / last coupons use the
  ICMA notional reference periods;
* coupons are **paid** on the next TARGET business day (Following);
* settlement T+2 TARGET; a coupon belongs to the buyer if settlement is strictly
  before its (unadjusted) coupon date;
* yield: ICMA / street convention, compounded at the yield frequency
  :math:`F_y` with fractional exponents

.. math::
    P_{dirty} = \sum_k \frac{CF_k}{(1 + y/F_y)^{w_k}},\qquad
    w_k = F_y \sum_{j \le k} \tau^{ICMA}_j

where :math:`\tau^{ICMA}_1` runs from settlement to the next coupon date and the
following :math:`\tau_j` are the (regular or irregular) coupon periods.

Prices are per 100 nominal. Analytics (durations, convexity, DV01, spreads)
come in phases 5 and 6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import brentq

from rates_analytics.conventions.calendar import BusinessDayConvention
from rates_analytics.conventions.dates import settlement_date
from rates_analytics.conventions.daycount import DayCounter
from rates_analytics.conventions.market import BondConvention
from rates_analytics.conventions.period import Frequency
from rates_analytics.conventions.schedule import (
    AccrualPeriod,
    DateGenerationRule,
    build_accrual_periods,
    generate_schedule,
)
from rates_analytics.instruments.common import InstrumentError

if TYPE_CHECKING:
    from rates_analytics.curves.yield_curve import YieldCurve

BDC = BusinessDayConvention


@dataclass(frozen=True)
class CashFlow:
    """A future cash flow per 100 nominal."""

    payment_date: date
    amount: float
    kind: str                 # "coupon" | "redemption"
    accrual: AccrualPeriod | None = None


@dataclass(frozen=True)
class FixedRateBond:
    """Bullet fixed-rate bond.

    Args:
        isin: identifier.
        coupon: annual coupon rate (decimal, 0.025 = 2.5 %).
        maturity: redemption date (unadjusted).
        issue_date: issue / interest accrual start date.
        convention: market convention (``BUND``, ``OAT``, ``BTP``...).
        first_coupon_date: first coupon date when the first period is irregular.
        day_counter: overrides the convention day count (from static data).
        frequency: overrides the convention coupon frequency.
        redemption: redemption amount per 100.
        name / issuer: labels.
    """

    isin: str
    coupon: float
    maturity: date
    issue_date: date
    convention: BondConvention
    first_coupon_date: date | None = None
    day_counter: DayCounter = None  # type: ignore[assignment]  # None -> convention default (__post_init__)
    frequency: Frequency = None  # type: ignore[assignment]  # None -> convention default
    redemption: float = 100.0
    name: str = ""
    issuer: str = ""
    periods: tuple[AccrualPeriod, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not np.isfinite(self.coupon) or not -0.01 <= self.coupon <= 0.25:
            raise InstrumentError(f"{self.isin}: implausible coupon {self.coupon!r}")
        if not self.issue_date < self.maturity:
            raise InstrumentError(f"{self.isin}: issue date {self.issue_date} must precede maturity {self.maturity}")
        if self.first_coupon_date is not None and not self.issue_date < self.first_coupon_date <= self.maturity:
            raise InstrumentError(f"{self.isin}: first coupon date {self.first_coupon_date} outside the bond life")
        if self.redemption <= 0:
            raise InstrumentError(f"{self.isin}: redemption must be positive")
        c = self.convention
        freq = self.frequency or c.frequency
        if freq is Frequency.ONCE:
            raise InstrumentError(f"{self.isin}: zero-coupon bonds must use frequency ANNUAL with coupon 0")
        object.__setattr__(self, "frequency", freq)
        object.__setattr__(self, "day_counter", self.day_counter or c.day_counter)
        first = self.first_coupon_date
        if first is not None and first == self.maturity:
            first = None
        sched = generate_schedule(self.issue_date, self.maturity, freq.period, c.calendar, c.accrual_bdc,
                                  c.accrual_bdc, DateGenerationRule.BACKWARD, c.end_of_month, first_date=first)
        periods = build_accrual_periods(sched, self.day_counter, accrue_on_adjusted=False,
                                        payment_calendar=c.calendar, payment_bdc=c.payment_bdc)
        object.__setattr__(self, "periods", tuple(periods))

    # ------------------------------------------------------------- dates
    def settlement_date(self, trade_date: date) -> date:
        """Settlement date T+lag on the bond calendar."""
        return settlement_date(trade_date, self.convention.calendar, self.convention.settlement_lag)

    def _check_alive(self, settlement: date) -> None:
        if settlement < self.issue_date:
            raise InstrumentError(f"{self.isin}: settlement {settlement} before issue date {self.issue_date}")
        if settlement >= self.maturity:
            raise InstrumentError(f"{self.isin}: settlement {settlement} on/after maturity {self.maturity}")

    def current_period(self, settlement: date) -> AccrualPeriod:
        """Coupon period containing ``settlement`` (start <= settlement < end)."""
        self._check_alive(settlement)
        for p in self.periods:
            if p.start <= settlement < p.end:
                return p
        raise InstrumentError(f"{self.isin}: no coupon period contains {settlement}")  # pragma: no cover

    def coupon_amount(self, p: AccrualPeriod) -> float:
        return self.redemption * self.coupon * p.year_fraction

    # -------------------------------------------------------- cash flows
    def cashflows(self, settlement: date) -> list[CashFlow]:
        """Cash flows received by a buyer settling on ``settlement``."""
        self._check_alive(settlement)
        out = [CashFlow(p.payment_date, self.coupon_amount(p), "coupon", p)
               for p in self.periods if p.end > settlement and self.coupon != 0.0]
        out.append(CashFlow(self.periods[-1].payment_date, self.redemption, "redemption"))
        return out

    def accrued_interest(self, settlement: date) -> float:
        """Accrued coupon per 100 at ``settlement`` (ACT/ACT ICMA with reference period)."""
        p = self.current_period(settlement)
        return self.redemption * self.coupon * self.day_counter.year_fraction(p.start, settlement,
                                                                             p.ref_start, p.ref_end)

    # ------------------------------------------------------ curve pricing
    def dirty_price_from_curve(self, curve: YieldCurve, settlement: date) -> float:
        r""":math:`\sum_k CF_k\,P(t_k) / P(t_{settle})` – forward value at settlement."""
        cfs = self.cashflows(settlement)
        dates = np.array([cf.payment_date for cf in cfs], dtype="datetime64[D]")
        amounts = np.array([cf.amount for cf in cfs])
        return float(np.dot(amounts, curve.discount(dates))) / curve.discount(settlement)

    def clean_price_from_curve(self, curve: YieldCurve, settlement: date) -> float:
        return self.dirty_price_from_curve(curve, settlement) - self.accrued_interest(settlement)

    # ------------------------------------------------------------- yield
    def _yield_exponents(self, settlement: date) -> tuple[np.ndarray, np.ndarray, int]:
        """(amounts, exponents w_k, yield frequency) for the ICMA yield formula."""
        fy = int(self.convention.yield_frequency)
        cfs = self.cashflows(settlement)
        current = self.current_period(settlement)
        w0 = self.day_counter.year_fraction(settlement, current.end, current.ref_start, current.ref_end)
        expos, cum = [], w0
        future = [p for p in self.periods if p.end > settlement]
        for i, p in enumerate(future):
            if i > 0:
                cum += p.year_fraction
            expos.append(cum)
        w = fy * np.array(expos)
        amounts = np.array([cf.amount for cf in cfs[:-1]] + [0.0] * (len(future) - (len(cfs) - 1)))
        amounts[-1] += cfs[-1].amount  # redemption on the last coupon date
        return amounts, w, fy

    def dirty_price_from_yield(self, ytm: float, settlement: date) -> float:
        """Dirty price per 100 for an ICMA yield (compounded at the yield frequency)."""
        amounts, w, fy = self._yield_exponents(settlement)
        base = 1.0 + ytm / fy
        if base <= 0:
            raise InstrumentError(f"{self.isin}: yield {ytm:.4%} below -{fy}00 %")
        return float(np.dot(amounts, base ** (-w)))

    def clean_price_from_yield(self, ytm: float, settlement: date) -> float:
        return self.dirty_price_from_yield(ytm, settlement) - self.accrued_interest(settlement)

    def yield_from_clean_price(self, clean: float, settlement: date) -> float:
        """ICMA yield to maturity from a clean price (Brent on [-50 %, 100 %])."""
        if not np.isfinite(clean) or clean <= 0:
            raise InstrumentError(f"{self.isin}: invalid clean price {clean!r}")
        target = clean + self.accrued_interest(settlement)
        amounts, w, fy = self._yield_exponents(settlement)

        def f(y: float) -> float:
            return float(np.dot(amounts, (1.0 + y / fy) ** (-w))) - target

        lo, hi = -0.5 * fy / 2, 1.0
        if f(lo) * f(hi) > 0:
            raise InstrumentError(f"{self.isin}: no yield for clean price {clean}")
        return brentq(f, lo, hi, xtol=1e-14, rtol=4 * np.finfo(float).eps, maxiter=200)

    def residual_years(self, as_of: date) -> float:
        return (self.maturity - as_of).days / 365.25

    def __str__(self) -> str:
        return self.name or f"{self.convention.name} {self.coupon * 100:.3f} {self.maturity:%d/%m/%Y}"
