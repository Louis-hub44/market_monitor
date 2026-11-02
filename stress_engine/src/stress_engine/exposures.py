"""Résultat du mapping : expositions de chaque ligne aux facteurs de risque.

Convention de signe et d'unité, partout dans le moteur :

* ``delta`` = P&L en devise de base pour **+1 unité** de facteur (``+1 bp`` sur un taux ou un spread,
  ``+1.0`` = +100 % sur un rendement d'actions / de change / de matière première, ``+1 point`` de vol).
  Une obligation détenue a donc un delta de taux **négatif** (−DV01) ;
* le P&L au premier ordre d'un choc ``x`` est ``Σ delta_f · x_f``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pandas as pd

from stress_engine.factors import family_of
from stress_engine.portfolio import AssetClass, Holding


class MappingMethod(str, Enum):
    CURVE = "CURVE"                 # revalorisation sur courbe, Z-spread calibré sur le prix
    CURVE_THEORETICAL = "CURVE_THEORETICAL"   # idem, sans prix : prix théorique sur la courbe
    CTD = "CTD"                     # future : sensibilité du moins cher à livrer / facteur de conversion
    NOTIONAL_CTD = "NOTIONAL_CTD"   # future sans panier : obligation notionnelle 6 % (approximation)
    LOOKTHROUGH = "LOOKTHROUGH"     # fonds : somme des lignes de la composition
    PROXY = "PROXY"                 # fonds : profil de fund_proxies.yaml (hypothèse)
    MONEY_MARKET = "MONEY_MARKET"   # fonds monétaire : WAM / WAL
    DIRECT = "DIRECT"               # action, change, matière première, cash
    EXCLUDED = "EXCLUDED"           # données insuffisantes : aucune exposition, raison dans les notes


QUALITY = {MappingMethod.CURVE: "élevée", MappingMethod.CTD: "élevée", MappingMethod.LOOKTHROUGH: "élevée",
           MappingMethod.DIRECT: "élevée", MappingMethod.CURVE_THEORETICAL: "moyenne",
           MappingMethod.MONEY_MARKET: "moyenne", MappingMethod.NOTIONAL_CTD: "faible",
           MappingMethod.PROXY: "faible", MappingMethod.EXCLUDED: "-"}


@dataclass(frozen=True)
class Exposure:
    factor: str
    delta: float
    component: str = ""            # ligne sous-jacente (transparence), sinon vide

    def scaled(self, k: float, component: str | None = None) -> Exposure:
        return Exposure(self.factor, self.delta * k, self.component if component is None else component)


@dataclass
class PositionMapping:
    """Projection d'une ligne sur les facteurs.

    Attributes:
        market_value: valeur de marché en devise de base (prix dirty pour les obligations ; 0 pour un
            future, dont la valeur est réglée chaque jour par appel de marge).
        exposure_value: exposition économique (notionnel d'un future, valeur de marché sinon).
        convexity_value: convexité effective × valeur dirty (devise de base, par unité de Δy² en
            décimal), base du terme d'ordre 2 de l'approximation delta-gamma.
        pricing: objets nécessaires à la revalorisation complète (phase 3).
        children: mappings des lignes sous-jacentes (transparence).
    """

    holding: Holding
    method: MappingMethod
    market_value: float = 0.0
    exposure_value: float = 0.0
    exposures: list[Exposure] = field(default_factory=list)
    convexity_value: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    pricing: Any = None
    children: list[PositionMapping] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.holding.id

    @property
    def asset_class(self) -> AssetClass:
        return self.holding.asset_class

    @property
    def quality(self) -> str:
        return QUALITY[self.method]

    def delta(self, factor: str) -> float:
        return sum(e.delta for e in self.exposures if e.factor == factor)

    def deltas(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.exposures:
            out[e.factor] = out.get(e.factor, 0.0) + e.delta
        return out


def merge(exposures: Iterable[Exposure]) -> list[Exposure]:
    """Somme par (facteur, composante), ordre stable, deltas nuls retirés."""
    acc: dict[tuple[str, str], float] = {}
    for e in exposures:
        acc[(e.factor, e.component)] = acc.get((e.factor, e.component), 0.0) + e.delta
    return [Exposure(f, d, c) for (f, c), d in acc.items() if d != 0.0]


@dataclass
class MappingResult:
    """Projection de tout le portefeuille, avec la couverture du calcul."""

    positions: list[PositionMapping]
    base_currency: str = "EUR"
    warnings: list[str] = field(default_factory=list)

    # -------------------------------------------------------------- tables
    def frame(self) -> pd.DataFrame:
        """Une ligne par (position, facteur, composante)."""
        rows = [{"position": p.id, "name": p.holding.name, "asset_class": p.asset_class.value,
                 "method": p.method.value, "factor": e.factor, "family": family_of(e.factor).value,
                 "component": e.component, "delta": e.delta}
                for p in self.positions for e in p.exposures]
        cols = ["position", "name", "asset_class", "method", "factor", "family", "component", "delta"]
        return pd.DataFrame(rows, columns=cols)

    def matrix(self) -> pd.DataFrame:
        """Positions × facteurs (deltas), facteurs en colonnes."""
        f = self.frame()
        if f.empty:
            return pd.DataFrame()
        return f.pivot_table(index="position", columns="factor", values="delta", aggfunc="sum", fill_value=0.0)

    def by_factor(self) -> pd.Series:
        f = self.frame()
        return f.groupby("factor")["delta"].sum().sort_index() if not f.empty else pd.Series(dtype=float)

    def summary(self) -> pd.DataFrame:
        """Une ligne par position : méthode, qualité, valeurs, DV01 / CS01 totaux, notes."""
        rows = []
        for p in self.positions:
            d = p.deltas()
            rows.append({"position": p.id, "name": p.holding.name, "asset_class": p.asset_class.value,
                         "method": p.method.value, "quality": p.quality, "market_value": p.market_value,
                         "exposure_value": p.exposure_value,
                         "dv01": sum(v for k, v in d.items() if k.startswith("IR.")),
                         "cs01": sum(v for k, v in d.items() if k.startswith("CS.")),
                         "notes": " | ".join(p.notes)})
        return pd.DataFrame(rows)

    # -------------------------------------------------------------- agrégats
    @property
    def total_market_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    def coverage(self) -> float:
        """Part de la valeur absolue (valeur de marché ou notionnel) effectivement projetée."""
        total = sum(abs(p.exposure_value) for p in self.positions)
        mapped = sum(abs(p.exposure_value) for p in self.positions if p.method is not MappingMethod.EXCLUDED)
        return mapped / total if total else 1.0

    def excluded(self) -> list[PositionMapping]:
        return [p for p in self.positions if p.method is MappingMethod.EXCLUDED]

    def get(self, position_id: str) -> PositionMapping:
        for p in self.positions:
            if p.id == position_id:
                return p
        raise KeyError(position_id)
