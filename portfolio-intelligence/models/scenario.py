"""Modèle de domaine des scénarios et de leurs résultats.

Le point clé est l'explicabilité (§23) : un `ScenarioResult` n'est pas un
nombre, c'est un arbre de contributions dans lequel chaque feuille porte la
formule qui l'a produite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.enums import AssetClass, Confidence, ShockType


@dataclass(frozen=True)
class Shock:
    """Choc élémentaire sur un facteur de risque."""

    shock_type: ShockType
    magnitude: float                 # % en décimal, ou bp selon le type
    target: str = "ALL"              # indice, devise, segment de courbe, secteur…
    curve: dict[str, float] | None = None   # {tenor: bp} pour RATES_CURVE
    description: str = ""

    def label(self) -> str:
        if self.shock_type in (ShockType.RATES_PARALLEL, ShockType.CREDIT_SPREAD):
            return f"{self.target} {self.magnitude:+.0f} bp"
        if self.shock_type is ShockType.RATES_CURVE:
            parts = ", ".join(f"{k} {v:+.0f}bp" for k, v in (self.curve or {}).items())
            return f"Courbe {self.target} ({parts})"
        return f"{self.target} {self.magnitude:+.1%}"


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    shocks: tuple[Shock, ...]
    family: str = "CUSTOM"           # RATES / EQUITY / FX / MACRO / CUSTOM
    narrative: str = ""

    def labels(self) -> list[str]:
        return [s.label() for s in self.shocks]


@dataclass
class Contribution:
    """Impact d'un choc sur une position, avec sa justification."""

    position_id: str
    position_name: str
    asset_class: AssetClass
    weight: float
    impact_pct: float                # impact sur la NAV totale, en décimal
    shock_label: str
    formula: str                     # ex. "-D×Δy + ½C×Δy² = -5.80×0.0100 + …"
    confidence: Confidence = Confidence.MEDIUM
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    scenario: Scenario
    total_impact_pct: float
    contributions: list[Contribution]
    confidence: Confidence
    coverage: float = 1.0            # part de la NAV effectivement modélisée
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- agrégations utilisées par l'UI ------------------------------- #
    def by(self, attr: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in self.contributions:
            k = getattr(c, attr)
            k = k.value if hasattr(k, "value") else str(k)
            out[k] = out.get(k, 0.0) + c.impact_pct
        return dict(sorted(out.items(), key=lambda kv: kv[1]))

    def by_asset_class(self) -> dict[str, float]:
        return self.by("asset_class")

    def by_shock(self) -> dict[str, float]:
        return self.by("shock_label")

    def top_contributors(self, n: int = 10) -> list[Contribution]:
        return sorted(self.contributions, key=lambda c: c.impact_pct)[:n]

    def explain(self) -> str:
        """Résumé texte auditable du calcul (utilisé aussi dans les exports)."""
        lines = [f"Scénario : {self.scenario.name}", f"Impact portefeuille : {self.total_impact_pct:+.2%}", ""]
        for k, v in self.by_asset_class().items():
            lines.append(f"  {k:<16} {v:+.2%}")
        if self.coverage < 0.999:
            lines.append(f"\nCouverture du calcul : {self.coverage:.1%} de la NAV")
        if self.assumptions:
            lines.append("\nHypothèses :")
            lines.extend(f"  - {a}" for a in self.assumptions)
        return "\n".join(lines)
