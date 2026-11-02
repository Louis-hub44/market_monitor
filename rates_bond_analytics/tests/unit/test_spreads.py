"""Unit tests for phase 6: G / I / Z spreads, par-par asset swap, Z-calibrated YAS."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.bonds import (
    FixedRateBond,
    asw_par_par,
    bonds_from_static,
    g_spread,
    i_spread,
    par_yield_on_curve,
    spread_analytics,
    spread_table,
    swap_rate_to_maturity,
    yas,
    yield_to_maturity,
    z_spread,
)
from rates_analytics.conventions import get_convention
from rates_analytics.curves import (
    NelsonSiegelSvenssonCurve,
    NSSParams,
    SpreadedCurve,
    build_eur_curves,
)
from rates_analytics.instruments import InstrumentError, VanillaSwap

AS_OF = date(2026, 10, 6)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"
BUND, BTP = get_convention("BUND"), get_convention("BTP")
GOV = NelsonSiegelSvenssonCurve(AS_OF, NSSParams(0.0277, -0.0082, -0.0158, 1.62, beta3=0.0145, tau2=13.4))


@pytest.fixture(scope="module")
def curves():
    q = pd.read_csv(SAMPLE / "history.csv", index_col=0).loc[AS_OF.isoformat()].to_dict()
    return build_eur_curves(AS_OF, q)


@pytest.fixture
def btp() -> FixedRateBond:
    return FixedRateBond("BTP35", 0.034, date(2035, 4, 1), date(2025, 4, 1), BTP)


# ------------------------------------------------------------------ Z-spread
def test_z_spread_definition(btp):
    s = btp.settlement_date(AS_OF)
    on_curve = btp.clean_price_from_curve(GOV, s)
    assert z_spread(btp, on_curve, s, GOV) == pytest.approx(0.0, abs=1e-12)
    cheap = btp.clean_price_from_curve(SpreadedCurve(GOV, 0.0035), s)
    assert z_spread(btp, cheap, s, GOV) == pytest.approx(0.0035, abs=1e-12)
    assert z_spread(btp, cheap + 1, s, GOV) < 0.0035
    with pytest.raises(InstrumentError):
        z_spread(btp, -1.0, s, GOV)


# ------------------------------------------------------------------ G-spread
def test_par_yield_and_g_spread(btp):
    s = btp.settlement_date(AS_OF)
    par_y = par_yield_on_curve(btp, s, GOV)
    # a bond with the same schedule paying the par yield as coupon prices ~100 on the curve
    twin = FixedRateBond("T", par_y, btp.maturity, btp.issue_date, BTP)
    assert twin.clean_price_from_curve(GOV, s) == pytest.approx(100.0, abs=0.02)
    g, bench = g_spread(btp, 98.977, s, GOV)
    assert bench == pytest.approx(par_y)
    assert g == pytest.approx(yield_to_maturity(btp, 98.977, s) - par_y)
    bench_yields = pd.Series({2.0: 0.020, 5.0: 0.023, 10.0: 0.027})
    _g2, b2 = g_spread(btp, 98.977, s, GOV, benchmark_yields=bench_yields)
    t = (btp.maturity - s).days / 365.25
    assert b2 == pytest.approx(np.interp(t, [2, 5, 10], [0.020, 0.023, 0.027]))


# ------------------------------------------------------------------ I-spread
def test_i_spread(btp, curves):
    s = btp.settlement_date(AS_OF)
    disc, fwd = curves.discount_curve, curves.forward_curve("EURIBOR6M")
    i, rate = i_spread(btp, 98.977, s, disc, fwd)
    assert rate == pytest.approx(VanillaSwap(s, btp.maturity).fair_rate(disc, fwd))
    assert rate == pytest.approx(swap_rate_to_maturity(btp, s, disc, fwd))
    y = yield_to_maturity(btp, 98.977, s)
    assert i == pytest.approx((1 + y / 2) ** 2 - 1 - rate)


# ----------------------------------------------------------------------- ASW
def test_asw_single_curve_identity(btp, curves):
    s = btp.settlement_date(AS_OF)
    c = curves.discount_curve
    res = asw_par_par(btp, 98.977, s, c, c, index="EURIBOR6M")
    model_dirty = btp.dirty_price_from_curve(c, s)
    dirty = 98.977 + btp.accrued_interest(s)
    assert res.spread == pytest.approx(c.discount(s) * (model_dirty - dirty) / res.bpv_float, abs=1e-12)
    assert res.upfront == pytest.approx(100 - dirty)


def test_asw_dual_curve_and_estr(btp, curves):
    s = btp.settlement_date(AS_OF)
    disc, fwd = curves.discount_curve, curves.forward_curve("EURIBOR6M")
    e6 = asw_par_par(btp, 98.977, s, disc, fwd)
    estr = asw_par_par(btp, 98.977, s, disc, index="ESTR")
    assert estr.spread > e6.spread                                     # €STR < EURIBOR -> wider spread
    assert (estr.spread - e6.spread) * 1e4 == pytest.approx(curves.basis_spreads(("8Y",))["basis_bp"][0], abs=3)
    z_ois = z_spread(btp, 98.977, s, disc)
    assert abs(estr.spread - z_ois) * 1e4 < 3                           # near-par bond: ASW ~ Z-spread
    with pytest.raises(InstrumentError):
        asw_par_par(btp, 98.977, s, disc, None, "EURIBOR6M")


# --------------------------------------------------------------- reports / YAS
def test_spread_table_on_sample(curves):
    static = pd.read_csv(SAMPLE / "bond_static.csv", index_col="isin")
    prices = pd.read_csv(SAMPLE / "bond_prices.csv", parse_dates=["date"])
    mids = prices[prices["date"] == pd.Timestamp(AS_OF)].set_index("isin")["mid"].to_dict()
    bonds, _ = bonds_from_static(static[static["issuer"] == "IT"])
    tab = spread_table(bonds, mids, AS_OF, curves, GOV)
    assert len(tab) == 48 and tab["maturity"].is_monotonic_increasing
    ten = tab[(tab["maturity"] > date(2035, 1, 1)) & (tab["maturity"] < date(2037, 1, 1))]
    assert ten["g_spread"].between(80, 110).all()                       # BTP-Bund ~ 90-95bp at 10Y
    assert (tab["z_spread_ois"] > tab["z_spread_swap"]).all()
    one = spread_analytics(bonds[10], AS_OF, mids[bonds[10].isin], curves).as_series()
    assert one["g_spread"] is None and np.isfinite(one["asw_estr"])


def test_yas_with_z_calibrated_curve(btp, curves):
    rep = yas(btp, AS_OF, clean=98.977, curve=curves.discount_curve, spread="z")
    assert rep.curve.model_dirty == pytest.approx(rep.analytics.dirty, abs=1e-9)
    assert rep.extra["z_spread"] == pytest.approx(z_spread(btp, 98.977, btp.settlement_date(AS_OF),
                                                           curves.discount_curve))
    assert rep.cashflows["pv_curve"].sum() == pytest.approx(rep.analytics.dirty * 1e4, rel=1e-10)
    with pytest.raises(InstrumentError):
        yas(btp, AS_OF, clean=98.977, spread="z")
    with pytest.raises(InstrumentError):
        yas(btp, AS_OF, clean=98.977, curve=GOV, spread="oas")
