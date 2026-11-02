"""Taxonomie des facteurs et lecture des fichiers portefeuille."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stress_engine import FactorFamily, FactorSpace, InstrumentType, StressConfigError, load_lookthrough
from stress_engine.factors import family_of, ir_id, rating_bucket, sector_bucket
from stress_engine.portfolio import AssetClass, PortfolioFileError, holdings_from_frame, load_portfolio


# ------------------------------------------------------------------ facteurs
def test_factor_space_from_configuration(space):
    assert space.pillars == (2.0, 5.0, 10.0, 30.0)
    rates = space.of_family(FactorFamily.RATES)
    assert len(rates) == 4 * 4 + 1 and "IR.DE.10Y" in space and "IR.EUR.ESTR" in space
    assert len(space.of_family(FactorFamily.CREDIT)) == 7 * 2
    assert {"EQ.SX5E", "FX.USD", "VOL.V2X", "CMD.BRENT", "CS.EUR.BBB.FIN"} <= set(space.ids)
    assert space.get("IR.IT.30Y").pillar_years == 30.0 and space.get("IR.IT.30Y").unit == "bp"
    assert space.get("EQ.SX5E").unit == "rendement"


def test_unknown_or_unsorted_configuration_is_rejected(config):
    bad = {**config.factors, "rates": {**config.factors["rates"], "pillars": [10, 2]}}
    with pytest.raises(StressConfigError):
        FactorSpace.from_config(bad)
    with pytest.raises(StressConfigError):
        family_of("XX.FOO")
    with pytest.raises(StressConfigError):
        FactorSpace.from_config(config.factors).get("IR.PT.10Y")


@pytest.mark.parametrize("rating,bucket", [
    ("AAA", "AAA"), ("AA+", "AA"), ("Aa3", "AA"), ("A-", "A"), ("A2", "A"), ("BBB+", "BBB"), ("Baa1", "BBB"),
    ("BB-", "BB"), ("Ba2", "BB"), ("B+", "B"), ("B3", "B"), ("CCC+", "CCC"), ("Caa1", "CCC"), ("D", "CCC"),
    ("Aa1/AA+", "AA"), ("NR", None), ("", None), (None, None),
])
def test_rating_buckets(rating, bucket):
    assert rating_bucket(rating) == bucket


def test_sector_buckets_and_credit_factor(space):
    kw = space.financial_keywords
    assert sector_bucket("Banks", kw) == "FIN" and sector_bucket("Insurance", kw) == "FIN"
    assert sector_bucket("Banks covered", kw) == "FIN" and sector_bucket("Real Estate", kw) == "FIN"
    assert sector_bucket("Utilities", kw) == "NONFIN" and sector_bucket(None, kw) == "NONFIN"
    assert space.credit_factor("BBB-", "Banks") == ("CS.EUR.BBB.FIN", [])
    factor, notes = space.credit_factor("NR", "Industrials")
    assert factor == "CS.EUR.BBB.NONFIN" and "non reconnue" in notes[0]


def test_country_curve_resolution(space):
    assert space.curve_country("IT") == ("IT", False)
    assert space.curve_country("NL") == ("DE", True) and space.curve_country("BE") == ("FR", True)
    assert space.curve_country("XX") == ("DE", True)                 # inconnu : courbe de référence
    assert space.rate_factors("ES") == ["IR.ES.2Y", "IR.ES.5Y", "IR.ES.10Y", "IR.ES.30Y"]
    assert ir_id("de", 0.25) == "IR.DE.3M"


def test_ensure_adds_undeclared_factors(space):
    f = space.ensure("FX.SEK")
    assert f.family is FactorFamily.FX and "FX.SEK" in space


# ------------------------------------------------------------------ fichiers
def test_french_headers_semicolon_units_and_inference(tmp_path):
    (tmp_path / "ptf.csv").write_text(
        "ISIN;Libellé;Type;Devise;Nominal;Prix;Coupon;Échéance;Pays;Notation;Secteur;Contrat;Lots\n"
        "XS1;Corp A;obligation;eur;1 000 000;99,5;3,25;15/06/2031;fr;BBB+;Banks;;\n"
        "FUT1;Bund déc.;;EUR;;;;;;;;FGBL DEC26;-10\n"
        ";sans id;BOND;EUR;1;100;1;2030-01-01;DE;;;;\n", encoding="utf-8")
    pf = load_portfolio(tmp_path / "ptf.csv")
    assert len(pf) == 2 and "ligne 4 ignorée" in pf.notes[0]
    bond, fut = pf.holdings
    assert bond.instrument is InstrumentType.BOND and bond.coupon == pytest.approx(0.0325)
    assert bond.maturity == date(2031, 6, 15) and bond.nominal == 1_000_000 and bond.price == 99.5
    assert bond.country == "FR" and bond.currency == "EUR" and bond.asset_class is AssetClass.GOV_BOND
    assert fut.instrument is InstrumentType.FUTURE and fut.lots == -10 and fut.contract == "FGBL DEC26"


def test_missing_id_column_and_unknown_instrument():
    with pytest.raises(PortfolioFileError):
        holdings_from_frame(pd.DataFrame({"nom": ["x"]}))
    holdings, notes = holdings_from_frame(pd.DataFrame({"id": ["A"], "type": ["swap exotique"]}))
    assert holdings == [] and "type d'instrument" in notes[0]


def test_lookthrough_weights_in_percent_per_fund(tmp_path):
    (tmp_path / "lt.csv").write_text(
        "fund,weight,id,instrument,coupon,maturity\n"
        "F1,60,A,BOND,3,2030-01-01\nF1,40,B,BOND,2,2031-01-01\n"
        "F2,0.5,C,BOND,3,2032-01-01\nF2,0.5,D,BOND,1,2033-01-01\n", encoding="utf-8")
    lt = load_lookthrough(tmp_path / "lt.csv")
    assert [w for _, w in lt["F1"]] == [0.6, 0.4] and [w for _, w in lt["F2"]] == [0.5, 0.5]


def test_test_portfolio_is_complete(test_portfolio, test_lookthrough):
    kinds = {h.instrument for h in test_portfolio}
    assert kinds == {InstrumentType.BOND, InstrumentType.FUTURE, InstrumentType.MMF, InstrumentType.FUND,
                     InstrumentType.CASH}
    assert sum(h.asset_class is AssetClass.CREDIT for h in test_portfolio) == 21
    assert sum(h.instrument is InstrumentType.FUTURE for h in test_portfolio) == 6
    assert all(h.id.startswith(("TEST-", "SMPL-")) for h in test_portfolio)       # fictif, déclaré
    assert set(test_lookthrough) == {"MMF-B", "CREDIT-LT"}
