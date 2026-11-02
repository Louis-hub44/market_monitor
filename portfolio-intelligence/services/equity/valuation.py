"""Historique de valorisation (§3).

Compare les multiples courants à leur propre historique. Deux garde-fous :
* l'historique est celui réellement renvoyé par le provider — si seules trois
  années sont disponibles, la comparaison est présentée comme « 3 ans », pas
  comme une moyenne 5 ans ;
* la décote/prime est calculée uniquement quand la valeur courante et la
  moyenne historique existent toutes les deux.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from data.providers.base import ProviderUnavailable
from models.datapoint import DataPoint, missing
from models.enums import DataQuality

MULTIPLES: dict[str, tuple[str, ...]] = {
    # libellé -> clés possibles dans les payloads FMP (ratios / key-metrics)
    "PER": ("priceToEarningsRatio", "peRatio", "priceEarningsRatio"),
    "EV/EBITDA": ("evToEBITDA", "enterpriseValueOverEBITDA", "evToOperatingCashFlow"),
    "EV/Sales": ("evToSales", "enterpriseValueOverRevenue"),
    "P/B": ("priceToBookRatio", "pbRatio"),
    "P/S": ("priceToSalesRatio", "psRatio"),
    "FCF Yield": ("freeCashFlowYield",),
    "Dividend Yield": ("dividendYield",),
}


@dataclass
class MultipleHistory:
    label: str
    current: float | None
    mean: float | None
    median: float | None
    minimum: float | None
    maximum: float | None
    years: int
    series: pd.Series
    quality: DataQuality

    @property
    def discount_vs_mean(self) -> float | None:
        if self.current is None or not self.mean:
            return None
        return self.current / self.mean - 1.0

    @property
    def percentile(self) -> float | None:
        if self.current is None or self.series.empty:
            return None
        return float((self.series < self.current).mean())


def _first_key(row: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def valuation_history(symbol: str, market_data, years: int = 5) -> DataPoint[list[MultipleHistory]]:
    """Historique des multiples.

    Chemin principal : ratios annuels FMP. À défaut, l'historique est
    **reconstruit** à partir des comptes annuels Yahoo et des cours de bourse
    (PER = cours à la clôture de l'exercice / EPS dilué publié). C'est une
    approximation — pas de retraitement, pas de multiple d'entreprise — donc
    marquée ESTIMATED et signalée comme telle à l'écran.
    """
    fmp = market_data.fmp
    rows: list[dict] = []
    metrics: list[dict] = []
    stale = False
    try:
        rows, stale = fmp.ratios(symbol, period="annual", limit=max(years, 3))
        metrics, _ = fmp.key_metrics(symbol, period="annual", limit=max(years, 3))
    except ProviderUnavailable as exc:
        rows, metrics = [], []

    if not rows and not metrics:
        return _reconstruct_from_yahoo(symbol, market_data, years)

    merged: dict[str, dict] = {}
    for row in list(rows or []) + list(metrics or []):
        key = str(row.get("date") or row.get("calendarYear") or "")[:10]
        merged.setdefault(key, {}).update(row)

    quality = DataQuality.CACHED if stale else DataQuality.API_CONFIRMED
    histories: list[MultipleHistory] = []

    for label, keys in MULTIPLES.items():
        points = {
            date_key: _first_key(row, keys)
            for date_key, row in sorted(merged.items())
            if _first_key(row, keys) is not None
        }
        if len(points) < 2:
            continue
        series = pd.Series(points).astype(float)
        series = series[series.abs() < 500]        # écarte les valeurs aberrantes de ratio
        if series.empty:
            continue
        current = float(series.iloc[-1])
        histories.append(
            MultipleHistory(
                label=label,
                current=current,
                mean=float(series.mean()),
                median=float(series.median()),
                minimum=float(series.min()),
                maximum=float(series.max()),
                years=len(series),
                series=series,
                quality=quality,
            )
        )

    if not histories:
        return _reconstruct_from_yahoo(symbol, market_data, years)
    return DataPoint(histories, quality, "FMP", note=f"{len(histories)} multiples reconstitués")


def _reconstruct_from_yahoo(symbol: str, market_data, years: int) -> DataPoint[list[MultipleHistory]]:
    """Reconstruit un historique de PER et de P/S à partir des comptes publiés.

    Méthode : pour chaque exercice, on prend le cours de clôture le plus proche
    de la date d'arrêté et on le divise par l'EPS dilué publié. Le multiple
    courant vient de `Ticker.info`, plus fiable car calculé sur le TTM.

    Limites assumées, affichées à l'utilisateur : pas de retraitement des
    éléments exceptionnels, pas d'ajustement du nombre d'actions, et le CA par
    action est approché via le ratio CA/EPS.
    """
    accounts = market_data.annual_accounts(symbol)
    prices = market_data.price_series(symbol, lookback_days=365 * (years + 1))
    if not accounts.is_usable or not accounts.value:
        return missing(note=f"Comptes annuels indisponibles pour {symbol} : "
                            f"aucun historique de valorisation reconstructible")
    if not prices.is_usable or not prices.value:
        return missing(note=f"Historique de cours indisponible pour {symbol}")

    series = pd.Series(
        {row["date"]: float(row["close"]) for row in prices.value}
    ).sort_index()

    per_points: dict[str, float] = {}
    for row in accounts.value:
        eps = row.get("eps")
        if not eps or eps <= 0:
            continue
        nearest = series[series.index <= row["date"]]
        if nearest.empty:
            continue
        per_points[row["date"]] = float(nearest.iloc[-1]) / eps

    if len(per_points) < 2:
        return missing(note=f"Moins de deux exercices exploitables pour {symbol}")

    per_series = pd.Series(per_points).astype(float)
    per_series = per_series[(per_series > 0) & (per_series < 200)]

    current = None
    info = market_data.fundamentals(symbol)
    if info.is_usable and info.value:
        current = info.value.get("PER") or info.value.get("trailingPE")
    if current is None and len(series):
        last_eps = next((r["eps"] for r in reversed(accounts.value) if r.get("eps")), None)
        if last_eps and last_eps > 0:
            current = float(series.iloc[-1]) / last_eps

    if current is None or per_series.empty:
        return missing(note=f"PER courant non calculable pour {symbol}")

    history = MultipleHistory(
        label="PER (reconstruit)",
        current=float(current),
        mean=float(per_series.mean()),
        median=float(per_series.median()),
        minimum=float(per_series.min()),
        maximum=float(per_series.max()),
        years=len(per_series),
        series=per_series,
        quality=DataQuality.ESTIMATED,
    )
    return DataPoint(
        [history], DataQuality.ESTIMATED, "YAHOO",
        note=("Historique reconstruit : cours de clôture d'exercice / EPS dilué publié. "
              "Sans retraitement — à lire comme un ordre de grandeur."),
    )
