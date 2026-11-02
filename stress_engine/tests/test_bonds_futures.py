"""Obligations (Z-spread, DV01 par pilier, CS01) et futures EUREX (CF, panier, CTD) contre des références fermées."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import AS_OF
from rates_analytics.bonds import FixedRateBond
from rates_analytics.conventions.market import get_convention
from rates_analytics.curves.shifted import SpreadedCurve

from stress_engine import Holding, InstrumentType, Mapper, MappingMethod
from stress_engine.instruments.bonds import map_bond
from stress_engine.instruments.futures import (
    ContractSpec,
    FutureMappingError,
    basket,
    conversion_factor,
    delivery_date,
    parse_contract,
    select_ctd,
)


def bond_holding(**kw) -> Holding:
    base = dict(id="X", instrument=InstrumentType.BOND, issuer_type="CORP", nominal=1_000_000, coupon=0.03,
                maturity=date(2033, 10, 8), issue_date=date(2023, 10, 8), country="FR", rating="A", sector="Utilities")
    base.update(kw)
    return Holding(**base)


# ------------------------------------------------------------------ obligations
def test_zero_coupon_on_flat_curve_matches_closed_form(space, flat_market):
    """Zéro-coupon, courbe plate 3 % : D_eff = t (ACT/365F), KRD répartie par la tente entre 5 et 10 ans."""
    h = bond_holding(coupon=0.0, maturity=date(2033, 10, 6), issue_date=date(2023, 10, 6), rating="AAA")
    m = map_bond(h, space, flat_market, clean=80.0)
    # P = 100·DF(t_paiement)/DF(t_règlement) : la duration est le temps du règlement (T+2) au paiement
    t = (date(2033, 10, 6) - m.details["settlement"]).days / 365.0
    assert m.details["duration"] == pytest.approx(t, abs=1e-5)
    dirty = m.details["dirty"]
    scale = 1_000_000 / 100 * dirty * 1e-4
    # Tentes en temps de courbe (depuis la date de courbe) : le paiement tombe entre 5 et 10 ans, le
    # règlement (T+2) avant le premier pilier, dont la tente est plate à gauche.
    tau = (date(2033, 10, 6) - AS_OF).days / 365.0
    tau_s = (m.details["settlement"] - AS_OF).days / 365.0
    assert m.delta("IR.DE.5Y") == pytest.approx(-tau * (10 - tau) / 5 * scale, rel=1e-6)
    assert m.delta("IR.DE.10Y") == pytest.approx(-tau * (tau - 5) / 5 * scale, rel=1e-6)
    assert m.delta("IR.DE.2Y") == pytest.approx(tau_s * scale, rel=1e-4)
    assert m.delta("CS.EUR.AAA.NONFIN") == pytest.approx(-t * scale, rel=1e-6)   # CS01 = DV01 total
    assert m.details["convexity"] == pytest.approx(t * t, rel=1e-3)


def test_z_spread_reprices_the_observed_price(space, market):
    h = bond_holding(price=101.25)
    m = map_bond(h, space, market)
    p = m.pricing
    curve, _ = market.curve_for("DE")                          # crédit : spread contre le Bund
    model = p.bond.dirty_price_from_curve(SpreadedCurve(curve, p.z_spread), p.settlement)
    assert model == pytest.approx(101.25 + p.bond.accrued_interest(p.settlement), abs=1e-8)
    assert m.method is MappingMethod.CURVE and m.details["z_spread_bp"] > 0


def test_sum_of_pillar_dv01_equals_effective_duration(space, market):
    for h in (bond_holding(price=99.0), bond_holding(issuer_type="GOV", country="IT", price=97.0,
                                                    maturity=date(2047, 3, 1), issue_date=date(2017, 3, 1))):
        m = map_bond(h, space, market)
        rates = sum(v for k, v in m.deltas().items() if k.startswith("IR."))
        assert rates == pytest.approx(-m.details["duration"] * m.market_value * 1e-4, rel=1e-6)


def test_government_bonds_have_no_credit_factor_and_use_their_country(space, market):
    m = map_bond(bond_holding(issuer_type="GOV", country="IT", price=100.0), space, market)
    assert all(k.startswith("IR.IT.") for k in m.deltas())


def test_corporates_carry_bund_rates_and_rating_sector_spread(space, market):
    m = map_bond(bond_holding(country="NL", rating="Baa3", sector="Banks", price=98.0), space, market)
    assert set(m.deltas()) == {"IR.DE.2Y", "IR.DE.5Y", "IR.DE.10Y", "CS.EUR.BBB.FIN"}


def test_missing_curve_keeps_the_country_factors(space, market):
    m = map_bond(bond_holding(issuer_type="GOV", country="ES", price=101.0), space, market)
    assert all(k.startswith("IR.ES.") for k in m.deltas())
    assert any("forme de courbe FR" in n for n in m.notes)


def test_money_market_maturity_goes_to_the_short_rate(space, market):
    m = map_bond(bond_holding(coupon=0.0, maturity=date(2027, 2, 15), issue_date=date(2026, 2, 15), price=99.3),
                 space, market)
    assert set(m.deltas()) == {"IR.EUR.ESTR", "CS.EUR.A.NONFIN"}


def test_theoretical_price_and_exclusion(space, market):
    m = map_bond(bond_holding(), space, market)                        # pas de prix
    assert m.method is MappingMethod.CURVE_THEORETICAL and m.details["z_spread_bp"] == 0
    mapper = Mapper(market)
    out = mapper.map_holding(bond_holding(coupon=None, price=100.0))
    assert out.method is MappingMethod.EXCLUDED and "coupon" in out.notes[0]
    assert out.exposure_value == pytest.approx(1_000_000)               # compte dans la couverture


def test_universe_bond_uses_its_static_data(space, market):
    isin = "SMPL-DE-20330215"
    m = map_bond(Holding(id=isin, instrument=InstrumentType.BOND, issuer_type="GOV", country="DE",
                         nominal=10_000_000), space, market)
    assert m.pricing.bond.coupon == pytest.approx(0.029) and m.details["clean"] == market.prices[isin]


# ------------------------------------------------------------------ futures
@pytest.mark.parametrize("text,expected", [
    ("FGBL DEC26", ("FGBL", 2026, 12)), ("FGBLZ6", ("FGBL", 2026, 12)), ("FGBM H27", ("FGBM", 2027, 3)),
    ("fgbs z26", ("FGBS", 2026, 12)), ("FOAT 03/2027", ("FOAT", 2027, 3)), ("FGBX MAR2027", ("FGBX", 2027, 3)),
    ("FGBLU6", ("FGBL", 2036, 9)),                       # U6 déjà passé en 2026 : décennie suivante
])
def test_contract_codes(text, expected):
    assert parse_contract(text, AS_OF) == expected


def test_contract_errors():
    with pytest.raises(FutureMappingError):
        parse_contract("BUND", AS_OF)


def test_delivery_date_is_the_tenth_or_next_target_business_day():
    assert delivery_date(2026, 12) == date(2026, 12, 10)            # jeudi
    assert delivery_date(2027, 4) == date(2027, 4, 12)              # samedi 10 -> lundi 12


def _annual_bond(coupon: float, maturity: date) -> FixedRateBond:
    return FixedRateBond(isin="B", coupon=coupon, maturity=maturity,
                         issue_date=maturity.replace(year=maturity.year - 15), convention=get_convention("BUND"))


def test_conversion_factor_closed_forms():
    """Sur une date de coupon : CF = c/6 % × (1 − 1,06^−n) + 1,06^−n ; un 6 % vaut exactement 1."""
    delivery = date(2026, 12, 10)
    assert conversion_factor(_annual_bond(0.06, date(2036, 12, 10)), delivery) == pytest.approx(1.0, abs=1e-6)
    n, c = 10, 0.025
    ref = c / 0.06 * (1 - 1.06 ** -n) + 1.06 ** -n
    assert conversion_factor(_annual_bond(c, date(2036, 12, 10)), delivery) == pytest.approx(round(ref, 6), abs=1e-6)


SPEC = ContractSpec("FGBL", "Euro-Bund", "DE", 8.5, 10.5)


def test_basket_window_and_country(market):
    names = [b.isin for b in basket(SPEC, date(2026, 12, 10), market.bonds)]
    assert names == ["SMPL-DE-20350815", "SMPL-DE-20360215", "SMPL-DE-20360815"]


def test_ctd_rules(market):
    d = date(2026, 12, 10)
    given = select_ctd(SPEC, d, market.bonds, market.prices, None, "SMPL-DE-20360215")
    assert given.bond.isin == "SMPL-DE-20360215" and "fourni" in given.rule
    converted = select_ctd(SPEC, d, market.bonds, market.prices, None)
    best = min(converted.table, key=lambda r: r["clean"] / r["cf"])
    assert converted.bond.isin == best["isin"] and "converti" in converted.rule
    gross = select_ctd(SPEC, d, market.bonds, market.prices, 130.0)
    assert gross.bond.isin == min(gross.table, key=lambda r: r["gross_basis"])["isin"]
    assert select_ctd(SPEC, d, {}, {}, None) is None


def test_future_dv01_is_ctd_dv01_over_cf_times_lots(space, market):
    mapper = Mapper(market)
    fut = mapper.map_holding(Holding(id="F", instrument=InstrumentType.FUTURE, contract="FGBL DEC26", lots=-50))
    assert fut.method is MappingMethod.CTD and fut.market_value == 0.0
    ctd, cf = fut.details["ctd"], fut.details["cf"]
    one = map_bond(Holding(id=ctd, instrument=InstrumentType.BOND, issuer_type="GOV", country="DE",
                           nominal=100_000), space, market)
    for factor, delta in one.deltas().items():
        assert fut.delta(factor) == pytest.approx(delta / cf * -50, rel=1e-9)
    assert fut.exposure_value == pytest.approx(-50 * 100_000 * fut.details["futures_price"] / 100)
    assert fut.details["futures_price"] == pytest.approx(fut.details["ctd_clean"] / cf)   # prix théorique


def test_future_without_basket_uses_a_notional_bond(space, flat_market):
    fut = Mapper(flat_market).map_holding(Holding(id="F", instrument=InstrumentType.FUTURE, contract="FGBX DEC26",
                                                  lots=10))
    assert fut.method is MappingMethod.NOTIONAL_CTD and fut.details["ctd"].endswith("NOTIONNEL")
    assert sum(fut.deltas().values()) < 0 and any("approximation" in n for n in fut.notes)


def test_future_errors_are_reported(market):
    mapper = Mapper(market)
    assert "lots" in mapper.map_holding(Holding(id="F", instrument=InstrumentType.FUTURE, contract="FGBL DEC26")
                                        ).notes[0]
    assert "eurex.yaml" in mapper.map_holding(Holding(id="F", instrument=InstrumentType.FUTURE, contract="FXXX DEC26",
                                                      lots=1)).notes[0]
    assert "échu" in mapper.map_holding(Holding(id="F", instrument=InstrumentType.FUTURE, contract="FGBL SEP26",
                                                lots=1)).notes[0]
