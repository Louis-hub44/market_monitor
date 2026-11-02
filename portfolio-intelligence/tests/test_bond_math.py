"""Tests des calculs obligataires contre des formules fermées connues."""

from __future__ import annotations

import math

import pytest

from services.fixed_income.bond_math import (
    ZeroCurve,
    bond_risk,
    cashflows,
    effective_convexity,
    effective_duration,
    key_rate_durations,
    price_from_curve,
    price_from_yield,
    spread_duration,
    yield_from_price,
)


# ---------------------------------------------------------------- #
# Zéro-coupon : P = 100/(1+y)^T, D_mac = T, D_mod = T/(1+y), C = T(T+1)/(1+y)²
# ---------------------------------------------------------------- #
def test_zero_coupon_closed_form():
    T, y = 10.0, 0.05
    risk = bond_risk(face=100, coupon_rate=0.0, ttm=T, ytm=y, frequency=1)
    assert risk.price == pytest.approx(100 / (1 + y) ** T, rel=1e-10)
    assert risk.macaulay_duration == pytest.approx(T, rel=1e-10)
    assert risk.modified_duration == pytest.approx(T / (1 + y), rel=1e-10)
    assert risk.convexity == pytest.approx(T * (T + 1) / (1 + y) ** 2, rel=1e-10)


def test_par_bond_prices_at_100():
    for freq in (1, 2, 4):
        price = price_from_yield(100, 0.04, 7.0, 0.04, freq)
        assert price == pytest.approx(100.0, rel=1e-9)


def test_par_bond_macaulay_matches_annuity_formula():
    """D = ((1+y)/y) × (1 - (1+y)^-T) pour une obligation au pair, coupon annuel."""
    y, T = 0.06, 3.0
    expected = ((1 + y) / y) * (1 - (1 + y) ** -T)
    risk = bond_risk(100, y, T, y, frequency=1)
    assert risk.macaulay_duration == pytest.approx(expected, rel=1e-9)
    assert risk.macaulay_duration == pytest.approx(2.8334, abs=1e-3)


def test_modified_duration_is_price_derivative():
    """-dP/dy / P doit converger vers la duration modifiée."""
    face, c, T, y, f = 100, 0.035, 8.0, 0.042, 2
    risk = bond_risk(face, c, T, y, f)
    h = 1e-6
    numeric = -(price_from_yield(face, c, T, y + h, f) - price_from_yield(face, c, T, y - h, f)) / (2 * h * risk.price)
    assert numeric == pytest.approx(risk.modified_duration, rel=1e-5)


def test_convexity_is_second_derivative():
    face, c, T, y, f = 100, 0.05, 12.0, 0.045, 2
    risk = bond_risk(face, c, T, y, f)
    h = 1e-5
    p_up = price_from_yield(face, c, T, y + h, f)
    p_dn = price_from_yield(face, c, T, y - h, f)
    numeric = (p_up + p_dn - 2 * risk.price) / (risk.price * h ** 2)
    assert numeric == pytest.approx(risk.convexity, rel=1e-3)


def test_yield_price_roundtrip():
    for coupon, ttm, ytm in [(0.02, 5.0, 0.035), (0.06, 20.0, 0.041), (0.0, 3.0, -0.002)]:
        price = price_from_yield(100, coupon, ttm, ytm, 2)
        assert yield_from_price(price, 100, coupon, ttm, 2) == pytest.approx(ytm, abs=1e-8)


def test_duration_ranking_is_monotonic():
    """À coupon égal, une maturité plus longue donne une duration plus élevée."""
    durations = [bond_risk(100, 0.03, t, 0.03, 2).modified_duration for t in (2, 5, 10, 30)]
    assert durations == sorted(durations)


def test_price_yield_inverse_relationship():
    base = price_from_yield(100, 0.04, 10, 0.04, 2)
    assert price_from_yield(100, 0.04, 10, 0.05, 2) < base
    assert price_from_yield(100, 0.04, 10, 0.03, 2) > base


def test_second_order_approximation_matches_full_repricing():
    """-D×Δy + ½C×Δy² doit approcher le repricing exact pour un choc modéré."""
    face, c, T, y, f = 100, 0.035, 10.0, 0.04, 2
    risk = bond_risk(face, c, T, y, f)
    for dy in (0.0025, 0.005, 0.01):
        exact = price_from_yield(face, c, T, y + dy, f) / risk.price - 1
        assert risk.approx_return(dy) == pytest.approx(exact, abs=2e-4)


def test_cashflows_sum_to_principal_plus_coupons():
    flows = cashflows(100, 0.05, 3.0, 2)
    assert len(flows) == 6
    assert sum(cf for _, cf in flows) == pytest.approx(100 + 6 * 2.5)


# ---------------------------------------------------------------- #
# Courbe zéro-coupon
# ---------------------------------------------------------------- #
@pytest.fixture
def curve() -> ZeroCurve:
    return ZeroCurve({0.25: 0.030, 1.0: 0.031, 2.0: 0.032, 5.0: 0.034,
                      10.0: 0.037, 20.0: 0.039, 30.0: 0.040}, "TEST")


def test_curve_interpolation(curve):
    assert curve.rate(1.0) == pytest.approx(0.031)
    assert curve.rate(3.5) == pytest.approx(0.033, abs=1e-9)     # mi-chemin 2Y-5Y
    assert curve.rate(50.0) == pytest.approx(0.040)              # extrapolation plate


def test_effective_duration_close_to_analytic(curve):
    """Sur une courbe quasi plate, duration effective ≈ duration modifiée."""
    flat = ZeroCurve({t: 0.035 for t in (0.25, 1, 2, 5, 10, 20, 30)})
    flows = cashflows(100, 0.035, 10.0, 1)
    eff = effective_duration(flows, flat)
    analytic = bond_risk(100, 0.035, 10.0, 0.035, 1).modified_duration
    assert eff == pytest.approx(analytic, rel=5e-3)


def test_key_rate_durations_sum_to_effective_duration(curve):
    flows = cashflows(100, 0.04, 12.0, 2)
    krd = key_rate_durations(flows, curve)
    assert sum(krd.values()) == pytest.approx(effective_duration(flows, curve), rel=1e-4)


def test_key_rate_durations_concentrate_near_maturity(curve):
    """Un zéro-coupon 10 ans doit charger le segment 10Y et presque rien ailleurs."""
    flows = [(10.0, 100.0)]
    krd = key_rate_durations(flows, curve)
    assert krd[10.0] == pytest.approx(max(krd.values()))
    assert krd[10.0] > 0.9 * sum(krd.values())
    assert abs(krd[0.25]) < 1e-6


def test_effective_convexity_positive_for_vanilla_bond(curve):
    flows = cashflows(100, 0.04, 15.0, 2)
    assert effective_convexity(flows, curve) > 0


def test_spread_duration_positive_and_below_rate_duration(curve):
    flows = cashflows(100, 0.045, 8.0, 2)
    sd = spread_duration(flows, curve, spread=0.01)
    assert sd > 0
    assert sd < effective_duration(flows, curve) + 1e-6


def test_price_from_curve_matches_manual_discounting(curve):
    flows = [(1.0, 5.0), (2.0, 105.0)]
    manual = 5 / (1 + curve.rate(1.0)) ** 1 + 105 / (1 + curve.rate(2.0)) ** 2
    assert price_from_curve(flows, curve) == pytest.approx(manual)


def test_matured_bond_has_no_future_cashflow():
    assert cashflows(100, 0.05, 0.0, 2) == [(0.0, 100.0)]
    with pytest.raises(ValueError):
        yield_from_price(0, 100, 0.05, 5.0)
