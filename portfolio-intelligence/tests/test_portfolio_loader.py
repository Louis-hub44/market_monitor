"""Tests du moteur d'import de portefeuilles."""

from __future__ import annotations

import io
from datetime import date

import pytest

from models.enums import AssetClass, PortfolioType
from services.portfolio.loader import load_portfolio, map_columns, _to_float
import pandas as pd


EQUITY_CSV = """ticker;name;asset class;quantity;price;currency;sector;country
MSFT;Microsoft;Equity;1000;400,50;USD;Technology;US
SAN.PA;Sanofi;Equity;5000;92,10;EUR;Healthcare;FR
CASH.EUR;Cash EUR;Cash;100000;1;EUR;;
"""

BOND_CSV = """ISIN,Bond name,Nominal,Price,Coupon,Maturity,Currency,Country,Rating
DE0001102614,Bund 2.30% 2033,1000000,98.40,2.30,2033-02-15,EUR,DE,AAA
XS2434891404,Corporate 3.75% 2030,500000,99.40,3.75,15/03/2030,EUR,FR,A
"""


def test_column_mapping_recognises_french_and_english():
    df = pd.DataFrame(columns=["ISIN", "Libellé", "Poids", "Devise", "Échéance", "Coupon"])
    mapping, unmapped = map_columns(df)
    assert mapping["identifier"] == "ISIN"
    assert mapping["name"] == "Libellé"
    assert mapping["weight"] == "Poids"
    assert mapping["currency"] == "Devise"
    assert mapping["maturity"] == "Échéance"
    assert unmapped == []


def test_decimal_comma_is_parsed():
    assert _to_float("400,50") == pytest.approx(400.50)
    assert _to_float("1 234,56") == pytest.approx(1234.56)
    assert _to_float("1,234.56") == pytest.approx(1234.56)
    assert _to_float("") is None


def test_equity_import_detects_type_and_separates_cash():
    portfolio, report = load_portfolio(EQUITY_CSV.encode(), file_name="equity.csv")
    assert report.ok
    assert report.detected_type is PortfolioType.EQUITY
    assert len(portfolio.positions) == 2          # le cash est sorti des positions
    assert portfolio.cash == pytest.approx(100_000)
    assert portfolio.nav == pytest.approx(1000 * 400.50 + 5000 * 92.10 + 100_000)
    assert all(p.asset_class is AssetClass.EQUITY for p in portfolio.positions)


def test_bond_import_infers_asset_class_and_scales_price():
    portfolio, report = load_portfolio(BOND_CSV.encode(), file_name="bonds.csv")
    assert report.ok
    assert report.detected_type is PortfolioType.FIXED_INCOME
    bund = next(p for p in portfolio.positions if p.identifier == "DE0001102614")
    assert bund.asset_class is AssetClass.GOV_BOND
    assert bund.coupon == pytest.approx(0.023)                 # 2.30 -> décimal
    assert bund.maturity == date(2033, 2, 15)
    assert bund.market_value == pytest.approx(1_000_000 * 98.40 / 100)   # prix en % du nominal
    corp = next(p for p in portfolio.positions if p.identifier == "XS2434891404")
    assert corp.asset_class is AssetClass.CREDIT
    assert corp.maturity == date(2030, 3, 15)                  # format jour/mois/année


def test_missing_valuation_column_is_rejected_cleanly():
    portfolio, report = load_portfolio(b"isin,name\nFR0000,Test\n", file_name="bad.csv")
    assert not report.ok
    assert report.errors
    assert portfolio.positions == []


def test_weights_and_exposures_sum_to_one():
    portfolio, _ = load_portfolio(EQUITY_CSV.encode(), file_name="equity.csv")
    assert sum(portfolio.weights().values()) + portfolio.cash / portfolio.nav == pytest.approx(1.0)
    assert sum(portfolio.exposure_by_currency().values()) == pytest.approx(1.0)


# ------------------------------------------------------------------ #
# Fichiers fournis en poids (régressions)
# ------------------------------------------------------------------ #
WEIGHT_CSV = """ticker,name,asset class,weight,currency
MSFT,Microsoft,Equity,0.60,USD
SAN.PA,Sanofi,Equity,0.30,EUR
CASH.EUR,Cash EUR,Cash,0.10,EUR
"""


def test_weight_only_file_gets_a_readable_notional_nav():
    """Régression : la NAV valait 1,0 € et l'interface affichait « NAV 1 »."""
    portfolio, report = load_portfolio(WEIGHT_CSV.encode(), file_name="w.csv")
    assert portfolio.is_notional is True
    assert portfolio.nav == pytest.approx(1_000_000)
    assert portfolio.cash == pytest.approx(100_000)
    weights = portfolio.weights()
    assert weights["MSFT"] == pytest.approx(0.60)
    assert weights["SAN.PA"] == pytest.approx(0.30)
    assert any("notionnelle" in note for note in report.inferences)


