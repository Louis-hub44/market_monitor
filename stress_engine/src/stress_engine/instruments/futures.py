"""Futures EUREX sur emprunts d'État : sensibilité par le moins cher à livrer (CTD).

* **Livraison** : le 10 du mois d'échéance, reporté au jour ouvré TARGET suivant.
* **Panier** : titres de l'univers du pays du contrat dont la maturité résiduelle à la livraison est
  dans la fenêtre du contrat (``eurex.yaml``).
* **Facteur de conversion** : prix clean du titre à la date de livraison pour un rendement égal au
  coupon notionnel (6 %), divisé par 100, arrondi à 6 décimales — c'est la définition EUREX. Pour les
  BTP (coupons semestriels), le rendement est pris à la fréquence du titre : approximation signalée,
  à remplacer par le facteur publié par EUREX (colonne ``cf`` d'un panier fourni) si besoin.
* **CTD**, par ordre de préférence :

  1. l'ISIN fourni dans la ligne (colonne ``ctd``) ;
  2. avec un prix de future, la **base brute** minimale ``P − F × CF`` ;
  3. sans prix de future, le plus petit **prix converti** ``P / CF`` (même titre que 2. à portage nul) ;
  4. sans panier, une obligation notionnelle à 6 % au milieu de la fenêtre, valorisée sur la courbe
     du pays — **approximation**, signalée comme telle.

* **Sensibilité** : ``DV01_future = DV01_CTD(nominal du contrat) / CF × lots``, ventilée sur les
  piliers comme les KRD du CTD. Le portage et l'option de livraison sont ignorés (P&L du future
  approché par la variation du prix du CTD divisée par le CF). Valeur de marché nulle (appel de marge).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from rates_analytics.bonds import FixedRateBond
from rates_analytics.conventions.calendar import TARGET
from rates_analytics.conventions.market import get_convention
from rates_analytics.exceptions import RatesAnalyticsError

from stress_engine.exposures import MappingMethod, PositionMapping
from stress_engine.factors import FactorSpace
from stress_engine.instruments.bonds import SOVEREIGN_CONVENTIONS, BondMappingError, map_bond
from stress_engine.market import MarketState
from stress_engine.portfolio import Holding

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
MONTH_NAMES = {"JAN": 1, "FEB": 2, "FEV": 2, "MAR": 3, "APR": 4, "AVR": 4, "MAY": 5, "MAI": 5, "JUN": 6, "JUI": 6,
               "JUL": 7, "AUG": 8, "AOU": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


class FutureMappingError(ValueError):
    """Future impossible à projeter (raison reportée dans le rapport)."""


@dataclass(frozen=True)
class ContractSpec:
    code: str
    name: str
    country: str
    min_years: float
    max_years: float
    notional_coupon: float = 0.06
    size: float = 100_000.0
    delivery_day: int = 10


def contract_specs(config: Mapping[str, Any]) -> dict[str, ContractSpec]:
    coupon = float(config.get("notional_coupon", 0.06))
    size = float(config.get("contract_size", 100_000))
    day = int(config.get("delivery_day", 10))
    return {code.upper(): ContractSpec(code.upper(), str(c.get("name", code)), str(c["country"]).upper(),
                                       float(c["min_years"]), float(c["max_years"]), coupon, size, day)
            for code, c in (config.get("contracts") or {}).items()}


def parse_contract(text: str, as_of: date) -> tuple[str, int, int]:
    """``FGBL DEC26`` / ``FGBLZ6`` / ``FGBL Z26`` / ``FGBL 12/2026`` -> (code, année, mois).

    Une année à un chiffre est la prochaine occurrence à partir de ``as_of``.
    """
    t = re.sub(r"\s+", " ", text.strip().upper())
    m = re.fullmatch(r"(F[A-Z]{3,4}?)\s?([FGHJKMNQUVXZ])(\d{1,2})", t) or \
        re.fullmatch(r"(F[A-Z]{3,4}) ([A-Z]{3})\s?(\d{2,4})", t) or \
        re.fullmatch(r"(F[A-Z]{3,4}) (\d{1,2})[/-](\d{2,4})", t)
    if not m:
        raise FutureMappingError(f"contrat illisible : {text!r} (attendu « FGBL DEC26 », « FGBLZ6 »…)")
    code, month_txt, year_txt = m.groups()
    month = MONTH_CODES.get(month_txt) or MONTH_NAMES.get(month_txt) or (int(month_txt) if month_txt.isdigit() else 0)
    if not 1 <= month <= 12:
        raise FutureMappingError(f"mois d'échéance illisible dans {text!r}")
    y = int(year_txt)
    if len(year_txt) == 1:
        year = as_of.year - as_of.year % 10 + y
        if (year, month) < (as_of.year, as_of.month):
            year += 10
    elif len(year_txt) == 2:
        year = 2000 + y
    else:
        year = y
    return code, year, month


def delivery_date(year: int, month: int, day: int = 10) -> date:
    day = min(day, calendar.monthrange(year, month)[1])
    return TARGET.adjust(date(year, month, day))


def years_between(a: date, b: date) -> float:
    return (b - a).days / 365.25


def conversion_factor(bond: FixedRateBond, delivery: date, notional_coupon: float = 0.06) -> float:
    """Prix clean / 100 au rendement notionnel à la date de livraison, 6 décimales."""
    return round(bond.clean_price_from_yield(notional_coupon, delivery) / 100.0, 6)


def basket(spec: ContractSpec, delivery: date, universe: Mapping[str, FixedRateBond]) -> list[FixedRateBond]:
    """Titres livrables : pays du contrat, maturité résiduelle à la livraison dans la fenêtre."""
    out = []
    for bond in universe.values():
        issuer = (bond.issuer or bond.convention.issuer or "").upper()
        if issuer != spec.country:
            continue
        rest = years_between(delivery, bond.maturity)
        if spec.min_years - 1e-9 <= rest <= spec.max_years + 1e-9:
            out.append(bond)
    return sorted(out, key=lambda b: b.maturity)


@dataclass
class CTDChoice:
    bond: FixedRateBond
    cf: float
    clean: float | None
    rule: str
    table: list[dict[str, Any]]


def select_ctd(spec: ContractSpec, delivery: date, universe: Mapping[str, FixedRateBond],
               prices: Mapping[str, float], futures_price: float | None, ctd_isin: str | None = None
               ) -> CTDChoice | None:
    """CTD selon la règle documentée dans le module ; ``None`` si le panier est vide."""
    deliverable = basket(spec, delivery, universe)
    table = []
    for b in deliverable:
        cf = conversion_factor(b, delivery, spec.notional_coupon)
        p = prices.get(b.isin)
        row: dict[str, Any] = {"isin": b.isin, "maturity": b.maturity, "coupon": b.coupon, "cf": cf, "clean": p}
        if p is not None:
            row["converted_price"] = p / cf
            if futures_price is not None:
                row["gross_basis"] = p - futures_price * cf
        table.append(row)
    if ctd_isin:
        for b, row in zip(deliverable, table):
            if b.isin == ctd_isin:
                return CTDChoice(b, row["cf"], row["clean"], "CTD fourni dans le fichier", table)
        bond = universe.get(ctd_isin)
        if bond is not None:
            cf = conversion_factor(bond, delivery, spec.notional_coupon)
            return CTDChoice(bond, cf, prices.get(ctd_isin), "CTD fourni (hors panier calculé)", table)
    priced = [(b, r) for b, r in zip(deliverable, table) if r["clean"] is not None]
    if priced and futures_price is not None:
        b, r = min(priced, key=lambda br: br[1]["gross_basis"])
        return CTDChoice(b, r["cf"], r["clean"], "base brute minimale", table)
    if priced:
        b, r = min(priced, key=lambda br: br[1]["converted_price"])
        return CTDChoice(b, r["cf"], r["clean"], "prix converti minimal (portage nul)", table)
    if deliverable:
        b = deliverable[0]
        return CTDChoice(b, table[0]["cf"], None, "panier sans prix : premier titre livrable", table)
    return None


def notional_bond(spec: ContractSpec, delivery: date, as_of: date) -> FixedRateBond:
    """Obligation à 6 % de maturité « milieu de fenêtre » (approximation sans panier)."""
    years = (spec.min_years + spec.max_years) / 2
    months = round(years * 12)
    y, m = divmod(delivery.month - 1 + months, 12)
    maturity = date(delivery.year + y, m + 1, min(delivery.day, 28))
    conv = get_convention(SOVEREIGN_CONVENTIONS.get(spec.country, "BUND"))
    issue = maturity.replace(year=maturity.year - int(years) - 2)
    return FixedRateBond(isin=f"{spec.code}-NOTIONNEL", coupon=spec.notional_coupon, maturity=maturity,
                         issue_date=issue, convention=conv, name=f"{spec.name} notionnel 6 %", issuer=spec.country)


def map_future(holding: Holding, space: FactorSpace, market: MarketState,
               specs: Mapping[str, ContractSpec]) -> PositionMapping:
    if holding.lots is None or holding.lots == 0:
        raise FutureMappingError("nombre de lots manquant")
    if not holding.contract:
        raise FutureMappingError("contrat manquant (colonne contract)")
    code, year, month = parse_contract(holding.contract, market.as_of)
    spec = specs.get(code)
    if spec is None:
        raise FutureMappingError(f"contrat {code} absent de eurex.yaml")
    delivery = delivery_date(year, month, spec.delivery_day)
    if delivery < market.as_of:
        raise FutureMappingError(f"contrat {holding.contract} échu le {delivery}")
    rate_country, proxied = space.curve_country(spec.country)
    notes = [f"pays {spec.country} sans courbe propre : facteurs {rate_country}"] if proxied else []
    choice = select_ctd(spec, delivery, market.bonds, market.prices, holding.price, holding.ctd)
    method = MappingMethod.CTD
    if choice is None:
        bond = notional_bond(spec, delivery, market.as_of)
        cf = conversion_factor(bond, delivery, spec.notional_coupon)
        choice = CTDChoice(bond, cf, None, "aucun titre livrable connu : obligation notionnelle 6 %", [])
        method = MappingMethod.NOTIONAL_CTD
        notes.append("panier livrable inconnu : sensibilité d'une obligation notionnelle (approximation)")
    if spec.country == "IT":
        notes.append("facteur de conversion BTP recalculé au rendement semestriel de 6 % : à confronter au "
                     "facteur publié par EUREX")
    units = holding.lots * spec.size / 100.0 / choice.cf
    sub = Holding(id=choice.bond.isin, instrument=holding.instrument, name=choice.bond.name or choice.bond.isin,
                  issuer_type="GOV", country=spec.country)
    try:
        ctd_map = map_bond(sub, space, market, units=units, clean=choice.clean, rate_country=rate_country,
                           credit=False, bond=choice.bond)
    except (BondMappingError, RatesAnalyticsError) as exc:
        raise FutureMappingError(f"CTD {choice.bond.isin} non valorisable : {exc}") from exc
    ctd_clean = ctd_map.details["clean"]
    futures_price = holding.price if holding.price is not None else ctd_clean / choice.cf
    if holding.price is None:
        notes.append(f"prix du future absent : prix théorique CTD / CF = {futures_price:.3f} (portage ignoré)")
    notional = holding.lots * spec.size * futures_price / 100.0
    notes = notes + [n for n in ctd_map.notes if "aucun prix" not in n]
    if choice.clean is None and method is MappingMethod.CTD:
        notes.append("CTD sans prix : valorisé sur la courbe (prix théorique)")
    return PositionMapping(
        holding=holding, method=method, market_value=0.0, exposure_value=notional,
        exposures=ctd_map.exposures, convexity_value=ctd_map.convexity_value,
        details={"contract": code, "delivery": delivery, "ctd": choice.bond.isin, "ctd_rule": choice.rule,
                 "cf": choice.cf, "futures_price": futures_price, "ctd_clean": ctd_clean,
                 "ctd_duration": ctd_map.details["duration"], "basket": choice.table, "lots": holding.lots,
                 "contract_size": spec.size},
        notes=notes, pricing=ctd_map.pricing)
