"""Fonds (transparence, proxy, monétaires), lignes simples et projection du portefeuille de test."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from terminal_core.guards import insecure_tls_uses

from stress_engine import Holding, InstrumentType, Mapper, MappingMethod, map_portfolio
from stress_engine.instruments.funds import pillar_weights


def fund(**kw) -> Holding:
    base = dict(id="F", instrument=InstrumentType.FUND, market_value=10_000_000)
    base.update(kw)
    return Holding(**base)


def test_pillar_weights_are_linear_tents():
    assert pillar_weights(4.5, (2, 5, 10, 30)) == pytest.approx({2: 1 / 6, 5: 5 / 6})
    assert pillar_weights(1.0, (2, 5, 10, 30)) == {2: 1.0}
    assert pillar_weights(40, (2, 5, 10, 30)) == {30: 1.0}
    assert pillar_weights(10, (2, 5, 10, 30)) == {10: 1.0}


def test_proxy_profile(market):
    m = Mapper(market).map_holding(fund(proxy="EUR_IG_CORP"))
    assert m.method is MappingMethod.PROXY and m.quality == "faible"
    rates = sum(v for k, v in m.deltas().items() if k.startswith("IR.DE."))
    assert rates == pytest.approx(-10_000_000 * 4.5 * 1e-4)
    assert m.delta("IR.DE.5Y") == pytest.approx(rates * 5 / 6)
    assert m.delta("CS.EUR.A.NONFIN") == pytest.approx(-10_000_000 * 4.5 * 1e-4)
    eq = Mapper(market).map_holding(fund(proxy="EQ_EUROPE"))
    assert eq.deltas() == {"EQ.SX5E": pytest.approx(10_000_000)}


def test_money_market_fund(market):
    m = Mapper(market).map_holding(fund(instrument=InstrumentType.MMF, wam_days=40, wal_days=90))
    assert m.delta("IR.EUR.ESTR") == pytest.approx(-10_000_000 * 40 / 365 * 1e-4)
    assert m.delta("CS.EUR.A.FIN") == pytest.approx(-10_000_000 * 90 / 365 * 1e-4)
    default = Mapper(market).map_holding(fund(instrument=InstrumentType.MMF))
    assert default.details == {"wam_days": 45.0, "wal_days": 90.0} and len(default.notes) == 2
    odd = Mapper(market).map_holding(fund(instrument=InstrumentType.MMF, wam_days=60, wal_days=30))
    assert odd.details["wal_days"] == 60 and "incohérent" in odd.notes[0]


def _bond(i: str, coupon: float, maturity: date, rating: str = "A") -> Holding:
    return Holding(id=i, instrument=InstrumentType.BOND, issuer_type="CORP", coupon=coupon, maturity=maturity,
                   issue_date=maturity.replace(year=maturity.year - 10), country="DE", rating=rating,
                   sector="Industrials", price=100.0)


def test_lookthrough_scales_components_to_the_fund_value(market):
    comp = [(_bond("A", 0.03, date(2031, 5, 1)), 0.6), (_bond("B", 0.04, date(2035, 5, 1), "BBB"), 0.3),
            (Holding(id="C", instrument=InstrumentType.CASH, market_value=1.0), 0.1)]
    m = Mapper(market, lookthrough={"LT": comp}).map_holding(fund(lookthrough="LT"))
    assert m.method is MappingMethod.LOOKTHROUGH and not m.notes
    assert sum(c.market_value for c in m.children) == pytest.approx(10_000_000)
    assert [round(c.market_value) for c in m.children] == [6_000_000, 3_000_000, 1_000_000]
    direct = Mapper(market).map_holding(replace(_bond("A", 0.03, date(2031, 5, 1)), nominal=1_000_000))
    k = 6_000_000 / direct.market_value
    assert m.delta("IR.DE.5Y") == pytest.approx(direct.delta("IR.DE.5Y") * k + m.children[1].delta("IR.DE.5Y"))
    assert {e.component for e in m.exposures} == {"A", "B"}
    assert m.children[0].pricing.units == pytest.approx(direct.pricing.units * k)   # revalorisation cohérente


def test_lookthrough_renormalises_and_reports(market):
    comp = [(_bond("A", 0.03, date(2031, 5, 1)), 0.5), (_bond("B", 0.03, date(2031, 5, 1)), 0.3)]
    m = Mapper(market, lookthrough={"LT": comp}).map_holding(fund(lookthrough="LT"))
    assert "renormalisés" in m.notes[0]
    assert sum(c.market_value for c in m.children) == pytest.approx(10_000_000)


def test_missing_composition_falls_back_and_unknown_fund_is_excluded(market):
    m = Mapper(market).map_holding(fund(lookthrough="ABSENT", proxy="EUR_HY"))
    assert m.method is MappingMethod.PROXY and "introuvable" in m.notes[0]
    assert Mapper(market).map_holding(fund()).method is MappingMethod.EXCLUDED


def test_equity_commodity_cash_and_fx(market):
    mapper = Mapper(market)
    eq = mapper.map_holding(Holding(id="E", instrument=InstrumentType.EQUITY, currency="USD", market_value=1_000_000,
                                    fx_rate=0.9, beta=1.2))
    assert eq.deltas() == {"EQ.SPX": pytest.approx(1_080_000), "FX.USD": pytest.approx(900_000)}
    cmd = mapper.map_holding(Holding(id="GLD", instrument=InstrumentType.COMMODITY, index="GOLD",
                                     market_value=2_000_000))
    assert cmd.deltas() == {"CMD.GOLD": pytest.approx(2_000_000)}
    assert mapper.map_holding(Holding(id="C", instrument=InstrumentType.CASH, market_value=5.0)).exposures == []
    no_fx = mapper.map_holding(Holding(id="J", instrument=InstrumentType.CASH, currency="JPY", market_value=5.0))
    assert no_fx.method is MappingMethod.EXCLUDED and "change" in no_fx.notes[0]


# ------------------------------------------------------------------ portefeuille de test
def test_test_portfolio_mapping(market, test_portfolio, test_lookthrough):
    res = map_portfolio(test_portfolio, market, lookthrough=test_lookthrough)
    assert len(res.positions) == 43 and res.coverage() == 1.0 and not res.excluded()
    summary = res.summary()
    futures = summary[summary.asset_class == "RATES_FUTURE"]
    assert (futures.market_value == 0).all() and (futures.dv01 > 0).all()       # couvertures vendeuses
    assert (summary[summary.asset_class == "CREDIT"].cs01 < 0).all()
    by_factor = res.by_factor()
    assert by_factor["FX.USD"] == pytest.approx(891_500)
    assert by_factor[[f for f in by_factor.index if f.startswith("IR.")]].sum() < 0      # long duration nette
    matrix = res.matrix()
    assert matrix.shape[0] == 42 and set(matrix.columns) == set(by_factor.index)       # trésorerie EUR : aucune
    assert res.frame()["delta"].sum() == pytest.approx(by_factor.sum())


# ------------------------------------------------------------------ garde-fous
def test_engine_imports_no_ui_and_never_disables_tls():
    src = Path(__file__).resolve().parents[1] / "src" / "stress_engine"
    assert insecure_tls_uses([src]) == []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import streamlit" not in text and "import plotly" not in text, path
        assert "requests" not in text, f"{path} : le moteur ne fait aucun appel réseau"
