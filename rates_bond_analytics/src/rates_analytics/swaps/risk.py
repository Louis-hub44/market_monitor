r"""Bucketed (per market quote) risk of any instrument priced on a :class:`CurveSet`.

**Jacobian method** (default, the market-standard way to get par-rate risk without
re-bootstrapping). With :math:`y` the curve nodes (:math:`-\ln P` at every pillar of
the €STR and EURIBOR curves) and :math:`q(y)` the quotes implied by the curves,

.. math::
    \frac{\partial V}{\partial q} = \left(\frac{\partial q}{\partial y}\right)^{-\top}
    \frac{\partial V}{\partial y}

Both derivatives are central finite differences on the nodes (no root finding):
the Jacobian :math:`J = \partial q/\partial y` is square because the bootstrap
reprices every instrument exactly; it is block lower-triangular in the dual-curve
set (EURIBOR quotes depend on €STR nodes through discounting, not the reverse).
The bucket of quote :math:`i` is :math:`\partial V/\partial q_i \times 1\text{bp}`.

**Rebuild method**: bump each quote by ±1bp, re-bootstrap the whole set, reprice
(central difference). Slower; used to validate the Jacobian.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.market_curves import CurveSet
from rates_analytics.curves.yield_curve import InterpolatedCurve

Pricer = Callable[[CurveSet], float]


@dataclass
class BucketedRisk:
    """Per-quote DV01 (value change for +1bp on each market quote, first order)."""

    table: pd.DataFrame
    method: str
    bump: float
    base_value: float

    @property
    def total(self) -> float:
        return float(self.table["dv01"].sum())

    def by_curve(self) -> pd.Series:
        return self.table.groupby("curve")["dv01"].sum()


def _blocks(curves: CurveSet):
    blocks = [(curves.overnight_index, curves.discount)]
    blocks += list(curves.forwards.items())
    for name, res in blocks:
        if not isinstance(res.curve, InterpolatedCurve):
            raise CurveError(f"Bucketed risk needs bootstrapped curves ({name} is not)")
    return blocks


def _rebuild_curve(c: InterpolatedCurve, y: np.ndarray) -> InterpolatedCurve:
    return InterpolatedCurve(c.reference_date, c.dates, np.exp(-y), c.interpolation, c.calendar, c.name,
                             c.positivity)


def _instrument_rows(curves: CurveSet) -> pd.DataFrame:
    rows = []
    for name, res in _blocks(curves):
        for h in res.helpers:
            rows.append({"curve": name, "instrument": h.label, "market_id": h.market_id, "pillar": h.pillar_date,
                         "quote": h.quote})
    return pd.DataFrame(rows)


def _jacobian_risk(curves: CurveSet, pricer: Pricer, bump: float, node_bump: float) -> BucketedRisk:
    blocks = _blocks(curves)
    sizes = [len(res.curve.dates) for _, res in blocks]
    y0 = np.concatenate([-np.log(res.curve.node_discounts) for _, res in blocks])
    cuts = np.cumsum([0] + sizes)

    def evaluate(y: np.ndarray) -> tuple[np.ndarray, float]:
        new = [_rebuild_curve(res.curve, y[cuts[k]:cuts[k + 1]]) for k, (_, res) in enumerate(blocks)]
        cs = curves.with_curves(new[0], {name: c for (name, _), c in zip(blocks[1:], new[1:])})
        disc = cs.discount_curve
        q = [h.implied_quote_given(disc, disc) for h in cs.discount.helpers]
        for name, res in cs.forwards.items():
            fwd = cs.forward_curve(name)
            q += [h.implied_quote_given(fwd, disc) for h in res.helpers]
        return np.array(q), float(pricer(cs))

    n = y0.size
    jac, grad = np.empty((n, n)), np.empty(n)
    for j in range(n):
        up, dn = y0.copy(), y0.copy()
        up[j] += node_bump
        dn[j] -= node_bump
        q_up, v_up = evaluate(up)
        q_dn, v_dn = evaluate(dn)
        jac[:, j] = (q_up - q_dn) / (2 * node_bump)
        grad[j] = (v_up - v_dn) / (2 * node_bump)
    try:
        dv_dq = np.linalg.solve(jac.T, grad)
    except np.linalg.LinAlgError as exc:  # pragma: no cover - would mean a broken bootstrap
        raise CurveError("Singular quote/node Jacobian") from exc
    table = _instrument_rows(curves)
    table["dv01"] = dv_dq * bump
    return BucketedRisk(table, "jacobian", bump, float(pricer(curves)))


def _rebuild_risk(curves: CurveSet, pricer: Pricer, bump: float, market_ids=None,
                  quote_bump: float | None = None) -> BucketedRisk:
    table = _instrument_rows(curves)
    if market_ids is not None:
        table = table[table["market_id"].isin(list(market_ids))].reset_index(drop=True)
    base = dict(curves.quotes)
    h = quote_bump or bump
    dv = []
    for mid in table["market_id"]:
        up, dn = dict(base), dict(base)
        up[mid] += h
        dn[mid] -= h
        dv.append((pricer(curves.rebuild(up)) - pricer(curves.rebuild(dn))) / (2 * h) * bump)
    table["dv01"] = dv
    return BucketedRisk(table, "rebuild", bump, float(pricer(curves)))


def bucketed_risk(curves: CurveSet, pricer: Pricer, bump: float = 1e-4, method: str = "jacobian",
                  node_bump: float = 1e-6, market_ids=None, quote_bump: float | None = None) -> BucketedRisk:
    """DV01 per market quote of ``pricer`` (``method`` = ``"jacobian"`` or ``"rebuild"``).

    Buckets are expressed per ``bump`` (1bp). The rebuild method differentiates with
    ``quote_bump`` (default ``bump``) and can be restricted to ``market_ids``.
    Monotone convex curves are only piecewise smooth (the Hagan-West shape changes
    region with the data): 1bp re-bootstraps and the Jacobian then differ slightly,
    while a small ``quote_bump`` recovers the Jacobian.
    """
    if bump <= 0 or node_bump <= 0:
        raise CurveError("Bumps must be positive")
    if method == "jacobian":
        return _jacobian_risk(curves, pricer, bump, node_bump)
    if method == "rebuild":
        return _rebuild_risk(curves, pricer, bump, market_ids, quote_bump)
    raise CurveError(f"Unknown risk method {method!r}")


def parallel_dv01(curves: CurveSet, pricer: Pricer, bump: float = 1e-4) -> float:
    """Value change for +``bump`` on every market quote, full re-bootstrap (SWPM-style DV01)."""
    up = {k: v + bump for k, v in curves.quotes.items()}
    return float(pricer(curves.rebuild(up)) - pricer(curves))
