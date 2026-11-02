"""État de marché hors ligne : instantané d'exemple de Rates & Bond Analytics (06/10/2026)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest
from rates_analytics import api
from rates_analytics.config import load_settings
from rates_analytics.curves.proxy import FunctionCurve

from stress_engine import EngineConfig, FactorSpace, MarketState, load_lookthrough, load_portfolio

AS_OF = date(2026, 10, 6)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def config() -> EngineConfig:
    return EngineConfig.load()


@pytest.fixture
def space(config) -> FactorSpace:
    return FactorSpace.from_config(config.factors)


@pytest.fixture(scope="session")
def snapshot():
    return api.market_snapshot(AS_OF, settings=load_settings(overrides={"provider": "static", "fallback": []}))


@pytest.fixture(scope="session")
def market(snapshot, config) -> MarketState:
    r = config.factors["rates"]
    return MarketState.from_snapshot(snapshot, r["curve_names"], r["missing_curve_fallback"])


def flat_curve(rate: float, as_of: date = AS_OF) -> FunctionCurve:
    """Courbe zéro plate (continue, ACT/365F) : références analytiques fermées."""
    return FunctionCurve(as_of, lambda t: np.full_like(np.asarray(t, dtype=float), rate), name=f"flat {rate:.2%}")


@pytest.fixture
def flat_market() -> MarketState:
    c = flat_curve(0.03)
    return MarketState(as_of=AS_OF, curves={"DE": c, "FR": c, "IT": c}, missing_curve_fallback={"ES": "FR"})


@pytest.fixture(scope="session")
def test_portfolio():
    return load_portfolio(ROOT / "samples" / "test_credit_eur.csv")


@pytest.fixture(scope="session")
def test_lookthrough():
    return load_lookthrough(ROOT / "samples" / "test_lookthrough.csv")
