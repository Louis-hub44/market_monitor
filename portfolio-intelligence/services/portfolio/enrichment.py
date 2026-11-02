"""Enrichissement du portefeuille par les analytiques Bloomberg.

Quand le Terminal est disponible, Bloomberg fournit ses **propres** mesures de
risque : duration ajustée, convexité, duration de spread, key rate durations,
Z-spread. Elles sont préférables à notre calcul interne pour deux raisons :

* elles intègrent les optionalités (call, put, step-up, FRN) que notre noyau
  ne traite pas — c'est la limite documentée de `bond_math.py` ;
* elles sont calculées sur la courbe Bloomberg, donc cohérentes avec ce que le
  gérant voit dans son Terminal. Un écart de duration entre l'outil et YAS<GO>
  est une source de défiance immédiate, même quand les deux sont défendables.

Le module ne remplace jamais une valeur par une valeur *moins* fiable : si un
champ revient vide (droits, titre exotique), la position garde sa sensibilité
calculée en interne, et la ligne est comptée dans le rapport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from config.constants import KEY_RATE_TENORS
from models.enums import AssetClass, DataQuality
from models.portfolio import Portfolio, Position

#: Champs Bloomberg → attribut de `Position`, par ordre de préférence.
#: YAS_MOD_DUR vient de l'écran YAS (option-adjusted) ; DUR_ADJ_MID est le repli
#: analytique quand le titre n'a pas d'optionalité.
DURATION_FIELDS = ("YAS_MOD_DUR", "DUR_ADJ_MID")
CONVEXITY_FIELDS = ("CONVEXITY_MID",)
SPREAD_DURATION_FIELDS = ("SPREAD_DUR_MID", "YAS_RISK")
SPREAD_FIELDS = ("OAS_SPREAD_MID", "Z_SPRD_MID")
YIELD_FIELDS = ("YLD_YTM_MID",)


@dataclass
class EnrichmentReport:
    enriched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    fields_used: set[str] = field(default_factory=set)
    krd_coverage: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.enriched)

    def summary(self, total: int) -> str:
        if not self.enriched:
            return "Aucune sensibilité Bloomberg appliquée."
        text = (f"{len(self.enriched)}/{total} lignes obligataires enrichies par Bloomberg "
                f"(duration, convexité, spread)")
        if self.krd_coverage:
            text += f", dont {self.krd_coverage} avec key rate durations"
        return text + "."


def _first(fields: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        value = fields.get(name)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def apply_bond_analytics(position: Position, fields: dict[str, Any]) -> bool:
    """Applique les champs Bloomberg à une position. Renvoie True si utile."""
    from data.providers.bloomberg import extract_krd

    touched = False

    duration = _first(fields, DURATION_FIELDS)
    if duration is not None:
        position.modified_duration = duration
        touched = True

    convexity = _first(fields, CONVEXITY_FIELDS)
    if convexity is not None:
        position.convexity = convexity
        touched = True

    spread_duration = _first(fields, SPREAD_DURATION_FIELDS)
    if spread_duration is not None:
        position.spread_duration = spread_duration
        touched = True

    spread = _first(fields, SPREAD_FIELDS)
    if spread is not None:
        position.spread_bp = spread
        touched = True

    ytm = _first(fields, YIELD_FIELDS)
    if ytm is not None:
        # Bloomberg publie les rendements en pourcents.
        position.ytm = ytm / 100.0
        touched = True

    krd = extract_krd(fields)
    if krd:
        position.key_rate_durations = {t: krd[t] for t in KEY_RATE_TENORS if t in krd}
        touched = True

    # Caractéristiques contractuelles : elles complètent un fichier incomplet,
    # sans jamais écraser ce que le gérant a fourni.
    if position.coupon is None and fields.get("CPN") is not None:
        try:
            position.coupon = float(fields["CPN"]) / 100.0
            touched = True
        except (TypeError, ValueError):
            pass
    if position.maturity is None:
        maturity = _as_date(fields.get("MATURITY"))
        if maturity:
            position.maturity = maturity
            touched = True
    if fields.get("CPN_FREQ") is not None:
        try:
            position.frequency = int(float(fields["CPN_FREQ"])) or position.frequency
        except (TypeError, ValueError):
            pass
    if position.issuer_rating is None:
        position.issuer_rating = fields.get("RTG_SP") or fields.get("RTG_MOODY")
    if position.country is None and fields.get("COUNTRY_ISO"):
        position.country = str(fields["COUNTRY_ISO"])

    if touched:
        position.quality = DataQuality.API_CONFIRMED
        position.metadata["risk_source"] = "BLOOMBERG"
        if fields.get("NXT_CALL_DT"):
            position.metadata["next_call"] = str(fields["NXT_CALL_DT"])[:10]
    return touched


def enrich_bond_risks(portfolio: Portfolio, market_data) -> EnrichmentReport:
    """Récupère les sensibilités Bloomberg et les applique au portefeuille."""
    report = EnrichmentReport()
    bonds = portfolio.positions_of(AssetClass.GOV_BOND, AssetClass.CREDIT)
    if not bonds:
        return report

    analytics = market_data.bond_analytics(bonds)
    if not analytics:
        return report

    for position in bonds:
        fields = analytics.get(position.identifier)
        if not fields:
            report.skipped.append(position.identifier)
            continue
        if apply_bond_analytics(position, fields):
            report.enriched.append(position.identifier)
            report.fields_used.update(fields)
            if position.key_rate_durations:
                report.krd_coverage += 1
        else:
            report.skipped.append(position.identifier)
    return report
