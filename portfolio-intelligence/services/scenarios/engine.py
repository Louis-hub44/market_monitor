"""Moteur de scénarios commun aux trois modules.

    Portefeuille → expositions → choc → impact actif → impact position → impact total

Principe directeur : **aucune boîte noire**. Chaque impact élémentaire est
enregistré sous forme de `Contribution` portant la formule appliquée, les
entrées utilisées et le niveau de confiance associé. Le total n'est jamais
qu'une somme de contributions consultables.

Hiérarchie des sensibilités, de la meilleure à la moins bonne :
1. sensibilité calculée sur données réelles (duration par repricing, bêta par
   régression sur historiques) ;
2. sensibilité analytique (duration/convexité à partir du rendement) ;
3. hypothèse de modèle documentée (`config/constants.py`) → confiance LOW.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.constants import KEY_RATE_TENORS, load_model_assumptions
from models.enums import AssetClass, Confidence, ShockType
from models.portfolio import Portfolio, Position
from models.scenario import Contribution, Scenario, ScenarioResult, Shock
from services.fixed_income.analytics import FixedIncomeAnalytics, PositionRisk
from services.fixed_income.curves import interpolate_shape


@dataclass
class SensitivitySet:
    """Sensibilités connues du portefeuille, alimentées par les autres services."""

    equity_betas: dict[str, float] = field(default_factory=dict)      # identifiant -> bêta estimé
    beta_quality: dict[str, str] = field(default_factory=dict)        # identifiant -> "REGRESSION"/"DEFAULT"
    fi_risks: dict[str, PositionRisk] = field(default_factory=dict)   # identifiant -> risques obligataires
    commodity_betas: dict[str, float] = field(default_factory=dict)   # identifiant -> bêta pétrole estimé

    @classmethod
    def from_fixed_income(cls, analytics: FixedIncomeAnalytics | None) -> "SensitivitySet":
        s = cls()
        if analytics:
            s.fi_risks = {r.position.identifier: r for r in analytics.positions}
        return s


class ScenarioEngine:
    def __init__(self, portfolio: Portfolio, sensitivities: SensitivitySet | None = None) -> None:
        self.portfolio = portfolio
        self.sens = sensitivities or SensitivitySet()
        self.assumptions = load_model_assumptions()

    # ------------------------------------------------------------------ #
    # Sensibilités élémentaires
    # ------------------------------------------------------------------ #
    def _beta(self, position: Position) -> tuple[float, bool]:
        """(bêta, est_une_hypothèse_par_défaut)."""
        if position.beta is not None:
            return position.beta, False
        if position.identifier in self.sens.equity_betas:
            return self.sens.equity_betas[position.identifier], False
        return float(self.assumptions["default_equity_beta"]), True

    def _sector_param(self, position: Position, table: str, default: float = 0.0) -> float:
        return float(self.assumptions.get(table, {}).get(position.sector or "", default))

    # ------------------------------------------------------------------ #
    # Impact d'un choc sur une position
    # ------------------------------------------------------------------ #
    def _impact(self, position: Position, shock: Shock) -> tuple[float, str, Confidence, dict] | None:
        """Renvoie (rendement de l'actif, formule, confiance, entrées) ou None."""
        ac = position.asset_class
        risk = self.sens.fi_risks.get(position.identifier)

        # ---------------- Actions -------------------------------------- #
        if ac == AssetClass.EQUITY:
            if shock.shock_type is ShockType.EQUITY:
                beta, is_default = self._beta(position)
                r = beta * shock.magnitude
                conf = Confidence.LOW if is_default else Confidence.HIGH
                return r, f"β × Δindice = {beta:.2f} × {shock.magnitude:+.1%}", conf, {"beta": beta}

            if shock.shock_type in (ShockType.RATES_PARALLEL, ShockType.RATES_CURVE):
                bp = shock.magnitude if shock.shock_type is ShockType.RATES_PARALLEL else \
                    sum((shock.curve or {}).values()) / max(len(shock.curve or {}), 1)
                sens = self._sector_param(position, "sector_rates_beta_100bp")
                if sens == 0.0:
                    return None
                r = sens * bp / 100.0
                return (r, f"sensibilité secteur ({position.sector}) {sens:+.3f} pour 100 bp × {bp:+.0f} bp",
                        Confidence.LOW, {"sector_sensitivity": sens})

            if shock.shock_type is ShockType.COMMODITY:
                sens = self.sens.commodity_betas.get(
                    position.identifier, self._sector_param(position, "sector_oil_beta")
                )
                if sens == 0.0:
                    return None
                r = sens * shock.magnitude
                conf = Confidence.MEDIUM if position.identifier in self.sens.commodity_betas else Confidence.LOW
                return r, f"β pétrole ({position.sector}) {sens:+.2f} × {shock.magnitude:+.1%}", conf, {"beta_oil": sens}

            if shock.shock_type is ShockType.CREDIT_SPREAD:
                sens = float(self.assumptions["equity_credit_beta_100bp"])
                r = sens * shock.magnitude / 100.0
                return (r, f"contagion crédit→actions {sens:+.3f} pour 100 bp × {shock.magnitude:+.0f} bp",
                        Confidence.LOW, {"credit_beta": sens})

        # ---------------- Obligations ---------------------------------- #
        if ac in (AssetClass.GOV_BOND, AssetClass.CREDIT):
            if shock.shock_type is ShockType.RATES_PARALLEL:
                if not risk or risk.modified_duration is None:
                    return None
                dy = shock.magnitude * 1e-4
                d, c = risk.modified_duration, (risk.convexity or 0.0)
                r = -d * dy + 0.5 * c * dy ** 2
                return (r,
                        f"-D×Δy + ½C×Δy² = -{d:.2f}×{dy:.4f} + ½×{c:.1f}×{dy:.4f}²",
                        Confidence.HIGH if risk.quality.rank <= 1 else Confidence.MEDIUM,
                        {"duration": d, "convexity": c, "dy_bp": shock.magnitude})

            if shock.shock_type is ShockType.RATES_CURVE:
                if not risk:
                    return None
                shape = interpolate_shape(shock.curve or {})
                if risk.key_rate_durations:
                    terms = [(t, risk.key_rate_durations.get(t, 0.0), shape.get(t, 0.0)) for t in KEY_RATE_TENORS]
                    r = -sum(krd * bp * 1e-4 for _, krd, bp in terms)
                    detail = " ".join(f"{t}:{krd:.2f}×{bp:+.0f}bp" for t, krd, bp in terms if abs(krd) > 1e-6)
                    return r, f"-Σ KRDₜ × Δyₜ = {detail}", Confidence.HIGH, {"krd": risk.key_rate_durations}
                if risk.modified_duration is not None:
                    # Repli : on applique le choc du segment le plus proche de la duration.
                    nearest = min(KEY_RATE_TENORS,
                                  key=lambda t: abs({"3M": .25, "1Y": 1, "2Y": 2, "5Y": 5, "10Y": 10,
                                                     "20Y": 20, "30Y": 30}[t] - (risk.modified_duration or 0)))
                    bp = shape.get(nearest, 0.0)
                    r = -risk.modified_duration * bp * 1e-4
                    return (r, f"-D × Δy({nearest}) = -{risk.modified_duration:.2f} × {bp:+.0f} bp",
                            Confidence.LOW, {"tenor": nearest, "bp": bp})
                return None

            if shock.shock_type is ShockType.CREDIT_SPREAD and ac is AssetClass.CREDIT:
                if not risk:
                    return None
                sd = risk.spread_duration
                if sd is None and risk.modified_duration is not None:
                    sd = risk.modified_duration * float(self.assumptions["spread_duration_ratio"])
                if sd is None:
                    return None
                ds = shock.magnitude * 1e-4
                return (-sd * ds, f"-SD×Δs = -{sd:.2f}×{ds:.4f}", Confidence.MEDIUM, {"spread_duration": sd})

        # ---------------- Matières premières --------------------------- #
        if ac is AssetClass.COMMODITY and shock.shock_type is ShockType.COMMODITY:
            return shock.magnitude, "exposition directe (1:1)", Confidence.HIGH, {}

        # ---------------- Change --------------------------------------- #
        if shock.shock_type is ShockType.FX:
            base = self.portfolio.base_currency
            target = shock.target.upper()
            if target == base:
                # Appréciation de la devise de base : les actifs en devises perdent.
                if position.currency == base:
                    return None
                r = -shock.magnitude / (1 + shock.magnitude)
                return (r, f"actif en {position.currency}, {base} {shock.magnitude:+.1%} ⇒ 1/(1+x)-1",
                        Confidence.HIGH, {"currency": position.currency})
            if position.currency.upper() == target:
                return (shock.magnitude, f"exposition directe {target} {shock.magnitude:+.1%}",
                        Confidence.HIGH, {"currency": target})
            return None

        return None

    # ------------------------------------------------------------------ #
    def run(self, scenario: Scenario) -> ScenarioResult:
        nav = self.portfolio.nav
        contributions: list[Contribution] = []
        confidences: list[Confidence] = []
        touched_value = 0.0
        assumptions_used: set[str] = set()

        for position in self.portfolio.positions:
            weight = position.market_value_base / nav if nav else 0.0
            touched = False
            for shock in scenario.shocks:
                outcome = self._impact(position, shock)
                if outcome is None:
                    continue
                asset_return, formula, confidence, inputs = outcome
                if abs(asset_return) < 1e-12:
                    continue
                touched = True
                contributions.append(
                    Contribution(
                        position_id=position.identifier,
                        position_name=position.name,
                        asset_class=position.asset_class,
                        weight=weight,
                        impact_pct=weight * asset_return,
                        shock_label=shock.label(),
                        formula=f"{formula} = {asset_return:+.2%} × poids {weight:.2%}",
                        confidence=confidence,
                        inputs=inputs,
                    )
                )
                confidences.append(confidence)
                if confidence is Confidence.LOW:
                    assumptions_used.add(formula.split("=")[0].strip())
            if touched:
                touched_value += position.market_value_base

        total = sum(c.impact_pct for c in contributions)
        n_low = sum(1 for c in confidences if c is Confidence.LOW)
        if not confidences:
            overall = Confidence.LOW
        elif n_low / len(confidences) > 0.5:
            overall = Confidence.LOW
        elif any(c is Confidence.LOW for c in confidences):
            overall = Confidence.MEDIUM
        else:
            overall = Confidence.HIGH

        warnings: list[str] = []
        coverage = touched_value / nav if nav else 0.0
        if coverage < 0.95:
            warnings.append(
                f"{1 - coverage:.0%} de la NAV n'est pas sensible à ce scénario "
                "ou ne dispose pas des sensibilités nécessaires."
            )

        return ScenarioResult(
            scenario=scenario,
            total_impact_pct=total,
            contributions=contributions,
            confidence=overall,
            coverage=coverage,
            assumptions=sorted(assumptions_used),
            warnings=warnings,
        )

    def run_all(self, scenarios: list[Scenario]) -> list[ScenarioResult]:
        return [self.run(s) for s in scenarios]

    def worst_scenarios(self, scenarios: list[Scenario], n: int = 8) -> list[ScenarioResult]:
        """« Qu'est-ce qui peut faire mal à mon portefeuille ? » (§17)."""
        results = self.run_all(scenarios)
        return sorted(results, key=lambda r: r.total_impact_pct)[:n]
