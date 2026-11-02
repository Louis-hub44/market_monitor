"""Estimation des bêtas et des expositions factorielles.

Les bêtas sont **estimés sur données réelles** (régression des rendements sur
un proxy d'indice) chaque fois qu'un historique suffisant est disponible ;
sinon la position est signalée comme reposant sur l'hypothèse par défaut.

Les scores factoriels (Value, Growth, Quality, Size, Momentum, Volatility)
sont construits par z-score transversal sur les ratios réellement collectés :
aucune valeur n'est imputée, une société sans donnée n'entre pas dans le score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config.constants import MARKET_PROXIES
from models.enums import AssetClass, DataQuality
from models.portfolio import Portfolio
from services.risk.metrics import beta_and_r2, to_series

FACTOR_DEFINITIONS: dict[str, list[tuple[str, int]]] = {
    # facteur -> [(métrique, sens)] ; sens = +1 si « plus haut = plus exposé »
    "Value": [("earningsYield", 1), ("freeCashFlowYield", 1), ("priceToBookRatio", -1)],
    "Growth": [("revenueGrowth", 1), ("epsGrowth", 1)],
    "Quality": [("returnOnEquity", 1), ("returnOnInvestedCapital", 1), ("netProfitMargin", 1)],
    "Size": [("marketCap", -1)],
    "Momentum": [("momentum12m", 1)],
    "Volatility": [("volatility1y", 1)],
}


@dataclass
class BetaEstimate:
    identifier: str
    beta: float
    r2: float | None
    proxy: str
    quality: DataQuality
    note: str = ""


@dataclass
class FactorProfile:
    scores: dict[str, float] = field(default_factory=dict)     # facteur -> exposition pondérée
    coverage: dict[str, float] = field(default_factory=dict)   # facteur -> part de la poche couverte
    detail: pd.DataFrame | None = None


def estimate_betas(portfolio: Portfolio, market_data, proxy_key: str = "GLOBAL",
                   lookback_days: int = 750, min_obs: int = 120) -> dict[str, BetaEstimate]:
    """Régresse chaque ligne actions sur le proxy de marché choisi."""
    proxy_symbol = MARKET_PROXIES.get(proxy_key, MARKET_PROXIES["GLOBAL"])
    market_dp = market_data.price_series(proxy_symbol, lookback_days)
    if not market_dp.is_usable or not market_dp.value:
        return {}

    market = to_series(market_dp.value)
    out: dict[str, BetaEstimate] = {}
    for position in portfolio.positions_of(AssetClass.EQUITY):
        dp = market_data.price_series(position.identifier, lookback_days)
        if not dp.is_usable or not dp.value:
            continue
        beta, r2 = beta_and_r2(to_series(dp.value), market, min_obs=min_obs)
        if beta is None:
            continue
        out[position.identifier] = BetaEstimate(
            identifier=position.identifier,
            beta=float(beta),
            r2=r2,
            proxy=proxy_symbol,
            quality=DataQuality.API_CONFIRMED if dp.quality.rank <= 1 else dp.quality,
            note=f"MCO sur {lookback_days} jours vs {proxy_symbol} (R²={r2:.2f})" if r2 is not None else "",
        )
    return out


def _zscore(series: pd.Series) -> pd.Series:
    valid = series.dropna()
    if len(valid) < 3 or valid.std(ddof=1) == 0:
        return pd.Series(index=series.index, dtype=float)
    z = (series - valid.mean()) / valid.std(ddof=1)
    return z.clip(-3, 3)


def factor_profile(metrics: pd.DataFrame, weights: dict[str, float]) -> FactorProfile:
    """`metrics` : index = identifiant, colonnes = ratios collectés."""
    if metrics.empty:
        return FactorProfile()

    scores: dict[str, float] = {}
    coverage: dict[str, float] = {}
    detail = pd.DataFrame(index=metrics.index)
    total_weight = sum(weights.get(i, 0.0) for i in metrics.index) or 1.0

    for factor, components in FACTOR_DEFINITIONS.items():
        available = [(c, sign) for c, sign in components if c in metrics.columns]
        if not available:
            continue
        parts = [ _zscore(pd.to_numeric(metrics[c], errors="coerce")) * sign for c, sign in available ]
        combined = pd.concat(parts, axis=1).mean(axis=1, skipna=True)
        detail[factor] = combined

        w = pd.Series({i: weights.get(i, 0.0) for i in metrics.index}, dtype=float)
        mask = combined.notna() & (w > 0)
        if mask.sum() == 0:
            continue
        scores[factor] = float((combined[mask] * w[mask]).sum() / w[mask].sum())
        coverage[factor] = float(w[mask].sum() / total_weight)

    return FactorProfile(scores=scores, coverage=coverage, detail=detail)


def portfolio_beta(portfolio: Portfolio, betas: dict[str, BetaEstimate],
                   default_beta: float = 1.0) -> tuple[float, float]:
    """(bêta pondéré de la poche actions, part de la poche réellement estimée)."""
    equities = portfolio.positions_of(AssetClass.EQUITY)
    sleeve = sum(p.market_value_base for p in equities)
    if not sleeve:
        return 0.0, 0.0
    estimated_value = sum(p.market_value_base for p in equities if p.identifier in betas)
    beta = sum(
        p.market_value_base / sleeve * (betas[p.identifier].beta if p.identifier in betas
                                        else (p.beta if p.beta is not None else default_beta))
        for p in equities
    )
    return beta, estimated_value / sleeve


def realised_volatility(series: pd.Series, window: int = 252) -> float | None:
    if len(series) < 60:
        return None
    r = series.pct_change().dropna().tail(window)
    return float(r.std(ddof=1) * np.sqrt(252))
