r"""Fixed vs IBOR vanilla interest rate swap, dual-curve valuation.

The floating leg projects EURIBOR on the *forwarding* curve :math:`P_f` and every
cash flow is discounted on the collateral (€STR) curve :math:`P_d`:

.. math::
    F_j = \frac{1}{\tau_j}\left(\frac{P_f(s_j)}{P_f(e_j)} - 1\right), \qquad
    \text{PV}_{float} = N \sum_j (F_j + \text{spread})\,\tau_j\,P_d(p_j)

.. math::
    \text{PV}_{fixed} = N K \sum_i \tau_i^{30/360} P_d(p_i) = N K A, \qquad
    K^{par} = \frac{\sum_j F_j \tau_j P_d(p_j)}{\sum_i \tau_i P_d(p_i)}

Forwards are computed over the accrual period ("par coupons", the QuantLib
default and the standard approximation); ``indexed_coupons=True`` projects
the rate over the index's own period ``[value date, value date + 6M]``
instead. In single-curve mode (no separate forwarding curve) :math:`P_f = P_d`.
Seasoned swaps: past fixings are taken from a :class:`~rates_analytics.instruments.fixings.Fixings`
series and periods already paid are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

from rates_analytics.conventions.market import EUR_EURIBOR_6M_SWAP, SwapConvention
from rates_analytics.conventions.period import Period
from rates_analytics.instruments.common import InstrumentError, swap_start_and_maturity
from rates_analytics.instruments.legs import Leg, build_leg

if TYPE_CHECKING:
    from rates_analytics.curves.yield_curve import YieldCurve


def _fixing(period) -> date:
    """Fixing date of a floating period (always set on floating legs)."""
    if period.fixing_date is None:
        raise InstrumentError("floating period without fixing date")
    return period.fixing_date


@dataclass(frozen=True)
class VanillaSwap:
    """Fixed vs IBOR swap (default: EUR fixed annual 30/360 vs EURIBOR 6M ACT/360).

    Args:
        start: effective date (unadjusted, usually spot).
        maturity: unadjusted termination date.
        fixed_rate: fixed coupon (decimal).
        notional: positive notional.
        pay_fixed: ``True`` for a payer swap.
        spread: spread over the IBOR fixing.
        convention: swap convention (index, legs, calendar, EOM).
        indexed_coupons: project over the index period instead of the accrual period.
    """

    start: date
    maturity: date
    fixed_rate: float = 0.0
    notional: float = 1.0
    pay_fixed: bool = True
    spread: float = 0.0
    convention: SwapConvention = EUR_EURIBOR_6M_SWAP
    indexed_coupons: bool = False
    fixed_leg: Leg = field(init=False, repr=False)
    float_leg: Leg = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.start < self.maturity:
            raise InstrumentError(f"Swap start {self.start} must precede maturity {self.maturity}")
        if not (np.isfinite(self.fixed_rate) and np.isfinite(self.spread)):
            raise InstrumentError("fixed_rate and spread must be finite")
        if not self.notional > 0:
            raise InstrumentError("notional must be positive")
        c = self.convention
        if c.index.is_overnight:
            raise InstrumentError(f"{c.name} is an overnight convention: use OvernightIndexedSwap")
        object.__setattr__(self, "fixed_leg", build_leg(self.start, self.maturity, c.fixed_leg, c.calendar,
                                                        c.end_of_month))
        object.__setattr__(self, "float_leg", build_leg(self.start, self.maturity, c.float_leg, c.calendar,
                                                        c.end_of_month, fixing_lag=c.index.fixing_lag))

    @classmethod
    def from_tenor(cls, trade_date: date, tenor: str | Period, fixed_rate: float = 0.0,
                   convention: SwapConvention = EUR_EURIBOR_6M_SWAP, forward_start: str | Period = "0D",
                   **kwargs) -> VanillaSwap:
        """Spot (or forward) starting swap, e.g. ``VanillaSwap.from_tenor(d, "10Y", 0.026)``."""
        start, maturity = swap_start_and_maturity(trade_date, tenor, convention, forward_start)
        return cls(start, maturity, fixed_rate, convention=convention, **kwargs)

    # ------------------------------------------------------------- dates
    @property
    def effective_date(self) -> date:
        return self.float_leg.periods[0].start

    def _projection_dates(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(start, end, accrual fraction) of the projected rate for each float period."""
        cached = self.__dict__.get("_proj_cache")
        if cached is None:
            idx, leg = self.convention.index, self.float_leg
            if self.indexed_coupons:
                value = [idx.calendar.advance(_fixing(p), idx.fixing_lag) for p in leg.periods]
                end = [idx.calendar.advance(v, idx.tenor, idx.bdc, idx.end_of_month) for v in value]
            else:
                value = [p.start for p in leg.periods]
                end = [max(p.end, p.start) for p in leg.periods]
            tau = np.array([idx.day_counter.year_fraction(a, b) for a, b in zip(value, end)])
            cached = (np.array(value, dtype="datetime64[D]"), np.array(end, dtype="datetime64[D]"), tau)
            object.__setattr__(self, "_proj_cache", cached)
        return cached

    @property
    def last_relevant_date(self) -> date:
        """Last date the valuation depends on (bootstrap pillar)."""
        _, ends, _ = self._projection_dates()
        return max(self.fixed_leg.last_payment_date, self.float_leg.last_payment_date,
                   ends.max().astype(object))

    # --------------------------------------------------------- valuation
    def is_seasoned(self, reference_date: date) -> bool:
        """True if the first fixing is before ``reference_date`` (fixings needed)."""
        return _fixing(self.float_leg.periods[0]) < reference_date

    def forwards(self, forward_curve: YieldCurve) -> np.ndarray:
        """Projected IBOR fixings of every floating period (forward-starting swaps)."""
        s, e, tau = self._projection_dates()
        return (forward_curve.discount(s) / forward_curve.discount(e) - 1.0) / tau

    def float_rates(self, forward_curve: YieldCurve, fixings=None) -> tuple[np.ndarray, np.ndarray]:
        """(index rate per period, True where the rate is a known fixing).

        Fixing dates before the curve date need a fixing; a fixing on the curve
        date is used if provided, otherwise projected. Paid periods get NaN.
        """
        from rates_analytics.instruments.fixings import Fixings

        ref = forward_curve.reference_date
        fx = Fixings.coerce(fixings, self.convention.index.name)
        periods = self.float_leg.periods
        rates = np.full(len(periods), np.nan)
        known = np.zeros(len(periods), dtype=bool)
        s, e, tau = self._projection_dates()
        to_project = []
        for i, p in enumerate(periods):
            if p.payment_date <= ref:
                continue
            fd = _fixing(p)
            if fd < ref or (fd == ref and fd in fx):
                rates[i], known[i] = fx.require(fd), True
            else:
                to_project.append(i)
        if to_project:
            idx = np.array(to_project)
            rates[idx] = (forward_curve.discount(s[idx]) / forward_curve.discount(e[idx]) - 1.0) / tau[idx]
        return rates, known

    def _legs_pv(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None,
                 fixings=None) -> tuple[float, float]:
        """(float leg PV, fixed annuity) on the periods still to be paid."""
        fwd = forward_curve or discount_curve
        ref = discount_curve.reference_date
        fl, fx = self.float_leg, self.fixed_leg
        from rates_analytics.instruments.fixings import Fixings

        first_fix = _fixing(fl.periods[0])
        # a fixing already published on the curve date overrides the projection (same rule as float_rates)
        fixed_today = fixings is not None and first_fix == ref and first_fix in Fixings.coerce(
            fixings, self.convention.index.name)
        if first_fix >= ref and not fixed_today and fx.periods[0].payment_date > ref:
            rates = self.forwards(fwd) + self.spread
            flt = self.notional * float(np.dot(rates * fl.accruals, discount_curve.discount(fl.payment_dates)))
            ann = self.notional * float(np.dot(fx.accruals, discount_curve.discount(fx.payment_dates)))
            return flt, ann
        rates, _ = self.float_rates(fwd, fixings)
        alive_f = np.array([p.payment_date > ref for p in fl.periods])
        alive_x = np.array([p.payment_date > ref for p in fx.periods])
        flt = self.notional * float(np.dot((rates[alive_f] + self.spread) * fl.accruals[alive_f],
                                           discount_curve.discount(fl.payment_dates[alive_f])))
        ann = self.notional * float(np.dot(fx.accruals[alive_x], discount_curve.discount(fx.payment_dates[alive_x])))
        return flt, ann

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
        """PV of 1 (decimal) of spread on the floating periods still to be paid."""
        ref = discount_curve.reference_date
        alive = [p for p in self.float_leg.periods if p.payment_date > ref]
        if not alive:
            return 0.0
        acc = np.array([p.year_fraction for p in alive])
        pay = np.array([p.payment_date for p in alive], dtype="datetime64[D]")
        return self.notional * float(np.dot(acc, discount_curve.discount(pay)))

    def float_leg_pv(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
                     fixings=None) -> float:
        return self._legs_pv(discount_curve, forward_curve, fixings)[0]

    def fixed_leg_pv(self, discount_curve: YieldCurve) -> float:
        return self.fixed_rate * self.annuity(discount_curve)

    def fair_rate(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
                  fixings=None) -> float:
        """Par fixed rate (on the remaining periods for a seasoned swap)."""
        flt, ann = self._legs_pv(discount_curve, forward_curve, fixings)
        if ann == 0.0:
            raise InstrumentError("No fixed cash flow left: the swap has matured")
        return flt / ann

    def fair_spread(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
                    fixings=None) -> float:
        """Spread over IBOR that sets the NPV to zero at the current fixed rate."""
        flt, ann = self._legs_pv(discount_curve, forward_curve, fixings)
        return self.spread + (self.fixed_rate * ann - flt) / self.float_bpv(discount_curve)

    def npv(self, discount_curve: YieldCurve, forward_curve: YieldCurve | None = None,
            fixings=None) -> float:
        """NPV from the holder's view (payer: float - fixed)."""
        flt, ann = self._legs_pv(discount_curve, forward_curve, fixings)
        v = flt - self.fixed_rate * ann
        return v if self.pay_fixed else -v
