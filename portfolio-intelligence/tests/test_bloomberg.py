"""Tests du provider Bloomberg.

`blpapi` n'est pas installable hors poste équipé : ces tests couvrent donc les
**fonctions pures** (résolution de tickers, normalisation des réponses,
enrichissement des positions) et le comportement de dégradation quand le
Terminal est absent. La couche transport (`_send`, `_connect`) reste à valider
sur un poste réel — voir `self_test()` dans la page Qualité des données.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from data.providers.bloomberg import (
    BLOOMBERG_ECO_TICKERS,
    BloombergProvider,
    chunked,
    extract_krd,
    parse_earnings_dates,
    parse_eco_releases,
    parse_historical_rows,
    parse_reference_rows,
    to_bloomberg_ticker,
)
from models.enums import AssetClass
from models.portfolio import Portfolio, Position
from services.portfolio.enrichment import apply_bond_analytics, enrich_bond_risks


# ---------------------------------------------------------------- #
# Résolution des identifiants
# ---------------------------------------------------------------- #
def test_isin_gets_a_yellow_key_matching_the_asset_class():
    assert to_bloomberg_ticker("XS3038485689", "CREDIT") == "/isin/XS3038485689 Corp"
    assert to_bloomberg_ticker("DE0001102614", "GOV_BOND") == "/isin/DE0001102614 Govt"


def test_plain_ticker_gets_the_equity_key():
    assert to_bloomberg_ticker("MSFT", "EQUITY") == "MSFT Equity"
    assert to_bloomberg_ticker("SAN.PA") == "SAN.PA Equity"


def test_already_qualified_tickers_are_left_untouched():
    for ticker in ("MSFT US Equity", "GTDEM10Y Govt", "USGG10YR Index",
                   "EURUSD Curncy", "/isin/XS3038485689"):
        assert to_bloomberg_ticker(ticker) == ticker


def test_empty_identifier_is_not_turned_into_a_ticker():
    assert to_bloomberg_ticker("") == ""
    assert to_bloomberg_ticker("   ") == ""


def test_requests_are_batched():
    assert [len(b) for b in chunked(list(range(250)), 100)] == [100, 100, 50]
    assert list(chunked([], 100)) == []


# ---------------------------------------------------------------- #
# Normalisation des réponses
# ---------------------------------------------------------------- #
def test_reference_response_drops_empty_fields_but_keeps_the_rest():
    """Un droit manquant sur un champ ne doit pas invalider les autres."""
    rows = [
        {"security": "MSFT US Equity",
         "fields": {"PX_LAST": 412.5, "PE_RATIO": None, "CRNCY": "USD", "GICS_SECTOR_NAME": ""}},
        {"security": "VIDE Equity", "fields": {"PX_LAST": None}},
        {"security": None, "fields": {"PX_LAST": 1}},
    ]
    parsed = parse_reference_rows(rows)
    assert parsed == {"MSFT US Equity": {"PX_LAST": 412.5, "CRNCY": "USD"}}


def test_historical_response_is_sorted_and_typed():
    rows = [
        {"date": date(2026, 3, 2), "PX_LAST": 101.5},
        {"date": date(2026, 3, 1), "PX_LAST": "100.25"},
        {"date": date(2026, 3, 3), "PX_LAST": None},
    ]
    series = parse_historical_rows(rows)
    assert series == [{"date": "2026-03-01", "close": 100.25},
                      {"date": "2026-03-02", "close": 101.5}]


def test_key_rate_durations_are_mapped_onto_the_internal_grid():
    fields = {"KEY_RATE_DUR_2YR": 0.42, "KEY_RATE_DUR_10YR": "2.73",
              "KEY_RATE_DUR_30YR": None, "AUTRE": 9}
    assert extract_krd(fields) == {"2Y": 0.42, "10Y": 2.73}


def test_earnings_parsing_prefers_the_expected_date():
    record = parse_earnings_dates("MSFT US Equity", {
        "EXPECTED_REPORT_DT": datetime(2026, 10, 28),
        "ANNOUNCEMENT_DT": date(2026, 7, 29),
        "BEST_EPS": 3.42, "BEST_SALES": 68_500.0,
    })
    assert record["date"] == "2026-10-28"
    assert record["epsEstimated"] == 3.42
    assert parse_earnings_dates("X", {"BEST_EPS": 1.0}) is None


# ---------------------------------------------------------------- #
# Calendrier macro — la capacité sans équivalent gratuit
# ---------------------------------------------------------------- #
def test_eco_bulk_field_produces_dated_events_inside_the_window():
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    events = parse_eco_releases("FDTR Index", {
        "ECO_FUTURE_RELEASE_DATE_LIST": [
            {"Release Date": date(2026, 9, 16)},
            {"Release Date": date(2026, 11, 4)},      # hors fenêtre
        ],
        "BN_SURVEY_MEDIAN": 3.75, "PX_LAST": 4.00,
    }, start, end)
    assert len(events) == 1
    assert events[0]["date"] == "2026-09-16"
    assert events[0]["factor"] == "RATES"
    assert events[0]["estimate"] == 3.75
    assert events[0]["event"] == BLOOMBERG_ECO_TICKERS["FDTR Index"]["label"]


def test_eco_falls_back_to_the_scalar_field_when_the_bulk_list_is_denied():
    """Le champ « bulk » dépend de l'abonnement ; le scalaire est plus courant."""
    events = parse_eco_releases("CPI YOY Index", {"ECO_RELEASE_DT": date(2026, 9, 10)},
                                date(2026, 9, 1), date(2026, 9, 30))
    assert len(events) == 1
    assert events[0]["factor"] == "INFLATION"


