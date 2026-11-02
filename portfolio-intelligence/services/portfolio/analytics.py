"""Orchestration au niveau portefeuille : valorisation, expositions, performance.

Cette couche fait le lien entre le modèle de domaine et les données de marché.
Elle ne contient aucun appel Streamlit : elle est utilisable en batch, en test
ou depuis un futur service HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from models.datapoint import DataPoint
from models.enums import AssetClass, DataQuality
from models.portfolio import Portfolio
from services.risk.metrics import (
    PerformanceMetrics,
    coverage,
    correlation_matrix,
    performance_metrics,
    portfolio_price_series,
    to_series,
)


@dataclass
class PortfolioSnapshot:
    portfolio: Portfolio
    nav: float
    exposures: dict[str, dict[str, float]]
    concentration: dict[str, float]
    performance: PerformanceMetrics | None
    performance_coverage: float
    nav_series: pd.Series
    price_quality: dict[str, DataQuality] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def refreshed_share(self) -> float:
        """Part des lignes dont le prix a pu être rafraîchi auprès d'un provider."""
        if not self.price_quality:
            return 0.0
        good = sum(1 for q in self.price_quality.values() if q.is_decision_grade)
        return good / len(self.price_quality)


def refresh_prices(portfolio: Portfolio, market_data, cross_check: bool = False) -> dict[str, DataPoint[float]]:
    """Rafraîchit les prix des lignes cotées et met à jour les valorisations.

    Les lignes obligataires ne sont pas repricées ici : leur prix vient du
    fichier ou d'un moteur de pricing dédié (le prix action d'un ISIN
    obligataire n'a pas de sens).
    """
    quotable = [p.identifier for p in portfolio.positions
                if p.asset_class in (AssetClass.EQUITY, AssetClass.FUND, AssetClass.COMMODITY)]
    quotes = market_data.prices(quotable, cross_check=cross_check)

    for position in portfolio.positions:
        dp = quotes.get(position.identifier)
        if dp is None or not dp.is_usable or position.quantity in (None, 0):
            continue
        position.price = dp.value
        position.market_value = position.quantity * dp.value
        position.quality = dp.quality
    return quotes


def build_snapshot(portfolio: Portfolio, market_data, lookback_days: int = 500,
                   risk_free_rate: float | None = None,
                   with_history: bool = True) -> PortfolioSnapshot:
    warnings: list[str] = []
    series_by_id: dict[str, pd.Series] = {}
    quality: dict[str, DataQuality] = {}

    if with_history:
        for position in portfolio.positions:
            if position.asset_class not in (AssetClass.EQUITY, AssetClass.FUND, AssetClass.COMMODITY):
                continue
            dp = market_data.price_series(position.identifier, lookback_days)
            quality[position.identifier] = dp.quality
            if dp.is_usable and dp.value:
                series_by_id[position.identifier] = to_series(dp.value)

    weights = portfolio.weights()
    nav_series = portfolio_price_series(series_by_id, weights) if series_by_id else pd.Series(dtype=float)
    perf_coverage = coverage(series_by_id, weights) if weights else 0.0
    perf = performance_metrics(nav_series, risk_free_rate) if not nav_series.empty else None

    if perf_coverage < 0.9 and with_history:
        warnings.append(
            f"Performance calculée sur {perf_coverage:.0%} de la NAV "
            "(historiques indisponibles pour le reste du portefeuille)."
        )
    if portfolio.coverage("maturity") == 0 and portfolio.positions_of(AssetClass.GOV_BOND, AssetClass.CREDIT):
        warnings.append("Lignes obligataires sans maturité : duration et scénarios de taux indisponibles.")

    return PortfolioSnapshot(
        portfolio=portfolio,
        nav=portfolio.nav,
        exposures={
            "classe d'actif": portfolio.exposure_by_asset_class(),
            "secteur": portfolio.exposure_by_sector(),
            "pays": portfolio.exposure_by_country(),
            "devise": portfolio.exposure_by_currency(),
        },
        concentration=portfolio.concentration(),
        performance=perf,
        performance_coverage=perf_coverage,
        nav_series=nav_series,
        price_quality=quality,
        warnings=warnings + portfolio.warnings,
    )


def correlations(portfolio: Portfolio, market_data, lookback_days: int = 500, top_n: int = 20) -> pd.DataFrame:
    top = [p for p, _ in portfolio.top_holdings(top_n)
           if p.asset_class in (AssetClass.EQUITY, AssetClass.FUND)]
    series = {}
    for position in top:
        dp = market_data.price_series(position.identifier, lookback_days)
        if dp.is_usable and dp.value:
            series[position.identifier] = to_series(dp.value)
    return correlation_matrix(series)
