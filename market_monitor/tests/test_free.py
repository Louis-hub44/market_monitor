import pandas as pd
import pytest

from market_monitor.data.models import Field
from market_monitor.data.providers.free import (
    ECBClient,
    FreeProvider,
    YahooClient,
    extract_yahoo_field,
    parse_ecb_csv,
)
from market_monitor.exceptions import DataProviderError
from tests.conftest import FakeResponse, FakeSession

ECB_CSV = "KEY,FREQ,TIME_PERIOD,OBS_VALUE\nEST.B.X,B,2026-10-01,1.927\nEST.B.X,B,2026-10-02,1.931\n"


def _yahoo_frame(tickers):
    idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
    cols = pd.MultiIndex.from_product([["Close", "Open"], tickers], names=["Price", "Ticker"])
    return pd.DataFrame(1.0, index=idx, columns=cols)


def test_parse_ecb_csv():
    series = parse_ecb_csv(ECB_CSV)
    assert series.tolist() == [1.927, 1.931]
    with pytest.raises(DataProviderError):
        parse_ecb_csv("A,B\n1,2\n")


def test_ecb_404_means_no_data_and_bad_key_is_rejected():
    client = ECBClient(session=FakeSession([FakeResponse(404)]))
    assert client.fetch("EST/B.EU000A2X2A25.WT", pd.Timestamp("2026-10-01").date(),
                        pd.Timestamp("2026-10-02").date()).empty
    with pytest.raises(DataProviderError):
        client.fetch("no-slash", pd.Timestamp("2026-10-01").date(), pd.Timestamp("2026-10-02").date())


@pytest.mark.parametrize("layout", ["price_first", "ticker_first", "flat"])
def test_extract_yahoo_layouts(layout):
    frame = _yahoo_frame(["^VIX"])
    if layout == "ticker_first":
        frame = frame.swaplevel(axis=1)
    elif layout == "flat":
        frame.columns = frame.columns.get_level_values(0)
    out = extract_yahoo_field(frame, ["^VIX"], "Close")
    assert out["^VIX"].tolist() == [1.0, 1.0]


def test_free_routes_by_prefix_and_isolates_failures():
    seen = {}

    def downloader(tickers, start, end):
        seen["tickers"] = tickers
        return _yahoo_frame(tickers)

    ecb = ECBClient(session=FakeSession([FakeResponse(500)]), max_retries=0)
    provider = FreeProvider(ecb, YahooClient(downloader))
    result = provider.get_history(["yf:^VIX", "EURUSD=X", "ecb:EST/B.EU000A2X2A25.WT"],
                                  "2026-10-01", "2026-10-02")
    assert seen["tickers"] == ["^VIX", "EURUSD=X"]
    assert result.sources == {"yf:^VIX": "free", "EURUSD=X": "free"}
    assert "ECB" in result.errors["ecb:EST/B.EU000A2X2A25.WT"]


def test_yahoo_exceptions_are_normalised():
    def boom(*_):
        raise RuntimeError("yahoo changed its API again")

    result = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(boom)).get_history(
        "^VIX", "2026-10-01", "2026-10-02")
    assert "yfinance download failed" in result.errors["^VIX"]


def test_ecb_only_supports_last():
    provider = FreeProvider(ECBClient(session=FakeSession([])), YahooClient(lambda *a: None))
    result = provider.get_history("ecb:A/B", "2026-10-01", "2026-10-02", Field.HIGH)
    assert "LAST" in result.errors["ecb:A/B"]
