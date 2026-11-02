"""Tests du moteur de scénarios : cohérence, additivité, explicabilité."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from models.enums import AssetClass, Confidence, PortfolioType, ShockType
from models.portfolio import Portfolio, Position
from models.scenario import Scenario, Shock
from services.fixed_income.analytics import analyse_portfolio
from services.fixed_income.bond_math import ZeroCurve
from services.scenarios.engine import ScenarioEngine, SensitivitySet
from services.scenarios.library import stress_battery


@pytest.fixture
def curve() -> ZeroCurve:
    return ZeroCurve({0.25: 0.03, 1.0: 0.031, 2.0: 0.032, 5.0: 0.034,
                      10.0: 0.037, 20.0: 0.039, 30.0: 0.040}, "TEST")


@pytest.fixture
def portfolio() -> Portfolio:
    today = date.today()
    return Portfolio(
        name="test",
        portfolio_type=PortfolioType.MULTI_ASSET,
        base_currency="EUR",
        positions=[
            Position("EQ1", "Action EUR", AssetClass.EQUITY, market_value=400_000,
                     currency="EUR", sector="Technology", beta=1.2),
            Position("EQ2", "Action USD", AssetClass.EQUITY, market_value=200_000,
                     currency="USD", sector="Energy", beta=0.8),
            Position("GOV1", "Souverain 10 ans", AssetClass.GOV_BOND, market_value=300_000,
                     currency="EUR", country="DE", coupon=0.03,
                     maturity=today + timedelta(days=3652), frequency=1),
            Position("CR1", "Crédit 5 ans", AssetClass.CREDIT, market_value=100_000,
                     currency="EUR", country="FR", coupon=0.04,
                     maturity=today + timedelta(days=1826), frequency=1, spread_bp=120),
        ],
    )


@pytest.fixture
def engine(portfolio, curve) -> ScenarioEngine:
    analytics = analyse_portfolio(portfolio, {"DEFAULT": curve, "DE": curve, "FR": curve})
    return ScenarioEngine(portfolio, SensitivitySet.from_fixed_income(analytics))


# ---------------------------------------------------------------- #
def test_equity_shock_uses_beta_and_weight(engine, portfolio):
    scenario = Scenario("eq", "Actions -10 %", (Shock(ShockType.EQUITY, -0.10),))
    result = engine.run(scenario)
    expected = 0.4 * 1.2 * -0.10 + 0.2 * 0.8 * -0.10
    assert result.total_impact_pct == pytest.approx(expected, rel=1e-9)
    assert result.confidence is Confidence.HIGH


def test_rates_shock_hurts_bonds_and_scales_with_duration(engine):
    up = engine.run(Scenario("r", "+100 bp", (Shock(ShockType.RATES_PARALLEL, 100),)))
    small = engine.run(Scenario("r", "+25 bp", (Shock(ShockType.RATES_PARALLEL, 25),)))
    bonds_up = sum(v for k, v in up.by_asset_class().items() if k in ("GOV_BOND", "CREDIT"))
    bonds_small = sum(v for k, v in small.by_asset_class().items() if k in ("GOV_BOND", "CREDIT"))
    assert bonds_up < 0
    assert bonds_up < bonds_small                      # plus le choc est fort, plus la perte est grande
    assert bonds_up / bonds_small == pytest.approx(4, rel=0.05)   # quasi linéaire à ce niveau


def test_rate_cut_is_positive_for_bonds(engine):
    result = engine.run(Scenario("r", "-50 bp", (Shock(ShockType.RATES_PARALLEL, -50),)))
    assert sum(v for k, v in result.by_asset_class().items() if k in ("GOV_BOND", "CREDIT")) > 0


def test_convexity_makes_gains_exceed_losses(engine):
    """Pour un même choc en valeur absolue, la baisse de taux rapporte plus que la hausse ne coûte."""
    up = engine.run(Scenario("u", "+100", (Shock(ShockType.RATES_PARALLEL, 100),)))
    down = engine.run(Scenario("d", "-100", (Shock(ShockType.RATES_PARALLEL, -100),)))
    bonds_up = sum(v for k, v in up.by_asset_class().items() if k in ("GOV_BOND", "CREDIT"))
    bonds_down = sum(v for k, v in down.by_asset_class().items() if k in ("GOV_BOND", "CREDIT"))
    assert bonds_down > abs(bonds_up)


def test_credit_shock_only_touches_credit(engine):
    result = engine.run(Scenario("c", "+150 bp", (Shock(ShockType.CREDIT_SPREAD, 150, "CREDIT"),)))
    by_class = result.by_asset_class()
    assert by_class["CREDIT"] < 0
    assert "GOV_BOND" not in by_class


def test_fx_shock_on_base_currency_only_hits_foreign_positions(engine):
    result = engine.run(Scenario("fx", "EUR +10 %", (Shock(ShockType.FX, 0.10, "EUR"),)))
    assert len(result.contributions) == 1
    contribution = result.contributions[0]
    assert contribution.position_id == "EQ2"
    assert contribution.impact_pct == pytest.approx(0.2 * (-0.10 / 1.10), rel=1e-9)


def test_total_equals_sum_of_contributions(engine):
    for scenario in stress_battery("EUR"):
        result = engine.run(scenario)
        assert result.total_impact_pct == pytest.approx(
            sum(c.impact_pct for c in result.contributions), rel=1e-12
        )


def test_composite_scenario_equals_sum_of_its_shocks(engine):
    combined = Scenario("mix", "Actions -10 % et taux +100 bp",
                        (Shock(ShockType.EQUITY, -0.10), Shock(ShockType.RATES_PARALLEL, 100)))
    a = engine.run(Scenario("a", "a", (Shock(ShockType.EQUITY, -0.10),)))
    b = engine.run(Scenario("b", "b", (Shock(ShockType.RATES_PARALLEL, 100),)))
    assert engine.run(combined).total_impact_pct == pytest.approx(
        a.total_impact_pct + b.total_impact_pct, rel=1e-9
    )


def test_every_contribution_carries_a_formula(engine):
    result = engine.run(Scenario("r", "+100 bp", (Shock(ShockType.RATES_PARALLEL, 100),)))
    assert result.contributions
    for contribution in result.contributions:
        assert contribution.formula
        assert contribution.shock_label
        assert contribution.confidence in Confidence


def test_curve_scenario_uses_key_rate_durations(engine):
    bear = Scenario("bear", "Bear steepener",
                    (Shock(ShockType.RATES_CURVE, 0.0, "ALL",
                           curve={"2Y": 25, "5Y": 40, "10Y": 60, "30Y": 75}),))
    result = engine.run(bear)
    assert result.total_impact_pct < 0
    bond_contributions = [c for c in result.contributions if c.asset_class.is_rate_sensitive]
    assert bond_contributions
    assert any("KRD" in c.formula for c in bond_contributions)


def test_worst_scenarios_are_sorted_by_severity(engine):
    worst = engine.worst_scenarios(stress_battery("EUR"), n=5)
    impacts = [r.total_impact_pct for r in worst]
    assert impacts == sorted(impacts)
    assert len(worst) == 5


def test_coverage_flags_unmodelled_share(engine, portfolio):
    portfolio.positions.append(
        Position("GOLD", "Or", AssetClass.COMMODITY, market_value=1_000_000, currency="EUR")
    )
    result = engine.run(Scenario("eq", "Actions -10 %", (Shock(ShockType.EQUITY, -0.10),)))
    assert result.coverage < 0.5
    assert result.warnings


def test_missing_bond_characteristics_are_skipped_not_guessed():
    portfolio = Portfolio(
        name="incomplet", portfolio_type=PortfolioType.FIXED_INCOME,
        positions=[Position("X", "Sans maturité", AssetClass.GOV_BOND, market_value=100_000)],
    )
    analytics = analyse_portfolio(portfolio, {})
    engine = ScenarioEngine(portfolio, SensitivitySet.from_fixed_income(analytics))
    result = engine.run(Scenario("r", "+100 bp", (Shock(ShockType.RATES_PARALLEL, 100),)))
    assert result.total_impact_pct == 0.0
    assert result.coverage == 0.0
    assert analytics.warnings
