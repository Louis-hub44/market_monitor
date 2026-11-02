"""Shared instrument utilities."""

from __future__ import annotations

from datetime import date

from rates_analytics.conventions.calendar import BusinessDayConvention
from rates_analytics.conventions.dates import maturity_from_tenor, spot_date
from rates_analytics.conventions.market import SwapConvention
from rates_analytics.conventions.period import Period
from rates_analytics.exceptions import RatesAnalyticsError


class InstrumentError(RatesAnalyticsError, ValueError):
    """Invalid instrument definition or valuation request."""


def swap_start_and_maturity(trade_date: date, tenor: str | Period, convention: SwapConvention,
                            forward_start: str | Period = "0D") -> tuple[date, date]:
    """Effective date (spot + forward start, Following) and unadjusted maturity of a swap."""
    c = convention
    start = spot_date(trade_date, c.calendar, c.spot_lag)
    fwd = Period.parse(forward_start)
    if not fwd.is_zero:
        start = c.calendar.advance(start, fwd, BusinessDayConvention.FOLLOWING)
    maturity = maturity_from_tenor(start, tenor, c.calendar, c.float_leg.bdc, c.end_of_month)
    return start, maturity
