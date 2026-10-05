import pandas as pd
import pytest

from market_monitor.data.service import MarketDataService
from market_monitor.exceptions import ConfigError, InvalidRequestError, ProviderUnavailableError
from tests.conftest import FakeProvider, bday_series

SYMBOLS = {
    "SX5E": {"bloomberg": "SX5E Index", "free": "^STOXX50E"},
    "ESTR": {"free": "ecb:EST/B.EU000A2X2A25.WT"},
    "ITRAXX": {"bloomberg": "ITRXEBE CBIN Curncy"},
}


def _providers():
    bbg = FakeProvider("bloomberg", {"SX5E Index": bday_series("2026-09-01", "2026-10-02")})
    free = FakeProvider("free", {"^STOXX50E": bday_series("2026-09-01", "2026-10-02", 1.0),
                                 "ecb:EST/B.EU000A2X2A25.WT": bday_series("2026-09-01", "2026-10-02", 2.0)})
    return bbg, free


def test_priority_and_fallback_per_instrument():
    bbg, free = _providers()
    result = MarketDataService([bbg, free]).get_history(SYMBOLS, "2026-09-01", "2026-10-02")
    assert result.sources == {"SX5E": "bloomberg", "ESTR": "free"}
    assert list(result.data.columns) == ["SX5E", "ESTR", "ITRAXX"]
    assert "bloomberg" in result.errors["ITRAXX"]
    assert free.calls[0].tickers == ("ecb:EST/B.EU000A2X2A25.WT",)  # SX5E not re-requested


def test_unavailable_primary_falls_back():
    bbg, free = _providers()
    bbg.fail = ProviderUnavailableError("no terminal")
    result = MarketDataService([bbg, free]).get_history(SYMBOLS, "2026-09-01", "2026-10-02")
    assert result.sources["SX5E"] == "free"
    assert result.data["SX5E"].iloc[0] == 1.0
    assert "no terminal" in result.errors["ITRAXX"]


def test_snapshot_fallback():
    bbg, free = _providers()
    bbg.data.clear()
    today = pd.Timestamp.today().normalize()
    free.data["^STOXX50E"] = bday_series(str(today - pd.Timedelta(days=30)), str(today))
    snap = MarketDataService([bbg, free]).get_snapshot({"SX5E": SYMBOLS["SX5E"]})
    assert snap.sources == {"SX5E": "free"}
    assert snap.data.loc["SX5E", "as_of"] == free.data["^STOXX50E"].index[-1]


def test_unmapped_id_is_reported():
    bbg, _ = _providers()
    result = MarketDataService([bbg]).get_history({"X": {"fmp": "X"}}, "2026-09-01", "2026-10-02")
    assert "no ticker mapped" in result.errors["X"]


@pytest.mark.parametrize("symbols", [{}, {"A": {"free": ""}}, {"A": {"free": " X"}}, {"A": "X"}])
def test_invalid_symbol_maps(symbols):
    with pytest.raises(InvalidRequestError):
        MarketDataService([FakeProvider()]).get_history(symbols, "2026-09-01", "2026-10-02")


def test_service_requires_unique_providers():
    with pytest.raises(ConfigError):
        MarketDataService([])
    with pytest.raises(ConfigError):
        MarketDataService([FakeProvider("x"), FakeProvider("x")])
