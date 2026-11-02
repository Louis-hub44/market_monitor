"""Tests des mesures de risque et de l'agrégation obligataire."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from models.enums import AssetClass, PortfolioType
from models.portfolio import Portfolio, Position
from services.fixed_income.analytics import analyse_portfolio
from services.fixed_income.bond_math import ZeroCurve, bond_risk
from services.risk.metrics import (
    beta_and_r2,
    coverage,
    max_drawdown,
    performance_metrics,
    portfolio_price_series,
    to_series,
)


@pytest.fixture
def curve() -> ZeroCurve:
    return ZeroCurve({0.25: 0.03, 1.0: 0.031, 2.0: 0.032, 5.0: 0.034,
                      10.0: 0.037, 20.0: 0.039, 30.0: 0.040}, "TEST")


def _dates(n: int) -> pd.DatetimeIndex:
    """Index de n jours ouvrés se terminant au dernier jour ouvré connu.

    `bdate_range(end=<un week-end>)` renvoie n-1 dates : pandas génère puis
    élague la borne non ouvrée. Le test échouait donc les samedis et dimanches,
    et lui seul — le genre de test qui devient rouge sans qu'aucun code ait
    changé. On ancre l'échéance sur un jour ouvré.
    """
    end = pd.Timestamp.today().normalize()
    while end.weekday() >= 5:
        end -= pd.Timedelta(days=1)
    index = pd.bdate_range(end=end, periods=n)
    assert len(index) == n
    return index


# ---------------------------------------------------------------- #
# Risque / performance
# ---------------------------------------------------------------- #
def test_max_drawdown_on_known_path():
    prices = pd.Series([100, 120, 90, 110], index=_dates(4))
    assert max_drawdown(prices) == pytest.approx(90 / 120 - 1)


def test_volatility_matches_manual_computation():
    rng = np.random.default_rng(7)
    rets = rng.normal(0, 0.01, 500)
    prices = pd.Series(100 * np.exp(np.cumsum(rets)), index=_dates(500))
    metrics = performance_metrics(prices)
    manual = float(prices.pct_change().dropna().std(ddof=1) * np.sqrt(252))
    assert metrics.volatility == pytest.approx(manual, rel=1e-12)
    assert metrics.observations == 500


def test_sharpe_requires_explicit_risk_free_rate():
    prices = pd.Series(np.linspace(100, 130, 400), index=_dates(400))
    assert performance_metrics(prices).sharpe is None
    assert performance_metrics(prices, risk_free_rate=0.02).sharpe is not None


def test_short_series_returns_empty_metrics():
    metrics = performance_metrics(pd.Series([100.0], index=_dates(1)))
    assert metrics.is_empty is False or metrics.volatility is None
    assert metrics.total_return is None


def test_beta_of_a_leveraged_replica_is_two():
    rng = np.random.default_rng(3)
    market_ret = rng.normal(0, 0.01, 400)
    market = pd.Series(100 * np.exp(np.cumsum(market_ret)), index=_dates(400))
    asset = pd.Series((1 + 2 * market.pct_change().fillna(0)).cumprod() * 100, index=market.index)
    beta, r2 = beta_and_r2(asset, market)
    assert beta == pytest.approx(2.0, rel=0.02)
    assert r2 > 0.99


def test_portfolio_series_respects_weights():
    idx = _dates(50)
    flat = pd.Series(100.0, index=idx)
    rising = pd.Series(np.linspace(100, 120, 50), index=idx)
    nav = portfolio_price_series({"A": flat, "B": rising}, {"A": 0.5, "B": 0.5})
    assert 100 < nav.iloc[-1] < 120


def test_coverage_reports_missing_history():
    idx = _dates(10)
    assert coverage({"A": pd.Series(1.0, index=idx)}, {"A": 0.6, "B": 0.4}) == pytest.approx(0.6)


def test_to_series_sorts_and_deduplicates():
    rows = [{"date": "2026-01-02", "close": 2}, {"date": "2026-01-01", "close": 1},
            {"date": "2026-01-02", "close": 3}]
    series = to_series(rows)
    assert list(series.values) == [1, 3]


# ---------------------------------------------------------------- #
# Agrégation obligataire
# ---------------------------------------------------------------- #
def _bond(identifier: str, years: float, coupon: float, mv: float, country: str = "DE") -> Position:
    return Position(identifier, identifier, AssetClass.GOV_BOND, market_value=mv, currency="EUR",
                    country=country, coupon=coupon,
                    maturity=date.today() + timedelta(days=int(years * 365.25)), frequency=1)


def test_portfolio_duration_is_value_weighted(curve):
    portfolio = Portfolio(name="fi", portfolio_type=PortfolioType.FIXED_INCOME,
                          positions=[_bond("A", 2, 0.03, 700_000), _bond("B", 10, 0.03, 300_000)])
    analytics = analyse_portfolio(portfolio, {"DEFAULT": curve})
    durations = {r.position.identifier: r.modified_duration for r in analytics.positions}
    expected = 0.7 * durations["A"] + 0.3 * durations["B"]
    assert analytics.modified_duration == pytest.approx(expected, rel=1e-9)
    assert durations["A"] < durations["B"]
    assert analytics.coverage == pytest.approx(1.0)


def test_duration_contributions_sum_to_portfolio_duration(curve):
    portfolio = Portfolio(name="fi", portfolio_type=PortfolioType.FIXED_INCOME,
                          positions=[_bond("A", 3, 0.02, 400_000), _bond("B", 7, 0.04, 350_000),
                                     _bond("C", 20, 0.035, 250_000, country="FR")])
    analytics = analyse_portfolio(portfolio, {"DEFAULT": curve})
    total = sum(r.duration_contribution for r in analytics.positions)
    assert total == pytest.approx(analytics.modified_duration, rel=1e-9)
    assert sum(analytics.breakdown("country").values()) == pytest.approx(analytics.modified_duration, rel=1e-9)


def test_portfolio_key_rate_durations_sum_to_duration(curve):
    portfolio = Portfolio(name="fi", portfolio_type=PortfolioType.FIXED_INCOME,
                          positions=[_bond("A", 5, 0.03, 500_000), _bond("B", 15, 0.04, 500_000)])
    analytics = analyse_portfolio(portfolio, {"DEFAULT": curve})
    assert sum(analytics.key_rate_durations.values()) == pytest.approx(analytics.modified_duration, rel=2e-3)


def test_dv01_matches_duration_times_value(curve):
    portfolio = Portfolio(name="fi", portfolio_type=PortfolioType.FIXED_INCOME,
                          positions=[_bond("A", 8, 0.03, 1_000_000)])
    analytics = analyse_portfolio(portfolio, {"DEFAULT": curve})
    assert analytics.dv01 == pytest.approx(analytics.modified_duration * 1_000_000 * 1e-4, rel=1e-9)


def test_analytic_duration_used_when_no_curve_available():
    portfolio = Portfolio(name="fi", portfolio_type=PortfolioType.FIXED_INCOME,
                          positions=[Position("A", "A", AssetClass.GOV_BOND, market_value=1_000_000,
                                              price=98.0, coupon=0.03, frequency=1,
                                              maturity=date.today() + timedelta(days=1826))])
    analytics = analyse_portfolio(portfolio, {})
    risk = analytics.positions[0]
    assert risk.modified_duration is not None
    reference = bond_risk(100, 0.03, risk.ttm, risk.ytm, 1).modified_duration
    assert risk.modified_duration == pytest.approx(reference, rel=1e-9)
    assert risk.quality.value == "ESTIMATED"
    assert not risk.key_rate_durations
