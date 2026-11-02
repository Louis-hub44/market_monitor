"""Unit tests: compounding, interpolators, curve object, OIS, bootstrap, €STR builder."""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from rates_analytics.conventions import ACT_360, ACT_365F
from rates_analytics.curves import (
    CurveError,
    InterpolatedCurve,
    LogLinearInterpolator,
    MonotoneConvexInterpolator,
    OISHelper,
    bootstrap,
    build_estr_curve,
    discount_from_rate,
    estr_curve_from_market,
    rate_from_discount,
)
from rates_analytics.curves.helpers import DepositHelper
from rates_analytics.data import MarketDataService, StaticProvider
from rates_analytics.exceptions import ConventionError
from rates_analytics.instruments import InstrumentError, OvernightIndexedSwap

AS_OF = date(2026, 10, 7)
SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"


@pytest.fixture(scope="module")
def quotes() -> dict[str, float]:
    return pd.read_csv(SAMPLE_DIR / "history.csv", index_col=0).loc["2026-10-06"].to_dict()


@pytest.fixture(scope="module")
def ll_curve(quotes):
    return build_estr_curve(AS_OF, quotes, "log_linear")


@pytest.fixture(scope="module")
def mc_curve(quotes):
    return build_estr_curve(AS_OF, quotes, "monotone_convex")


# --------------------------------------------------------------- compounding
@pytest.mark.parametrize("comp,freq", [("continuous", 1), ("simple", 1), ("compounded", 1), ("compounded", 2)])
def test_compounding_roundtrip(comp, freq):
    df = discount_from_rate(0.031, 2.5, comp, freq)
    assert rate_from_discount(df, 2.5, comp, freq) == pytest.approx(0.031, abs=1e-15)


def test_compounding_values_and_errors():
    assert discount_from_rate(0.02, 0.5, "simple") == pytest.approx(1 / 1.01)
    assert discount_from_rate(0.04, 1, "compounded", 2) == pytest.approx(1 / 1.02**2)
    with pytest.raises(ConventionError):
        rate_from_discount(0.9, 0.0)
    with pytest.raises(ConventionError):
        discount_from_rate(0.01, 1, "weekly")


# -------------------------------------------------------------- interpolators
T = np.array([0.0, 0.25, 1.0, 2.0, 5.0, 10.0, 30.0])
Z = np.array([0.0, 0.019, 0.0195, 0.021, 0.024, 0.026, 0.025])
Y = Z * T


@pytest.mark.parametrize("cls", [LogLinearInterpolator, MonotoneConvexInterpolator])
def test_interpolators_reproduce_nodes_and_integral(cls):
    it = cls(T, Y)
    assert np.allclose(it.value(T), Y, atol=1e-15)
    # average instantaneous forward over each segment = discrete forward
    for a, b in zip(T[:-1], T[1:]):
        n = 20000
        mid = a + (np.arange(n) + 0.5) * (b - a) / n            # midpoint rule (no node on the grid)
        integral = float(np.sum(it.forward(mid)) * (b - a) / n)
        assert integral == pytest.approx(it.value(b) - it.value(a), abs=1e-9)


def test_log_linear_forwards_are_flat_per_segment():
    it = LogLinearInterpolator(T, Y)
    assert it.forward(1.5) == pytest.approx((Y[3] - Y[2]) / 1.0)
    assert it.forward(40.0) == pytest.approx((Y[-1] - Y[-2]) / 20.0)   # flat extrapolation


def test_monotone_convex_forward_is_continuous():
    it = MonotoneConvexInterpolator(T, Y)
    for t in T[1:-1]:
        assert it.forward(t - 1e-9) == pytest.approx(it.forward(t + 1e-9), abs=1e-7)
    assert it.forward(T[1:-1]) == pytest.approx(it.f_nodes[1:-1], abs=1e-12)


def test_monotone_convex_covers_all_regions_and_integrates_to_zero():
    rng = np.random.default_rng(3)
    seen = set()
    for _ in range(300):
        t = np.concatenate(([0.0], np.cumsum(rng.uniform(0.1, 3.0, 6))))
        f = rng.normal(0.02, 0.02, 6)
        y = np.concatenate(([0.0], np.cumsum(f * np.diff(t))))
        it = MonotoneConvexInterpolator(t, y)
        seen |= set(it.region.tolist())
        assert np.allclose(it.value(t), y, atol=1e-13)
        k = np.arange(6)
        _, G1 = it._g_and_G(k, np.ones(6))
        assert np.allclose(G1, 0.0, atol=1e-13)
    assert {1, 2, 3, 4} <= seen


