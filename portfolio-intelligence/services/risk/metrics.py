"""Mesures de risque et de performance.

Toutes les fonctions travaillent sur des séries de prix réelles fournies par
le service de données. Aucune série n'est simulée : si l'historique manque,
la métrique n'est pas calculée et l'UI affiche « non disponible ».
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config.constants import TRADING_DAYS_PER_YEAR


def to_series(rows: list[dict]) -> pd.Series:
    """[{date, close}] -> série pandas indexée par date, triée."""
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series(
        {pd.Timestamp(r["date"]): float(r["close"]) for r in rows if r.get("close")}
    ).sort_index()
    return s[~s.index.duplicated(keep="last")]


def returns(prices: pd.Series) -> pd.Series:
    if prices.empty:
        return pd.Series(dtype=float)
    return prices.pct_change().dropna()


@dataclass(frozen=True)
class PerformanceMetrics:
    total_return: float | None
    ytd: float | None
    m1: float | None
    m3: float | None
    y1: float | None
    volatility: float | None
    max_drawdown: float | None
    sharpe: float | None
    var_95_1d: float | None
    observations: int

    @property
    def is_empty(self) -> bool:
        return self.observations == 0


def _window_return(prices: pd.Series, days: int) -> float | None:
    if prices.empty:
        return None
    end = prices.index[-1]
    start_target = end - pd.Timedelta(days=days)
    window = prices[prices.index <= start_target]
    if window.empty:
        return None
    return float(prices.iloc[-1] / window.iloc[-1] - 1)


def max_drawdown(prices: pd.Series) -> float | None:
    if prices.empty:
        return None
    running_max = prices.cummax()
    dd = prices / running_max - 1.0
    return float(dd.min())


def performance_metrics(prices: pd.Series, risk_free_rate: float | None = None) -> PerformanceMetrics:
    """`risk_free_rate` annualisé, en décimal. Sans taux sans risque fourni,
    le Sharpe n'est pas calculé plutôt que d'être calculé avec un taux nul."""
    if prices.empty or len(prices) < 3:
        return PerformanceMetrics(None, None, None, None, None, None, None, None, None, len(prices))

    r = returns(prices)
    vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(r) > 2 else None

    ytd = None
    year_start = prices[prices.index.year == prices.index[-1].year]
    if not year_start.empty and len(year_start) > 1:
        ytd = float(prices.iloc[-1] / year_start.iloc[0] - 1)

    y1 = _window_return(prices, 365)
    sharpe = None
    if vol and y1 is not None and risk_free_rate is not None and vol > 0:
        sharpe = (y1 - risk_free_rate) / vol

    var95 = float(np.percentile(r, 5)) if len(r) >= 60 else None

    return PerformanceMetrics(
        total_return=float(prices.iloc[-1] / prices.iloc[0] - 1),
        ytd=ytd,
        m1=_window_return(prices, 30),
        m3=_window_return(prices, 91),
        y1=y1,
        volatility=vol,
        max_drawdown=max_drawdown(prices),
        sharpe=sharpe,
        var_95_1d=var95,
        observations=len(prices),
    )


def portfolio_price_series(series_by_id: dict[str, pd.Series], weights: dict[str, float]) -> pd.Series:
    """Reconstruit une série de NAV à poids constants (rebalancement quotidien).

    Les positions sans historique sont retirées et les poids renormalisés sur
    la partie couverte ; le taux de couverture est renvoyé par `coverage()`.
    """
    usable = {k: s for k, s in series_by_id.items() if not s.empty and k in weights}
    if not usable:
        return pd.Series(dtype=float)
    frame = pd.DataFrame(usable).dropna(how="all").ffill().dropna()
    if frame.empty:
        return pd.Series(dtype=float)
    w = pd.Series({k: weights[k] for k in frame.columns}, dtype=float)
    if w.sum() == 0:
        return pd.Series(dtype=float)
    w = w / w.sum()
    rets = frame.pct_change().dropna()
    port_ret = rets.mul(w, axis=1).sum(axis=1)
    return (1 + port_ret).cumprod() * 100.0


def coverage(series_by_id: dict[str, pd.Series], weights: dict[str, float]) -> float:
    covered = sum(w for k, w in weights.items() if k in series_by_id and not series_by_id[k].empty)
    total = sum(weights.values())
    return covered / total if total else 0.0


def correlation_matrix(series_by_id: dict[str, pd.Series], min_obs: int = 60) -> pd.DataFrame:
    usable = {k: s for k, s in series_by_id.items() if len(s) >= min_obs}
    if len(usable) < 2:
        return pd.DataFrame()
    frame = pd.DataFrame(usable).ffill().dropna()
    return frame.pct_change().dropna().corr()


def beta_and_r2(asset: pd.Series, market: pd.Series, min_obs: int = 60) -> tuple[float | None, float | None]:
    """Bêta par régression MCO des rendements de l'actif sur ceux du marché."""
    frame = pd.DataFrame({"a": asset, "m": market}).ffill().dropna()
    if len(frame) < min_obs:
        return None, None
    r = frame.pct_change().dropna()
    if len(r) < min_obs or r["m"].var() == 0:
        return None, None
    cov = float(np.cov(r["a"], r["m"])[0, 1])
    var_m = float(np.var(r["m"], ddof=1))
    beta = cov / var_m
    corr = float(np.corrcoef(r["a"], r["m"])[0, 1])
    return beta, corr ** 2


def tracking_error(portfolio: pd.Series, benchmark: pd.Series) -> float | None:
    frame = pd.DataFrame({"p": portfolio, "b": benchmark}).ffill().dropna()
    if len(frame) < 60:
        return None
    diff = frame["p"].pct_change().dropna() - frame["b"].pct_change().dropna()
    return float(diff.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
