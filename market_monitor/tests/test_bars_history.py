"""Phase 2 (v1.6): daily OHLCV bars, long histories in tranches, depth per source."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_monitor.analytics.history import load_universe_bars
from market_monitor.cli import main
from market_monitor.data.base import DataProvider
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.models import (
    BAR_COLUMNS,
    BarsResult,
    Field,
    HistoryRequest,
    HistoryResult,
    empty_bars,
    ohlc_share,
)
from market_monitor.data.providers.fmp import FMPProvider
from market_monitor.data.providers.free import ECBClient, FreeProvider, YahooClient
from market_monitor.data.providers.manual import ManualProvider, ManualQuotes
from market_monitor.data.providers.public import (
    CnbcClient,
    StooqClient,
    parse_cnbc_bars,
    parse_cnbc_ohlc,
    parse_stooq_bars,
)
from market_monitor.data.quality import assemble_frame, clean_bars
from market_monitor.data.service import MarketDataService
from market_monitor.exceptions import DataProviderError
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import referential_from_dicts
from tests.conftest import FakeProvider, FakeResponse, FakeSession, bday_series


def _bars(start: str, end: str, base: float = 100.0, ohlc: bool = True) -> pd.DataFrame:
    index = pd.bdate_range(start, end, name="date")
    close = base + np.arange(len(index), dtype="float64") * 0.01
    frame = pd.DataFrame({"last": close}, index=index)
    if ohlc:
        frame["open"], frame["high"], frame["low"] = close - 0.5, close + 1.0, close - 1.0
        frame["volume"] = 1000.0
    return frame.reindex(columns=list(BAR_COLUMNS))


class BarsFake(DataProvider):
    """In-memory provider with whole bars; records every bars / history window."""

    def __init__(self, data: dict[str, pd.DataFrame], name: str = "fake",
                 origins: dict[str, str] | None = None, fail_windows: tuple = ()) -> None:
        self.name = name
        self.data = data
        self.origins = origins or {}
        self.fail_windows = fail_windows
        self.calls: list[tuple[str, tuple[str, ...], date, date]] = []

    def _slice(self, ticker: str, request: HistoryRequest) -> pd.DataFrame:
        frame = self.data.get(ticker, empty_bars())
        return frame.loc[pd.Timestamp(request.start):pd.Timestamp(request.end)]

    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        self.calls.append(("history", request.tickers, request.start, request.end))
        series = {t: self._slice(t, request)[request.field.value] for t in request.tickers}
        return HistoryResult(data=assemble_frame(series, request.tickers),
                             origins={t: self.origins[t] for t in request.tickers if t in self.origins})

    def _fetch_bars(self, request: HistoryRequest) -> BarsResult:
        self.calls.append(("bars", request.tickers, request.start, request.end))
        if any(a <= request.end and request.start <= b for a, b in self.fail_windows):
            raise DataProviderError("tranche refused")
        return BarsResult(bars={t: self._slice(t, request) for t in request.tickers},
                          origins={t: self.origins[t] for t in request.tickers if t in self.origins})


# --------------------------------------------------------------------- cleaning
def test_clean_bars_drops_missing_closes_before_deduplicating():
    index = pd.to_datetime(["2026-10-05", "2026-10-05", "2026-10-02"])
    raw = pd.DataFrame({"last": [3.48, np.nan, 3.47], "open": [3.4, 3.5, 3.45]}, index=index)
    out = clean_bars(raw)
    assert list(out.columns) == list(BAR_COLUMNS)
    assert out["last"].tolist() == [3.47, 3.48] and out["open"].tolist() == [3.45, 3.4]


def test_clean_bars_blanks_contradictory_bars_and_keeps_negative_yields():
    index = pd.bdate_range("2026-10-01", periods=3)
    raw = pd.DataFrame({"open": [-0.2, 10.0, 1.0], "high": [-0.1, 9.0, 1.2], "low": [-0.3, 8.0, 0.9],
                        "last": [-0.15, 9.5, 1.1], "volume": [0, 5, -1]}, index=index)
    out = clean_bars(raw)
    assert out.loc[index[0], ["open", "high", "low", "last"]].tolist() == [-0.2, -0.1, -0.3, -0.15]
    assert out.loc[index[1], ["open", "high", "low"]].isna().all()   # open above high
    assert out.loc[index[1], "last"] == 9.5
    assert np.isnan(out.loc[index[2], "volume"])
    assert clean_bars(None).empty and clean_bars(pd.DataFrame({"open": [1.0]})).empty


def test_bars_result_helpers():
    full, close_only = _bars("2026-09-01", "2026-09-30"), _bars("2026-09-15", "2026-09-30", ohlc=False)
    result = BarsResult(bars={"A": full, "B": close_only})
    assert result.has_ohlc("A") and not result.has_ohlc("B") and not result.has_ohlc("C")
    assert ohlc_share(full) == 1.0 and ohlc_share(close_only) == 0.0 and ohlc_share(empty_bars()) == 0.0
    closes = result.field(Field.LAST)
    assert list(closes.columns) == ["A", "B"]
    assert closes["B"].first_valid_index() == pd.Timestamp("2026-09-15")
    assert BarsResult(bars={"A": empty_bars()}).field("last").empty


# ---------------------------------------------------------------- providers
def test_default_bars_ask_each_field_and_tolerate_refusals():
    class FieldFake(FakeProvider):
        def _fetch_history(self, request):
            if request.field is Field.VOLUME:
                raise DataProviderError("no volume")
            result = super()._fetch_history(request)
            offset = {Field.LAST: 0.0, Field.OPEN: -0.5, Field.HIGH: 1.0, Field.LOW: -1.0}[request.field]
            return HistoryResult(data=result.data + offset)

    provider = FieldFake(data={"X": bday_series("2026-09-01", "2026-09-30")})
    bars = provider.get_bars(["X", "MISSING"], "2026-09-01", "2026-09-30")
    frame = bars.bars["X"]
    assert (frame["high"] - frame["last"]).eq(1.0).all() and frame["volume"].isna().all()
    assert {c.field for c in provider.calls} == {Field.LAST, Field.OPEN, Field.HIGH, Field.LOW}
    assert "MISSING" in bars.errors and bars.bars["MISSING"].empty
    assert bars.sources == {"X": "fake"}


def test_manual_quotes_are_close_only(tmp_path):
    store = ManualQuotes(tmp_path / "q.csv")
    store.set("ITRX_MAIN", "2026-10-02", 55.4)
    bars = ManualProvider(store).get_bars(["ITRX_MAIN"], "2026-10-01", "2026-10-05")
    assert bars.bars["ITRX_MAIN"]["last"].tolist() == [55.4]
    assert bars.bars["ITRX_MAIN"]["open"].isna().all()


def test_yahoo_bars_come_from_one_download():
    calls = []

    def downloader(tickers, start, end):
        calls.append(list(tickers))
        idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
        values = {"Open": 10.0, "High": 12.0, "Low": 9.0, "Close": 11.0, "Volume": 500.0}
        cols = pd.MultiIndex.from_product([list(values), tickers], names=["Price", "Ticker"])
        row = [values[f] for f, _ in cols]
        return pd.DataFrame([row, row], index=idx, columns=cols)

    provider = FreeProvider(ECBClient(session=FakeSession([FakeResponse(404)])), YahooClient(downloader))
    bars = provider.get_bars(["^GSPC", "yf:^VIX"], "2026-10-01", "2026-10-02")
    assert calls == [["^GSPC", "^VIX"]]
    assert bars.bars["^GSPC"].iloc[0].tolist() == [10.0, 12.0, 9.0, 11.0, 500.0]
    assert bars.has_ohlc("yf:^VIX")


STOOQ = ("Date,Open,High,Low,Close,Volume\n2026-10-01,2.60,2.70,2.55,2.66,0\n"
         "2026-10-02,2.66,2.72,2.65,2.70,0\n")


def test_stooq_bars_and_close_are_consistent():
    bars = parse_stooq_bars(STOOQ, "10dey.b")
    assert bars["high"].tolist() == [2.70, 2.72] and bars["last"].tolist() == [2.66, 2.70]
    assert parse_stooq_bars("No data", "x").empty
    session = FakeSession([FakeResponse(200, text=STOOQ)])
    window = StooqClient(session=session).fetch_bars("10DEY.B", date(2026, 10, 2), date(2026, 10, 2))
    assert window["last"].tolist() == [2.70]


def _cnbc(bars):
    return {"barData": {"priceBars": bars}}


def test_cnbc_bars_keep_the_monday_rule_and_match_the_close_parser():
    payload = _cnbc([
        {"tradeTime": "20261002000000", "open": "3.47", "high": "3.50", "low": "3.45", "close": "3.486"},
        {"tradeTime": "20261003000000", "open": "3.49", "high": "3.55", "low": "3.48", "close": "3.53"},
    ])
    ohlc = parse_cnbc_ohlc(payload, "DE10Y-DE")
    assert list(ohlc.index) == [pd.Timestamp("2026-10-02"), pd.Timestamp("2026-10-05")]  # Saturday -> Monday
    assert ohlc.loc["2026-10-05", "high"] == 3.55 and ohlc["volume"].isna().all()
    pd.testing.assert_series_equal(parse_cnbc_bars(payload), ohlc["last"].rename(None),
                                   check_names=False)


def test_cnbc_fetch_bars_through_the_free_provider(monkeypatch):
    payload = _cnbc([{"tradeTime": "20261002000000", "open": 3.47, "high": 3.5, "low": 3.45, "close": 3.486}])
    client = CnbcClient(session=FakeSession([FakeResponse(200, payload=payload)]))
    monkeypatch.setattr(CnbcClient, "_sleep", staticmethod(lambda s: None))
    provider = FreeProvider(ECBClient(session=FakeSession([FakeResponse(404)])), YahooClient(lambda *a: None),
                            sources={"cnbc:": client})
    bars = provider.get_bars(["cnbc:DE10Y-DE"], "2026-10-01", "2026-10-02")
    assert bars.bars["cnbc:DE10Y-DE"].iloc[0].tolist()[:4] == [3.47, 3.5, 3.45, 3.486]


def test_free_chain_and_close_only_sources():
    ecb_csv = "KEY,FREQ,TIME_PERIOD,OBS_VALUE\nX,B,2026-10-01,1.927\nX,B,2026-10-02,1.931\n"
    stooq = StooqClient(session=FakeSession([FakeResponse(200, text=STOOQ)]))
    provider = FreeProvider(ECBClient(session=FakeSession([FakeResponse(200, text=ecb_csv)])),
                            YahooClient(lambda *a: pd.DataFrame()),
                            sources={"stooq:": stooq})
    bars = provider.get_bars(["ZZZ|stooq:10dey.b", "ecb:EST/B.X"], "2026-10-01", "2026-10-02")
    assert bars.origins["ZZZ|stooq:10dey.b"] == "stooq:10dey.b"
    assert "fallback" in bars.warnings["ZZZ|stooq:10dey.b"]
    assert bars.has_ohlc("ZZZ|stooq:10dey.b")
    ecb = bars.bars["ecb:EST/B.X"]
    assert ecb["last"].tolist() == [1.927, 1.931] and ecb["open"].isna().all()


def test_fmp_bars_one_call_per_symbol_treasury_close_only():
    eod = [{"date": "2026-10-02", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 7},
           {"date": "2026-10-01", "open": 9, "high": 11, "low": 8, "close": 10, "volume": 6}]
    treasury = [{"date": "2026-10-02", "year10": 4.1}]

    class Routed(FakeSession):
        def get(self, url, params=None, timeout=None, headers=None):
            self.calls.append({"url": url, "params": dict(params or {})})
            return FakeResponse(200, payload=treasury if "treasury" in url else eod)

    session = Routed([])
    provider = FMPProvider("key", session=session, sleep=lambda s: None)
    bars = provider.get_bars(["^GSPC", "treasury:year10"], "2026-10-01", "2026-10-02")
    assert bars.bars["^GSPC"]["volume"].tolist() == [6.0, 7.0]
    assert bars.bars["treasury:year10"]["last"].tolist() == [4.1]
    assert bars.bars["treasury:year10"]["open"].isna().all()
    assert sum("historical-price-eod" in c["url"] for c in session.calls) == 1


# -------------------------------------------------------------------- cache
@pytest.fixture
def cached_bars(tmp_path, clock):
    def make(data, **kwargs):
        inner = BarsFake(data, origins=kwargs.pop("origins", None),
                         fail_windows=kwargs.pop("fail_windows", ()))
        return inner, CachedProvider(inner, ParquetCache(tmp_path), clock=clock, **kwargs)
    return make


def test_bars_are_cached_field_by_field_from_one_call(cached_bars, tmp_path):
    inner, cached = cached_bars({"A": _bars("2026-01-01", "2026-10-05")})
    first = cached.get_bars(["A"], "2026-06-01", "2026-10-05")
    second = cached.get_bars(["A"], "2026-07-01", "2026-10-02")
    history = cached.get_history(["A"], "2026-06-01", "2026-10-05", Field.HIGH)
    assert [c[0] for c in inner.calls] == ["bars"]
    assert first.has_ohlc("A") and len(second.bars["A"]) < len(first.bars["A"])
    assert history.data["A"].iloc[-1] == first.bars["A"]["high"].iloc[-1]
    assert len(list(tmp_path.rglob("*.parquet"))) == 5


def test_close_only_source_is_not_asked_again(cached_bars):
    inner, cached = cached_bars({"Y": _bars("2026-01-01", "2026-10-05", ohlc=False)})
    cached.get_bars(["Y"], "2026-06-01", "2026-10-05")
    again = cached.get_bars(["Y"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 1 and again.bars["Y"]["open"].isna().all()
    assert again.bars["Y"]["last"].notna().all()


def test_close_cached_alone_gets_its_bars_once(cached_bars):
    inner, cached = cached_bars({"A": _bars("2026-01-01", "2026-10-05")})
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    bars = cached.get_bars(["A"], "2026-06-01", "2026-10-05")
    cached.get_bars(["A"], "2026-06-01", "2026-10-05")
    assert [c[0] for c in inner.calls] == ["history", "bars"] and bars.has_ohlc("A")


def test_max_window_loads_backwards_in_tranches_and_stops_at_the_source_start(cached_bars):
    inner, cached = cached_bars({"A": _bars("2010-03-01", "2026-10-05")}, tranche_days=1830)
    result = cached.get_bars(["A"], "1990-01-01", "2026-10-05")
    windows = [(c[2], c[3]) for c in inner.calls]
    assert windows[0][1] == date(2026, 10, 5) and len(windows) == 5     # 2026 -> 2001, then stop
    assert all(b[1] < a[0] for a, b in zip(windows, windows[1:]))     # walked backwards
    assert result.bars["A"].index[0] == pd.Timestamp("2010-03-01")
    entry = cached.coverage("A")
    assert entry is not None and entry.covered_start == date(1990, 1, 1)  # depth reached: recorded
    cached.get_bars(["A"], "1990-01-01", "2026-10-05")
    assert len(inner.calls) == 5                                         # never asked again


def test_refused_tranche_leaves_no_hole(cached_bars):
    inner, cached = cached_bars({"A": _bars("2000-01-03", "2026-10-05")}, tranche_days=1830,
                                fail_windows=((date(2014, 1, 1), date(2014, 12, 31)),))
    result = cached.get_bars(["A"], "1990-01-01", "2026-10-05")
    entry = cached.coverage("A")
    assert entry is not None and entry.covered_start > date(2014, 12, 31)   # stopped, contiguous
    assert result.bars["A"].index[0] > pd.Timestamp("2014-12-31") and "A" not in result.errors
    assert "tranche refused" in result.warnings["A"]


def test_forward_extension_walks_ahead(cached_bars, clock):
    inner, cached = cached_bars({"A": _bars("2000-01-03", "2026-10-05")}, tranche_days=1830)
    clock.now = clock.now.replace(year=2010)
    cached.get_bars(["A"], "2005-01-03", "2010-10-05")
    clock.now = clock.now.replace(year=2026)
    cached.get_bars(["A"], "2005-01-03", "2026-10-05")
    later = [(c[2], c[3]) for c in inner.calls[1:]]
    assert all(b[0] > a[1] for a, b in zip(later, later[1:])) and later[-1][1] == date(2026, 10, 5)
    series = cached.coverage("A").series
    assert series.index.to_series().diff().max() < pd.Timedelta(days=5)   # no hole


def test_tranches_disabled_and_short_windows_unchanged(cached_bars):
    inner, cached = cached_bars({"A": _bars("2000-01-03", "2026-10-05")}, tranche_days=0)
    cached.get_bars(["A"], "1990-01-01", "2026-10-05")
    assert len(inner.calls) == 1
    with pytest.raises(ValueError):
        CachedProvider(inner, ParquetCache("x"), tranche_days=-1)


def test_older_tranches_are_asked_from_the_cached_origin(cached_bars):
    data = {"A|B": _bars("2012-01-02", "2026-10-05"), "B": _bars("2012-01-02", "2026-10-05")}
    inner, cached = cached_bars(data, origins={"A|B": "B"}, tranche_days=1830)
    cached.get_bars(["A|B"], "2015-01-01", "2026-10-05")
    assert [c[1] for c in inner.calls] == [("A|B",), ("B",), ("B",)]  # 3 tranches, then the origin


# --------------------------------------------------------- service / monitor
REF = referential_from_dicts(
    {"defaults": {"equity": {"quote": "price"}, "rates": {"quote": "yield", "unit": "%"}},
     "instruments": {
         "SPX": {"name": "S&P 500", "class": "equity", "tickers": {"free": "^GSPC", "fmp": "SPX"}},
         "BUND": {"name": "Bund 10 ans", "class": "rates",
                  "tickers": {"free": {"ticker": "DE10", "scale": 0.1}}},
         "OAT": {"name": "OAT 10 ans", "class": "rates", "tickers": {"free": "FR10"}},
         "OAT_BUND": {"name": "OAT-Bund", "class": "rates", "quote": "spread", "unit": "pb",
                      "derived": {"multiplier": 100, "legs": {"OAT": 1, "BUND": -1}}},
     }},
    {"watchlists": {"all": {"instruments": ["*"]}}},
)


def _world(tmp_path, clock):
    fake = BarsFake({"^GSPC": _bars("2016-01-04", "2026-10-02", 4000.0),
                     "DE10": _bars("2018-01-01", "2026-10-02", 30.0),
                     "FR10": _bars("2018-01-01", "2026-10-02", 3.5, ohlc=False)}, name="free")
    cached = CachedProvider(fake, ParquetCache(tmp_path / "cache"), clock=clock, tranche_days=1830)
    other = BarsFake({"SPX": _bars("2026-01-01", "2026-10-02", 1.0)}, name="fmp")
    return fake, MarketMonitor(MarketDataService([cached, other]), REF)


def test_service_bars_fall_back_per_instrument(tmp_path, clock):
    first = BarsFake({}, name="free")
    second = BarsFake({"SPX": _bars("2026-09-01", "2026-10-02")}, name="fmp")
    result = MarketDataService([first, second]).get_bars({"SPX": {"free": "X", "fmp": "SPX"}},
                                                         "2026-09-01", "2026-10-02")
    assert result.sources == {"SPX": "fmp"} and result.has_ohlc("SPX")
    missing = MarketDataService([first]).get_bars({"SPX": {"free": "X"}}, "2026-09-01", "2026-10-02")
    assert missing.bars["SPX"].empty and "SPX" in missing.errors


def test_universe_bars_scale_prices_not_volume_and_derive_closes(tmp_path, clock):
    _, monitor = _world(tmp_path, clock)
    out = load_universe_bars(monitor.service, REF, ["BUND", "OAT_BUND", "OAT"], "2026-09-01", "2026-10-02")
    bund = out.bars["BUND"]
    raw = monitor.service.providers[0].inner.data["DE10"]
    assert bund["last"].iloc[0] == pytest.approx(0.1 * raw.loc["2026-09-01", "last"])
    assert bund["volume"].iloc[0] == 1000.0 and out.ohlc_share("BUND") == 1.0
    spread = out.bars["OAT_BUND"]
    assert spread["open"].isna().all() and out.sources["OAT_BUND"] == "derived"
    assert spread["last"].iloc[-1] == pytest.approx(100 * (out.bars["OAT"]["last"].iloc[-1]
                                                           - bund["last"].iloc[-1]))
    assert list(out.levels.columns) == ["BUND", "OAT_BUND", "OAT"] and out.ohlc_share("OAT") == 0.0


def test_monitor_depth_reports_the_source_limit(tmp_path, clock):
    fake, monitor = _world(tmp_path, clock)
    assert monitor.depth(["SPX"]).empty                       # nothing cached yet, nothing fetched
    monitor.bars(["SPX", "OAT_BUND"], "1990-01-01", "2026-10-02")
    table = monitor.depth(["SPX", "OAT_BUND"]).set_index("id")
    spx = table.loc["SPX"]
    assert spx["first"] == date(2016, 1, 4) and spx["loaded_from"] == date(1990, 1, 1)
    assert bool(spx["depth_reached"]) and spx["ohlc_share"] == 1.0 and spx["provider"] == "free"
    spread = table.loc["OAT_BUND"]
    assert spread["provider"] == "derived" and spread["first"] == date(2018, 1, 1)
    assert spread["source"] == "Yahoo FR10 / Yahoo DE10"


def test_cli_depth(tmp_path, clock, capsys, monkeypatch):
    _, monitor = _world(tmp_path, clock)
    monkeypatch.setattr(MarketMonitor, "from_settings", classmethod(lambda cls, s: monitor))
    config = tmp_path / "config.yaml"
    config.write_text("cache:\n  directory: c\n", encoding="utf-8")
    assert main(["--config", str(config), "depth", "SPX"]) == 1      # nothing in cache yet
    assert main(["--config", str(config), "depth", "SPX", "BUND", "--load"]) == 0
    out = capsys.readouterr().out
    assert "Max atteint" in out and "2016-01-04" in out and "100 %" in out
