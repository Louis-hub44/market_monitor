"""Obligations à taux fixe : objets de Rates & Bond Analytics, Z-spread, DV01 par pilier, CS01.

Méthode (revalorisation sur courbe, la même que celle de la phase 3) :

1. l'obligation est l'objet ``FixedRateBond`` de Rates & Bond Analytics : celui de l'univers si
   l'identifiant y figure, sinon construit depuis la ligne (convention du pays ; pour les
   entreprises, annuel ACT/ACT ICMA, T+2 TARGET) ;
2. la courbe de valorisation est la courbe souveraine du pays (la courbe Bund pour le crédit :
   spread contre l'État de référence, comme les OAS des indices) ;
3. le Z-spread (continu, ACT/365F) est calibré pour que la courbe reproduise le **prix observé** ;
4. les DV01 par pilier sont les durations partielles de bumps en tente centrés sur les piliers des
   facteurs (``Σ`` = duration effective) ; le CS01 est la sensibilité au Z-spread (égale à la
   duration effective, puisqu'un spread constant translate la courbe) ; la convexité effective est
   conservée pour l'approximation delta-gamma.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date

from rates_analytics.bonds import FixedRateBond, curve_risk, z_spread
from rates_analytics.bonds.analytics import CurveRisk
from rates_analytics.bonds.static import convention_for_issuer
from rates_analytics.conventions.market import get_convention
from rates_analytics.conventions.period import Frequency
from rates_analytics.curves import YieldCurve
from rates_analytics.curves.shifted import SpreadedCurve
from rates_analytics.exceptions import RatesAnalyticsError

from stress_engine.exposures import Exposure, MappingMethod, PositionMapping
from stress_engine.factors import FactorSpace, ir_id
from stress_engine.market import MarketState
from stress_engine.portfolio import Holding

SOVEREIGN_CONVENTIONS = {"DE": "BUND", "FR": "OAT", "IT": "BTP", "ES": "BONO"}


class BondMappingError(ValueError):
    """Ligne obligataire impossible à projeter (la raison est reportée dans le rapport)."""


@dataclass
class BondPricing:
    """Ce qu'il faut pour revaloriser la ligne sur une courbe choquée (phase 3).

    ``units`` = nombre de « 100 de nominal » (nominal / 100, ou lots × taille / CF pour un future),
    ``fx`` = devise de base par unité de devise de cotation. P&L = units × ΔP_dirty × fx.
    """

    bond: FixedRateBond
    settlement: date
    curve_country: str            # courbe effectivement utilisée pour valoriser
    rate_country: str             # pays des facteurs de taux (IR.<pays>.*)
    z_spread: float               # décimal
    credit_factor: str | None     # CS.* ou None (souverain)
    units: float
    fx: float
    clean: float
    dirty: float
    effective_duration: float
    effective_convexity: float
    krd: dict[float, float]       # pilier (années) -> KRD (années)


def convention_for(holding: Holding, country: str):
    issuer = (holding.issuer_type or "GOV").upper()
    if issuer == "GOV" and country in SOVEREIGN_CONVENTIONS:
        conv = convention_for_issuer(country)
    else:
        conv = replace(get_convention("BUND"), name="EUR_CREDIT", issuer=country)
    return conv


def build_bond(holding: Holding, as_of: date, country: str) -> tuple[FixedRateBond, list[str]]:
    """``FixedRateBond`` depuis les champs de la ligne (coupon, échéance, jouissance, fréquence)."""
    notes: list[str] = []
    if holding.coupon is None or holding.maturity is None:
        raise BondMappingError("coupon ou échéance manquant")
    if holding.maturity <= as_of:
        raise BondMappingError(f"titre échu ({holding.maturity})")
    conv = convention_for(holding, country)
    freq = Frequency(int(holding.frequency)) if holding.frequency else conv.frequency
    issue = holding.issue_date
    if issue is None:
        years = math.ceil((holding.maturity - as_of).days / 365.25) + 1
        issue = holding.maturity.replace(year=holding.maturity.year - years)
        notes.append(f"date de jouissance déduite de l'échéance ({issue}) : premier coupon régulier supposé")
    try:
        bond = FixedRateBond(isin=holding.id, coupon=float(holding.coupon), maturity=holding.maturity,
                             issue_date=issue, convention=conv, frequency=freq, name=holding.name or holding.id,
                             issuer=country)
    except (RatesAnalyticsError, ValueError) as exc:
        raise BondMappingError(str(exc)) from exc
    return bond, notes


def price_bond(bond: FixedRateBond, curve: YieldCurve, settlement: date, pillars: tuple[float, ...],
               clean: float | None, spread_bp: float | None = None) -> tuple[float, float, CurveRisk, bool]:
    """(clean, z, risque courbe, prix_théorique) : Z-spread calibré sur ``clean`` ou prix théorique."""
    theoretical = clean is None
    if theoretical:
        z = (spread_bp or 0.0) * 1e-4
        dirty = bond.dirty_price_from_curve(SpreadedCurve(curve, z) if z else curve, settlement)
        clean = dirty - bond.accrued_interest(settlement)
    else:
        assert clean is not None
        try:
            z = z_spread(bond, clean, settlement, curve)
        except RatesAnalyticsError as exc:
            raise BondMappingError(f"Z-spread introuvable pour le prix {clean} : {exc}") from exc
    risk = curve_risk(bond, curve, settlement, key_tenors=pillars, spread=z)
    return float(clean), float(z), risk, theoretical


def map_bond(holding: Holding, space: FactorSpace, market: MarketState, *, units: float | None = None,
             clean: float | None = None, rate_country: str | None = None, credit: bool | None = None,
             bond: FixedRateBond | None = None, fx: float = 1.0) -> PositionMapping:
    """Projection d'une obligation ; ``units`` / ``clean`` / ``bond`` servent aux futures (CTD)."""
    notes: list[str] = []
    issuer = (holding.issuer_type or "GOV").upper()
    is_credit = issuer in ("CORP", "SSA") if credit is None else credit
    if rate_country is None:
        if is_credit:
            rate_country = space.credit_benchmark
        else:
            rate_country, proxied = space.curve_country(holding.country)
            if proxied:
                notes.append(f"pays {holding.country or '?'} sans courbe propre : facteurs de taux {rate_country}")
    if bond is None:
        bond = market.bonds.get(holding.id)
    if bond is None:
        bond, extra = build_bond(holding, market.as_of, holding.country or rate_country)
        notes += extra
    curve, curve_country = market.curve_for(rate_country)
    if curve_country != rate_country:
        notes.append(f"courbe {rate_country} absente : forme de courbe {curve_country} "
                     f"(risque porté par IR.{rate_country}.*)")
    if clean is None:
        clean = holding.price if holding.price is not None else market.prices.get(holding.id)
    settlement = bond.settlement_date(market.as_of)
    clean, z, risk, theoretical = price_bond(bond, curve, settlement, space.pillars, clean)
    if theoretical:
        notes.append("aucun prix : prix théorique sur la courbe (Z-spread nul)")
    if units is None:
        if holding.nominal is None:
            raise BondMappingError("nominal manquant")
        units = holding.nominal / 100.0
    dirty = float(risk.model_dirty)
    krd = {float(k): float(v) for k, v in risk.krd.items()}
    scale = units * dirty * 1e-4 * fx                  # devise de base par bp, par année de duration
    if bond.residual_years(market.as_of) < 1.0:
        # Titre monétaire (< 1 an) : son taux de référence est le taux monétaire, pas le pilier 2 ans.
        exposures = [Exposure(space.short_rate, -float(risk.effective_duration) * scale)]
        notes.append(f"maturité < 1 an : risque de taux porté par {space.short_rate}")
    else:
        exposures = [Exposure(ir_id(rate_country, t), -k * scale) for t, k in krd.items() if k != 0.0]
    credit_factor = None
    if is_credit:
        credit_factor, cnotes = space.credit_factor(holding.rating, holding.sector)
        notes += cnotes
        exposures.append(Exposure(credit_factor, -risk.effective_duration * scale))
    value = units * dirty * fx
    pricing = BondPricing(bond=bond, settlement=settlement, curve_country=curve_country, rate_country=rate_country,
                          z_spread=z, credit_factor=credit_factor, units=units, fx=fx, clean=clean, dirty=dirty,
                          effective_duration=float(risk.effective_duration),
                          effective_convexity=float(risk.effective_convexity), krd=krd)
    return PositionMapping(
        holding=holding, method=MappingMethod.CURVE_THEORETICAL if theoretical else MappingMethod.CURVE,
        market_value=value, exposure_value=value, exposures=exposures,
        convexity_value=float(risk.effective_convexity) * value,
        details={"clean": clean, "dirty": dirty, "z_spread_bp": z * 1e4, "duration": float(risk.effective_duration),
                 "convexity": float(risk.effective_convexity), "krd": krd, "curve": curve_country,
                 "settlement": settlement, "credit_factor": credit_factor},
        notes=notes, pricing=pricing)