def test_monotone_convex_positivity_collar():
    t = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    f = np.array([0.001, 0.03, 0.0005, 0.02])
    y = np.concatenate(([0.0], np.cumsum(f)))
    plain = MonotoneConvexInterpolator(t, y)
    collared = MonotoneConvexInterpolator(t, y, positivity=True)
    grid = np.linspace(0, 4, 4001)
    assert plain.forward(grid).min() < 0
    assert collared.forward(grid).min() >= -1e-15
    with pytest.raises(CurveError):
        MonotoneConvexInterpolator(t, -y, positivity=True)


@pytest.mark.parametrize("t,y", [
    ([0.0], [0.0]), ([0.0, 1.0, 1.0], [0.0, 0.01, 0.02]), ([0.1, 1.0], [0.0, 0.01]),
    ([0.0, 1.0], [0.0, np.nan]),
])
def test_interpolator_rejects_bad_nodes(t, y):
    with pytest.raises(CurveError):
        LogLinearInterpolator(t, y)


# ---------------------------------------------------------------- curve object
def test_interpolated_curve_api():
    dates = [date(2027, 10, 7), date(2031, 10, 7)]
    c = InterpolatedCurve(AS_OF, dates, [0.98, 0.90], name="TEST")
    assert c.discount(AS_OF) == 1.0
    assert c.discount(dates[1]) == pytest.approx(0.90)
    t1 = c.time(dates[0])
    assert c.zero_rate(dates[0]) == pytest.approx(-np.log(0.98) / t1)
    assert c.zero_rate(dates[0], "compounded", 1) == pytest.approx(0.98 ** (-1 / t1) - 1)
    fwd = c.forward_rate(dates[0], dates[1], ACT_360)
    assert fwd == pytest.approx((0.98 / 0.90 - 1) / ACT_360.year_fraction(*dates))
    assert np.allclose(c.discount(np.array([0.5, 1.0])), [c.discount(0.5), c.discount(1.0)])
    assert list(c.to_frame().columns) == ["date", "time", "discount", "zero_cc", "inst_forward"]
    with pytest.raises(CurveError):
        c.discount(AS_OF - timedelta(days=1))
    with pytest.raises(CurveError):
        c.forward_rate(dates[1], dates[0])


@pytest.mark.parametrize("dates,dfs", [
    ([AS_OF], [0.99]), ([date(2028, 1, 1), date(2027, 1, 1)], [0.98, 0.99]),
    ([date(2028, 1, 1)], [-0.5]), ([date(2028, 1, 1)], [0.9, 0.8]),
])
def test_interpolated_curve_validation(dates, dfs):
    with pytest.raises(CurveError):
        InterpolatedCurve(AS_OF, dates, dfs)


# ------------------------------------------------------------------------- OIS
def test_ois_structure():
    short = OvernightIndexedSwap.from_tenor(AS_OF, "6M")
    assert len(short.fixed_leg) == len(short.float_leg) == 1
    assert short.start == date(2026, 10, 9)
    eighteen = OvernightIndexedSwap.from_tenor(AS_OF, "18M")
    assert [p.is_regular for p in eighteen.fixed_leg.periods] == [False, True]   # short front stub
    ten = OvernightIndexedSwap.from_tenor(AS_OF, "10Y")
    assert len(ten.fixed_leg) == 10
    assert ten.last_relevant_date == date(2036, 10, 10)                          # maturity + 1bd lag


def test_ois_valuation(ll_curve):
    curve = ll_curve.curve
    swap = OvernightIndexedSwap.from_tenor(AS_OF, "7Y")
    k = swap.fair_rate(curve)
    at_par = OvernightIndexedSwap(swap.start, swap.maturity, k, notional=1e7)
    assert at_par.npv(curve) == pytest.approx(0.0, abs=1e-6)
    payer = OvernightIndexedSwap(swap.start, swap.maturity, k - 0.001, notional=1e7)
    receiver = OvernightIndexedSwap(swap.start, swap.maturity, k - 0.001, notional=1e7, pay_fixed=False)
    assert payer.npv(curve) == pytest.approx(0.001 * payer.annuity(curve), rel=1e-12)
    assert receiver.npv(curve) == pytest.approx(-payer.npv(curve))
    spread = OvernightIndexedSwap(swap.start, swap.maturity, k, spread=0.001)
    assert spread.fair_rate(curve) == pytest.approx(k + 0.001, abs=1e-12)  # same schedules on both legs


def test_ois_validation(ll_curve):
    with pytest.raises(InstrumentError):
        OvernightIndexedSwap(date(2030, 1, 1), date(2029, 1, 1))
    with pytest.raises(InstrumentError):
        OvernightIndexedSwap(date(2027, 1, 1), date(2029, 1, 1), notional=-1)
    seasoned = OvernightIndexedSwap(date(2026, 1, 5), date(2028, 1, 5))
    with pytest.raises(InstrumentError):
        seasoned.fair_rate(ll_curve.curve)


