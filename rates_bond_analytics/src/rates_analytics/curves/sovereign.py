r"""Sovereign curves (Bund, OAT, BTP...) fitted with Nelson-Siegel(-Svensson).

Fitting problem (same objective as the Bundesbank / ECB and QuantLib's
``FittedBondDiscountCurve``): minimise duration-weighted clean-price errors,
a first-order proxy of yield errors,

.. math::
    \min_\theta \sum_i \left(\frac{\hat P_i(\theta) - P_i}{D^{mod}_i}\right)^2,
    \qquad
    \hat P_i(\theta) = \frac{\sum_k CF_{ik}\,e^{-z_\theta(t_{ik})\,t_{ik}}}{e^{-z_\theta(t_{s})\,t_s}} - AI_i

where :math:`D^{mod}_i` is the modified duration at the market yield.

Practicalities:

* the problem is non-convex in :math:`(\tau_1, \tau_2)`: bounded Levenberg-Marquardt
  (``scipy.optimize.least_squares``, TRF) is run from a grid of starting decays and the
  best solution kept;
* bonds with residual maturity below ``min_maturity_years`` (default 3 months) are
  excluded – money-market effects, as in the ECB methodology;
* outliers (specials, illiquid lines) are removed one at a time while the largest
  yield error exceeds ``max(outlier_floor_bp, outlier_k x RMSE)``;
* every bond, used or not, gets a model price and a **rich/cheap** in bp
  (market yield - model yield: > 0 means the bond is cheap versus the curve).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from rates_analytics.bonds.bond import FixedRateBond
from rates_analytics.bonds.static import bonds_from_static
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.nss import NelsonSiegelSvenssonCurve, NSSParams, ParametricModel
from rates_analytics.curves.yield_curve import YieldCurve

logger = logging.getLogger(__name__)

# Optimisation vector: NS  [b0, b1, b2, tau1]
#                       NSS [b0, b1, b2, b3, tau1, delta] with tau2 = tau1 + delta.
# The two hump terms of NSS have the same functional form, so ordering tau1 < tau2 is
# without loss of generality; the minimum gap delta >= 1Y removes the classic
# collinearity (tau1 ~ tau2 with huge offsetting b2 / b3).
_LOWER_NSS = np.array([-0.10, -0.30, -0.20, -0.20, 0.05, 1.0])
_UPPER_NSS = np.array([0.25, 0.30, 0.20, 0.20, 30.0, 50.0])
_TAU1_GRID = (0.5, 1.0, 2.0, 4.0)
_TAU2_GRID = (3.0, 6.0, 10.0, 20.0)
_NS_TAU_GRID = (0.5, 1.0, 2.0, 4.0, 8.0)
MIN_TAU_GAP = 1.0


def _to_params(model: ParametricModel, theta: np.ndarray) -> NSSParams:
    if model is ParametricModel.NS:
        return NSSParams.from_array(model, theta)
    b0, b1, b2, b3, t1, gap = theta
    return NSSParams(b0, b1, b2, t1, beta3=b3, tau2=t1 + gap)


def _to_theta(params: NSSParams) -> np.ndarray:
    x = params.to_array()
    if params.model is ParametricModel.NSS:
        x[5] = x[5] - x[4]
    return x


class _BondSet:
    """Vectorised pricing of a bond universe on a parametric curve."""

    def __init__(self, as_of: date, bonds: Sequence[FixedRateBond], clean: np.ndarray) -> None:
        self.bonds = list(bonds)
        self.clean = np.asarray(clean, dtype=float)
        t, amt, idx, ts, ai = [], [], [], [], []
        for i, b in enumerate(self.bonds):
            s = b.settlement_date(as_of)
            ts.append((s - as_of).days / 365.0)
            ai.append(b.accrued_interest(s))
            for cf in b.cashflows(s):
                t.append((cf.payment_date - as_of).days / 365.0)
                amt.append(cf.amount)
                idx.append(i)
        self.t, self.amt, self.idx = np.array(t), np.array(amt), np.array(idx)
        self.ts, self.ai = np.array(ts), np.array(ai)

    def model_clean(self, params: NSSParams) -> np.ndarray:
        df = np.exp(-params.zero_rate(self.t) * self.t)
        df_s = np.exp(-params.zero_rate(self.ts) * self.ts)
        pv = np.bincount(self.idx, weights=self.amt * df, minlength=len(self.bonds))
        return pv / df_s - self.ai

    def jacobian(self, params: NSSParams) -> np.ndarray:
        r"""d clean / d params (natural order):
        :math:`\frac{\sum_k CF_k P_k (-t_k \nabla z_k)}{P_s} + \frac{PV}{P_s} t_s \nabla z_s`."""
        n = len(self.bonds)
        df = np.exp(-params.zero_rate(self.t) * self.t)
        df_s = np.exp(-params.zero_rate(self.ts) * self.ts)
        pv = np.bincount(self.idx, weights=self.amt * df, minlength=n)
        g = params.zero_rate_gradient(self.t) * (-(self.amt * df * self.t))[:, None]
        jac = np.column_stack([np.bincount(self.idx, weights=g[:, j], minlength=n) for j in range(g.shape[1])])
        jac = jac / df_s[:, None]
        return jac + (pv / df_s * self.ts)[:, None] * params.zero_rate_gradient(self.ts)


@dataclass
class SovereignCurveFit:
    """Result of a sovereign curve fit.

    Attributes:
        curve: fitted :class:`NelsonSiegelSvenssonCurve` (a :class:`YieldCurve`).
        bonds: one row per bond – market / model prices and yields, yield error,
            rich/cheap, whether the bond was used and why not.
        rmse_bp: root mean square yield error on the bonds used.
    """

    name: str
    as_of: date
    curve: NelsonSiegelSvenssonCurve
    bonds: pd.DataFrame
    rmse_bp: float
    cost: float
    starts: int
    excluded: dict[str, str] = field(default_factory=dict)

    @property
    def params(self) -> NSSParams:
        return self.curve.params

    @property
    def n_used(self) -> int:
        return int(self.bonds["used"].sum())

    def zero_rates(self, tenors: Sequence[float] = (1, 2, 5, 10, 20, 30)) -> pd.Series:
        """Continuously compounded zero rates at times in years."""
        return pd.Series(self.curve.zero_rate_cc(np.asarray(tenors, dtype=float)), index=list(tenors))

    def par_yields(self, tenors: Sequence[float] = (2, 5, 10, 30), frequency: int | None = None) -> pd.Series:
        r"""Par yields :math:`c = F\,(1 - P(T)) / \sum_k P(T - k/F)` of fictitious bonds starting today."""
        f = frequency or int(self.bonds["frequency"].mode().iloc[0])
        out = {}
        for T in tenors:
            times = np.arange(T, 0, -1.0 / f)[::-1]
            df = self.curve.discount(times)
            out[T] = f * (1.0 - df[-1]) / df.sum()
        return pd.Series(out)

    def rich_cheap(self) -> pd.DataFrame:
        """Bonds sorted from cheapest to richest versus the fitted curve."""
        cols = ["name", "maturity", "coupon", "ytm_mkt", "ytm_model", "rich_cheap_bp", "used", "exclusion"]
        return self.bonds.sort_values("rich_cheap_bp", ascending=False)[cols]

    def summary(self) -> dict:
        return {"curve": self.name, "as_of": self.as_of, "model": self.params.model.value, "n_bonds": len(self.bonds),
                "n_used": self.n_used, "rmse_bp": self.rmse_bp, **self.params.as_dict()}


def _starts(model: ParametricModel, y_short: float, y_long: float) -> list[np.ndarray]:
    b0, b1 = y_long, y_short - y_long
    if model is ParametricModel.NS:
        return [np.array([b0, b1, 0.0, t]) for t in _NS_TAU_GRID]
    return [np.array([b0, b1, 0.0, 0.0, t1, t2]) for t1 in _TAU1_GRID for t2 in _TAU2_GRID
            if t2 >= t1 + MIN_TAU_GAP]


def _bounds(model: ParametricModel) -> tuple[np.ndarray, np.ndarray]:
    if model is ParametricModel.NS:
        return _LOWER_NSS[[0, 1, 2, 4]], _UPPER_NSS[[0, 1, 2, 4]]
    return _LOWER_NSS, _UPPER_NSS


def _fit_params(bs: _BondSet, weights: np.ndarray, model: ParametricModel, y_short: float, y_long: float
                ) -> tuple[NSSParams, float, int]:
    lo, hi = _bounds(model)
    n_tau = 1 if model is ParametricModel.NS else 2
    scale = np.r_[np.full(len(lo) - n_tau, 0.01), np.ones(n_tau)]
    w = weights * 1e4

    def residuals(theta: np.ndarray) -> np.ndarray:
        return (bs.model_clean(_to_params(model, theta)) - bs.clean) * w

    def jac(theta: np.ndarray) -> np.ndarray:
        j = bs.jacobian(_to_params(model, theta))
        if model is ParametricModel.NSS:          # chain rule: tau2 = tau1 + delta
            j = j.copy()
            j[:, 4] = j[:, 4] + j[:, 5]
        return j * w[:, None]

    best, best_cost, n = None, np.inf, 0
    for x0 in _starts(model, y_short, y_long):
        theta0 = np.clip(_to_theta(NSSParams.from_array(model, x0)), lo + 1e-9, hi - 1e-9)
        try:
            res = least_squares(residuals, theta0, jac=jac, bounds=(lo, hi), method="trf", xtol=1e-12,
                                ftol=1e-12, gtol=1e-12, max_nfev=2000, x_scale=scale)
        except (ValueError, FloatingPointError) as exc:  # pragma: no cover - defensive
            logger.debug("start %s failed: %s", x0, exc)
            continue
        n += 1
        if res.cost < best_cost:
            best, best_cost = res.x, float(res.cost)
    if best is None:
        raise CurveError("Parametric fit failed from every starting point")
    return _to_params(model, best), best_cost, n


def _modified_duration(b: FixedRateBond, ytm: float, settle: date) -> float:
    h = 1e-5
    p0 = b.dirty_price_from_yield(ytm, settle)
    return (b.dirty_price_from_yield(ytm - h, settle) - b.dirty_price_from_yield(ytm + h, settle)) / (2 * h * p0)


def fit_sovereign_curve(
    as_of: date,
    bonds: Sequence[FixedRateBond],
    clean_prices: Mapping[str, float],
    model: ParametricModel | str = "auto",
    name: str = "",
    min_maturity_years: float = 0.25,
    outlier_k: float = 3.0,
    outlier_floor_bp: float = 5.0,
    max_outliers: int = 3,
    min_dof: int = 1,
) -> SovereignCurveFit:
    """Fit a NS/NSS curve to a bond universe.

    Args:
        as_of: trade date (curve reference date); bonds settle T+2.
        bonds: candidate bonds.
        clean_prices: market clean prices (mid) keyed by ISIN.
        model: ``"nss"``, ``"ns"`` or ``"auto"`` (NSS from 12 usable bonds, NS below).
        min_maturity_years: residual maturity filter.
        outlier_k / outlier_floor_bp / max_outliers: iterative outlier removal
            (set ``max_outliers=0`` to disable).
        min_dof: usable bonds required beyond the number of parameters (0 allows an exact
            fit, e.g. NS on four generic benchmark yields).

    Raises:
        CurveError: fewer usable bonds than parameters.
    """
    rows, excluded, usable = [], {}, []
    for b in bonds:
        price = clean_prices.get(b.isin)
        reason = None
        if price is None or not np.isfinite(price) or price <= 0:
            reason = "no price"
        elif b.maturity <= as_of or b.settlement_date(as_of) >= b.maturity:
            reason = "matured"
        elif b.residual_years(as_of) < min_maturity_years:
            reason = f"residual maturity < {min_maturity_years:g}Y"
        if reason == "no price" or reason == "matured":
            excluded[b.isin] = reason
            continue
        assert price is not None
        settle = b.settlement_date(as_of)
        ytm = b.yield_from_clean_price(float(price), settle)
        rows.append({"isin": b.isin, "name": str(b), "coupon": b.coupon, "maturity": b.maturity,
                     "residual_years": b.residual_years(as_of), "frequency": int(b.frequency),
                     "clean_mkt": float(price), "ytm_mkt": ytm,
                     "mod_duration": _modified_duration(b, ytm, settle), "used": reason is None,
                     "exclusion": reason or ""})
        usable.append(b)
    if not rows:
        raise CurveError(f"{name or 'sovereign curve'}: no priced bond")
    table = pd.DataFrame(rows).set_index("isin")
    for isin, r in table[~table["used"]].iterrows():
        excluded[isin] = r["exclusion"]

    m = ParametricModel.NSS if str(model).lower() == "auto" and table["used"].sum() >= 12 else (
        ParametricModel.NS if str(model).lower() == "auto" else ParametricModel.parse(model))
    n_par = 6 if m is ParametricModel.NSS else 4
    full = _BondSet(as_of, usable, table["clean_mkt"].to_numpy())
    by_isin = {b.isin: b for b in usable}

    while True:
        used = table["used"].to_numpy()
        if used.sum() < n_par + min_dof:
            raise CurveError(f"{name or 'sovereign curve'}: {int(used.sum())} usable bonds for a {m.value.upper()} fit")
        sub = _BondSet(as_of, [b for b, u in zip(usable, used) if u], table.loc[used, "clean_mkt"].to_numpy())
        w = 1.0 / table.loc[used, "mod_duration"].to_numpy()
        ordered = table[used].sort_values("residual_years")["ytm_mkt"]
        params, cost, starts = _fit_params(sub, w, m, float(ordered.iloc[0]), float(ordered.iloc[-1]))
        table["clean_model"] = full.model_clean(params)
        table["ytm_model"] = [by_isin[i].yield_from_clean_price(p, by_isin[i].settlement_date(as_of))
                              for i, p in table["clean_model"].items()]
        table["rich_cheap_bp"] = (table["ytm_mkt"] - table["ytm_model"]) * 1e4
        err = table.loc[table["used"], "rich_cheap_bp"]
        rmse = float(np.sqrt(np.mean(err ** 2)))
        n_out = sum(1 for v in excluded.values() if v.startswith("outlier"))
        worst = err.abs().idxmax()
        threshold = max(outlier_floor_bp, outlier_k * rmse)
        if n_out >= max_outliers or abs(err[worst]) <= threshold:
            break
        reason = f"outlier ({err[worst]:+.1f} bp > {threshold:.1f} bp)"
        table.loc[worst, ["used", "exclusion"]] = [False, reason]
        excluded[worst] = reason
        logger.info("%s: excluding %s – %s", name, worst, reason)

    table["price_error"] = table["clean_model"] - table["clean_mkt"]
    curve = NelsonSiegelSvenssonCurve(as_of, params, name=name)
    return SovereignCurveFit(name, as_of, curve, table, rmse, cost, starts, excluded)


def sovereign_curve_from_market(service, as_of: date, isins: Sequence[str], name: str = "", **kwargs
                                ) -> SovereignCurveFit:
    """Static + prices from a :class:`~rates_analytics.data.MarketDataService`, then fit."""
    static = service.bond_static(isins).dropna(how="all")
    bonds, rejected = bonds_from_static(static)
    prices = service.bond_prices([b.isin for b in bonds], as_of)
    mids = prices["mid"].dropna().to_dict()
    fit = fit_sovereign_curve(as_of, bonds, mids, name=name, **kwargs)
    fit.excluded.update({k: f"static: {v}" for k, v in rejected.items()})
    return fit


def resolve_universe(settings, curve_name: str, service=None) -> tuple[list[str], dict]:
    """ISIN universe and fit options of a configured sovereign curve.

    ``settings.sovereign_curves[curve_name]`` may give ``isins`` (list), ``file``
    (CSV with an ``isin`` column) or nothing, in which case every bond of the
    issuer held by a :class:`~rates_analytics.data.StaticProvider` of ``service``
    is used. Returns ``(isins, fit_kwargs)``.
    """
    from rates_analytics.config import PROJECT_ROOT
    from rates_analytics.data.providers.static import StaticProvider

    cfg = dict(settings.sovereign_curves.get(curve_name) or {})
    if not cfg:
        raise CurveError(f"Sovereign curve {curve_name!r} not configured (sovereign_curves in config.yaml)")
    issuer = str(cfg.get("issuer", "")).upper()
    isins = list(cfg.get("isins") or [])
    if not isins and cfg.get("file"):
        path = PROJECT_ROOT / cfg["file"]
        isins = pd.read_csv(path)["isin"].dropna().astype(str).tolist()
    if not isins and service is not None:
        for prov in service.providers:
            if isinstance(prov, StaticProvider):
                static = prov.get_bond_static(list(prov._static.index))
                isins = static.index[static["issuer"].astype(str).str.upper() == issuer].tolist()
                if isins:
                    break
    if not isins:
        raise CurveError(f"No bond universe for {curve_name!r}: set 'isins' or 'file' in config.yaml")
    kwargs = {k: cfg[k] for k in ("model", "min_maturity_years", "outlier_k", "outlier_floor_bp", "max_outliers")
              if k in cfg}
    return isins, kwargs


def zero_spreads(fits: Mapping[str, SovereignCurveFit | YieldCurve], base: str,
                 tenors: Sequence[float] = (2, 5, 10, 30)) -> pd.DataFrame:
    """Zero-rate spreads (bp, continuous ACT/365F) of each curve versus ``base`` (e.g. OAT-Bund)."""
    def zero(c, t):
        curve = c.curve if isinstance(c, SovereignCurveFit) else c
        return np.asarray(curve.zero_rate(np.asarray(t, dtype=float)))

    if base not in fits:
        raise CurveError(f"Base curve {base!r} not in {sorted(fits)}")
    zb = zero(fits[base], tenors)
    return pd.DataFrame({k: (zero(v, tenors) - zb) * 1e4 for k, v in fits.items() if k != base},
                        index=pd.Index(list(tenors), name="tenor_years"))

