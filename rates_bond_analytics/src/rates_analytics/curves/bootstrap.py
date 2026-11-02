r"""Iterative bootstrap of a discount curve from rate helpers.

Algorithm
---------
1. Helpers are sorted by pillar date (last relevant date); duplicated pillars
   are rejected. Node 0 is :math:`(t=0, P=1)`.
2. **First pass (sequential):** for :math:`i = 1..n`, the curve is built on nodes
   :math:`0..i` and :math:`P_i` is found with Brent's method so that helper
   :math:`i` reprices exactly (pillars beyond :math:`i` do not exist yet;
   flat-forward extrapolation is used inside the last segment if needed).
3. **Further passes (global schemes only):** monotone convex is not local – node
   :math:`i+1` changes the forward shape on segment :math:`i` – so each
   :math:`P_i` is re-solved on the full node set until
   :math:`\max_i |\Delta \ln P_i| <` ``accuracy``.

Log-linear interpolation is local: one pass is exact.
The unknown is solved on :math:`y_i = -\ln P_i`, bracketed by segment forwards
in [-20 %, +50 %] (widened automatically if needed).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from rates_analytics.conventions.calendar import TARGET, Calendar
from rates_analytics.curves.helpers import RateHelper
from rates_analytics.curves.interpolation import CurveError, InterpolationMethod
from rates_analytics.curves.yield_curve import InterpolatedCurve, YieldCurve

logger = logging.getLogger(__name__)


@dataclass
class BootstrapResult:
    """Bootstrapped curve plus the instruments and diagnostics behind it."""

    curve: YieldCurve
    helpers: tuple[RateHelper, ...]
    passes: int
    skipped: dict[str, str] = field(default_factory=dict)

    def implied_quotes(self) -> np.ndarray:
        return np.array([h.implied_quote(self.curve) for h in self.helpers])

    def max_abs_error_bp(self) -> float:
        q = np.array([h.quote for h in self.helpers])
        return float(np.max(np.abs(self.implied_quotes() - q)) * 1e4)

    def report(self) -> pd.DataFrame:
        """One row per instrument: quote, implied, error, pillar DF and zero rate."""
        c = self.curve
        rows = []
        for h, imp in zip(self.helpers, self.implied_quotes()):
            t = c.time(h.pillar_date)
            df = c.discount(h.pillar_date)
            rows.append({
                "instrument": h.label, "market_id": h.market_id, "quote": h.quote,
                "implied": imp, "error_bp": (imp - h.quote) * 1e4, "pillar": h.pillar_date,
                "time": t, "discount": df, "zero_cc": -np.log(df) / t,
            })
        return pd.DataFrame(rows)


def _validate(reference_date: date, helpers: Sequence[RateHelper]) -> list[RateHelper]:
    if not helpers:
        raise CurveError("Bootstrap needs at least one instrument")
    hs = sorted(helpers, key=lambda h: h.pillar_date)
    for h in hs:
        if h.pillar_date <= reference_date:
            raise CurveError(f"{h.label}: pillar {h.pillar_date} not after reference date {reference_date}")
        if h.earliest_date < reference_date:
            raise CurveError(f"{h.label}: starts before the reference date (seasoned instrument)")
    for a, b in zip(hs, hs[1:]):
        if a.pillar_date == b.pillar_date:
            raise CurveError(f"Instruments {a.label} and {b.label} share the pillar {a.pillar_date}")
    return hs


def _solve_node(i: int, helper: RateHelper, times: np.ndarray, y: np.ndarray, n_nodes: int,
                make_curve) -> float:
    """Solve y[i] (= -ln P_i) so that ``helper`` reprices; nodes ``0..n_nodes-1`` are used."""
    t_prev = times[i - 1] if i > 0 else 0.0
    y_prev = y[i - 1] if i > 0 else 0.0
    dt = times[i] - t_prev

    def objective(yi: float) -> float:
        y[i] = yi
        return helper.error(make_curve(n_nodes))

    lo, hi = y_prev - 0.20 * dt, y_prev + 0.50 * dt
    f_lo, f_hi = objective(lo), objective(hi)
    widen = 0
    while f_lo * f_hi > 0 and widen < 8:
        lo, hi = lo - dt, hi + dt
        f_lo, f_hi = objective(lo), objective(hi)
        widen += 1
    if f_lo * f_hi > 0:
        raise CurveError(f"{helper.label}: cannot bracket the root (quote {helper.quote:.6%})")
    root = brentq(objective, lo, hi, xtol=1e-15, rtol=4 * np.finfo(float).eps, maxiter=200)
    y[i] = root
    return root


def bootstrap(
    reference_date: date,
    helpers: Sequence[RateHelper],
    interpolation: InterpolationMethod | str = InterpolationMethod.LOG_LINEAR,
    calendar: Calendar = TARGET,
    name: str = "",
    accuracy: float = 1e-13,
    max_passes: int = 100,
    positivity: bool = False,
) -> BootstrapResult:
    """Bootstrap a discount curve.

    Args:
        reference_date: valuation date (t = 0).
        helpers: instruments (any order).
        interpolation: ``"log_linear"`` or ``"monotone_convex"``.
        accuracy: convergence threshold on :math:`-\\ln P` across passes.
        max_passes: cap on global passes (monotone convex).
        positivity: monotone convex positivity collar.

    Raises:
        CurveError: invalid instruments, no root, or no convergence.
    """
    method = InterpolationMethod.parse(interpolation)
    hs = _validate(reference_date, helpers)
    dates = [h.pillar_date for h in hs]
    probe = InterpolatedCurve(reference_date, dates, np.ones(len(dates)), method, calendar, name)
    times = probe.times.copy()
    y = np.array([h.quote * t for h, t in zip(hs, times)])  # flat initial guess

    def make_curve(n_nodes: int) -> InterpolatedCurve:
        return InterpolatedCurve(reference_date, dates[:n_nodes], np.exp(-y[:n_nodes]), method,
                                 calendar, name, positivity)

    for i, h in enumerate(hs):  # pass 1: sequential, local
        _solve_node(i, h, times, y, i + 1, make_curve)
    passes = 1
    if method is not InterpolationMethod.LOG_LINEAR and len(hs) > 1:
        while True:
            previous = y.copy()
            for i, h in enumerate(hs):
                _solve_node(i, h, times, y, len(hs), make_curve)
            passes += 1
            change = float(np.max(np.abs(y - previous)))
            if change < accuracy:
                break
            if passes >= max_passes:
                raise CurveError(f"Bootstrap did not converge after {passes} passes (last change {change:.2e})")
    curve = make_curve(len(hs))
    logger.debug("Bootstrapped %s with %d instruments in %d pass(es)", name, len(hs), passes)
    return BootstrapResult(curve, tuple(hs), passes)
