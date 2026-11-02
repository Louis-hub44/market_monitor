"""Taxonomie des facteurs de risque.

Identifiants (chaînes stables, utilisées dans les scénarios YAML et les rapports) :

==========  ==================================  ======  =========================================
Famille     Identifiant                         Unité   Exemple
==========  ==================================  ======  =========================================
RATES       ``IR.<pays>.<pilier>``              bp      ``IR.DE.10Y`` (courbe Bund, pilier 10 ans)
RATES       ``IR.EUR.ESTR``                     bp      taux monétaire (€STR)
CREDIT      ``CS.<devise>.<notation>.<secteur>`` bp     ``CS.EUR.BBB.FIN``
EQUITY      ``EQ.<indice>``                     décimal ``EQ.SX5E`` (0.10 = +10 %)
FX          ``FX.<devise>``                     décimal ``FX.USD`` : +0.10 = USD +10 % contre EUR
VOL         ``VOL.<indice>``                    points  ``VOL.V2X``
COMMODITY   ``CMD.<nom>``                       décimal ``CMD.BRENT``
==========  ==================================  ======  =========================================

Le spread BTP-Bund n'est pas un facteur à part : c'est ``IR.IT.10Y − IR.DE.10Y``. Un choc de spread
souverain choque la courbe du pays à courbe allemande inchangée.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from stress_engine.config import StressConfigError


class FactorFamily(str, Enum):
    RATES = "RATES"
    CREDIT = "CREDIT"
    EQUITY = "EQUITY"
    FX = "FX"
    VOL = "VOL"
    COMMODITY = "COMMODITY"


UNITS = {FactorFamily.RATES: "bp", FactorFamily.CREDIT: "bp", FactorFamily.EQUITY: "rendement",
         FactorFamily.FX: "rendement", FactorFamily.VOL: "points", FactorFamily.COMMODITY: "rendement"}
_PREFIX = {"IR": FactorFamily.RATES, "CS": FactorFamily.CREDIT, "EQ": FactorFamily.EQUITY, "FX": FactorFamily.FX,
           "VOL": FactorFamily.VOL, "CMD": FactorFamily.COMMODITY}


@dataclass(frozen=True, order=True)
class Factor:
    id: str
    family: FactorFamily
    description: str = ""
    pillar_years: float | None = None     # piliers de taux uniquement

    @property
    def unit(self) -> str:
        return UNITS[self.family]


def family_of(factor_id: str) -> FactorFamily:
    try:
        return _PREFIX[factor_id.split(".", 1)[0]]
    except KeyError as exc:
        raise StressConfigError(f"identifiant de facteur inconnu : {factor_id!r}") from exc


def tenor_label(years: float) -> str:
    return f"{round(years * 12)}M" if years < 1 else f"{years:g}Y"


def ir_id(country: str, years: float) -> str:
    return f"IR.{country.upper()}.{tenor_label(years)}"


def cs_id(currency: str, rating: str, sector: str) -> str:
    return f"CS.{currency.upper()}.{rating.upper()}.{sector.upper()}"


# ------------------------------------------------------------------ notations
_SP_BASE = {"AAA": "AAA", "AA": "AA", "A": "A", "BBB": "BBB", "BB": "BB", "B": "B",
            "CCC": "CCC", "CC": "CCC", "C": "CCC", "D": "CCC", "SD": "CCC", "RD": "CCC"}
_MOODYS_BASE = {"AAA": "AAA", "AA": "AA", "A": "A", "BAA": "BBB", "BA": "BB", "B": "B", "CAA": "CCC", "CA": "CCC",
                "C": "CCC"}


def rating_bucket(rating: str | None, buckets: Sequence[str] = ("AAA", "AA", "A", "BBB", "BB", "B", "CCC")
                  ) -> str | None:
    """``AA-`` / ``Aa3`` / ``BBB+`` / ``Baa1`` -> bucket (``AA``, ``BBB``…) ; ``None`` si illisible.

    Notations S&P / Fitch (``+`` / ``-``) et Moody's (``1`` / ``2`` / ``3``) ; tout ce qui est sous ``B``
    tombe dans le dernier bucket (``CCC``).
    """
    if not rating:
        return None
    r = re.sub(r"\s|\(.*?\)|\*", "", str(rating)).split("/")[0]
    sp = re.fullmatch(r"(AAA|AA|A|BBB|BB|B|CCC|CC|C|D|SD|RD)[+-]?", r.upper())
    moodys = re.fullmatch(r"(Aaa|Aa|A|Baa|Ba|B|Caa|Ca|C)[123]?", r, flags=re.IGNORECASE)
    if sp and (r.upper() == r or not moodys):
        base = _SP_BASE[sp.group(1)]
    elif moodys:
        base = _MOODYS_BASE[moodys.group(1).upper()]
    else:
        return None
    return base if base in buckets else buckets[-1]


def sector_bucket(sector: str | None, financial_keywords: Iterable[str]) -> str:
    """Secteur libre -> ``FIN`` ou ``NONFIN`` (mots-clés de la configuration)."""
    text = (sector or "").strip().lower()
    if text in ("fin", "financial", "financials", "financières", "financieres"):
        return "FIN"
    return "FIN" if text and any(k in text for k in financial_keywords) else "NONFIN"


# ------------------------------------------------------------------ espace
class FactorSpace:
    """Ensemble ordonné des facteurs déclarés par ``risk_factors.yaml``."""

    def __init__(self, factors: Iterable[Factor], config: Mapping[str, Any]) -> None:
        self._factors = {f.id: f for f in sorted(factors)}
        self.config = config
        rates = config.get("rates", {})
        self.pillars: tuple[float, ...] = tuple(float(p) for p in rates.get("pillars", (2, 5, 10, 30)))
        self.countries: tuple[str, ...] = tuple(rates.get("countries", ()))
        self.short_rate: str = str(rates.get("short_rate", "IR.EUR.ESTR"))
        self.credit_benchmark: str = str(rates.get("credit_benchmark", "DE"))
        self.country_proxies: dict[str, str] = {str(k).upper(): str(v).upper()
                                                for k, v in (rates.get("country_proxies") or {}).items()}
        credit = config.get("credit", {})
        self.credit_currency: str = str(credit.get("currency", "EUR"))
        self.ratings: tuple[str, ...] = tuple(credit.get("ratings", ("AAA", "AA", "A", "BBB", "BB", "B", "CCC")))
        self.sectors: tuple[str, ...] = tuple(credit.get("sectors", ("FIN", "NONFIN")))
        self.financial_keywords: tuple[str, ...] = tuple(credit.get("financial_keywords", ()))
        self.default_rating: str = str(credit.get("default_rating", "BBB"))
        equity = config.get("equity", {})
        self.default_index_by_region: dict[str, str] = dict(equity.get("default_index_by_region") or {})
        self.default_beta: float = float(equity.get("default_beta", 1.0))
        self.base_currency: str = str(config.get("base_currency", "EUR"))

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> FactorSpace:
        rates = config.get("rates") or {}
        pillars = [float(p) for p in rates.get("pillars", (2, 5, 10, 30))]
        if sorted(pillars) != pillars or len(set(pillars)) != len(pillars) or not pillars:
            raise StressConfigError("rates.pillars doit être une liste croissante, sans doublon")
        out: list[Factor] = []
        for c in rates.get("countries", ()):
            out += [Factor(ir_id(c, p), FactorFamily.RATES, f"Taux {c} {tenor_label(p)}", p) for p in pillars]
        short = str(rates.get("short_rate", "IR.EUR.ESTR"))
        out.append(Factor(short, FactorFamily.RATES, "Taux monétaire (€STR)", 1 / 365))
        credit = config.get("credit") or {}
        ccy = str(credit.get("currency", "EUR"))
        for r in credit.get("ratings", ()):
            out += [Factor(cs_id(ccy, r, s), FactorFamily.CREDIT, f"Spread {ccy} {r} {s}")
                    for s in credit.get("sectors", ())]
        out += [Factor(f"EQ.{i}", FactorFamily.EQUITY, f"Indice actions {i}")
                for i in (config.get("equity") or {}).get("indices", ())]
        out += [Factor(f"FX.{c}", FactorFamily.FX, f"{c} contre {config.get('base_currency', 'EUR')}")
                for c in (config.get("fx") or {}).get("currencies", ())]
        out += [Factor(f"VOL.{v}", FactorFamily.VOL, f"Volatilité implicite {v}")
                for v in (config.get("volatility") or {}).get("indices", ())]
        out += [Factor(f"CMD.{c}", FactorFamily.COMMODITY, f"Matière première {c}")
                for c in config.get("commodities", ())]
        return cls(out, config)

    # -------------------------------------------------------------- accès
    def __contains__(self, factor_id: object) -> bool:
        return factor_id in self._factors

    def __iter__(self) -> Iterator[Factor]:
        return iter(self._factors.values())

    def __len__(self) -> int:
        return len(self._factors)

    @property
    def ids(self) -> list[str]:
        return list(self._factors)

    def get(self, factor_id: str) -> Factor:
        try:
            return self._factors[factor_id]
        except KeyError as exc:
            raise StressConfigError(f"facteur non déclaré dans risk_factors.yaml : {factor_id}") from exc

    def ensure(self, factor_id: str, description: str = "") -> Factor:
        """Facteur déclaré, ou ajouté à la volée (devise ou indice non listé), famille déduite du préfixe."""
        if factor_id not in self._factors:
            self._factors[factor_id] = Factor(factor_id, family_of(factor_id), description or factor_id)
        return self._factors[factor_id]

    def of_family(self, family: FactorFamily) -> list[Factor]:
        return [f for f in self if f.family is family]

    # -------------------------------------------------------------- résolution
    def curve_country(self, country: str | None) -> tuple[str, bool]:
        """Pays dont la courbe porte le risque de taux : (pays, est_un_proxy)."""
        c = (country or self.credit_benchmark).upper()
        if c in self.countries:
            return c, False
        return self.country_proxies.get(c, self.credit_benchmark), True

    def rate_factors(self, country: str) -> list[str]:
        return [ir_id(country, p) for p in self.pillars]

    def credit_factor(self, rating: str | None, sector: str | None) -> tuple[str, list[str]]:
        """(facteur crédit, avertissements) pour une notation et un secteur libres."""
        notes: list[str] = []
        bucket = rating_bucket(rating, self.ratings)
        if bucket is None:
            bucket = self.default_rating
            notes.append(f"notation {rating!r} non reconnue : bucket {bucket} par défaut")
        return cs_id(self.credit_currency, bucket, sector_bucket(sector, self.financial_keywords)), notes

    def equity_index(self, index: str | None, region: str | None, currency: str | None) -> str:
        if index:
            return index.upper()
        for key in (region, currency):
            if key and key.upper() in self.default_index_by_region:
                return self.default_index_by_region[key.upper()]
        return self.default_index_by_region.get(self.base_currency, "SX5E")
