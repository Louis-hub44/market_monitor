r"""Curve sets: one discounting curve plus forwarding curves per index.

:class:`CurveSet` is the object the other modules of the terminal consume
(Pricing Lab, CVA, Portfolio Intelligence)::

    curves = build_eur_curves(as_of, quotes)
    p = curves.discount_curve.discount(d)                  # €STR discounting
    f = curves.forward_curve("EURIBOR6M").forward_rate(s, e)

It also exposes the €STR / EURIBOR 6M basis, quoted as the spread :math:`b`
added to the compounded €STR leg that equates both floating legs:

.. math::
    b(T) = \frac{\text{PV}_{6M}(T) - \text{PV}_{€STR}(T)}{\sum_j \tau_j P_d(p_j)}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from rates_analytics.conventions.daycount import ACT_360
from rates_analytics.conventions.market import EUR_ESTR_OIS, EUR_EURIBOR_6M_SWAP, RateIndex
from rates_analytics.curves.bootstrap import BootstrapResult
from rates_analytics.curves.estr import ESTR_CURVE_INSTRUMENTS, build_estr_curve
from rates_analytics.curves.ibor import EURIBOR6M_CURVE_INSTRUMENTS, build_ibor_curve
from rates_analytics.curves.interpolation import CurveError, InterpolationMethod
from rates_analytics.curves.yield_curve import YieldCurve
from rates_analytics.instruments.irs import VanillaSwap
from rates_analytics.instruments.ois import OvernightIndexedSwap

DEFAULT_BASIS_TENORS = ("2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y", "50Y")


@dataclass
class CurveSet:
    """Discounting curve + forwarding curves (keyed by index name, e.g. ``EURIBOR6M``)."""

    as_of: date
    discount: BootstrapResult
    forwards: dict[str, BootstrapResult] = field(default_factory=dict)
    overnight_index: str = "ESTR"
    quotes: dict[str, float] = field(default_factory=dict)
    build_options: dict = field(default_factory=dict)

    def rebuild(self, quotes: dict[str, float]) -> CurveSet:
        """Re-bootstrap the whole set from new quotes (same instruments and options)."""
        if not self.build_options:
            raise CurveError("This CurveSet was not built by build_eur_curves: cannot rebuild")
        return build_eur_curves(self.as_of, quotes, **self.build_options)

    def with_curves(self, discount_curve: YieldCurve, forward_curves: Mapping[str, YieldCurve]) -> CurveSet:
        """Same instruments, other curve objects (used for node bumps)."""
        def swap_curve(res: BootstrapResult, curve: YieldCurve) -> BootstrapResult:
            return BootstrapResult(curve, res.helpers, 0, dict(res.skipped))

        fwd = {k: swap_curve(v, forward_curves.get(k, v.curve)) for k, v in self.forwards.items()}
        return CurveSet(self.as_of, swap_curve(self.discount, discount_curve), fwd, self.overnight_index,
                        dict(self.quotes), dict(self.build_options))

    @property
    def discount_curve(self) -> YieldCurve:
        return self.discount.curve

    def forward_curve(self, index: str | RateIndex) -> YieldCurve:
        """Projection curve of ``index`` (the discount curve for the overnight index)."""
        name = index.name if isinstance(index, RateIndex) else str(index).upper()
        if name == self.overnight_index:
            return self.discount_curve
        try:
            return self.forwards[name].curve
        except KeyError as exc:
            raise CurveError(f"No forwarding curve for {name}; available: {sorted(self.forwards)}") from exc

    def basis_spreads(self, tenors: Sequence[str] = DEFAULT_BASIS_TENORS, index: str = "EURIBOR6M") -> pd.DataFrame:
        """€STR vs IBOR basis per tenor: OIS par, IRS par and basis-swap spread (bp)."""
        disc, fwd = self.discount_curve, self.forward_curve(index)
        rows = []
        for t in tenors:
            ois = OvernightIndexedSwap.from_tenor(self.as_of, t, convention=EUR_ESTR_OIS)
            irs = VanillaSwap.from_tenor(self.as_of, t, convention=EUR_EURIBOR_6M_SWAP)
            pv_ibor = irs.float_leg_pv(disc, fwd)
            pv_estr = ois.float_leg_pv(disc)
            leg = ois.float_leg
            bpv = float(np.dot(leg.accruals, disc.discount(leg.payment_dates)))
            rows.append({"tenor": t, "ois_par": ois.fair_rate(disc), "irs_par": irs.fair_rate(disc, fwd),
                         "basis_bp": (pv_ibor - pv_estr) / bpv * 1e4})
        return pd.DataFrame(rows)

    def forward_comparison(self, index: str = "EURIBOR6M", step_months: int = 6, horizon_years: int = 30,
                           ) -> pd.DataFrame:
        """6M EURIBOR forwards vs 6M €STR (compounded) forwards on a semi-annual grid."""
        disc, fwd = self.discount_curve, self.forward_curve(index)
        cal = disc.calendar
        start = cal.advance(self.as_of, 2)
        rows = []
        for k in range(0, horizon_years * 12, step_months):
            s = cal.advance(start, f"{k}M", "MF") if k else start
            e = cal.advance(s, "6M", "MF")
            fi, fo = fwd.forward_rate(s, e, ACT_360), disc.forward_rate(s, e, ACT_360)
            rows.append({"start": s, "end": e, "ibor_fwd": fi, "estr_fwd": fo, "spread_bp": (fi - fo) * 1e4})
        return pd.DataFrame(rows)


def build_eur_curves(
    as_of: date,
    quotes,
    interpolation: InterpolationMethod | str = InterpolationMethod.LOG_LINEAR,
    ibor_interpolation: InterpolationMethod | str | None = None,
    dual_curve: bool = True,
) -> CurveSet:
    """€STR discounting curve then EURIBOR 6M projection curve (dual curve by default)."""
    from rates_analytics.curves.templates import as_quote_mapping

    q = as_quote_mapping(quotes)
    estr = build_estr_curve(as_of, q, interpolation)
    e6m = build_ibor_curve(as_of, q, estr.curve if dual_curve else None,
                           interpolation=ibor_interpolation or interpolation)
    used = {h.market_id for h in (*estr.helpers, *e6m.helpers) if h.market_id}
    options = {"interpolation": interpolation, "ibor_interpolation": ibor_interpolation, "dual_curve": dual_curve}
    return CurveSet(as_of, estr, {"EURIBOR6M": e6m}, quotes={k: v for k, v in q.items() if k in used},
                    build_options=options)


def eur_curves_from_market(service, as_of: date, **kwargs) -> CurveSet:
    """Fetch every quote of both templates through the market-data service and build the set."""
    ids = [i.market_id for i in (*ESTR_CURVE_INSTRUMENTS, *EURIBOR6M_CURVE_INSTRUMENTS)]
    return build_eur_curves(as_of, service.quotes(ids, as_of), **kwargs)