def test_eco_returns_nothing_rather_than_guessing_a_date():
    assert parse_eco_releases("CPI YOY Index", {"BN_SURVEY_MEDIAN": 2.4},
                              date(2026, 9, 1), date(2026, 9, 30)) == []


# ---------------------------------------------------------------- #
# Enrichissement des positions
# ---------------------------------------------------------------- #
def _bond() -> Position:
    return Position("XS3038485689", "BLK 3.75% 07/35", AssetClass.CREDIT,
                    market_value=900_457, currency="EUR", coupon=0.0375,
                    maturity=date(2035, 7, 18), frequency=1)


def test_bloomberg_sensitivities_are_applied_to_the_position():
    position = _bond()
    applied = apply_bond_analytics(position, {
        "YAS_MOD_DUR": 7.82, "CONVEXITY_MID": 71.4, "SPREAD_DUR_MID": 7.55,
        "OAS_SPREAD_MID": 69.99, "YLD_YTM_MID": 3.81,
        "KEY_RATE_DUR_5YR": 2.1, "KEY_RATE_DUR_10YR": 5.4,
        "RTG_SP": "AA", "COUNTRY_ISO": "US", "NXT_CALL_DT": date(2035, 4, 18),
    })
    assert applied
    assert position.modified_duration == 7.82
    assert position.spread_bp == pytest.approx(69.99)
    assert position.ytm == pytest.approx(0.0381)        # pourcents -> décimal
    assert position.key_rate_durations == {"5Y": 2.1, "10Y": 5.4}
    assert position.metadata["risk_source"] == "BLOOMBERG"
    assert position.metadata["next_call"] == "2035-04-18"


def test_option_adjusted_duration_wins_over_the_analytic_field():
    position = _bond()
    apply_bond_analytics(position, {"YAS_MOD_DUR": 4.10, "DUR_ADJ_MID": 7.90})
    assert position.modified_duration == 4.10       # YAS intègre le call


def test_enrichment_never_overwrites_what_the_manager_supplied():
    position = _bond()
    apply_bond_analytics(position, {"YAS_MOD_DUR": 7.8, "CPN": 9.99,
                                    "MATURITY": date(2040, 1, 1)})
    assert position.coupon == pytest.approx(0.0375)   # valeur du fichier conservée
    assert position.maturity == date(2035, 7, 18)


def test_missing_fields_leave_the_position_untouched():
    position = _bond()
    assert apply_bond_analytics(position, {"RTG_SP": "AA"}) is False
    assert position.modified_duration is None


def test_enrichment_reports_lines_bloomberg_could_not_price():
    portfolio = Portfolio(name="p", portfolio_type="FIXED_INCOME",
                          positions=[_bond(),
                                     Position("XS_INCONNU", "Exotique", AssetClass.CREDIT,
                                              market_value=100_000, currency="EUR")])

    class FakeService:
        def bond_analytics(self, positions):
            return {"XS3038485689": {"YAS_MOD_DUR": 7.82, "KEY_RATE_DUR_10YR": 5.4}}

    report = enrich_bond_risks(portfolio, FakeService())
    assert report.enriched == ["XS3038485689"]
    assert report.skipped == ["XS_INCONNU"]
    assert report.krd_coverage == 1
    assert "1/2" in report.summary(2)


def test_analytics_uses_bloomberg_values_instead_of_recomputing():
    from services.fixed_income.analytics import analyse_position

    position = _bond()
    apply_bond_analytics(position, {"YAS_MOD_DUR": 7.82, "CONVEXITY_MID": 71.4,
                                    "SPREAD_DUR_MID": 7.55})
    risk = analyse_position(position, {})
    assert risk.modified_duration == 7.82
    assert risk.convexity == 71.4
    assert "Bloomberg" in risk.note


# ---------------------------------------------------------------- #
# Dégradation sans Terminal
# ---------------------------------------------------------------- #
def test_provider_declares_itself_unavailable_without_blpapi():
    provider = BloombergProvider()
    if provider.library_available:      # poste équipé : le test ne s'applique pas
        pytest.skip("blpapi présent sur ce poste")
    assert provider.is_configured is False
    assert "blpapi absent" in provider.health()["library"]


def test_bond_analytics_returns_empty_without_bloomberg():
    from data.market_data import MarketDataService

    service = MarketDataService()
    if service.bloomberg.is_configured:
        pytest.skip("Terminal disponible")
    assert service.bond_analytics([_bond()]) == {}


def test_non_us_curve_is_reported_as_needing_bloomberg(monkeypatch):
    from data.market_data import MarketDataService

    service = MarketDataService()
    if service.bloomberg.is_configured:
        pytest.skip("Terminal disponible")
    dp = service.treasury_curve("DE")
    assert not dp.is_usable
    assert "Bloomberg" in dp.note


def test_bloomberg_is_first_in_the_provider_chain():
    from data.market_data import MarketDataService

    assert [h["provider"] for h in MarketDataService().health()][0] == "BLOOMBERG"


def test_ssl_policy_is_a_no_op_for_a_local_socket():
    """La DAPI écoute sur localhost : aucun proxy d'inspection ne s'y interpose."""
    provider = BloombergProvider()
    provider.set_ssl_policy(True, None)
    assert provider._session.verify is not False     # session HTTP inchangée
