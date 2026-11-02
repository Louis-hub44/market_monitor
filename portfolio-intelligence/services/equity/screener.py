"""Screener actions (§2.1).

Le filtrage est délégué au provider quand il le permet (moins d'appels, moins
de données transférées), puis affiné localement sur les ratios enrichis.
Aucun univers de titres n'est stocké en dur : l'univers est celui du provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from data.providers.base import ProviderUnavailable
from models.datapoint import DataPoint, missing
from models.enums import DataQuality

# Colonnes exposées à l'utilisateur -> clés brutes possibles du provider
SCREENER_FIELDS: dict[str, tuple[str, ...]] = {
    "symbole": ("symbol",),
    "société": ("companyName", "name"),
    "pays": ("country",),
    "secteur": ("sector",),
    "industrie": ("industry",),
    "bourse": ("exchangeShortName", "exchange"),
    "devise": ("currency",),
    "capitalisation": ("marketCap", "marketCapitalization"),
    "prix": ("price",),
    "beta": ("beta",),
    "dividende": ("lastAnnualDividend", "dividendYield"),
    "volume": ("volume",),
}

RATIO_FIELDS: dict[str, tuple[str, ...]] = {
    "PER": ("priceToEarningsRatio", "peRatio"),
    "PER estimé": ("forwardPE", "priceToEarningsRatioForward"),
    "EV/EBITDA": ("evToEBITDA", "enterpriseValueOverEBITDA"),
    "EV/Sales": ("evToSales",),
    "P/B": ("priceToBookRatio", "pbRatio"),
    "P/S": ("priceToSalesRatio",),
    "FCF Yield": ("freeCashFlowYield",),
    "Div. Yield": ("dividendYield",),
    "PEG": ("priceEarningsToGrowthRatio", "pegRatio"),
    "ROE": ("returnOnEquity",),
    "ROIC": ("returnOnInvestedCapital",),
    "ROA": ("returnOnAssets",),
    "Marge brute": ("grossProfitMargin",),
    "Marge EBITDA": ("ebitdaMargin", "ebitdaratio"),
    "Marge opérationnelle": ("operatingProfitMargin",),
    "Marge nette": ("netProfitMargin",),
    "Croissance CA": ("revenueGrowth", "growthRevenue"),
    "Croissance EPS": ("epsgrowth", "growthEPS", "epsGrowth"),
}


@dataclass
class ScreenerFilters:
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    exchange: str | None = None
    market_cap_min: float | None = None
    market_cap_max: float | None = None
    limit: int = 200
    ratio_bounds: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)

    def to_provider_params(self) -> dict:
        return {
            "sector": self.sector,
            "industry": self.industry,
            "country": self.country,
            "exchange": self.exchange,
            "marketCapMoreThan": self.market_cap_min,
            "marketCapLowerThan": self.market_cap_max,
            "limit": self.limit,
            "isActivelyTrading": "true",
        }


def _pick(row: dict, keys: tuple[str, ...]):
    for k in keys:
        if row.get(k) is not None:
            return row[k]
    return None


def run_screener(filters: ScreenerFilters, market_data,
                 enrich_ratios: bool = False, enrich_limit: int = 25) -> DataPoint[pd.DataFrame]:
    try:
        rows, stale = market_data.fmp.screener(**filters.to_provider_params())
    except ProviderUnavailable as exc:
        return missing(note=f"Screener indisponible : {exc}")
    if not rows:
        return missing(source="FMP", note="Aucune société ne correspond aux critères")

    base = pd.DataFrame(
        [{label: _pick(r, keys) for label, keys in SCREENER_FIELDS.items()} for r in rows]
    )
    quality = DataQuality.CACHED if stale else DataQuality.API_CONFIRMED

    if enrich_ratios and not base.empty:
        # L'enrichissement coûte un appel par société : on le borne explicitement.
        enriched: list[dict] = []
        for symbol in base["symbole"].head(enrich_limit):
            try:
                ratios, _ = market_data.fmp.ratios(symbol, period="annual", limit=1)
                metrics, _ = market_data.fmp.key_metrics(symbol, period="annual", limit=1)
            except ProviderUnavailable:
                continue
            payload = {}
            for src in (ratios or []) + (metrics or []):
                payload.update(src)
            enriched.append(
                {"symbole": symbol, **{label: _pick(payload, keys) for label, keys in RATIO_FIELDS.items()}}
            )
        if enriched:
            base = base.merge(pd.DataFrame(enriched), on="symbole", how="left")

    for column, (low, high) in (filters.ratio_bounds or {}).items():
        if column in base.columns:
            series = pd.to_numeric(base[column], errors="coerce")
            if low is not None:
                base = base[series >= low]
            if high is not None:
                base = base[series <= high]

    return DataPoint(base.reset_index(drop=True), quality, "FMP", note=f"{len(base)} sociétés")
