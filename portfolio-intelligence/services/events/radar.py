"""Event Radar (§18) — moteur d'événements commun aux trois modules.

Un événement n'est retenu que s'il est **relié au portefeuille** :
* résultats d'une société détenue → pondération de la ligne ;
* réunion de banque centrale ou statistique macro → sensibilité du
  portefeuille au facteur concerné (taux, croissance, inflation).

Le score d'impact est une combinaison transparente de deux termes, affichée
en clair dans l'interface : importance de l'événement × exposition du fonds.
Il ne s'agit pas d'une prévision de mouvement de marché.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from models.datapoint import DataPoint
from models.enums import DataQuality
from models.portfolio import Portfolio

# Poids d'importance par type d'événement macro (convention, paramétrable).
MACRO_IMPORTANCE: dict[str, float] = {
    "fed": 1.00, "fomc": 1.00, "ecb": 0.95, "boe": 0.75, "boj": 0.75,
    "cpi": 0.90, "inflation": 0.90, "nfp": 0.85, "non farm": 0.85, "payroll": 0.85,
    "gdp": 0.80, "pmi": 0.65, "ppi": 0.60, "unemployment": 0.60, "retail sales": 0.55,
    "auction": 0.50, "adjudication": 0.50,
}

FACTOR_BY_KEYWORD: dict[str, str] = {
    "fed": "RATES", "fomc": "RATES", "ecb": "RATES", "boe": "RATES", "boj": "RATES",
    "auction": "RATES", "cpi": "INFLATION", "ppi": "INFLATION", "inflation": "INFLATION",
    "gdp": "GROWTH", "pmi": "GROWTH", "nfp": "GROWTH", "payroll": "GROWTH",
    "unemployment": "GROWTH", "retail sales": "GROWTH",
}


@dataclass
class PortfolioEvent:
    when: date
    title: str
    category: str                    # EARNINGS / CENTRAL_BANK / MACRO / DIVIDEND
    country: str | None
    linked_positions: list[str] = field(default_factory=list)
    portfolio_weight: float = 0.0
    factor: str | None = None
    importance: float = 0.5
    quality: DataQuality = DataQuality.API_CONFIRMED
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def impact_score(self) -> int:
        """0–100. Formule affichée à l'utilisateur : importance × exposition."""
        exposure = min(self.portfolio_weight / 0.05, 1.0) if self.portfolio_weight else 0.25
        return int(round(100 * self.importance * (0.4 + 0.6 * exposure)))

    @property
    def score_formula(self) -> str:
        exposure = min(self.portfolio_weight / 0.05, 1.0) if self.portfolio_weight else 0.25
        return (f"importance {self.importance:.2f} × (0,4 + 0,6 × exposition {exposure:.2f}) "
                f"= {self.impact_score}/100")

    @property
    def days_ahead(self) -> int:
        return (self.when - date.today()).days


def _match_importance(text: str) -> tuple[float, str | None]:
    low = text.lower()
    best, factor = 0.4, None
    for keyword, weight in MACRO_IMPORTANCE.items():
        if keyword in low and weight > best:
            best, factor = weight, FACTOR_BY_KEYWORD.get(keyword)
    return best, factor


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:19].replace(" ", "T")).date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def build_radar(portfolio: Portfolio, market_data, days_ahead: int = 21) -> tuple[list[PortfolioEvent], list[str]]:
    """Renvoie (événements triés par date, avertissements de collecte)."""
    events: list[PortfolioEvent] = []
    warnings: list[str] = []
    weights = portfolio.weights()
    symbols = {p.identifier.upper(): p for p in portfolio.positions}

    # --- résultats de sociétés détenues ------------------------------- #
    # Les symboles sont transmis : sans calendrier global FMP, l'orchestrateur
    # reconstitue les dates titre par titre via Yahoo.
    equity_symbols = [
        p.identifier for p in portfolio.positions
        if p.asset_class.value in ("EQUITY", "FUND")
    ]
    earnings: DataPoint = market_data.earnings_calendar(days_ahead, symbols=equity_symbols)
    if earnings.is_usable:
        for row in earnings.value:
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in symbols:
                continue
            when = _as_date(row.get("date"))
            if not when:
                continue
            position = symbols[symbol]
            events.append(
                PortfolioEvent(
                    when=when,
                    title=f"Résultats {position.name}",
                    category="EARNINGS",
                    country=position.country,
                    linked_positions=[symbol],
                    portfolio_weight=weights.get(position.identifier, 0.0),
                    importance=0.9,
                    quality=earnings.quality,
                    detail={"eps_estime": row.get("epsEstimated"), "ca_estime": row.get("revenueEstimated")},
                )
            )
    else:
        warnings.append(f"Calendrier de résultats : {earnings.note}")
    if earnings.is_usable and earnings.source == "YAHOO":
        warnings.append(
            "Résultats reconstitués via Yahoo, titre par titre : seules les lignes du "
            "portefeuille sont couvertes, et le chiffre d'affaires estimé n'est pas fourni."
        )

    # --- macro et banques centrales ----------------------------------- #
    macro: DataPoint = market_data.economic_calendar(days_ahead)
    if macro.is_usable:
        warnings.append(
            "Calendrier macro FMP : la couverture des banques centrales "
            "et des statistiques dépend du plan souscrit."
        )
        exposures = portfolio.exposure_by_asset_class()
        rate_exposure = exposures.get("GOV_BOND", 0.0) + exposures.get("CREDIT", 0.0)
        equity_exposure = exposures.get("EQUITY", 0.0)
        for row in macro.value:
            title = str(row.get("event") or row.get("name") or "").strip()
            if not title:
                continue
            when = _as_date(row.get("date"))
            if not when:
                continue
            importance, factor = _match_importance(title)
            # Si la source qualifie elle-même la série (taux, inflation, croissance),
            # on préfère son classement à notre reconnaissance par mots-clés.
            declared = row.get("factor")
            if declared:
                factor = declared
                importance = max(importance, 0.75)
            if importance <= 0.4:
                continue
            weight = rate_exposure if factor in ("RATES", "INFLATION") else equity_exposure
            category = "CENTRAL_BANK" if factor == "RATES" and "auction" not in title.lower() else "MACRO"
            events.append(
                PortfolioEvent(
                    when=when,
                    title=title,
                    category=category,
                    country=row.get("country"),
                    portfolio_weight=weight,
                    factor=factor,
                    importance=importance,
                    quality=macro.quality,
                    detail={"consensus": row.get("estimate"), "précédent": row.get("previous"),
                            "période": row.get("period"),
                            "impact_source": row.get("impact") or row.get("ticker")},
                )
            )
    else:
        warnings.append(f"Calendrier macro : {macro.note}")

    events.sort(key=lambda e: (e.when, -e.impact_score))
    return events, warnings


def to_rows(events: list[PortfolioEvent]) -> list[dict[str, Any]]:
    return [
        {
            "date": e.when.isoformat(),
            "j+": e.days_ahead,
            "événement": e.title,
            "catégorie": e.category,
            "pays": e.country or "",
            "poids portefeuille": round(e.portfolio_weight, 4),
            "score d'impact": e.impact_score,
            "qualité": e.quality.value,
        }
        for e in events
    ]
