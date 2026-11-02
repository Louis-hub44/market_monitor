"""Modèle de domaine : position et portefeuille.

Volontairement indépendant de Streamlit, de pandas et des providers :
c'est le contrat commun aux trois modules Equity / Fixed Income / Diversified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from models.enums import AssetClass, DataQuality, PortfolioType


@dataclass
class Position:
    identifier: str                      # ISIN, ticker ou code interne
    name: str
    asset_class: AssetClass
    quantity: float | None = None        # titres ou nominal
    price: float | None = None
    market_value: float = 0.0            # dans la devise de la position
    currency: str = "EUR"
    fx_rate_to_base: float = 1.0         # 1 unité de `currency` = X devise de base
    sector: str | None = None
    country: str | None = None
    region: str | None = None

    # --- caractéristiques obligataires (None pour les actions) ---
    coupon: float | None = None          # taux annuel en décimal (0.0325 = 3.25 %)
    maturity: date | None = None
    frequency: int = 2                   # nombre de coupons par an
    ytm: float | None = None             # en décimal
    spread_bp: float | None = None       # spread vs courbe souveraine, en bp
    issuer_rating: str | None = None

    # --- sensibilités (calculées ou importées) ---
    beta: float | None = None
    modified_duration: float | None = None
    convexity: float | None = None
    spread_duration: float | None = None
    key_rate_durations: dict[str, float] = field(default_factory=dict)

    quality: DataQuality = DataQuality.API_CONFIRMED
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def market_value_base(self) -> float:
        """Valorisation convertie en devise de base du portefeuille."""
        return self.market_value * self.fx_rate_to_base

    @property
    def is_bond(self) -> bool:
        return self.asset_class in (AssetClass.GOV_BOND, AssetClass.CREDIT)


@dataclass
class Portfolio:
    name: str
    portfolio_type: PortfolioType
    positions: list[Position] = field(default_factory=list)
    base_currency: str = "EUR"
    as_of: date | None = None
    benchmark: str | None = None
    cash: float = 0.0
    source_file: str | None = None
    #: Vrai quand le fichier ne fournissait que des poids : la NAV est alors
    #: une base notionnelle, pas un encours réel. L'interface doit le dire.
    is_notional: bool = False
    notional_base: float | None = None
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Agrégats de base
    # ------------------------------------------------------------------ #
    @property
    def nav(self) -> float:
        return sum(p.market_value_base for p in self.positions) + self.cash

    def weights(self) -> dict[str, float]:
        nav = self.nav
        if nav == 0:
            return {p.identifier: 0.0 for p in self.positions}
        return {p.identifier: p.market_value_base / nav for p in self.positions}

    def weight_of(self, position: Position) -> float:
        nav = self.nav
        return position.market_value_base / nav if nav else 0.0

    # ------------------------------------------------------------------ #
    # Expositions
    # ------------------------------------------------------------------ #
    def _breakdown(self, key) -> dict[str, float]:
        nav = self.nav
        out: dict[str, float] = {}
        for p in self.positions:
            k = key(p)
            k = "N/A" if k in (None, "") else str(k)
            out[k] = out.get(k, 0.0) + (p.market_value_base / nav if nav else 0.0)
        if self.cash:
            out["CASH"] = out.get("CASH", 0.0) + (self.cash / nav if nav else 0.0)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def exposure_by_asset_class(self) -> dict[str, float]:
        return self._breakdown(lambda p: p.asset_class.value)

    def exposure_by_sector(self) -> dict[str, float]:
        return self._breakdown(lambda p: p.sector)

    def exposure_by_country(self) -> dict[str, float]:
        return self._breakdown(lambda p: p.country)

    def exposure_by_currency(self) -> dict[str, float]:
        return self._breakdown(lambda p: p.currency)

    def positions_of(self, *asset_classes: AssetClass) -> list[Position]:
        wanted = set(asset_classes)
        return [p for p in self.positions if p.asset_class in wanted]

    def top_holdings(self, n: int = 10) -> list[tuple[Position, float]]:
        nav = self.nav
        ranked = sorted(self.positions, key=lambda p: -p.market_value_base)
        return [(p, p.market_value_base / nav if nav else 0.0) for p in ranked[:n]]

    def concentration(self) -> dict[str, float]:
        """Indicateurs de concentration : HHI, poids max, poids du top 10."""
        w = [x for x in self.weights().values()]
        w_sorted = sorted(w, reverse=True)
        return {
            "hhi": sum(x * x for x in w),
            "effective_n": (1.0 / sum(x * x for x in w)) if sum(x * x for x in w) else 0.0,
            "max_weight": w_sorted[0] if w_sorted else 0.0,
            "top10_weight": sum(w_sorted[:10]),
            "n_positions": len(w),
        }

    def coverage(self, attribute: str) -> float:
        """Part de la NAV pour laquelle un attribut est renseigné.

        Sert à qualifier la confiance d'un calcul : une duration calculée sur
        60 % de la NAV ne doit pas être présentée comme la duration du fonds.
        """
        nav = self.nav
        if not nav:
            return 0.0
        covered = sum(
            p.market_value_base for p in self.positions
            if getattr(p, attribute, None) not in (None, {}, [])
        )
        return covered / nav

    def extend(self, positions: Iterable[Position]) -> None:
        self.positions.extend(positions)