def test_notional_scaling_is_configurable():
    portfolio, _ = load_portfolio(WEIGHT_CSV.encode(), file_name="w.csv", notional=250_000_000)
    assert portfolio.nav == pytest.approx(250_000_000)
    assert portfolio.weights()["MSFT"] == pytest.approx(0.60)


def test_weights_not_summing_to_one_are_renormalised():
    csv = b"ticker,name,asset class,weight\nA,A,Equity,30\nB,B,Equity,70\n"
    portfolio, report = load_portfolio(csv, file_name="pct.csv")
    assert sum(portfolio.weights().values()) == pytest.approx(1.0)
    assert portfolio.weights()["A"] == pytest.approx(0.30)
    assert any("renormalis" in note for note in report.inferences)


def test_real_valuations_are_never_rescaled():
    portfolio, _ = load_portfolio(EQUITY_CSV.encode(), file_name="equity.csv")
    assert portfolio.is_notional is False
    assert portfolio.nav == pytest.approx(1000 * 400.50 + 5000 * 92.10 + 100_000)


def test_cash_quoted_at_par_is_not_multiplied_by_hundred():
    """Régression : 1 500 000 de trésorerie cotée 100 donnait 150 000 000."""
    csv = ("isin,name,asset class,nominal,price,coupon,maturity,currency\n"
           "DE0001102614,Bund,Government,1000000,98.40,2.30,2033-02-15,EUR\n"
           "CASH.EUR,Cash EUR,Cash,1500000,100,,,EUR\n").encode()
    portfolio, report = load_portfolio(csv, file_name="fi.csv")
    assert portfolio.cash == pytest.approx(1_500_000)
    assert portfolio.nav == pytest.approx(984_000 + 1_500_000)
    assert any("au pair" in note for note in report.inferences)


def test_sample_files_all_load_with_coherent_nav():
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent / "samples"
    for path in sorted(root.glob("*.csv")):
        portfolio, report = load_portfolio(path, file_name=path.name)
        assert report.ok, path.name
        assert portfolio.nav > 1_000, f"{path.name}: NAV incohérente ({portfolio.nav})"
        total = sum(portfolio.weights().values()) + portfolio.cash / portfolio.nav
        assert total == pytest.approx(1.0, abs=1e-6), path.name


# ------------------------------------------------------------------ #
# Détection d'unités et de conventions (fichiers réels de gérants)
# ------------------------------------------------------------------ #
VALUATION_CSV = """isin,bond Name,asset class,nominal,price,coupon,maturity,currency,rating,spread
FR0014014DW3,RTEFRA 3.875% 11/37,Bonds,504445.1123,97.976469,3.875,24/11/2037,EUR,A,85.231134
XS2325733413,STLA 0.625% 03/27,Bonds,301992.34758,98.481352,0.625,30/03/2027,EUR,BBB,120.5
FR001400EBN2,SIENNA TRESORERIE PLUS,Mutual funds,3141995.9716,1168.746,,,EUR,,
DE000F3A9ET3,EURO-BUND 09/26 EUREX,Futures,0,124.72,100000,15/08/2035,EUR,,
CASH.EUR,Cash EUR,Cash,3264909.11,100,,,EUR,,
"""


def test_market_value_column_is_not_multiplied_by_price():
    """Régression : une colonne « nominal » contenant des valorisations était
    multipliée par le prix — erreur d'un facteur 1 000 sur une part de fonds
    cotée 1 168 €."""
    portfolio, report = load_portfolio(VALUATION_CSV.encode(), file_name="ptf.csv")
    expected = 504445.1123 + 301992.34758 + 3141995.9716 + 0.0 + 3264909.11
    assert portfolio.nav == pytest.approx(expected, rel=1e-9)
    fund = next(p for p in portfolio.positions if p.identifier == "FR001400EBN2")
    assert fund.market_value == pytest.approx(3_141_995.9716)
    assert any("valorisation" in note.lower() for note in report.inferences)


def test_valuation_detection_can_be_forced_either_way():
    as_quantity, _ = load_portfolio(VALUATION_CSV.encode(), file_name="p.csv",
                                    quantity_means_value=False)
    as_value, _ = load_portfolio(VALUATION_CSV.encode(), file_name="p.csv",
                                 quantity_means_value=True)
    assert as_quantity.nav != pytest.approx(as_value.nav)
    assert as_value.nav == pytest.approx(504445.1123 + 301992.34758 + 3141995.9716 + 3264909.11)


