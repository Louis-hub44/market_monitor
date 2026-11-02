"""Unit tests for phase 5: yield analytics, shifted curves, effective duration and KRDs."""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.bonds import FixedRateBond, bonds_from_static
from rates_analytics.bonds.analytics import (
    YieldConvention,
    analytics_table,
    curve_risk,
    dirty_price,
    equivalent_yield,
    yas,
    yield_analytics,
    yield_risk,
    yield_to_maturity,
)
from rates_analytics.conventions import ACT_ACT_ICMA, get_convention
from rates_analytics.curves import CurveError, InterpolatedCurve, NelsonSiegelSvenssonCurve, NSSParams
from rates_analytics.curves.shifted import (
    PiecewiseLinearSpread,
    SpreadedCurve,
    key_rate_shift,
    parallel_shift,
    tent,
)
from rates_analytics.instruments import InstrumentError

AS_OF = date(2026, 10, 6)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample"
BUND, BTP = get_convention("BUND"), get_convention("BTP")
CURVE = NelsonSiegelSvenssonCurve(AS_OF, NSSParams(0.0277, -0.0082, -0.0158, 1.62, beta3=0.0145, tau2=13.4))


@pytest.fixture
def bund() -> FixedRateBond:
    return FixedRateBond("EX", 0.026, date(2036, 2, 15), date(2026, 1, 15), BUND, first_coupon_date=date(2026, 2, 15))


# ---------------------------------------------------------- yield analytics
def test_yas_calculator_price_and_yield_inputs(bund):
    s = bund.settlement_date(AS_OF)
    a = yield_analytics(bund, s, clean=99.2759)
    b = yield_analytics(bund, s, ytm=a.ytm)
    assert b.clean == pytest.approx(99.2759, abs=1e-10)
    assert (a.previous_coupon, a.next_coupon, a.accrued_days) == (date(2026, 2, 15), date(2027, 2, 15), 235)
    assert a.dirty == pytest.approx(a.clean + a.accrued)
    assert a.risk == pytest.approx(a.modified * a.dirty / 100)
    assert a.current_yield == pytest.approx(2.6 / 99.2759)
    with pytest.raises(InstrumentError):
        yield_analytics(bund, s)
    with pytest.raises(InstrumentError):
        yield_analytics(bund, s, clean=99.0, ytm=0.03)


@pytest.mark.parametrize("conv", [BUND, BTP])
def test_durations_and_convexity_match_finite_differences(conv):
    b = FixedRateBond("X", 0.031, date(2041, 9, 1), date(2025, 9, 1), conv)
    s = b.settlement_date(AS_OF)
    y, h = 0.0345, 1e-5
    r = yield_risk(b, y, s)
    p, pu, pd_ = (dirty_price(b, x, s) for x in (y, y + h, y - h))
    assert r["dirty"] == pytest.approx(p)
    assert r["modified"] == pytest.approx((pd_ - pu) / (2 * h * p), rel=1e-8)
    assert r["convexity"] == pytest.approx((pu + pd_ - 2 * p) / (p * h * h), rel=1e-5)
    f = int(conv.yield_frequency)
    assert r["macaulay"] == pytest.approx(r["modified"] * (1 + y / f))
    one_bp = (dirty_price(b, y - 1e-4, s) - dirty_price(b, y + 1e-4, s)) / 2
    assert r["dv01"] == pytest.approx(one_bp, rel=1e-6)


def test_zero_coupon_duration_is_its_maturity():
    z = FixedRateBond("Z", 0.0, date(2036, 8, 15), date(2021, 8, 15), BUND)
    s = z.settlement_date(AS_OF)
    t = ACT_ACT_ICMA.year_fraction(s, date(2027, 8, 15), date(2026, 8, 15), date(2027, 8, 15)) + 9
    r = yield_risk(z, 0.03, s)
    assert r["macaulay"] == pytest.approx(t)
    assert r["convexity"] == pytest.approx(t * (t + 1) / 1.03 ** 2)


def test_true_yield_equals_street_without_payment_shifts():
    # 15-Feb-2027 (Mon) ... 15-Feb-2030 (Fri): no weekend coupon date
    b = FixedRateBond("B", 0.02, date(2030, 2, 15), date(2025, 2, 15), BUND)
    s = b.settlement_date(AS_OF)
    assert all(cf.payment_date.weekday() < 5 for cf in b.cashflows(s))
    assert yield_to_maturity(b, 99.0, s, YieldConvention.TRUE) == pytest.approx(
        yield_to_maturity(b, 99.0, s, YieldConvention.STREET), abs=1e-14)


def test_equivalent_yields():
    assert equivalent_yield(0.04, 2, 1) == pytest.approx(1.02 ** 2 - 1)
    assert equivalent_yield(equivalent_yield(0.037, 2, 1), 1, 2) == pytest.approx(0.037)
    with pytest.raises(InstrumentError):
        equivalent_yield(0.03, 0, 1)


def test_icma_split_for_long_first_period_paid_late():
    rs, re_ = date(2026, 11, 27), date(2027, 11, 27)
    d1, d2 = date(2026, 10, 8), date(2027, 11, 29)
    expected = ACT_ACT_ICMA.year_fraction(d1, re_, rs, re_) + 2 / 366
    assert ACT_ACT_ICMA.year_fraction(d1, d2, rs, re_) == pytest.approx(expected)


