"""Event Radar : construction sans réseau, sur un fournisseur factice."""

from __future__ import annotations

from datetime import date, timedelta

from models.datapoint import DataPoint, missing
from models.enums import AssetClass, DataQuality, PortfolioType
from models.portfolio import Portfolio, Position
from services.events.radar import build_radar


class FakeMarketData:
    def __init__(self, macro_rows):
        self.macro_rows = macro_rows

    def earnings_calendar(self, days_ahead, symbols=None):
        return missing(note="pas de calendrier de résultats")

    def economic_calendar(self, days_ahead):
        if self.macro_rows is None:
            return missing(note="calendrier macro indisponible")
        return DataPoint(self.macro_rows, DataQuality.API_CONFIRMED, "FMP")


def _portfolio() -> Portfolio:
    return Portfolio("test", PortfolioType.FIXED_INCOME, positions=[
        Position("DE0001", "Bund", AssetClass.GOV_BOND, market_value=1_000_000.0)])


def test_radar_runs_with_a_macro_calendar():
    """Régression : `macro` était utilisé avant d'être lu (UnboundLocalError)."""
    when = (date.today() + timedelta(days=3)).isoformat()
    events, warnings = build_radar(_portfolio(), FakeMarketData([{"event": "ECB Interest Rate Decision",
                                                                  "date": when, "country": "EU"}]))
    assert any("ECB" in e.title for e in events)
    assert any("Calendrier macro FMP" in w for w in warnings)


def test_radar_runs_without_any_calendar():
    events, warnings = build_radar(_portfolio(), FakeMarketData(None))
    assert events == []
    assert any("indisponible" in w for w in warnings)
