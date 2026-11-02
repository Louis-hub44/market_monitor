r"""Curves shifted by a zero-rate spread function.

:math:`P'(t) = P(t)\,e^{-s(t)\,t}` where :math:`s(t)` is a (continuously compounded,
ACT/365F) zero-rate spread. Used for

* parallel shifts and key-rate (tent) shocks – durations, KRDs (phase 5);
* the Z-spread – a constant :math:`s` (phase 6);
* curve scenarios: steepener, flattener, butterfly, custom pillars (phase 8).

:class:`PiecewiseLinearSpread` interpolates linearly between knots (in years) and
is flat outside. A set of tent functions centred on key tenors sums to 1
everywhere, so key-rate durations add up to the parallel (effective) duration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.yield_curve import YieldCurve


@dataclass(frozen=True)
class PiecewiseLinearSpread:
    """Zero spread :math:`s(t)`: linear between knots, flat outside (decimal units)."""

    times: tuple[float, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        t, v = np.asarray(self.times, float), np.asarray(self.values, float)
        if t.size == 0 or t.size != v.size:
            raise CurveError("Spread knots and values must be non-empty and of equal length")
        if np.any(np.diff(t) <= 0) or np.any(t < 0):
            raise CurveError("Spread knot times must be non-negative and strictly increasing")
        if not np.all(np.isfinite(v)):
            raise CurveError("Spread values must be finite")

    @classmethod
    def constant(cls, spread: float) -> PiecewiseLinearSpread:
        return cls((0.0,), (float(spread),))

    def __call__(self, t) -> np.ndarray:
        return np.interp(np.asarray(t, float), self.times, self.values)

    def derivative(self, t) -> np.ndarray:
        """ds/dt (0 outside the knot range and for a single knot)."""
        t = np.asarray(t, float)
        if len(self.times) == 1:
            return np.zeros_like(t)
        slopes = np.diff(self.values) / np.diff(self.times)
        k = np.clip(np.searchsorted(self.times, t, side="right") - 1, 0, len(slopes) - 1)
        inside = (t > self.times[0]) & (t < self.times[-1])
        return np.where(inside, slopes[k], 0.0)

    def __add__(self, other: PiecewiseLinearSpread) -> PiecewiseLinearSpread:
        knots = np.union1d(self.times, other.times)
        return PiecewiseLinearSpread(tuple(knots), tuple(self(knots) + other(knots)))


def tent(key_times: Sequence[float], k: int, size: float) -> PiecewiseLinearSpread:
    """Triangular shock of ``size`` centred on ``key_times[k]`` (flat to the edges for the first/last key)."""
    keys = list(key_times)
    if not 0 <= k < len(keys):
        raise CurveError(f"Key index {k} out of range")
    values = [0.0] * len(keys)
    values[k] = size
    return PiecewiseLinearSpread(tuple(float(x) for x in keys), tuple(values))


class SpreadedCurve(YieldCurve):
    """Base curve shifted by a zero spread function (``P' = P e^{-s(t) t}``)."""

    def __init__(self, base: YieldCurve, spread: PiecewiseLinearSpread | float, name: str = "") -> None:
        super().__init__(base.reference_date, base.calendar, name or f"{base.name}+spread")
        self.base = base
        self.spread = spread if isinstance(spread, PiecewiseLinearSpread) else PiecewiseLinearSpread.constant(spread)

    def _discount_t(self, t):
        return self.base._discount_t(t) * np.exp(-self.spread(t) * t)

    def _inst_forward_t(self, t):
        return self.base._inst_forward_t(t) + self.spread(t) + t * self.spread.derivative(t)


def parallel_shift(curve: YieldCurve, size: float) -> SpreadedCurve:
    """Curve shifted by ``size`` (decimal) on all zero rates."""
    return SpreadedCurve(curve, PiecewiseLinearSpread.constant(size), name=f"{curve.name}{size * 1e4:+.1f}bp")


def key_rate_shift(curve: YieldCurve, key_times: Sequence[float], k: int, size: float) -> SpreadedCurve:
    """Curve with a tent shock of ``size`` on key tenor ``k``."""
    return SpreadedCurve(curve, tent(key_times, k, size), name=f"{curve.name} KR{key_times[k]:g}Y")
