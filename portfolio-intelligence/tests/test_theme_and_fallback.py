"""Tests du système de thème et des replis Yahoo."""

from __future__ import annotations

import pandas as pd
import pytest

from config.theme import (
    DARK,
    LIGHT,
    PALETTES,
    categorical_colors,
    palette,
    plotly_template,
    css,
)


# ---------------------------------------------------------------- #
# Palettes
# ---------------------------------------------------------------- #
def test_both_palettes_define_every_token():
    for name, pal in PALETTES.items():
        for field in ("ink", "paper", "surface", "rule", "accent", "positive", "negative"):
            assert getattr(pal, field).startswith("#"), f"{name}.{field}"
        assert len(pal.categorical) >= 8, name
        assert len(pal.asset_classes) >= 8, name
        assert set(pal.quality) == {"VERIFIED", "API_CONFIRMED", "CACHED",
                                   "ESTIMATED", "UNVERIFIED"}, name


def test_categorical_colours_are_all_distinct():
    """Le défaut à corriger : un dégradé monochrome rendait les parts indistinctes."""
    for name, pal in PALETTES.items():
        assert len(set(pal.categorical)) == len(pal.categorical), name
        assert len(set(pal.asset_classes.values())) == len(pal.asset_classes), name


def test_sign_colours_are_never_reused_as_categories():
    """Dans cette application, le rouge signifie une perte — et rien d'autre."""
    for pal in PALETTES.values():
        assert pal.positive not in pal.categorical
        assert pal.negative not in pal.categorical
        assert pal.positive not in pal.asset_classes.values()
        assert pal.negative not in pal.asset_classes.values()


def test_asset_classes_keep_their_colour_across_charts():
    keys = ["EQUITY", "GOV_BOND", "CREDIT"]
    assert categorical_colors(keys, "dark") == [DARK.asset_classes[k] for k in keys]
    reordered = ["CREDIT", "EQUITY"]
    assert categorical_colors(reordered, "dark")[1] == DARK.asset_classes["EQUITY"]


def test_unknown_categories_get_distinct_colours():
    sectors = ["Technology", "Energy", "Healthcare", "Utilities", "Financial Services"]
    colors = categorical_colors(sectors, "dark")
    assert len(set(colors)) == len(sectors)


def test_dark_is_the_default_mode():
    assert palette().name == "dark"
    assert palette("light") is LIGHT
    assert palette("inconnu").name == "dark"


def test_plotly_template_and_css_follow_the_mode():
    dark_layout = plotly_template("dark")["layout"]
    light_layout = plotly_template("light")["layout"]
    assert dark_layout["font"]["color"] == DARK.ink
    assert light_layout["font"]["color"] == LIGHT.ink
    assert dark_layout["paper_bgcolor"] == "rgba(0,0,0,0)"      # suit le fond de la page
    assert DARK.paper in css("dark")
    assert LIGHT.paper in css("light")


def test_dark_background_is_not_pure_black():
    """Le noir absolu écrase les contrastes et fait vibrer le texte fin."""
    assert DARK.paper.lower() not in ("#000", "#000000")


# ---------------------------------------------------------------- #
# Graphiques : ils doivent se construire dans les deux modes
# ---------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["dark", "light"])
def test_all_charts_build_in_both_modes(mode, monkeypatch):
    import config.theme as theme

    monkeypatch.setattr(theme, "active_mode", lambda: mode)
    from components import charts

    idx = pd.bdate_range("2025-01-01", periods=120)
    series = pd.Series(range(100, 220), index=idx, dtype=float)
    mix = {"EQUITY": 0.5, "GOV_BOND": 0.3, "CASH": 0.2}
    impacts = {"EQUITY": -0.03, "GOV_BOND": 0.01}

    figures = [
        charts.bar_breakdown(mix),
        charts.allocation_bar(mix),
        charts.donut(mix),
        charts.impact_bars(impacts),
        charts.ranked_impacts([("a", -0.04), ("b", 0.02)]),
        charts.waterfall(["a", "b"], [-0.02, 0.01]),
        charts.line_series({"NAV": series}),
        charts.nav_area(series),
        charts.yield_curve({"US": {"2Y": 0.03, "10Y": 0.04}}),
        charts.krd_bars({"2Y": 0.4, "5Y": 1.2, "10Y": 2.7}),
        charts.heatmap(pd.DataFrame([[0.01, -0.02]], index=["EQ"], columns=["s1", "s2"])),
        charts.valuation_history_chart(pd.Series({"2023": 20.0, "2024": 24.0}), 19.2, 22.0, "PER"),
    ]
    for fig in figures:
        assert fig.to_dict()["data"] is not None


