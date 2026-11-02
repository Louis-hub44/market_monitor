r"""Bootstrap instruments ("rate helpers").

Each helper knows its market quote, the date it fixes on the curve being built
(the *pillar*, its last relevant date) and the quote implied by that curve.

Single-curve helpers price entirely off the curve being bootstrapped. Dual-curve
helpers (``discount_curve`` given) project forwards on the curve being
bootstrapped and discount on the exogenous collateral curve (€STR).

* :class:`DepositHelper` – simple money-market rate
  :math:`r = \frac{1}{\tau}\left(\frac{P(s)}{P(e)} - 1\right)` (€STR ON, EURIBOR fixing);
* :class:`FRAHelper` – forward rate agreement on an IBOR index, same formula on
  :math:`[s, e] = [\text{spot} + m\text{M},\ s + \text{index tenor}]`;
* :class:`OISHelper` – par rate of a €STR OIS;
* :class:`SwapHelper` – par rate of a fixed vs IBOR swap, dual curve.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import date

from rates_analytics.conventions.dates import money_market_dates, spot_date
from rates_analytics.conventions.market import (
    EUR_DEPOSIT,
    EUR_ESTR_OIS,
    EUR_EURIBOR_6M_SWAP,
    EURIBOR_6M,
    DepositConvention,
    RateIndex,
    SwapConvention,
)
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.yield_curve import YieldCurve
from rates_analytics.instruments.irs import VanillaSwap
from rates_analytics.instruments.ois import OvernightIndexedSwap


def deposit_convention_for(index: RateIndex) -> DepositConvention:
    """Deposit convention matching an IBOR index (the fixing is a spot-starting deposit)."""
    return DepositConvention(f"{index.name}_DEPOSIT", index.day_counter, index.calendar, index.fixing_lag,
                             index.bdc, index.end_of_month)


class RateHelper(ABC):
    """Abstract bootstrap instrument."""

    kind: str = "abstract"

    def __init__(self, quote: float, label: str, market_id: str | None = None) -> None:
        if quote is None or not math.isfinite(float(quote)):
            raise CurveError(f"{label}: non-finite quote {quote!r}")
        self.quote = float(quote)
        self.label = label
        self.market_id = market_id

    @property
    @abstractmethod
    def earliest_date(self) -> date:
        """First date the instrument depends on."""

    @property
    @abstractmethod
    def pillar_date(self) -> date:
        """Curve node solved by this instrument (last relevant date)."""

    @abstractmethod
    def implied_quote(self, curve: YieldCurve) -> float:
        """Quote implied by ``curve`` (the curve being bootstrapped)."""

    def error(self, curve: YieldCurve) -> float:
        return self.implied_quote(curve) - self.quote

    def implied_quote_given(self, curve: YieldCurve, discount_curve: YieldCurve | None = None) -> float:
        """Implied quote with an explicit discounting curve (dual-curve helpers override this)."""
        return self.implied_quote(curve)

    def with_quote(self, quote: float) -> RateHelper:
        """Copy with another quote (bumps for bucketed risk)."""
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.quote = float(quote)
        return clone

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label} {self.quote:.6%} pillar={self.pillar_date}>"


class _SimpleRateHelper(RateHelper):
    """Helper quoted as a simple rate on ``[start, end]`` of the curve being built."""

    start: date
    end: date
    tau: float

    @property
    def earliest_date(self) -> date:
        return self.start

    @property
    def pillar_date(self) -> date:
        return self.end

    def implied_quote(self, curve):
        return (curve.discount(self.start) / curve.discount(self.end) - 1.0) / self.tau


class DepositHelper(_SimpleRateHelper):
    """Money-market deposit (ACT/360 by default). ``ON``/``TN``/``SN`` or a term tenor from spot."""

    kind = "deposit"

    def __init__(self, trade_date: date, tenor: str, rate: float,
                 convention: DepositConvention = EUR_DEPOSIT, market_id: str | None = None) -> None:
        super().__init__(rate, f"DEP {tenor.upper()}", market_id)
        self.tenor = tenor.upper()
        self.convention = convention
        self.start, self.end = money_market_dates(trade_date, tenor, convention.calendar, convention.spot_lag,
                                                  convention.bdc, convention.end_of_month)
        self.tau = convention.day_counter.year_fraction(self.start, self.end)


class FRAHelper(_SimpleRateHelper):
    """FRA ``m x (m + index tenor)`` on an IBOR index, e.g. ``FRAHelper(d, 6, 0.021)`` = 6x12 on 6M."""

    kind = "fra"

    def __init__(self, trade_date: date, months_to_start: int, rate: float, index: RateIndex = EURIBOR_6M,
                 market_id: str | None = None) -> None:
        if months_to_start < 1:
            raise CurveError(f"FRA start must be >= 1 month, got {months_to_start}")
        months_tenor = index.tenor.total_months or 0
        super().__init__(rate, f"FRA {months_to_start}X{months_to_start + months_tenor}", market_id)
        self.index = index
        spot = spot_date(trade_date, index.calendar, index.fixing_lag)
        self.start = index.calendar.advance(spot, f"{months_to_start}M", index.bdc, index.end_of_month)
        self.end = index.calendar.advance(self.start, index.tenor, index.bdc, index.end_of_month)
        self.fixing_date = index.calendar.advance(self.start, -index.fixing_lag)
        self.tau = index.day_counter.year_fraction(self.start, self.end)


class OISHelper(RateHelper):
    """Spot-starting overnight indexed swap quoted at its par rate (single curve)."""

    kind = "ois"

    def __init__(self, trade_date: date, tenor: str, rate: float,
                 convention: SwapConvention = EUR_ESTR_OIS, market_id: str | None = None,
                 forward_start: str = "0D") -> None:
        super().__init__(rate, f"OIS {tenor.upper()}", market_id)
        self.tenor = tenor.upper()
        self.swap = OvernightIndexedSwap.from_tenor(trade_date, tenor, rate, convention,
                                                    forward_start=forward_start)

    @property
    def earliest_date(self) -> date:
        return self.swap.effective_date

    @property
    def pillar_date(self) -> date:
        return self.swap.last_relevant_date

    def implied_quote(self, curve):
        return self.swap.fair_rate(curve)


class SwapHelper(RateHelper):
    """Fixed vs IBOR swap quoted at its par rate.

    With ``discount_curve`` the helper is dual-curve: forwards on the curve being
    bootstrapped, discounting on ``discount_curve`` (market standard since 2008,
    €STR discounting for EUR CSA trades since 2020). Without it, single-curve.
    """

    kind = "swap"

    def __init__(self, trade_date: date, tenor: str, rate: float,
                 convention: SwapConvention = EUR_EURIBOR_6M_SWAP, discount_curve: YieldCurve | None = None,
                 market_id: str | None = None, forward_start: str = "0D") -> None:
        super().__init__(rate, f"IRS {tenor.upper()}", market_id)
        self.tenor = tenor.upper()
        self.discount_curve = discount_curve
        self.swap = VanillaSwap.from_tenor(trade_date, tenor, rate, convention, forward_start=forward_start)
        if discount_curve is not None and discount_curve.reference_date > trade_date:
            raise CurveError("Discount curve reference date is after the trade date")

    @property
    def earliest_date(self) -> date:
        return self.swap.effective_date

    @property
    def pillar_date(self) -> date:
        return self.swap.last_relevant_date

    def implied_quote(self, curve):
        return self.swap.fair_rate(self.discount_curve or curve, curve)

    def implied_quote_given(self, curve, discount_curve=None):
        disc = (discount_curve or self.discount_curve) if self.discount_curve is not None else curve
        return self.swap.fair_rate(disc, curve)
