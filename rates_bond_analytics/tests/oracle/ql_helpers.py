"""Conversions between engine objects and QuantLib (used only in tests)."""

from __future__ import annotations

from datetime import date

import QuantLib as ql

from rates_analytics.conventions.calendar import BusinessDayConvention as BDC
from rates_analytics.conventions.period import Period, TimeUnit
from rates_analytics.conventions.schedule import DateGenerationRule


def to_ql(d: date) -> ql.Date:
    return ql.Date(d.day, d.month, d.year)


def from_ql(d: ql.Date) -> date:
    return date(d.year(), d.month(), d.dayOfMonth())


QL_BDC = {
    BDC.UNADJUSTED: ql.Unadjusted,
    BDC.FOLLOWING: ql.Following,
    BDC.MODIFIED_FOLLOWING: ql.ModifiedFollowing,
    BDC.PRECEDING: ql.Preceding,
    BDC.MODIFIED_PRECEDING: ql.ModifiedPreceding,
    BDC.HALF_MONTH_MODIFIED_FOLLOWING: ql.HalfMonthModifiedFollowing,
    BDC.NEAREST: ql.Nearest,
}

QL_UNIT = {TimeUnit.DAYS: ql.Days, TimeUnit.WEEKS: ql.Weeks,
           TimeUnit.MONTHS: ql.Months, TimeUnit.YEARS: ql.Years}

QL_RULE = {DateGenerationRule.BACKWARD: ql.DateGeneration.Backward,
           DateGenerationRule.FORWARD: ql.DateGeneration.Forward,
           DateGenerationRule.ZERO: ql.DateGeneration.Zero}


def ql_period(p: Period) -> ql.Period:
    return ql.Period(p.n, QL_UNIT[p.unit])
