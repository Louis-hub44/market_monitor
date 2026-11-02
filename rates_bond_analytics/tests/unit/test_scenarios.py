"""Unit tests for phase 8: scenario shapes, application modes, scenario P&L."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.bonds import FixedRateBond
from rates_analytics.bonds.analytics import curve_risk
from rates_analytics.conventions import get_convention
from rates_analytics.curves import CurveError, NelsonSiegelSvenssonCurve, NSSParams, build_eur_curves
from rates_analytics.instruments import OvernightIndexedSwap, VanillaSwap
from rates_analytics.scenarios import (
    CurveEnvironment,
    Position,
    apply_scenario,
    butterfly,
    custom,
    flattener,
    irrbb_scenarios,
    par_rate_table,
    parallel,
    scenario_pnl,
    shift_curve,
    standard_scenarios,
    steepener,
    twist,
)
from rates_analytics.swaps import parallel_dv01, swap_npv

AS_OF = date(2026, 10, 6)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"
GOV = NelsonSiegelSvenssonCurve(AS_OF, NSSParams(0.0277, -0.0082, -0.0158, 1.62, beta3=0.0145, tau2=13.4))


@pytest.fixture(scope="module")
def curves():
    q = pd.read_csv(SAMPLE / "history.csv", index_col=0).loc[AS_OF.isoformat()].dropna().to_dict()
    return build_eur_curves(AS_OF, q)


# ------------------------------------------------------------------ shapes
def test_scenario_shapes():
    assert parallel(25)(np.array([0.1, 7, 40])) == pytest.approx([25e-4] * 3)
    st = steepener(30, kind="bear")
    assert st(1.0) == 0.0 and st(2.0) == 0.0 and st(6.0) == pytest.approx(15e-4) and st(30.0) == pytest.approx(30e-4)
    assert steepener(30, kind="bull")(2.0) == pytest.approx(-30e-4)
    fl = flattener(20, kind="pivot")
    assert fl(2.0) == pytest.approx(10e-4) and fl(10.0) == pytest.approx(-10e-4)
    bf = butterfly(15, wings_bp=-5)
    assert bf(2.0) == pytest.approx(-5e-4) and bf(5.0) == pytest.approx(15e-4) and bf(10.0) == pytest.approx(-5e-4)
    cu = custom({"18M": 10, "10Y": -10})
    assert cu(1.5) == pytest.approx(10e-4) and cu(5.75) == pytest.approx(0.0) and cu(50) == pytest.approx(-10e-4)
    assert (parallel(10) + steepener(20))(10.0) == pytest.approx(30e-4)
    assert parallel(10).scaled(-2)(3.0) == pytest.approx(-20e-4)
    assert parallel(5).bp(("2Y", "10Y")) == {"2Y": pytest.approx(5.0), "10Y": pytest.approx(5.0)}
    assert len(standard_scenarios()) == 10


def test_scenario_validation():
    with pytest.raises(CurveError):
        twist(0, 10, "10Y", "2Y")
    with pytest.raises(CurveError):
        butterfly(10, short_t="5Y", belly_t="2Y")
    with pytest.raises(CurveError):
        custom({})
    with pytest.raises(CurveError):
        custom({"1Y": 1, "12M": 2})
    with pytest.raises(CurveError):
        irrbb_scenarios("JPY")


def test_irrbb_formulas():
    sc = {s.name: s for s in irrbb_scenarios("EUR")}
    t = np.array([0.25, 2.0, 10.0, 30.0])
    s_short, s_long = 250e-4 * np.exp(-t / 4), 100e-4 * (1 - np.exp(-t / 4))
    assert sc["IRRBB parallel up"](t) == pytest.approx([200e-4] * 4)
    assert sc["IRRBB short up"](t) == pytest.approx(s_short, abs=2e-6)          # dense-grid sampling
    assert sc["IRRBB steepener"](t) == pytest.approx(-0.65 * s_short + 0.9 * s_long, abs=2e-6)
    assert sc["IRRBB flattener"](t) == pytest.approx(0.8 * s_short - 0.6 * s_long, abs=2e-6)


# ------------------------------------------------------------ application
def test_zero_mode_shifts_zero_rates(curves):
    sc = steepener(40)
    new = apply_scenario(curves, sc, mode="zero")
    for t in (1.0, 5.0, 20.0):
        for old_c, new_c in ((curves.discount_curve, new.discount_curve),
                             (curves.forward_curve("EURIBOR6M"), new.forward_curve("EURIBOR6M"))):
            assert new_c.zero_rate(t) - old_c.zero_rate(t) == pytest.approx(float(sc(t)), abs=1e-12)
    basis_only = apply_scenario(curves, parallel(10), mode="zero", targets=["EURIBOR6M"])
    assert basis_only.discount_curve.discount(5.0) == curves.discount_curve.discount(5.0)
    assert shift_curve(GOV, parallel(10)).zero_rate(7.0) == pytest.approx(GOV.zero_rate(7.0) + 10e-4)


def test_quote_mode_moves_par_rates_by_the_shock(curves):
    sc = custom({"2Y": 10, "10Y": 30})
    new = apply_scenario(curves, sc, mode="quotes")
    table = par_rate_table(curves, {"s": new}, tenors=("2Y", "10Y", "30Y"))
    # quotes move by the shock at their pillar (2Y swap pillar = 2.01 years -> 10.03bp)
    assert table.loc["2Y", "s"] == pytest.approx(10.0, abs=0.05)
    assert table.loc["10Y", "s"] == pytest.approx(30.0, abs=0.05)
    assert table.loc["30Y", "s"] == pytest.approx(30.0, abs=0.01)
    ois = OvernightIndexedSwap.from_tenor(AS_OF, "5Y")
    assert (ois.fair_rate(new.discount_curve) - ois.fair_rate(curves.discount_curve)) * 1e4 == pytest.approx(
        float(sc(5.0)) * 1e4, abs=0.2)
    with pytest.raises(CurveError):
        apply_scenario(curves, sc, mode="spot")
    with pytest.raises(CurveError):
        apply_scenario(curves, sc, targets=["EURIBOR3M"])


# --------------------------------------------------------------------- P&L
def test_scenario_pnl_book(curves):
    swap = VanillaSwap.from_tenor(AS_OF, "10Y", 0.027, notional=1e7)
    bond = FixedRateBond("BTP35", 0.034, date(2035, 4, 1), date(2025, 4, 1), get_convention("BTP"))
    book = [Position("payer", swap), Position("btp", bond, nominal=1e7, clean_price=98.977, curve="BTP")]
    base = CurveEnvironment(curves, {"BTP": GOV})
    pnl = scenario_pnl(book, base, [parallel(1), parallel(100), parallel(-100)], mode="quotes")
    # +1bp on all quotes = SWPM DV01 of the swap
    assert pnl.loc["payer", "Parallel +1bp"] == pytest.approx(
        parallel_dv01(curves, lambda c: swap_npv(swap, c)), rel=1e-9)
    # bond at constant Z: P&L ~ -effective duration x dirty x 1bp
    s = bond.settlement_date(AS_OF)
    z = book[1]._z
    risk = curve_risk(bond, GOV, s, spread=z)
    assert pnl.loc["btp", "Parallel +1bp"] == pytest.approx(-risk.effective_duration * risk.model_dirty * 1e-4 * 1e5,
                                                           rel=1e-3)
    assert pnl.loc["btp", "base_value"] == pytest.approx((98.977 + bond.accrued_interest(s)) * 1e5)
    assert pnl.loc["payer", "Parallel +100bp"] > 0 > pnl.loc["btp", "Parallel +100bp"]
    assert pnl.loc["TOTAL", "Parallel -100bp"] == pytest.approx(pnl.loc[["payer", "btp"], "Parallel -100bp"].sum())
    # convexity: a bond gains more when rates fall than it loses when they rise
    assert pnl.loc["btp", "Parallel -100bp"] > -pnl.loc["btp", "Parallel +100bp"]
    with pytest.raises(CurveError):
        scenario_pnl([], base, [parallel(1)])
