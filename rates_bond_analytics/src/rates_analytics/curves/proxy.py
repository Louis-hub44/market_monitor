r"""Proxy curves built from free public data, for when no EUR swap quote is available.

Free sources (ECB, CNBC, Bundesbank…) publish **no** €STR OIS or EURIBOR swap quotes:
without a swap quote source the curves cannot be bootstrapped from the market. This module
rebuilds the *missing quotes* from what is public, then the normal bootstrap runs on
them, so every downstream module (SWPM, spreads, scenarios…) works unchanged. The
result is a **proxy**, flagged as such in :class:`ProxyReport` and in the UI.

€STR OIS
    .. math::
        z_{OIS}(t) = z_{AAA}(t) + s_\infty(t) + \big(z_{€STR} - z_{AAA}(0) - s_\infty(0)\big)\,e^{-t/h}

    :math:`z_{AAA}`: ECB euro-area AAA Svensson curve (published parameters);
    :math:`s_\infty(t)`: OIS − AAA spread, interpolated in the ``ois_minus_aaa_bp`` table;
    the exponential term pins the short end on the €STR fixing (decay :math:`h`, 1Y by default).
    The proxy OIS quotes are the par rates of the template OIS on that curve.

EURIBOR 6M
    projection zero rates :math:`z_{6M}(t) = z_{OIS}(t) + b(t)` with the basis table
    ``euribor6m_basis_bp``; the 6M "fixing" and the swap quotes are the deposit / par
    swap rates implied by the pair (dual curve, €STR discounting).

Sovereign curves without bond data
    :func:`benchmark_curve_from_market` fits Nelson-Siegel on generic par yields
    (CNBC / Bundesbank / Stooq), treated as par bonds priced 100, plus the Bundesbank
    zero-coupon pillars for the Bund when available.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from rates_analytics.conventions.dateutils import add_months
from rates_analytics.conventions.period import Period
from rates_analytics.curves.estr import ESTR_FIXING_ID, ESTR_OIS_TENORS
from rates_analytics.curves.ibor import EURIBOR6M_SWAP_TENORS
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.nss import NelsonSiegelSvenssonCurve, NSSParams
from rates_analytics.curves.yield_curve import YieldCurve

logger = logging.getLogger(__name__)

ECB_AAA_NSS_IDS = {"beta0": "EUR.ECB.AAA.NSS.BETA0", "beta1": "EUR.ECB.AAA.NSS.BETA1",
                   "beta2": "EUR.ECB.AAA.NSS.BETA2", "beta3": "EUR.ECB.AAA.NSS.BETA3",
                   "tau1": "EUR.ECB.AAA.NSS.TAU1", "tau2": "EUR.ECB.AAA.NSS.TAU2"}
EURIBOR6M_FIXING_ID = "EUR.EURIBOR6M.FIX"
DEFAULT_OIS_MINUS_AAA_BP = {"1Y": 0.0, "2Y": -3.0, "5Y": -8.0, "10Y": -12.0, "20Y": -15.0, "30Y": -18.0, "50Y": -18.0}
DEFAULT_BASIS_BP = {"6M": 20.0, "1Y": 19.0, "2Y": 17.0, "5Y": 13.0, "10Y": 10.0, "20Y": 7.0, "30Y": 6.0, "50Y": 5.0}
BENCHMARK_TENORS = ("2Y", "5Y", "10Y", "30Y")
BBK_ZERO_TENORS = ("1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y")


def _years(tenor: str) -> float:
    p = Period.parse(tenor)
    months = p.total_months
    if months is None:
        raise CurveError(f"Tenor {tenor!r} must be in months or years")
    return months / 12.0


@dataclass(frozen=True)
class ProxyCurveConfig:
    """Assumptions of the proxy swap curves (``proxy_curves:`` block of ``config.yaml``)."""

    enabled: bool = True
    short_end_decay_years: float = 1.0
    ois_minus_aaa_bp: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_OIS_MINUS_AAA_BP))
    euribor6m_basis_bp: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_BASIS_BP))

    def __post_init__(self) -> None:
        if self.short_end_decay_years <= 0:
            raise CurveError("short_end_decay_years must be positive")
        for name in ("ois_minus_aaa_bp", "euribor6m_basis_bp"):
            if not getattr(self, name):
                raise CurveError(f"{name} needs at least one tenor")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ProxyCurveConfig:
        raw = dict(raw or {})
        return cls(enabled=bool(raw.get("enabled", True)),
                   short_end_decay_years=float(raw.get("short_end_decay_years", 1.0)),
                   ois_minus_aaa_bp={str(k): float(v) for k, v in (raw.get("ois_minus_aaa_bp")
                                                                  or DEFAULT_OIS_MINUS_AAA_BP).items()},
                   euribor6m_basis_bp={str(k): float(v) for k, v in (raw.get("euribor6m_basis_bp")
                                                                    or DEFAULT_BASIS_BP).items()})


def term_structure(table: Mapping[str, float], scale: float = 1e-4):
    """Linear interpolation of a ``{tenor: value}`` table in years, flat outside -> ``f(t)``."""
    pts = sorted((_years(k), float(v) * scale) for k, v in table.items())
    xs, ys = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    return lambda t: np.interp(np.asarray(t, dtype=float), xs, ys)


class FunctionCurve(YieldCurve):
    """Discount curve from a continuous zero-rate function ``z(t)`` (ACT/365F)."""

    def __init__(self, reference_date: date, zero, name: str = "") -> None:
        super().__init__(reference_date, name=name)
        self._z = zero

    def _discount_t(self, t):
        t = np.asarray(t, dtype=float)
        return np.exp(-self._z(t) * t)

    def _inst_forward_t(self, t):
        t = np.asarray(t, dtype=float)
        h = 1e-5
        lo = np.maximum(t - h, 0.0)
        return (self._z(t + h) * (t + h) - self._z(lo) * lo) / (t + h - lo)


@dataclass
class ProxyReport:
    """What was proxied and from which inputs (shown in the UI)."""

    used: bool = False
    estr_fixing: float | None = None
    aaa_params: dict[str, float] = field(default_factory=dict)
    proxied_ids: list[str] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        if not self.used:
            return "courbes de swap issues des cotations de marché"
        return (f"courbes PROXY ({len(self.proxied_ids)} cotations reconstruites) : ECB AAA Svensson + "
                f"€STR {self.estr_fixing:.3%} + spreads/basis de config.yaml – {self.reason}")


def aaa_params_from_quotes(quotes: Mapping[str, float]) -> NSSParams:
    """ECB AAA Svensson parameters (betas decimal, taus years) from quotes keyed by id."""
    missing = [k for k, mid in ECB_AAA_NSS_IDS.items() if mid not in quotes or not math.isfinite(quotes[mid])]
    if missing:
        raise CurveError(f"ECB AAA Svensson parameters missing: {missing}")
    q = {k: float(quotes[mid]) for k, mid in ECB_AAA_NSS_IDS.items()}
    return NSSParams(q["beta0"], q["beta1"], q["beta2"], q["tau1"], beta3=q["beta3"], tau2=q["tau2"])


def proxy_curves(as_of: date, estr_fixing: float, aaa: NSSParams,
                 config: ProxyCurveConfig | None = None) -> tuple[FunctionCurve, FunctionCurve]:
    """(€STR OIS proxy curve, EURIBOR 6M projection proxy curve) as functions of time."""
    cfg = config or ProxyCurveConfig()
    aaa_curve = NelsonSiegelSvenssonCurve(as_of, aaa, name="ECB AAA")
    s_inf = term_structure(cfg.ois_minus_aaa_bp)
    basis = term_structure(cfg.euribor6m_basis_bp)
    z_estr = math.log1p(estr_fixing / 360.0) * 365.0        # ACT/360 overnight -> continuous ACT/365F
    z0 = float(aaa.beta0 + aaa.beta1)                        # AAA instantaneous short rate
    gap = z_estr - z0 - float(s_inf(0.0))
    h = cfg.short_end_decay_years

    def z_ois(t):
        t = np.maximum(np.asarray(t, dtype=float), 1e-6)
        return aaa_curve.params.zero_rate(t) + s_inf(t) + gap * np.exp(-t / h)

    def z_6m(t):
        return z_ois(t) + basis(t)

    return FunctionCurve(as_of, z_ois, "€STR proxy"), FunctionCurve(as_of, z_6m, "EURIBOR 6M proxy")


def proxy_swap_quotes(as_of: date, estr_fixing: float, aaa: NSSParams,
                      config: ProxyCurveConfig | None = None) -> dict[str, float]:
    """Par quotes of the €STR and EURIBOR 6M curve templates on the proxy curves."""
    from rates_analytics.conventions.market import EURIBOR_6M
    from rates_analytics.curves.helpers import DepositHelper, deposit_convention_for
    from rates_analytics.instruments import OvernightIndexedSwap, VanillaSwap

    ois_c, e6_c = proxy_curves(as_of, estr_fixing, aaa, config)
    quotes = {ESTR_FIXING_ID: float(estr_fixing)}
    for t in ESTR_OIS_TENORS:
        quotes[f"EUR.ESTR.OIS.{t}"] = float(OvernightIndexedSwap.from_tenor(as_of, t).fair_rate(ois_c))
    dep = DepositHelper(as_of, "6M", 0.0, deposit_convention_for(EURIBOR_6M))
    quotes[EURIBOR6M_FIXING_ID] = float(dep.implied_quote(e6_c))
    for t in EURIBOR6M_SWAP_TENORS:
        quotes[f"EUR.EURIBOR6M.SWAP.{t}"] = float(VanillaSwap.from_tenor(as_of, t).fair_rate(ois_c, e6_c))
    return quotes


def complete_with_proxy(as_of: date, quotes: Mapping[str, float], inputs: Mapping[str, float],
                        config: ProxyCurveConfig | None = None, min_market_swaps: int = 3
                        ) -> tuple[dict[str, float], ProxyReport]:
    """Fill the swap quotes with proxies when the market gives fewer than ``min_market_swaps``.

    Args:
        quotes: market quotes available (id -> decimal).
        inputs: €STR fixing and ECB AAA Svensson parameters (id -> value).
    """
    cfg = config or ProxyCurveConfig()
    market = {k: float(v) for k, v in quotes.items() if v is not None and math.isfinite(float(v))}
    n_ois = sum(1 for k in market if k.startswith("EUR.ESTR.OIS."))
    n_irs = sum(1 for k in market if k.startswith("EUR.EURIBOR6M.SWAP."))
    report = ProxyReport()
    if n_ois >= min_market_swaps and n_irs >= min_market_swaps:
        return market, report
    if not cfg.enabled:
        raise CurveError(f"Only {n_ois} €STR OIS / {n_irs} EURIBOR swap quotes and proxy_curves disabled")
    estr = market.get(ESTR_FIXING_ID, inputs.get(ESTR_FIXING_ID))
    if estr is None or not math.isfinite(float(estr)):
        raise CurveError("Proxy curves need the €STR fixing (ECB) – unavailable")
    aaa = aaa_params_from_quotes(inputs)
    proxy = proxy_swap_quotes(as_of, float(estr), aaa, cfg)
    out = dict(market)
    families = [p for p, n in (("EUR.ESTR.OIS.", n_ois), ("EUR.EURIBOR6M.", n_irs)) if n < min_market_swaps]
    for k, v in proxy.items():
        if any(k.startswith(f) for f in families) and k != ESTR_FIXING_ID:
            out[k] = v
            report.proxied_ids.append(k)
    out.setdefault(ESTR_FIXING_ID, float(estr))
    # a market fixing / FRA mixed with proxy swaps would bend the short end: proxy family wins
    if "EUR.EURIBOR6M." in families:
        for k in [k for k in out if k.startswith("EUR.EURIBOR6M.FRA.")]:
            out.pop(k)
    report.used = True
    report.estr_fixing = float(estr)
    report.aaa_params = {k: float(inputs[mid]) for k, mid in ECB_AAA_NSS_IDS.items()}
    report.reason = f"{n_ois} OIS / {n_irs} IRS de marché disponibles"
    logger.warning("Swap quotes missing (%d OIS / %d IRS): proxy curves used", n_ois, n_irs)
    return out, report


# ======================================================== sovereign benchmarks
def benchmark_bonds(as_of: date, country: str, convention, par_yields: Mapping[str, float],
                    zero_yields: Mapping[str, float] | None = None):
    """Generic par bonds (coupon = yield, price 100) and zero-coupon bonds -> (bonds, clean prices).

    Zero yields are annually compounded (Bundesbank convention): price = 100 / (1+z)^T.
    """
    from rates_analytics.bonds import FixedRateBond
    from rates_analytics.conventions.period import Frequency

    bonds, prices = [], {}
    settle = convention.calendar.advance(as_of, convention.settlement_lag)
    for tenor, y in sorted(par_yields.items(), key=lambda kv: _years(kv[0])):
        if y is None or not math.isfinite(y):
            continue
        mat = add_months(settle, round(_years(tenor) * 12))
        b = FixedRateBond(f"GEN-{country}-{tenor}", float(y), mat, settle, convention,
                          name=f"{country} {tenor} generic")
        bonds.append(b)
        prices[b.isin] = 100.0
    for tenor, z in sorted((zero_yields or {}).items(), key=lambda kv: _years(kv[0])):
        if z is None or not math.isfinite(z):
            continue
        mat = add_months(settle, round(_years(tenor) * 12))
        b = FixedRateBond(f"ZC-{country}-{tenor}", 0.0, mat, settle, convention, frequency=Frequency.ANNUAL,
                          name=f"{country} {tenor} zero (Bundesbank)")
        bonds.append(b)
        prices[b.isin] = 100.0 / (1.0 + float(z)) ** _years(tenor)
    return bonds, prices


def benchmark_curve_from_market(service, as_of: date, name: str, country: str,
                                tenors: Sequence[str] = BENCHMARK_TENORS):
    """NS fit on generic yields ``EUR.GOVT.<country>.<tenor>`` (+ Bundesbank zeros for DE)."""
    from rates_analytics.conventions.market import get_convention
    from rates_analytics.curves.sovereign import fit_sovereign_curve

    conv_name = {"DE": "BUND", "FR": "OAT", "IT": "BTP", "ES": "BONO"}.get(country.upper())
    if conv_name is None:
        raise CurveError(f"No bond convention for country {country!r}")
    ids = [f"EUR.GOVT.{country}.{t}" for t in tenors]
    zero_ids = [f"EUR.BBK.DE.ZERO.{t}" for t in BBK_ZERO_TENORS] if country.upper() == "DE" else []
    q = service.quotes(ids + zero_ids, as_of)["value"].dropna().to_dict()
    par = {mid.rsplit(".", 1)[1]: v for mid, v in q.items() if mid in ids}
    zeros = {mid.rsplit(".", 1)[1]: v for mid, v in q.items() if mid in zero_ids}
    if len(par) + len(zeros) < 4:
        raise CurveError(f"{name}: {len(par) + len(zeros)} generic yields available (4 needed)")
    bonds, prices = benchmark_bonds(as_of, country.upper(), get_convention(conv_name), par, zeros)
    fit = fit_sovereign_curve(as_of, bonds, prices, model="ns", name=name, min_maturity_years=0.5,
                              max_outliers=0, min_dof=0)
    return fit, bonds, prices