# ---------------------------------------------------------------- #
# Repli Yahoo sur les fonctions FMP
# ---------------------------------------------------------------- #
def test_yahoo_exposes_the_fmp_fallback_surface():
    from data.providers.yahoo import US_YIELD_INDICES, YahooProvider

    for method in ("treasury_curve", "earnings", "earnings_calendar", "fundamentals",
                   "annual_eps_and_revenue", "dividends", "price_history", "last_price"):
        assert callable(getattr(YahooProvider, method)), method
    assert set(US_YIELD_INDICES) == {"3M", "5Y", "10Y", "30Y"}


def test_orchestrator_falls_back_to_yahoo_for_the_curve(monkeypatch):
    from data.market_data import MarketDataService
    from data.providers.base import ProviderUnavailable

    service = MarketDataService()
    monkeypatch.setattr(service.fmp, "treasury_rates",
                        lambda *a, **k: (_ for _ in ()).throw(ProviderUnavailable("FMP hors plan")))
    monkeypatch.setattr(service.yahoo, "treasury_curve",
                        lambda: ({"3M": 0.0525, "5Y": 0.041, "10Y": 0.0428, "30Y": 0.045}, False))

    dp = service.treasury_curve()
    assert dp.source == "YAHOO"
    assert dp.value["10Y"] == pytest.approx(0.0428)
    # Une courbe interpolée sur quatre points n'est pas une donnée de valorisation.
    assert dp.quality.value == "ESTIMATED"
    assert "interpol" in dp.note.lower()


def test_orchestrator_falls_back_to_yahoo_for_earnings(monkeypatch):
    from data.market_data import MarketDataService
    from data.providers.base import ProviderUnavailable

    service = MarketDataService()
    monkeypatch.setattr(service.fmp, "earnings",
                        lambda *a, **k: (_ for _ in ()).throw(ProviderUnavailable("hors plan")))
    monkeypatch.setattr(service.yahoo, "earnings",
                        lambda symbol, limit=12: ([{"symbol": symbol, "date": "2026-10-28",
                                                    "epsEstimated": 2.1, "eps": None}], False))
    dp = service.earnings("AAPL")
    assert dp.source == "YAHOO"
    assert dp.value[0]["epsEstimated"] == 2.1
    assert "chiffre d'affaires" in dp.note


def test_earnings_calendar_uses_portfolio_symbols_when_fmp_is_absent(monkeypatch):
    from data.market_data import MarketDataService
    from data.providers.base import ProviderUnavailable

    service = MarketDataService()
    monkeypatch.setattr(service.fmp, "earnings_calendar",
                        lambda *a, **k: (_ for _ in ()).throw(ProviderUnavailable("hors plan")))
    called = {}

    def fake(symbols, start, end):
        called["symbols"] = symbols
        return [{"symbol": symbols[0], "date": str(start)}], False

    monkeypatch.setattr(service.yahoo, "earnings_calendar", fake)
    dp = service.earnings_calendar(14, symbols=["MSFT", "ASML.AS"])
    assert dp.source == "YAHOO"
    assert called["symbols"] == ["MSFT", "ASML.AS"]


def test_macro_calendar_states_that_no_fallback_exists(monkeypatch):
    from data.market_data import MarketDataService
    from data.providers.base import ProviderUnavailable

    service = MarketDataService()
    monkeypatch.setattr(service.fmp, "economic_calendar",
                        lambda *a, **k: (_ for _ in ()).throw(ProviderUnavailable("hors plan")))
    dp = service.economic_calendar(14)
    assert not dp.is_usable
    assert "Yahoo n'expose pas" in dp.note