def test_round_quantities_are_still_treated_as_nominal():
    """Un vrai nominal (rond) doit continuer d'être multiplié par le prix."""
    csv = ("isin,name,asset class,nominal,price,coupon,maturity,currency\n"
           "DE0001102614,Bund,Government,1000000,98.40,2.30,2033-02-15,EUR\n"
           "FR0014008LR2,OAT,Government,2000000,99.20,3.00,2034-05-25,EUR\n").encode()
    portfolio, _ = load_portfolio(csv, file_name="fi.csv")
    assert portfolio.nav == pytest.approx(984_000 + 1_984_000)


def test_sub_one_percent_coupons_survive():
    """Régression : la règle « si coupon > 1 alors /100 » détruisait un 0,625 %.

    L'unité est une propriété de la colonne, décidée sur la médiane."""
    portfolio, _ = load_portfolio(VALUATION_CSV.encode(), file_name="p.csv")
    stla = next(p for p in portfolio.positions if p.identifier == "XS2325733413")
    rte = next(p for p in portfolio.positions if p.identifier == "FR0014014DW3")
    assert stla.coupon == pytest.approx(0.00625)
    assert rte.coupon == pytest.approx(0.03875)


def test_decimal_coupon_column_is_left_alone():
    csv = ("isin,name,asset class,nominal,price,coupon,maturity\n"
           "A,A,Government,1000000,99,0.0325,2033-02-15\n"
           "B,B,Government,1000000,99,0.0410,2035-02-15\n").encode()
    portfolio, _ = load_portfolio(csv, file_name="d.csv")
    assert portfolio.positions[0].coupon == pytest.approx(0.0325)


def test_futures_are_isolated_not_priced_as_bonds():
    """Un « EURO-BUND FUTURE » contient les mots-clés d'un souverain et porte une
    taille de contrat dans la colonne coupon : il doit être mis à l'écart."""
    portfolio, report = load_portfolio(VALUATION_CSV.encode(), file_name="p.csv")
    future = next(p for p in portfolio.positions if p.identifier == "DE000F3A9ET3")
    assert future.asset_class is AssetClass.OTHER
    assert future.coupon is None
    assert any("dérivés" in note for note in report.inferences)
    assert any("avant couverture" in note for note in report.inferences)


def test_spread_unit_is_normalised_to_basis_points():
    decimal_spreads = ("isin,name,asset class,nominal,price,coupon,maturity,spread\n"
                       "A,A,Credit,1000000,99,3.5,2033-02-15,0.0085\n"
                       "B,B,Credit,1000000,99,4.0,2035-02-15,0.0120\n").encode()
    portfolio, _ = load_portfolio(decimal_spreads, file_name="s.csv")
    assert portfolio.positions[0].spread_bp == pytest.approx(85.0)


def test_credit_portfolio_produces_a_coherent_duration():
    """Contrôle de bout en bout sur un fichier au format d'un export de gérant."""
    from services.fixed_income.analytics import analyse_portfolio
    from services.fixed_income.bond_math import ZeroCurve

    portfolio, _ = load_portfolio(VALUATION_CSV.encode(), file_name="p.csv")
    # Dans cet extrait le fonds monétaire pèse 80 % : MULTI_ASSET est la bonne
    # réponse. La détection ne bascule sur FIXED_INCOME qu'au-delà de 85 % de
    # poche obligataire — vérifié sur un portefeuille complet ci-dessous.
    assert portfolio.portfolio_type is PortfolioType.MULTI_ASSET
    curve = ZeroCurve({0.25: 0.021, 2: 0.024, 5: 0.027, 10: 0.031, 30: 0.035}, "EUR")
    analytics = analyse_portfolio(portfolio, {"DEFAULT": curve})
    assert analytics.coverage == pytest.approx(1.0)     # aucune ligne obligataire perdue
    assert 0.5 < analytics.modified_duration < 20


def test_bond_dominated_file_is_detected_as_fixed_income():
    rows = ["isin,name,asset class,nominal,price,coupon,maturity,currency"]
    rows += [f"BOND{i:03d},Corp {i},Bonds,{500000 + i * 37.13:.4f},98.5,3.5,2033-06-15,EUR"
             for i in range(20)]
    rows.append("CASH.EUR,Cash EUR,Cash,300000.55,100,,,EUR")
    portfolio, _ = load_portfolio("\n".join(rows).encode(), file_name="credit.csv")
    assert portfolio.portfolio_type is PortfolioType.FIXED_INCOME
    assert portfolio.nav == pytest.approx(
        sum(500000 + i * 37.13 for i in range(20)) + 300000.55, rel=1e-9
    )
