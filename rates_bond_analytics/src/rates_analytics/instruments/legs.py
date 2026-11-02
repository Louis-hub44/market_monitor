"""Swap leg construction from market conventions.

A leg is a list of :class:`~rates_analytics.conventions.schedule.AccrualPeriod`
(accrual dates, payment date, fixing date, accrual fraction) generated with
the leg convention of a :class:`~rates_analytics.conventions.market.SwapConvention`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from rates_analytics.conventions.calendar import Calendar
from rates_analytics.conventions.market import SwapLegConvention
from rates_analytics.conventions.schedule import (
    AccrualPeriod,
    DateGenerationRule,
    build_accrual_periods,
    generate_schedule,
)


@dataclass(frozen=True)
class Leg:
    """Accrual periods of one swap leg plus vectorised date arrays."""

    periods: tuple[AccrualPeriod, ...]

    @property
    def accrual_starts(self) -> np.ndarray:
        return np.array([p.start for p in self.periods], dtype="datetime64[D]")

    @property
    def accrual_ends(self) -> np.ndarray:
        return np.array([p.end for p in self.periods], dtype="datetime64[D]")

    @property
    def payment_dates(self) -> np.ndarray:
        return np.array([p.payment_date for p in self.periods], dtype="datetime64[D]")

    @property
    def accruals(self) -> np.ndarray:
        return np.array([p.year_fraction for p in self.periods])

    @property
    def last_payment_date(self) -> date:
        return self.periods[-1].payment_date

    def __len__(self) -> int:
        return len(self.periods)


def build_leg(
    start: date,
    end: date,
    leg: SwapLegConvention,
    calendar: Calendar,
    end_of_month: bool,
    fixing_lag: int | None = None,
    rule: DateGenerationRule = DateGenerationRule.BACKWARD,
) -> Leg:
    """Generate a leg between ``start`` and ``end`` (unadjusted maturity).

    The termination date is adjusted with the leg convention (market practice
    for swaps), stubs are short front stubs (backward generation).
    """
    sched = generate_schedule(start, end, leg.tenor, calendar, leg.bdc, leg.bdc, rule, end_of_month)
    periods = build_accrual_periods(sched, leg.day_counter, accrue_on_adjusted=True,
                                    payment_calendar=calendar, payment_bdc=leg.bdc,
                                    payment_lag=leg.payment_lag, fixing_calendar=calendar,
                                    fixing_lag=fixing_lag)
    return Leg(tuple(periods))
