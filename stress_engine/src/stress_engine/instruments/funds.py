"""Fonds : transparence (look-through), profil proxy, fonds monétaires ; lignes simples.

* **Transparence** : chaque ligne de la composition est projetée comme une ligne directe, puis mise à
  l'échelle pour peser ``poids × valeur du fonds``. Les poids sont renormalisés s'ils ne somment
  pas à 100 % (écart signalé).
* **Proxy** (pas de composition) : profil déclaré dans ``fund_proxies.yaml`` — duration répartie sur
  les deux piliers qui l'encadrent (interpolation linéaire, comme une obligation zéro-coupon de cette
  maturité), spread duration sur le facteur crédit, bêtas actions, change. Marqué « proxy ».
* **Fonds monétaire** : ``DV01 = VM × WAM / 365`` sur le taux monétaire, ``CS01 = VM × WAL / 365`` sur
  le crédit bancaire court (la WAL mesure la durée d'exposition au spread des titres à taux variable).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from itertools import pairwise
from typing import Any

from stress_engine.exposures import Exposure, MappingMethod, PositionMapping, merge
from stress_engine.factors import FactorSpace, cs_id, ir_id
from stress_engine.portfolio import Holding


class FundMappingError(ValueError):
    """Fonds impossible à projeter."""


def fund_value(holding: Holding) -> float:
    if holding.market_value is not None:
        return float(holding.market_value)
    if holding.nominal is not None and holding.price is not None:
        return float(holding.nominal) * float(holding.price)
    raise FundMappingError("valorisation absente (market_value, ou parts × prix)")


def pillar_weights(maturity: float, pillars: Sequence[float]) -> dict[float, float]:
    """Poids des deux piliers encadrant ``maturity`` (tentes linéaires, plates aux extrémités)."""
    ps = sorted(pillars)
    if maturity <= ps[0]:
        return {ps[0]: 1.0}
    if maturity >= ps[-1]:
        return {ps[-1]: 1.0}
    for lo, hi in pairwise(ps):
        if lo <= maturity <= hi:
            w = (maturity - lo) / (hi - lo)
            return {k: v for k, v in ((lo, 1.0 - w), (hi, w)) if v > 0}
    return {ps[-1]: 1.0}  # pragma: no cover


def map_proxy(holding: Holding, space: FactorSpace, profile: Mapping[str, Any], value_base: float,
              fx_factor: str | None) -> PositionMapping:
    exposures: list[Exposure] = []
    notes = [f"profil proxy {holding.proxy} ({profile.get('description', '')}) : hypothèse, pas de composition"]
    duration = float(profile.get("duration", 0.0) or 0.0)
    if duration:
        country, proxied = space.curve_country(profile.get("country"))
        if proxied:
            notes.append(f"pays {profile.get('country')} du profil sans courbe : facteurs {country}")
        for pillar, w in pillar_weights(duration, space.pillars).items():
            exposures.append(Exposure(ir_id(country, pillar), -value_base * duration * w * 1e-4))
    sd = float(profile.get("spread_duration", 0.0) or 0.0)
    if sd:
        factor, cnotes = space.credit_factor(profile.get("rating"), profile.get("sector"))
        notes += cnotes
        exposures.append(Exposure(factor, -value_base * sd * 1e-4))
    for index, beta in (profile.get("equity") or {}).items():
        space.ensure(f"EQ.{index}")
        exposures.append(Exposure(f"EQ.{index}", value_base * float(beta)))
    if fx_factor:
        exposures.append(Exposure(fx_factor, value_base))
    return PositionMapping(holding=holding, method=MappingMethod.PROXY, market_value=value_base,
                           exposure_value=value_base, exposures=merge(exposures),
                           convexity_value=0.0, details={"profile": dict(profile), "duration": duration,
                                                         "spread_duration": sd}, notes=notes)


def map_money_market(holding: Holding, space: FactorSpace, config: Mapping[str, Any], value_base: float,
                     fx_factor: str | None) -> PositionMapping:
    mm = config.get("money_market") or {}
    notes: list[str] = []
    wam = holding.wam_days
    if wam is None:
        wam = float(mm.get("default_wam_days", 45))
        notes.append(f"WAM absente : {wam:g} jours par défaut (fund_proxies.yaml)")
    wal = holding.wal_days
    if wal is None:
        wal = float(mm.get("default_wal_days", 90))
        notes.append(f"WAL absente : {wal:g} jours par défaut (fund_proxies.yaml)")
    if wal < wam:
        notes.append(f"WAL ({wal:g} j) inférieure à la WAM ({wam:g} j) : incohérent, WAL portée à la WAM")
        wal = wam
    credit = mm.get("credit_factor") or {"rating": "A", "sector": "FIN"}
    factor = cs_id(space.credit_currency, credit.get("rating", "A"), credit.get("sector", "FIN"))
    exposures = [Exposure(space.short_rate, -value_base * wam / 365.0 * 1e-4),
                 Exposure(factor, -value_base * wal / 365.0 * 1e-4)]
    if fx_factor:
        exposures.append(Exposure(fx_factor, value_base))
    return PositionMapping(holding=holding, method=MappingMethod.MONEY_MARKET, market_value=value_base,
                           exposure_value=value_base, exposures=exposures,
                           details={"wam_days": wam, "wal_days": wal}, notes=notes)


def map_lookthrough(holding: Holding, value_base: float, components: Sequence[tuple[Holding, float]],
                    map_child: Callable[[Holding], PositionMapping]) -> PositionMapping:
    """Somme des lignes de la composition, chacune ramenée à ``poids × valeur du fonds``."""
    total_w = sum(w for _, w in components)
    if total_w <= 0:
        raise FundMappingError(f"composition {holding.lookthrough} vide ou de poids nul")
    notes = []
    if abs(total_w - 1.0) > 0.005:
        notes.append(f"poids de la composition = {total_w:.2%} : renormalisés à 100 %")
    exposures: list[Exposure] = []
    children: list[PositionMapping] = []
    convexity = 0.0
    excluded_w = 0.0
    for child, w in components:
        target = value_base * w / total_w
        m = map_child(child)
        if m.method is MappingMethod.EXCLUDED or m.exposure_value == 0:
            excluded_w += w / total_w
            children.append(m)
            continue
        k = target / m.exposure_value
        exposures += [e.scaled(k, component=child.id) for e in m.exposures]
        convexity += m.convexity_value * k
        m.market_value *= k
        m.exposure_value *= k
        m.exposures = [e.scaled(k) for e in m.exposures]
        m.convexity_value *= k
        if m.pricing is not None and hasattr(m.pricing, "units"):
            m.pricing = replace(m.pricing, units=m.pricing.units * k)
        children.append(m)
    if excluded_w:
        notes.append(f"{excluded_w:.1%} de la composition non projetée (voir les lignes sous-jacentes)")
    return PositionMapping(holding=holding, method=MappingMethod.LOOKTHROUGH, market_value=value_base,
                           exposure_value=value_base, exposures=exposures, convexity_value=convexity,
                           details={"components": len(components), "weights_sum": total_w}, notes=notes,
                           children=children)


def map_direct(holding: Holding, space: FactorSpace, value_base: float, fx_factor: str | None,
               kind: str) -> PositionMapping:
    """Actions (bêta × indice), matières premières (1:1), cash (change seul)."""
    exposures: list[Exposure] = []
    notes: list[str] = []
    details: dict[str, Any] = {}
    if kind == "EQUITY":
        index = space.equity_index(holding.index, holding.region, holding.currency)
        beta = holding.beta
        if beta is None:
            beta = space.default_beta
            notes.append(f"bêta absent : {beta:g} par défaut (à estimer par régression en phase 5)")
        space.ensure(f"EQ.{index}")
        exposures.append(Exposure(f"EQ.{index}", value_base * beta))
        details.update(index=index, beta=beta)
    elif kind == "COMMODITY":
        name = (holding.index or holding.id).upper()
        space.ensure(f"CMD.{name}")
        exposures.append(Exposure(f"CMD.{name}", value_base))
    if fx_factor:
        exposures.append(Exposure(fx_factor, value_base))
    return PositionMapping(holding=holding, method=MappingMethod.DIRECT, market_value=value_base,
                           exposure_value=value_base, exposures=exposures, details=details, notes=notes)
