r"""Yield curve objects shared by every module of the terminal.

A :class:`YieldCurve` maps a date (or a time in years) to a discount factor
:math:`P(0,t)`. Time is measured ACT/365F from the curve reference date
(the valuation date). All other quantities derive from :math:`P`:

* zero rate          :math:`z(t)` such that :math:`P = e^{-z t}` (or another compounding),
* simple forward     :math:`F(t_1,t_2) = \frac{1}{\tau}\left(\frac{P(t_1)}{P(t_2)} - 1\right)`,
* instantaneous forward :math:`f(t) = -\partial_t \ln P(t)`.

:class:`InterpolatedCurve` is defined by node discount factors and an
interpolation scheme on :math:`-\ln P` (see :mod:`.interpolation`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypeAlias

import numpy as np
import pandas as pd

from rates_analytics.conventions.calendar import TARGET, Calendar
from rates_analytics.conventions.daycount import ACT_360, ACT_365F, DayCounter
from rates_analytics.curves.compounding import Compounding, rate_from_discount
from rates_analytics.curves.interpolation import (
    CurveError,
    InterpolationMethod,
    LogDiscountInterpolator,
    make_interpolator,
)

DateOrTime: TypeAlias = date | float | int | np.ndarray | Sequence


class YieldCurve(ABC):
    """Abstract discount curve.

    Attributes:
        reference_date: valuation date (t = 0, P = 1).
        calendar: calendar of the instruments the curve was built from.
        name: label used in reports / UI (``"EUR-ESTR"``...).
    """

    time_day_counter: DayCounter = ACT_365F

    def __init__(self, reference_date: date, calendar: Calendar = TARGET, name: str = "") -> None:
        self.reference_date = reference_date
        self.calendar = calendar
        self.name = name

    # ------------------------------------------------------------- time
    def time(self, x: DateOrTime):
        """Year fraction ACT/365F from the reference date (dates, times or arrays of either)."""
        if isinstance(x, date):
            return self.time_day_counter.year_fraction(self.reference_date, x)
        if isinstance(x, (int, float, np.floating, np.integer)):
            return float(x)
        arr = np.asarray(x)
        if arr.dtype == object or np.issubdtype(arr.dtype, np.datetime64):
            ref = np.datetime64(self.reference_date, "D")
            return (arr.astype("datetime64[D]") - ref).astype(float) / 365.0
        return arr.astype(float)

    # ------------------------------------------------------- primitives
    @abstractmethod
    def _discount_t(self, t: np.ndarray) -> np.ndarray:
        """Discount factors at times ``t`` (array, t >= 0)."""

    @abstractmethod
    def _inst_forward_t(self, t: np.ndarray) -> np.ndarray:
        """Instantaneous forwards at times ``t``."""

    # ------------------------------------------------------- public API
    def discount(self, x: DateOrTime):
        """Discount factor :math:`P(0,t)` for a date, a time or an array."""
        t = self.time(x)
        ta = np.atleast_1d(np.asarray(t, dtype=float))
        if np.any(ta < -1e-14):
            raise CurveError(f"{self.name or 'curve'}: date before reference date {self.reference_date}")
        out = self._discount_t(np.maximum(ta, 0.0))
        return float(out[0]) if np.ndim(t) == 0 else out

    __call__ = discount

    def zero_rate(self, x: DateOrTime, compounding: Compounding | str = Compounding.CONTINUOUS,
                  frequency: int = 1):
        """Zero rate to ``x`` (ACT/365F time). At t=0 the short rate is returned."""
        t = np.atleast_1d(np.asarray(self.time(x), dtype=float))
        tt = np.where(t <= 0, 1e-8, t)
        out = rate_from_discount(self._discount_t(tt), tt, compounding, frequency)
        out = np.atleast_1d(out)
        return float(out[0]) if np.ndim(self.time(x)) == 0 else out

    def forward_rate(self, d1: date, d2: date, day_counter: DayCounter = ACT_360) -> float:
        """Simple forward rate between two dates under ``day_counter``."""
        if d2 <= d1:
            raise CurveError(f"forward_rate needs d1 < d2, got {d1} >= {d2}")
        tau = day_counter.year_fraction(d1, d2)
        return (self.discount(d1) / self.discount(d2) - 1.0) / tau

    def instantaneous_forward(self, x: DateOrTime):
        """Instantaneous forward :math:`f(t)`."""
        t = self.time(x)
        out = self._inst_forward_t(np.atleast_1d(np.asarray(t, dtype=float)))
        return float(out[0]) if np.ndim(t) == 0 else out

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} ref={self.reference_date}>"


@dataclass(frozen=True)
class CurveNode:
    """A pillar of an interpolated curve."""

    date: date
    time: float
    discount: float


class InterpolatedCurve(YieldCurve):
    """Curve defined by pillar discount factors and an interpolation on ``-ln P``.

    Args:
        reference_date: valuation date; an implicit node (t=0, P=1) is added.
        dates: pillar dates (strictly increasing, after ``reference_date``).
        discounts: discount factors at the pillars (positive).
        interpolation: ``"log_linear"`` or ``"monotone_convex"``.
        positivity: monotone convex positivity collar.
    """

    def __init__(self, reference_date: date, dates: Sequence[date], discounts: Sequence[float],
                 interpolation: InterpolationMethod | str = InterpolationMethod.LOG_LINEAR,
                 calendar: Calendar = TARGET, name: str = "", positivity: bool = False) -> None:
        super().__init__(reference_date, calendar, name)
        dates = list(dates)
        dfs = np.asarray(discounts, dtype=float)
        if len(dates) != dfs.size or not dates:
            raise CurveError("dates and discounts must be non-empty and of equal length")
        if dates[0] <= reference_date:
            raise CurveError(f"First pillar {dates[0]} must be after the reference date {reference_date}")
        if any(b <= a for a, b in zip(dates, dates[1:])):
            raise CurveError("Pillar dates must be strictly increasing")
        if np.any(~np.isfinite(dfs)) or np.any(dfs <= 0):
            raise CurveError("Discount factors must be finite and positive")
        self.dates = tuple(dates)
        self.interpolation = InterpolationMethod.parse(interpolation)
        self.positivity = positivity
        times = np.array([self.time(d) for d in dates])
        self._interp: LogDiscountInterpolator = make_interpolator(
            self.interpolation, np.concatenate(([0.0], times)), np.concatenate(([0.0], -np.log(dfs))),
            positivity=positivity)

    @property
    def times(self) -> np.ndarray:
        return self._interp.t[1:]

    @property
    def node_discounts(self) -> np.ndarray:
        return np.exp(-self._interp.y[1:])

    def nodes(self) -> list[CurveNode]:
        return [CurveNode(d, float(t), float(p)) for d, t, p in zip(self.dates, self.times, self.node_discounts)]

    def _discount_t(self, t):
        return np.exp(-self._interp.value(t))

    def _inst_forward_t(self, t):
        return self._interp.forward(t)

    def with_discounts(self, discounts: Iterable[float]) -> InterpolatedCurve:
        """Same pillars and interpolation, new node discount factors (used by bootstrap / bumps)."""
        return InterpolatedCurve(self.reference_date, self.dates, list(discounts), self.interpolation,
                                 self.calendar, self.name, self.positivity)

    def to_frame(self) -> pd.DataFrame:
        """Pillar table: date, time, DF, continuous zero (ACT/365F), instantaneous forward."""
        t = self.times
        return pd.DataFrame({
            "date": self.dates,
            "time": t,
            "discount": self.node_discounts,
            "zero_cc": -np.log(self.node_discounts) / t,
            "inst_forward": self._interp.forward(t),
        })
