"""Énumérations partagées par toute la plateforme.

Ce module ne dépend d'aucune librairie externe : il est importable
depuis les couches data, business et UI sans effet de bord.
"""

from __future__ import annotations

from enum import Enum


class PortfolioType(str, Enum):
    EQUITY = "EQUITY"
    FIXED_INCOME = "FIXED_INCOME"
    MULTI_ASSET = "MULTI_ASSET"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    GOV_BOND = "GOV_BOND"
    CREDIT = "CREDIT"
    CASH = "CASH"
    FX = "FX"
    COMMODITY = "COMMODITY"
    FUND = "FUND"
    OTHER = "OTHER"

    @property
    def is_rate_sensitive(self) -> bool:
        return self in (AssetClass.GOV_BOND, AssetClass.CREDIT)


class DataQuality(str, Enum):
    """Niveaux de fiabilité d'une donnée (cf. §19 du cahier des charges).

    L'ordre de la liste est l'ordre de préférence : une donnée VERIFIED
    remplace toujours une donnée CACHED, jamais l'inverse.
    """

    VERIFIED = "VERIFIED"            # source officielle ou 2 sources concordantes
    API_CONFIRMED = "API_CONFIRMED"  # source financière fiable, source unique
    CACHED = "CACHED"                # dernière donnée réelle connue, potentiellement périmée
    ESTIMATED = "ESTIMATED"          # valeur ou date déduite par un modèle
    UNVERIFIED = "UNVERIFIED"        # fiabilité insuffisante -> à ne pas utiliser en décision

    @property
    def rank(self) -> int:
        order = [
            DataQuality.VERIFIED,
            DataQuality.API_CONFIRMED,
            DataQuality.CACHED,
            DataQuality.ESTIMATED,
            DataQuality.UNVERIFIED,
        ]
        return order.index(self)

    @property
    def is_decision_grade(self) -> bool:
        """Vrai si la donnée peut servir de base à une décision de gestion."""
        return self.rank <= DataQuality.CACHED.rank


class ShockType(str, Enum):
    """Facteurs de risque élémentaires manipulés par le moteur de scénarios."""

    EQUITY = "EQUITY"                  # choc en % sur un indice actions
    RATES_PARALLEL = "RATES_PARALLEL"  # translation de courbe en bp
    RATES_CURVE = "RATES_CURVE"        # déformation par maturité, en bp
    CREDIT_SPREAD = "CREDIT_SPREAD"    # écartement des spreads en bp
    FX = "FX"                          # variation en % d'une devise vs devise de base
    COMMODITY = "COMMODITY"            # variation en % d'une matière première
    INFLATION = "INFLATION"            # variation en points, contextuelle
    GDP = "GDP"                        # variation en points, contextuelle


class Confidence(str, Enum):
    """Confiance dans le résultat d'un calcul de scénario."""

    HIGH = "HIGH"      # sensibilités calculées sur données réelles complètes
    MEDIUM = "MEDIUM"  # sensibilités partiellement estimées
    LOW = "LOW"        # hypothèses de modèle par défaut dominantes