# ------------------------------------------------------------------- bootstrap
@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
def test_bootstrap_reprices_all_instruments(quotes, method):
    res = build_estr_curve(AS_OF, quotes, method)
    assert res.max_abs_error_bp() < 1e-8
    assert len(res.helpers) == 28 and not res.skipped
    rep = res.report()
    assert rep["pillar"].is_monotonic_increasing and (rep["discount"].diff().dropna() < 0).all()


@pytest.mark.parametrize("method", ["log_linear", "monotone_convex"])
def test_bootstrap_recovers_a_known_flat_curve(method):
    flat = InterpolatedCurve(AS_OF, [date(2080, 1, 1)], [np.exp(-0.023 * ACT_365F.year_fraction(AS_OF, date(2080, 1, 1)))])
    tenors = ["1W", "1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "20Y", "30Y"]
    helpers = [OISHelper(AS_OF, t, OvernightIndexedSwap.from_tenor(AS_OF, t).fair_rate(flat)) for t in tenors]
    res = bootstrap(AS_OF, helpers, method)
    grid = [AS_OF + timedelta(days=k) for k in range(1, 365 * 30, 17)]
    assert max(abs(res.curve.discount(d) - flat.discount(d)) for d in grid) < 1e-12


def test_bootstrap_log_linear_is_local(quotes):
    base = build_estr_curve(AS_OF, quotes, "log_linear").curve
    bumped_quotes = dict(quotes, **{"EUR.ESTR.OIS.10Y": quotes["EUR.ESTR.OIS.10Y"] + 0.0001})
    bumped = build_estr_curve(AS_OF, bumped_quotes, "log_linear").curve
    nine = bumped.dates[bumped.times.searchsorted(9.0)]
    assert bumped.discount(nine - timedelta(days=5)) == base.discount(nine - timedelta(days=5))
    assert bumped.discount(date(2036, 1, 1)) < base.discount(date(2036, 1, 1))


def test_bootstrap_errors():
    h1 = OISHelper(AS_OF, "1Y", 0.02)
    with pytest.raises(CurveError):
        bootstrap(AS_OF, [])
    with pytest.raises(CurveError):
        bootstrap(AS_OF, [h1, OISHelper(AS_OF, "12M", 0.021)])          # same pillar
    with pytest.raises(CurveError):
        bootstrap(date(2026, 10, 12), [h1])                              # starts before ref date
    with pytest.raises(CurveError):
        OISHelper(AS_OF, "1Y", float("nan"))


def test_helpers_dates_and_bump():
    on = DepositHelper(AS_OF, "ON", 0.0193)
    assert (on.start, on.end) == (date(2026, 10, 7), date(2026, 10, 8))
    bumped = on.with_quote(0.0194)
    assert bumped.quote == 0.0194 and on.quote == 0.0193 and bumped.pillar_date == on.pillar_date


def test_estr_builder_skips_missing_quotes(quotes):
    partial = {k: v for k, v in quotes.items() if not k.endswith(("OIS.4M", "OIS.40Y"))}
    res = build_estr_curve(AS_OF, partial)
    assert set(res.skipped) == {"EUR.ESTR.OIS.4M", "EUR.ESTR.OIS.40Y"}
    assert res.max_abs_error_bp() < 1e-8
    with pytest.raises(CurveError):
        build_estr_curve(AS_OF, {"EUR.ESTR.FIX": 0.019})


def test_estr_curve_from_market_service():
    svc = MarketDataService([StaticProvider.from_directory(SAMPLE_DIR)], today=lambda: AS_OF)
    res = estr_curve_from_market(svc, AS_OF, interpolation="log_linear")
    assert svc.last_report.ok and len(res.helpers) == 28
    assert res.curve.zero_rate(date(2036, 10, 9)) == pytest.approx(0.0251, abs=5e-4)


def test_curves_are_close_between_methods(ll_curve, mc_curve):
    grid = [AS_OF + timedelta(days=k) for k in range(30, 365 * 50, 90)]
    diff = max(abs(ll_curve.curve.zero_rate(d) - mc_curve.curve.zero_rate(d)) for d in grid)
    assert diff < 5e-4   # same instruments, different shapes between pillars (< 5bp)


def test_random_quotes_reprice(quotes):
    rng = random.Random(5)
    for _ in range(3):
        noisy = {k: v + rng.gauss(0, 0.002) for k, v in quotes.items()}
        assert build_estr_curve(AS_OF, noisy, "monotone_convex").max_abs_error_bp() < 1e-8
