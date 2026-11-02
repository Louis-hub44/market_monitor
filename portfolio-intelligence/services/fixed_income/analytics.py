"""Analytique obligataire au niveau portefeuille.

Chaîne : position → échéancier → risques analytiques → agrégation pondérée.
Chaque agrégat est accompagné de sa **couverture** (part de la poche
obligataire réellement calculée) : une duration portefeuille calculée sur
70 % de l'encours n'est pas la duration du portefeuille, et l'UI doit le dire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from config.constants import KEY_RATE_TENORS, TENOR_YEARS
from models.enums import AssetClass, DataQuality
from models.portfolio import Portfolio, Position
from services.fixed_income.bond_math import (
    ZeroCurve,
    bond_risk,
    cashflows,
    price_from_curve,
    effective_convexity,
    effective_duration,
    key_rate_durations,
    spread_duration,
    yield_from_price,
    years_to_maturity,
)


@dataclass
class PositionRisk:
    position: Position
    weight_in_sleeve: float
    weight_in_nav: float
    ttm: float
    ytm: float | None
    modified_duration: float | None
    convexity: float | None
    spread_duration: float | None
    key_rate_durations: dict[str, float] = field(default_factory=dict)
    quality: DataQuality = DataQuality.API_CONFIRMED
    note: str = ""

    @property
    def duration_contribution(self) -> float:
        return (self.modified_duration or 0.0) * self.weight_in_sleeve

    @property
    def dv01_contribution(self) -> float:
        """DV01 en devise de base pour la position entière."""
        return (self.modified_duration or 0.0) * self.position.market_value_base * 1e-4


@dataclass
class FixedIncomeAnalytics:
    sleeve_value: float
    nav: float
    modified_duration: float
    convexity: float
    spread_duration: float
    yield_to_maturity: float
    dv01: float
    key_rate_durations: dict[str, float]
    positions: list[PositionRisk]
    coverage: float
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_contribution_to_nav(self) -> float:
        """Duration du portefeuille total (poche obligataire diluée dans la NAV)."""
        return self.modified_duration * (self.sleeve_value / self.nav if self.nav else 0.0)

    def krd_table(self) -> list[dict[str, float | str]]:
        return [{"tenor": t, "krd": v} for t, v in self.key_rate_durations.items()]

    def breakdown(self, attr: str) -> dict[str, float]:
        """Contribution à la duration par pays, devise, secteur…"""
        out: dict[str, float] = {}
        for pr in self.positions:
            k = getattr(pr.position, attr, None) or "N/A"
            k = k.value if hasattr(k, "value") else str(k)
            out[k] = out.get(k, 0.0) + pr.duration_contribution
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _position_curve(position: Position, curves: dict[str, ZeroCurve]) -> ZeroCurve | None:
    for key in (position.country, position.currency, "DEFAULT"):
        if key and key in curves:
            return curves[key]
    return next(iter(curves.values()), None)


def analyse_position(position: Position, curves: dict[str, ZeroCurve] | None = None,
                     settlement: date | None = None) -> PositionRisk:
    """Calcule les risques d'une ligne obligataire.

    Deux chemins :
    * si une courbe zéro-coupon est disponible → duration/convexité effectives
      par repricing, et KRD par bump triangulaire (méthode de référence) ;
    * sinon → formules analytiques sur le rendement actuariel, marquées
      ESTIMATED lorsque le rendement lui-même a dû être déduit du prix.
    """
    curves = curves or {}
    note = ""
    quality = DataQuality.API_CONFIRMED

    # Priorité aux sensibilités fournies par une source de référence (Bloomberg) :
    # elles intègrent les optionalités que notre noyau ne price pas, et elles
    # coïncident avec ce que le gérant lit dans son Terminal.
    if position.metadata.get("risk_source") == "BLOOMBERG" and position.modified_duration:
        return PositionRisk(
            position=position, weight_in_sleeve=0.0, weight_in_nav=0.0,
            ttm=years_to_maturity(position.maturity) if position.maturity else 0.0,
            ytm=position.ytm, modified_duration=position.modified_duration,
            convexity=position.convexity or 0.0,
            spread_duration=position.spread_duration,
            key_rate_durations=dict(position.key_rate_durations),
            quality=DataQuality.API_CONFIRMED,
            note="Sensibilités calculées par Bloomberg (YAS) — option-adjusted",
        )

    if not position.maturity or position.coupon is None:
        return PositionRisk(position=position, weight_in_sleeve=0.0, weight_in_nav=0.0,
                            ttm=0.0, ytm=None, modified_duration=None, convexity=None,
                            spread_duration=None, quality=DataQuality.UNVERIFIED,
                            note="Maturité ou coupon manquant — position exclue des calculs de duration")

    ttm = years_to_maturity(position.maturity, settlement)
    if ttm <= 0:
        return PositionRisk(position=position, weight_in_sleeve=0.0, weight_in_nav=0.0,
                            ttm=0.0, ytm=0.0, modified_duration=0.0, convexity=0.0,
                            spread_duration=0.0, quality=DataQuality.API_CONFIRMED,
                            note="Titre arrivé à échéance")

    face = 100.0
    price = position.price if position.price and position.price > 0 else None
    ytm = position.ytm
    curve = _position_curve(position, curves)

    if ytm is None and price is None and curve is not None:
        # Ni prix ni rendement : on valorise sur la courbe de marché, augmentée
        # du spread fourni. Le prix obtenu est un prix théorique, pas une cotation.
        flows_tmp = cashflows(face, position.coupon, ttm, position.frequency)
        price = price_from_curve(flows_tmp, curve, (position.spread_bp or 0.0) * 1e-4)
        quality = DataQuality.ESTIMATED
        note = f"Aucun prix fourni : valorisation théorique sur courbe {curve.label}"

    if ytm is None and price is not None:
        try:
            ytm = yield_from_price(price, face, position.coupon, ttm, position.frequency)
            quality = DataQuality.ESTIMATED
            note = note or "Rendement déduit du prix (pas de YTM fourni)"
        except ValueError:
            ytm = None
    if ytm is None:
        return PositionRisk(position=position, weight_in_sleeve=0.0, weight_in_nav=0.0,
                            ttm=ttm, ytm=None, modified_duration=None, convexity=None,
                            spread_duration=None, quality=DataQuality.UNVERIFIED,
                            note="Ni rendement ni prix exploitable")

    risk = bond_risk(face, position.coupon, ttm, ytm, position.frequency)
    flows = cashflows(face, position.coupon, ttm, position.frequency)

    krd: dict[str, float] = {}
    mod_dur, cvx, spr_dur = risk.modified_duration, risk.convexity, None

    if curve is not None:
        spread = (position.spread_bp or 0.0) * 1e-4
        # Le spread est calibré pour que la courbe reproduise le prix observé.
        mod_dur = effective_duration(flows, curve, spread)
        cvx = effective_convexity(flows, curve, spread)
        spr_dur = spread_duration(flows, curve, spread)
        raw = key_rate_durations(flows, curve, spread)
        for tenor in KEY_RATE_TENORS:
            years = TENOR_YEARS[tenor]
            if years in raw:
                krd[tenor] = raw[years]
        note = (note + " | " if note else "") + f"Sensibilités par repricing sur courbe {curve.label}"
    else:
        spr_dur = risk.modified_duration * 0.95
        note = (note + " | " if note else "") + "Pas de courbe disponible : sensibilités analytiques (approximation)"
        quality = DataQuality.ESTIMATED

    return PositionRisk(position=position, weight_in_sleeve=0.0, weight_in_nav=0.0, ttm=ttm,
                        ytm=ytm, modified_duration=mod_dur, convexity=cvx,
                        spread_duration=spr_dur, key_rate_durations=krd,
                        quality=quality, note=note)


def analyse_portfolio(portfolio: Portfolio, curves: dict[str, ZeroCurve] | None = None,
                      settlement: date | None = None) -> FixedIncomeAnalytics:
    bonds = portfolio.positions_of(AssetClass.GOV_BOND, AssetClass.CREDIT)
    sleeve_value = sum(p.market_value_base for p in bonds)
    nav = portfolio.nav
    warnings: list[str] = []

    risks = [analyse_position(p, curves, settlement) for p in bonds]
    computed = [r for r in risks if r.modified_duration is not None]
    computed_value = sum(r.position.market_value_base for r in computed)

    for r in risks:
        r.weight_in_sleeve = (r.position.market_value_base / computed_value) if computed_value else 0.0
        r.weight_in_nav = (r.position.market_value_base / nav) if nav else 0.0

    excluded = [r for r in risks if r.modified_duration is None]
    if excluded:
        warnings.append(
            f"{len(excluded)} ligne(s) exclue(s) des calculs de duration "
            f"({sum(r.position.market_value_base for r in excluded) / sleeve_value:.1%} de la poche) : "
            + ", ".join(r.position.identifier for r in excluded[:5])
        )

    def wavg(getter) -> float:
        return sum(r.weight_in_sleeve * (getter(r) or 0.0) for r in computed)

    krd_total: dict[str, float] = {t: 0.0 for t in KEY_RATE_TENORS}
    for r in computed:
        for tenor, value in r.key_rate_durations.items():
            krd_total[tenor] = krd_total.get(tenor, 0.0) + r.weight_in_sleeve * value

    analytics = FixedIncomeAnalytics(
        sleeve_value=sleeve_value,
        nav=nav,
        modified_duration=wavg(lambda r: r.modified_duration),
        convexity=wavg(lambda r: r.convexity),
        spread_duration=wavg(lambda r: r.spread_duration),
        yield_to_maturity=wavg(lambda r: r.ytm),
        dv01=sum(r.dv01_contribution for r in computed),
        key_rate_durations=krd_total,
        positions=risks,
        coverage=(computed_value / sleeve_value) if sleeve_value else 0.0,
        warnings=warnings,
    )
    return analytics
