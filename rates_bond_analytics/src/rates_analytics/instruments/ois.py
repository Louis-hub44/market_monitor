r"""Overnight indexed swap (€STR OIS) valuation.

Floating leg: daily compounded overnight rate, paid with a payment lag. On a
deterministic curve the compounded overnight rate over an accrual period
telescopes exactly into a ratio of discount factors of the *forwarding* curve:

.. math::
    \prod_{k}\left(1 + r_k \delta_k\right) = \frac{P_f(s)}{P_f(e)}
    \quad\Rightarrow\quad
    \text{PV}_{float} = N \sum_j \left(\frac{P_f(s_j)}{P_f(e_j)} - 1 + \text{spread}\,\tau_j\right) P_d(p_j)

Fixed leg: :math:`\text{PV}_{fixed} = N K \sum_i \tau_i P_d(p_i) = N K\,A`.
Par rate: :math:`K^* = \text{PV}_{float} / (N A)`.

Seasoned swaps compound the published €STR fixings up to the curve date and
project the rest (:meth:`OvernightIndexedSwap.compounded_growth`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

from rates_analytics.conventions.market import EUR_ESTR_OIS, SwapConvention
from rates_analytics.conventions.period import Period
from rates_analytics.instruments.common import InstrumentError, swap_start_and_maturity
from rates_analytics.instruments.legs import Leg, build_leg

if TYPE_CHECKING:  # avoid a runtime dependency instruments -> curves
    from rates_analytics.curves.yield_curve import YieldCurve


@dataclass(frozen=True)
class OvernightIndexedSwap:
    """Fixed vs compounded overnight swap.

    Args:
        start: effective date (unadjusted, usually spot).
        maturity: unadjusted termination date.
        fixed_rate: fixed coupon (decimal).
        notional: positive notional.
        pay_fixed: ``True`` for a payer swap (pay fixed, receive overnight).
        spread: spread over the compounded overnight rate.
        convention: swap convention (default €STR OIS).
    """

    start: date
    maturity: date
    fixed_rate: float = 0.0
    notional: float = 1.0
    pay_fixed: bool = True
    spread: float = 0.0
    convention: SwapConvention = EUR_ESTR_OIS
    fixed_leg: Leg = field(init=False, repr=False)
    float_leg: Leg = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.start < self.maturity:
            raise InstrumentError(f"OIS start {self.start} must precede maturity {self.maturity}")
        if not np.isfinite(self.fixed_rate) or not np.isfinite(self.spread):
            raise InstrumentError("fixed_rate and spread must be finite")
        if not self.notional > 0:
            raise InstrumentError("notional must be positive")
        c = self.convention
        object.__setattr__(self, "fixed_leg", build_leg(self.start, self.maturity, c.fixed_leg, c.calendar,
                                                        c.end_of_month))
        object.__setattr__(self, "float_leg", build_leg(self.start, self.maturity, c.float_leg, c.calendar,
                                                        c.end_of_month))

    @classmethod
    def from_tenor(cls, trade_date: date, tenor: str | Period, fixed_rate: float = 0.0,
                   convention: SwapConvention = EUR_ESTR_OIS, forward_start: str | Period = "0D",
                   **kwargs) -> OvernightIndexedSwap:
        """Spot (or forward) starting OIS quoted by tenor, e.g. ``from_tenor(d, "10Y", 0.025)``."""
        start, maturity = swap_start_and_maturity(trade_date, tenor, convention, forward_start)
        return cls(start, maturity, fixed_rate, convention=convention, **kwargs)

    # ------------------------------------------------------------ dates
    @property
    def effective_date(self) -> date:
        return self.fixed_leg.periods[0].start

    @property
    def last_relevant_date(self) -> date:
        """Last date the instrument depends on (= curve pillar in the bootstrap)."""
        return max(self.fixed_leg.last_payment_date, self.float_leg.last_payment_date,
                   self.float_leg.periods[-1].end)

    # -------------------------------------------------------- valuation
    def is_seasoned(self, reference_date: date) -> bool:
        return self.float_leg.periods[0].start < reference_date

    def _dates(self) -> np.ndarray:
        """Float starts | float ends | float payments | fixed payments (one vectorised curve call)."""
        cached = self.__dict__.get("_dates_cache")
        if cached is None:
            fl, fx = self.float_leg, self.fixed_leg
            cached = np.concatenate([fl.accrual_starts, fl.accrual_ends, fl.payment_dates, fx.payment_dates])
            object.__setattr__(self, "_dates_cache", cached)
        return cached

    def compounded_growth(self, forward_curve: YieldCurve, fixings=None) -> tuple[np.ndarray, np.ndarray]:
        r"""(compounded growth :math:`\prod(1+r_d\delta_d) - 1` per period, accrued-to-date growth).

        Past overnight fixings are compounded day by day (TARGET business days,
        ACT/360); from the first unknown fixing the remaining factor telescopes
        into :math:`P_f(d)/P_f(e)`. Paid periods get NaN. The second array is the
        growth compounded up to the curve date (used for the accrued interest).
        """
        from rates_analytics.instruments.fixings import Fixings

        ref = forward_curve.reference_date
        idx = self.convention.index
        cal, dc = idx.calendar, idx.day_counter
        fx = Fixings.coerce(fixings, idx.name)
        periods = self.float_leg.periods
        growth = np.full(len(periods), np.nan)
        to_date_growth = np.zeros(len(periods))
        for i, p in enumerate(periods):
            if p.payment_date <= ref:
                continue
            factor, d = 1.0, p.start
            while d < p.end and (d < ref or (d == ref and d in fx)):
                nxt = min(cal.advance(d, 1), p.end)
                factor *= 1.0 + fx.require(d) * dc.year_fraction(d, nxt)
                d = nxt
            to_date_growth[i] = factor - 1.0
            if d < p.end:
                factor *= forward_curve.discount(d) / forward_curve.discount(p.end)
            growth[i] = factor - 1.0
        return growth, to_date_growth

    def _legs_pv(self, discount_curve, forward_curve=None, fixings=None) -> tuple[float, float]:
        """(float leg PV, annuity) on the periods still to be paid."""
        ref = discount_curve.reference_date
        fl, fx = self.float_leg, self.fixed_leg
        n = len(fl)
        if not self.is_seasoned(ref) and fx.periods[0].payment_date > ref:
            dates = self._dates()
            p_disc = discount_curve.discount(dates)
            p_fwd = p_disc if forward_curve is None else forward_curve.discount(dates[: 2 * n])
            growth = p_fwd[:n] / p_fwd[n:2 * n] - 1.0 + self.spread * fl.accruals
            flt = self.notional * float(np.dot(growth, p_disc[2 * n:3 * n]))
            ann = self.notional * float(np.dot(fx.accruals, p_disc[3 * n:]))
            return flt, ann
        growth, _ = self.compounded_growth(forward_curve or discount_curve, fixings)
        alive_f = ~np.isnan(growth)
        cash = growth[alive_f] + self.spread * fl.accruals[alive_f]
        flt = self.notional * float(np.dot(cash, discount_curve.discount(fl.payment_dates[alive_f])))
        return flt, self.annuity(discount_curve)

    def annuity(self, discount_curve: YieldCurve) -> float:
        """:math:`N\\sum_i \\tau_i P_d(p_i)` over the fixed periods still to be paid."""
        ref = discount_curve.reference_date
        alive = [p for p in self.fixed_leg.periods if p.payment_date > ref]
        if not alive:
            return 0.0
        acc = np.array([p.year_fraction for p in alive])
        pay = np.array([p.payment_date for p in alive], dtype="datetime64[D]")
        return self.notional * float(np.dot(acc, discount_curve.discount(pay)))

    def float_bpv(self, discount_curve: YieldCurve) -> float:
        ref = discount_curve.reference_date
        alive = [p for p in self.float_leg.periods if p.payment_date > ref]
        if not alive:
            return 0.0
        acc = np.array([p.year_fraction for p in alive])
        pay = np.array([p.payment_date for p in alive], dtype="datetime64[D]")
        return self.notional * float(np.dot(acc, discount_curve.discount(pay)))

    def float_leg_pv(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
                     fixings=None) -> float:
        """PV of the compounded overnight leg (receiver view, positive)."""
        return self._legs_pv(discount_curve, forward_curve, fixings)[0]

    def fixed_leg_pv(self, discount_curve: YieldCurve) -> float:
        return self.fixed_rate * self.annuity(discount_curve)

    def fair_rate(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
                  fixings=None) -> float:
        """Par fixed rate."""
        flt, ann = self._legs_pv(discount_curve, forward_curve, fixings)
        if ann == 0.0:
            raise InstrumentError("No fixed cash flow left: the swap has matured")
        return flt / ann

    def fair_spread(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
                    fixings=None) -> float:
        """Spread over compounded €STR that sets the NPV to zero."""
        flt, ann = self._legs_pv(discount_curve, forward_curve, fixings)
        return self.spread + (self.fixed_rate * ann - flt) / self.float_bpv(discount_curve)

    def npv(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None, fixings=None) -> float:
        """NPV from the holder's view (payer: float - fixed)."""
        flt, ann = self._legs_pv(discount_curve, forward_curve, fixings)
        v = flt - self.fixed_rate * ann
        return v if self.pay_fixed else -v
