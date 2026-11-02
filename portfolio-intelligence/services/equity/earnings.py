"""Earnings & consensus (§4).

Trois blocs, tous alimentés par le provider :
* prochaine publication et historique des surprises ;
* niveau de consensus (EPS / CA) et sa révision dans le temps ;
* rapprochement avec les positions du portefeuille (poids, impact potentiel).

La révision de consensus n'est calculée que si le provider expose plusieurs
millésimes d'estimations ; sinon la fonction renvoie explicitement l'absence
de donnée plutôt qu'une variation reconstruite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from data.providers.base import ProviderUnavailable
from models.datapoint import DataPoint, missing
from models.enums import DataQuality


@dataclass
class EarningsRecord:
    date: date | None
    eps_estimated: float | None
    eps_actual: float | None
    revenue_estimated: float | None
    revenue_actual: float | None

    @property
    def eps_surprise(self) -> float | None:
        if self.eps_actual is None or not self.eps_estimated:
            return None
        return self.eps_actual / self.eps_estimated - 1.0

    @property
    def revenue_surprise(self) -> float | None:
        if self.revenue_actual is None or not self.revenue_estimated:
            return None
        return self.revenue_actual / self.revenue_estimated - 1.0

    @property
    def is_future(self) -> bool:
        return self.date is not None and self.date >= date.today() and self.eps_actual is None


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _d(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def earnings_history(symbol: str, market_data, limit: int = 12) -> DataPoint[list[EarningsRecord]]:
    """Historique des publications, quelle que soit la source disponible.

    L'orchestrateur choisit FMP puis Yahoo ; le format des lignes est commun,
    seul le chiffre d'affaires peut manquer côté Yahoo.
    """
    dp = market_data.earnings(symbol, limit=limit)
    if not dp.is_usable or not dp.value:
        return missing(note=dp.note)
    rows, quality, source = dp.value, dp.quality, dp.source

    records = [
        EarningsRecord(
            date=_d(r.get("date")),
            eps_estimated=_f(r.get("epsEstimated") or r.get("estimatedEps")),
            eps_actual=_f(r.get("eps") or r.get("epsActual")),
            revenue_estimated=_f(r.get("revenueEstimated") or r.get("estimatedRevenue")),
            revenue_actual=_f(r.get("revenue") or r.get("revenueActual")),
        )
        for r in rows
    ]
    records.sort(key=lambda r: r.date or date.min, reverse=True)
    return DataPoint(records, quality, source, note=dp.note)


def next_earnings(symbol: str, market_data) -> DataPoint[EarningsRecord]:
    dp = earnings_history(symbol, market_data)
    if not dp.is_usable:
        return missing(note=dp.note)
    upcoming = [r for r in dp.value if r.is_future]
    if not upcoming:
        return missing(source="FMP", note=f"Aucune date de publication future connue pour {symbol}")
    nxt = min(upcoming, key=lambda r: r.date)
    return DataPoint(nxt, dp.quality, dp.source, as_of=nxt.date, note="prochaine publication annoncée")


def consensus_revisions(symbol: str, market_data, period: str = "annual") -> DataPoint[list[dict]]:
    """Estimations analystes par exercice, telles que publiées par le provider.

    Réservé à FMP : Yahoo n'expose pas de consensus par exercice exploitable
    (seulement une estimation ponctuelle du trimestre à venir, déjà couverte
    par `next_earnings`). Sans plan FMP adéquat, la fonction renvoie
    explicitement l'absence de donnée.
    """
    try:
        rows, stale = market_data.fmp.analyst_estimates(symbol, period=period, limit=6)
    except ProviderUnavailable as exc:
        return missing(note=f"Consensus indisponible : {exc}")
    if not rows:
        return missing(note=f"Aucune estimation renvoyée pour {symbol}")

    cleaned = [
        {
            "exercice": str(r.get("date"))[:4],
            "eps_moyen": _f(r.get("epsAvg") or r.get("estimatedEpsAvg")),
            "eps_bas": _f(r.get("epsLow") or r.get("estimatedEpsLow")),
            "eps_haut": _f(r.get("epsHigh") or r.get("estimatedEpsHigh")),
            "ca_moyen": _f(r.get("revenueAvg") or r.get("estimatedRevenueAvg")),
            "analystes": r.get("numAnalystsEps") or r.get("numberAnalystEstimatedEps"),
        }
        for r in rows
    ]
    cleaned = [c for c in cleaned if c["eps_moyen"] is not None]
    if not cleaned:
        return missing(note="Estimations vides")
    quality = DataQuality.CACHED if stale else DataQuality.API_CONFIRMED
    return DataPoint(
        sorted(cleaned, key=lambda c: c["exercice"]), quality, "FMP",
        note="Révisions dans le temps disponibles uniquement si le plan FMP historise les estimations",
    )
