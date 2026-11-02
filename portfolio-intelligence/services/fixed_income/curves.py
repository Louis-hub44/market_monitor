"""Construction des courbes et déformations de courbe.

Les niveaux de taux proviennent toujours des providers (§20) : ce module ne
contient aucune valeur de marché en dur, uniquement des *formes* de scénario
(steepener, flattener…) qui sont, elles, des conventions de place.
"""

from __future__ import annotations

from config.constants import KEY_RATE_TENORS, TENOR_YEARS
from models.datapoint import DataPoint
from services.fixed_income.bond_math import ZeroCurve

# Déformations non parallèles standard, exprimées en points de base par segment.
CURVE_SHAPES: dict[str, dict[str, float]] = {
    "BEAR_STEEPENER": {"2Y": 25, "5Y": 40, "10Y": 60, "20Y": 70, "30Y": 75},
    "BULL_STEEPENER": {"2Y": -50, "5Y": -42, "10Y": -35, "20Y": -28, "30Y": -25},
    "BEAR_FLATTENER": {"2Y": 75, "5Y": 60, "10Y": 40, "20Y": 30, "30Y": 25},
    "BULL_FLATTENER": {"2Y": -50, "5Y": -42, "10Y": -35, "20Y": -28, "30Y": -25},
    "TWIST_STEEPENING": {"2Y": -25, "5Y": -10, "10Y": 10, "20Y": 20, "30Y": 25},
}

CURVE_SHAPE_LABELS = {
    "BEAR_STEEPENER": "Bear steepener — taux courts +25 bp, taux longs +75 bp",
    "BULL_STEEPENER": "Bull steepener — taux courts -50 bp, taux longs -25 bp",
    "BEAR_FLATTENER": "Bear flattener — taux courts +75 bp, taux longs +25 bp",
    "BULL_FLATTENER": "Bull flattener — taux courts -50 bp, taux longs -25 bp",
    "TWIST_STEEPENING": "Twist — pivot autour du 5-7 ans",
}


def curve_from_tenors(rates: dict[str, float], label: str = "curve") -> ZeroCurve:
    """Convertit {'10Y': 0.0428} en courbe indexée par maturité en années."""
    nodes = {TENOR_YEARS[t]: r for t, r in rates.items() if t in TENOR_YEARS}
    if not nodes:
        raise ValueError("Aucun tenor reconnu dans la courbe fournie")
    # On garantit la présence des nœuds clés utilisés par les KRD.
    base = ZeroCurve(nodes, label)
    complete = {TENOR_YEARS[t]: base.rate(TENOR_YEARS[t]) for t in KEY_RATE_TENORS}
    complete.update(nodes)
    return ZeroCurve(complete, label)


def build_curves(market_curves: dict[str, DataPoint[dict[str, float]]]) -> dict[str, ZeroCurve]:
    """Assemble les courbes utilisables et les indexe par pays *et* par devise.

    Les courbes non disponibles sont simplement absentes : aucune courbe
    n'est fabriquée par interpolation d'une autre devise.
    """
    from config.constants import SOVEREIGN_CURVES

    curves: dict[str, ZeroCurve] = {}
    for code, dp in market_curves.items():
        if not dp.is_usable or not dp.value:
            continue
        try:
            curve = curve_from_tenors(dp.value, label=SOVEREIGN_CURVES.get(code, {}).get("name", code))
        except ValueError:
            continue
        curves[code] = curve
        ccy = SOVEREIGN_CURVES.get(code, {}).get("currency")
        if ccy and ccy not in curves:
            curves[ccy] = curve
    if curves and "DEFAULT" not in curves:
        curves["DEFAULT"] = next(iter(curves.values()))
    return curves


def shape_to_bumps(shape: dict[str, float]) -> dict[float, float]:
    """{'10Y': 60} (bp) -> {10.0: 0.0060} (décimal), pour ZeroCurve.shifted."""
    return {TENOR_YEARS[t]: bp * 1e-4 for t, bp in shape.items() if t in TENOR_YEARS}


def interpolate_shape(shape: dict[str, float], tenors: tuple[str, ...] = KEY_RATE_TENORS) -> dict[str, float]:
    """Complète une forme de courbe sur tous les tenors clés (interpolation linéaire)."""
    known = sorted(((TENOR_YEARS[t], bp) for t, bp in shape.items() if t in TENOR_YEARS))
    if not known:
        return {t: 0.0 for t in tenors}

    def at(x: float) -> float:
        if x <= known[0][0]:
            return known[0][1]
        if x >= known[-1][0]:
            return known[-1][1]
        for i in range(1, len(known)):
            if x <= known[i][0]:
                (x0, y0), (x1, y1) = known[i - 1], known[i]
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return known[-1][1]

    return {t: at(TENOR_YEARS[t]) for t in tenors}