# ------------------------------------------------------------ shifted curves
def test_spreaded_curve_and_tents():
    s = PiecewiseLinearSpread((1.0, 5.0), (0.001, 0.003))
    c = SpreadedCurve(CURVE, s)
    for t in (0.5, 3.0, 12.0):
        assert c.discount(t) == pytest.approx(CURVE.discount(t) * np.exp(-s(t) * t))
    h = 1e-6  # forward consistency: f = -d ln P / dt
    for t in (0.5, 3.0, 12.0):
        num = -(np.log(c.discount(t + h)) - np.log(c.discount(t - h))) / (2 * h)
        assert c.instantaneous_forward(t) == pytest.approx(num, abs=1e-8)
    keys = (0.5, 1, 2, 5, 10, 30)
    grid = np.linspace(0, 50, 501)
    total = sum(tent(keys, k, 1.0)(grid) for k in range(len(keys)))
    assert np.allclose(total, 1.0)
    assert parallel_shift(CURVE, 0.01).zero_rate(7.0) == pytest.approx(CURVE.zero_rate(7.0) + 0.01)
    assert key_rate_shift(CURVE, keys, 3, 0.01).zero_rate(5.0) == pytest.approx(CURVE.zero_rate(5.0) + 0.01)
    with pytest.raises(CurveError):
        PiecewiseLinearSpread((2.0, 1.0), (0.0, 0.0))
    with pytest.raises(CurveError):
        tent(keys, 9, 0.01)


# ---------------------------------------------------------------- curve risk
def test_krd_sum_equals_effective_duration(bund):
    r = curve_risk(bund, CURVE, bund.settlement_date(AS_OF))
    assert r.krd_sum == pytest.approx(r.effective_duration, rel=1e-6)
    assert (r.krd[[15.0, 20.0, 30.0]] == 0).all()          # bond matures in 2036
    assert r.krd.idxmax() == 10.0
    assert r.krd_dv01().sum() == pytest.approx(r.effective_duration * r.model_dirty * 1e-4 * 1e4, rel=1e-6)


def test_effective_duration_on_flat_curve_is_curve_macaulay(bund):
    end = date(2080, 1, 1)
    flat = InterpolatedCurve(AS_OF, [end], [np.exp(-0.025 * (end - AS_OF).days / 365)])
    s = bund.settlement_date(AS_OF)
    r = curve_risk(bund, flat, s)
    ts = (s - AS_OF).days / 365
    cfs = bund.cashflows(s)
    t = np.array([(cf.payment_date - AS_OF).days / 365 for cf in cfs])
    pv = np.array([cf.amount for cf in cfs]) * np.exp(-0.025 * (t - ts))
    assert r.effective_duration == pytest.approx(np.dot(t - ts, pv) / pv.sum(), rel=1e-6)   # O(bump^2)


def test_curve_risk_with_spread_reprices_and_validates(bund):
    s = bund.settlement_date(AS_OF)
    r0 = curve_risk(bund, CURVE, s)
    r1 = curve_risk(bund, CURVE, s, spread=0.002)
    assert r1.model_dirty < r0.model_dirty
    with pytest.raises(InstrumentError):
        curve_risk(bund, CURVE, s, bump=0.0)


# ----------------------------------------------------------------------- YAS
def test_yas_report(bund):
    rep = yas(bund, AS_OF, clean=99.2759, curve=CURVE, nominal=5_000_000)
    a = rep.analytics
    assert rep.amounts["total"] == pytest.approx(a.dirty * 50_000)
    assert rep.amounts["dv01"] == pytest.approx(a.dv01 * 50_000)
    cf = rep.cashflows
    assert cf["type"].iloc[-1] == "redemption" and cf["amount"].iloc[-1] == 5_000_000
    assert cf["pv_yield"].sum() == pytest.approx(a.dirty * 50_000, rel=1e-12)
    assert cf["pv_curve"].sum() == pytest.approx(rep.curve.model_dirty * 50_000, rel=1e-12)
    assert {"effective_duration", "krd_sum", "amount_total"} <= set(rep.summary().index)
    no_curve = yas(bund, AS_OF, ytm=0.027)
    assert no_curve.curve is None and "df_curve" not in no_curve.cashflows
    with pytest.raises(InstrumentError):
        yas(bund, AS_OF, ytm=0.027, nominal=-1)


def test_analytics_table_on_sample_universe():
    static = pd.read_csv(SAMPLE / "bond_static.csv", index_col="isin")
    prices = pd.read_csv(SAMPLE / "bond_prices.csv", parse_dates=["date"])
    mids = prices[prices["date"] == pd.Timestamp(AS_OF)].set_index("isin")["mid"].to_dict()
    bonds, _ = bonds_from_static(static[static["issuer"] == "IT"])
    t = analytics_table(bonds, mids, AS_OF)
    assert len(t) == 49 and t["maturity"].is_monotonic_increasing
    assert (t["modified"] > 0).all() and (t["dv01"] > 0).all()
    assert t["modified"].iloc[-1] > 15


def test_random_bonds_roundtrip_both_conventions():
    rng = random.Random(9)
    for _ in range(50):
        mat = AS_OF + timedelta(days=rng.randint(100, 365 * 35))
        b = FixedRateBond("R", round(rng.uniform(0, 0.05), 4), mat, AS_OF - timedelta(days=rng.randint(10, 900)),
                          rng.choice([BUND, BTP]))
        s = b.settlement_date(AS_OF)
        for conv in YieldConvention:
            y = rng.uniform(-0.004, 0.07)
            clean = dirty_price(b, y, s, conv) - b.accrued_interest(s)
            assert yield_to_maturity(b, clean, s, conv) == pytest.approx(y, abs=1e-12)
