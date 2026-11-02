"""Phase 3 (v1.6): instrument page - level history, statistics, Bund spread, closes."""

import io
import math
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.analytics.instrument import candle_frequency as frequency
from market_monitor.analytics.instrument import (
    instrument_stats,
    load_start,
    moving_averages,
    period_start,
    realised_vol,
    reference,
    resample_bars,
    spread_vs,
    worst_move,
)
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import (
    CurvePoint,
    ReferentialError,
    load_referential,
    referential_from_dicts,
)
from market_monitor.ui.charts import instrument_figure, spread_figure
from market_monitor.ui.instrument_view import closes_frame, csv_bytes, display_closes, xlsx_bytes
from tests.test_bars_history import BarsFake, _bars

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 10, 26)

REF = referential_from_dicts(
    {"defaults": {"equity": {"quote": "price"}, "rates": {"quote": "yield", "unit": "%"}},
     "instruments": {
         "SPX": {"name": "S&P 500", "class": "equity", "tickers": {"free": "SPX", "fmp": "^GSPC"}},
         "BUND": {"name": "Bund 10 ans", "class": "rates", "curve": {"country": "DE", "tenor": 10},
                  "tickers": {"free": "DE10"}},
         "OAT": {"name": "OAT 10 ans", "class": "rates", "curve": {"country": "FR", "tenor": 10},
                 "tickers": {"free": "FR10"}},
         "OAT_BUND": {"name": "OAT-Bund", "class": "rates", "quote": "spread", "unit": "pb",
                      "derived": {"multiplier": 100, "legs": {"OAT": 1, "BUND": -1}}},
     }},
    {"watchlists": {"all": {"instruments": ["*"]}}},
)


def market() -> BarsFake:
    rng = np.random.default_rng(5)
    spx = _bars("2008-01-01", "2026-10-26", 1000.0)
    spx["last"] = 1000.0 * np.exp(np.cumsum(rng.normal(0, 0.01, len(spx))))
    spx["open"], spx["high"], spx["low"] = spx["last"], spx["last"] * 1.01, spx["last"] * 0.99
    return BarsFake({"SPX": spx, "DE10": _bars("2012-01-02", "2026-10-26", 2.0, ohlc=False) * 0.01 + 2.0,
                     "FR10": _bars("2012-01-02", "2026-10-26", 2.6, ohlc=False) * 0.01 + 2.6},
                    name="free")


# ----------------------------------------------------------------- referential
def test_curve_points_and_benchmark():
    assert REF.get("OAT").curve == CurvePoint("FR", 10.0)
    assert REF.benchmark_of("OAT") == "BUND" and REF.benchmark_of("BUND") is None
    assert REF.benchmark_of("SPX") is None and REF.curve_point("DE", 10) == "BUND"
    repo = load_referential(ROOT / "config" / "instruments.yaml", ROOT / "config" / "watchlists.yaml")
    curves = [i for i in repo.ids if repo.get(i).curve is not None]
    assert len(curves) == 22
    assert repo.benchmark_of("BTP_2Y") == "BUND_2Y" and repo.benchmark_of("UST_30Y") == "BUND_30Y"
    assert repo.benchmark_of("GILT_10Y") == "BUND_10Y" and repo.benchmark_of("BUND_5Y") is None


@pytest.mark.parametrize("curve, message", [
    ({"country": "FR", "tenor": 10}, "only yields"),
    ({"country": "fr", "tenor": 10}, "upper-case"),
    ({"country": "FR", "tenor": 0}, "positive"),
])
def test_curve_validation(curve, message):
    with pytest.raises(ReferentialError, match=message):
        asset_class = "equity" if message == "only yields" else "rates"
        referential_from_dicts({"defaults": {"equity": {"quote": "price"}, "rates": {"quote": "yield"}},
                                "instruments": {"X": {"name": "X", "class": asset_class, "curve": curve,
                                                      "tickers": {"free": "X"}}}}, {"watchlists": {}})


def test_duplicate_curve_point_is_rejected():
    raw = {"defaults": {"rates": {"quote": "yield"}}, "instruments": {
        n: {"name": n, "class": "rates", "curve": {"country": "FR", "tenor": 10}, "tickers": {"free": n}}
        for n in ("A", "B")}}
    with pytest.raises(ReferentialError, match="already used"):
        referential_from_dicts(raw, {"watchlists": {}})


# --------------------------------------------------------------------- engine
def test_periods_are_calendar_based():
    assert period_start("1M", date(2026, 3, 31)) == date(2026, 2, 28)
    assert period_start("3A", AS_OF) == date(2023, 10, 26)
    assert period_start("YTD", AS_OF) == date(2025, 12, 31)
    assert period_start("Max", AS_OF) == date(1990, 1, 1)
    assert period_start("1S", AS_OF) == date(2026, 10, 19)
    assert load_start("1M", AS_OF) == date(2020, 12, 30)          # 5A statistic + MA warm-up
    assert load_start("Max", AS_OF) == date(1990, 1, 1)
    with pytest.raises(ValueError):
        period_start("2M", AS_OF)


def test_reference_is_the_last_close_before_target_within_a_week():
    series = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-10-01", "2026-10-20"]))
    assert reference(series, date(2026, 10, 21)) == (2.0, pd.Timestamp("2026-10-20"))
    assert math.isnan(reference(series, date(2026, 10, 15))[0])   # 14 days stale
    assert math.isnan(reference(series, date(2026, 9, 1))[0])


def _closes(values, start="2026-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype="float64")


def test_stats_hand_computed_for_a_price():
    inst = REF.get("SPX")
    closes = _closes(100.0 * 1.001 ** np.arange(300))          # +0,1 % every session
    stats = instrument_stats(closes, inst, closes.index[-60].date())
    assert stats.changes["1J"] == pytest.approx(0.1)
    assert stats.vol_1m == pytest.approx(0.0, abs=1e-9)
    assert stats.worst_move == pytest.approx(0.0)
    assert stats.high_52w == closes.iloc[-1] and stats.from_high_52w == pytest.approx(0.0)
    ma50 = closes.tail(50).mean()
    assert stats.ma50 == pytest.approx(ma50)
    assert stats.to_ma50 == pytest.approx(100 * (closes.iloc[-1] / ma50 - 1))
    assert stats.sessions == 60
    assert math.isnan(stats.changes["5A"])                     # history too short


def test_vol_and_worst_move_per_convention():
    oat = REF.get("OAT")
    zigzag = _closes([3.0, 3.1] * 40)                           # +-10 bp every day
    assert realised_vol(zigzag, oat, 21) == pytest.approx(
        np.std([10.0, -10.0] * 10 + [10.0], ddof=1) * math.sqrt(252), rel=1e-6)
    assert worst_move(_closes([3.0, 2.5, 3.2, 3.1]), oat) == pytest.approx(70.0)   # rise from the low
    assert worst_move(_closes([100.0, 120.0, 90.0, 95.0]), REF.get("SPX")) == pytest.approx(-25.0)
    assert math.isnan(worst_move(_closes([1.0]), oat))
    assert math.isnan(realised_vol(_closes([1.0, 2.0]), oat, 21))


def test_moving_averages_need_the_full_window():
    averages = moving_averages(_closes(np.arange(250.0)))
    assert averages["ma50"].first_valid_index() == averages.index[49]
    assert averages["ma200"].iloc[-1] == pytest.approx(np.arange(50.0, 250.0).mean())


def test_empty_history_gives_nan_stats():
    stats = instrument_stats(pd.Series(dtype="float64"), REF.get("SPX"), AS_OF)
    assert math.isnan(stats.level) and stats.sessions == 0 and stats.level_date is None


def test_spread_vs_bund_and_candles():
    spread = spread_vs(_closes([3.0, 3.1, 3.2]), _closes([2.5, 2.5], start="2026-01-02"))
    assert spread.round(6).tolist() == [60.0, 70.0]
    daily = _bars("2026-01-05", "2026-01-16")
    weekly = resample_bars(daily, "W")
    assert list(weekly.index) == [pd.Timestamp("2026-01-09"), pd.Timestamp("2026-01-16")]
    first = daily.loc["2026-01-05":"2026-01-09"]
    assert weekly.iloc[0].tolist() == [first["open"].iloc[0], first["high"].max(), first["low"].min(),
                                       first["last"].iloc[-1], first["volume"].sum()]
    assert len(resample_bars(_bars("2026-01-05", "2026-03-31"), "M")) == 3
    assert frequency(date(2026, 1, 1), AS_OF) == "D"
    assert frequency(date(2021, 1, 1), AS_OF) == "W" and frequency(date(2000, 1, 1), AS_OF) == "M"


# -------------------------------------------------------------------- monitor
@pytest.fixture
def monitor(tmp_path):
    cached = CachedProvider(market(), ParquetCache(tmp_path), tranche_days=1830,
                            clock=lambda: datetime(2026, 10, 27, 8, 0, tzinfo=UTC))
    return MarketMonitor(MarketDataService([cached]), REF)


def test_profile_of_a_bond_with_its_bund_spread_and_an_overlay(monitor):
    profile = monitor.instrument_profile("OAT", "1A", AS_OF, overlay="SPX")
    assert profile.start == date(2025, 10, 26) and profile.benchmark_id == "BUND"
    levels = monitor.history(["OAT", "BUND"], AS_OF, AS_OF).levels.iloc[-1]
    assert profile.spread is not None
    assert profile.spread.iloc[-1] == pytest.approx(100 * (levels["OAT"] - levels["BUND"]))
    assert profile.overlay_id == "SPX" and profile.overlay_quote == "price"
    assert profile.depth is not None and profile.depth.iloc[0]["provider"] == "free"
    assert not profile.late_start and profile.source == "free"
    assert profile.bars.index[0] <= pd.Timestamp(load_start("1A", AS_OF)) + pd.Timedelta(days=5)


def test_max_starts_with_the_history_and_says_so(monitor):
    profile = monitor.instrument_profile("OAT", "Max", AS_OF)
    assert profile.late_start and profile.requested_start == date(1990, 1, 1)
    assert profile.start == date(2012, 1, 2) and profile.window.index[0] == pd.Timestamp("2012-01-02")
    table = monitor.depth(["OAT"])
    assert bool(table.iloc[0]["depth_reached"])


def test_open_session_is_dropped(monitor):
    profile = monitor.instrument_profile("SPX", "1M", AS_OF, provisional=["SPX"])
    assert profile.provisional and profile.bars.index[-1] < pd.Timestamp(AS_OF)
    assert monitor.instrument_profile("SPX", "1M", AS_OF).bars.index[-1] == pd.Timestamp(AS_OF)


def test_derived_spread_profile_is_close_only(monitor):
    profile = monitor.instrument_profile("OAT_BUND", "3M", AS_OF)
    assert profile.bars["open"].isna().all() and profile.benchmark_id is None
    assert profile.source == "derived"


def test_long_window_switches_to_the_deepest_source(tmp_path):
    shallow = BarsFake({"^GSPC": _bars("2021-10-27", "2026-10-26", 4000.0)}, name="fmp")
    deep = BarsFake({"SPX": _bars("2000-01-03", "2026-10-26", 1000.0)}, name="free")
    clock = lambda: datetime(2026, 10, 27, 8, 0, tzinfo=UTC)  # noqa: E731
    service = MarketDataService([CachedProvider(shallow, ParquetCache(tmp_path / "a"), clock=clock),
                                 CachedProvider(deep, ParquetCache(tmp_path / "b"), clock=clock)])
    monitor = MarketMonitor(service, REF)
    assert monitor.instrument_profile("SPX", "1A", AS_OF).source == "fmp"
    long = monitor.instrument_profile("SPX", "Max", AS_OF)
    assert long.source == "free" and long.start == date(2000, 1, 3)


# --------------------------------------------------------------------- figures
def test_figures(monitor):
    spx = monitor.instrument_profile("SPX", "3A", AS_OF, overlay="OAT")
    fig = instrument_figure(spx, candles=True)
    kinds = [t.type for t in fig.data]
    assert kinds[0] == "candlestick" and len(fig.data[0].x) < 200        # weekly candles over 3 years
    assert {t.name for t in fig.data} >= {"MM 50", "MM 200", "OAT 10 ans"}
    assert fig.layout.yaxis2.overlaying == "y"                           # % yield on its own axis
    # each axis spans its own series (regression: the overlay axis took the main axis' range)
    oat_line = next(t for t in fig.data if t.name == "OAT 10 ans")
    low2, high2 = fig.layout.yaxis2.range
    assert low2 <= min(oat_line.y) and max(oat_line.y) <= high2 and high2 < 50
    low1, high1 = fig.layout.yaxis.range
    assert low1 <= spx.window["low"].min() and spx.window["high"].max() <= high1 and low1 > 50
    line = instrument_figure(spx, candles=False, averages=False)
    assert line.data[0].type == "scatter" and "MM 50" not in {t.name for t in line.data}
    oat = monitor.instrument_profile("OAT", "1A", AS_OF, overlay="BUND")
    assert "yaxis2" not in instrument_figure(oat).layout.to_plotly_json()   # yields share the axis
    assert spread_figure(oat) is not None
    assert spread_figure(monitor.instrument_profile("BUND", "1A", AS_OF)) is None


def test_closes_table_and_downloads(monitor):
    profile = monitor.instrument_profile("OAT", "1M", AS_OF)
    table = closes_frame(profile)
    assert table.index[0] == AS_OF and "Variation (pb)" in table.columns and "Ouverture" not in table.columns
    shown = display_closes(table, profile.instrument, profile.stats.level)
    assert shown["Date"].iloc[0] == "26/10/2026" and "," in shown["Clôture"].iloc[0]
    csv = csv_bytes(table).decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("Date;Clôture;Variation (pb)") and "," in csv.splitlines()[1]
    back = pd.read_excel(io.BytesIO(xlsx_bytes(table, profile.instrument.name)))
    assert len(back) == len(table)


# --------------------------------------------------------------------------- UI
def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_TEST_CACHE", str(tmp_path / "cache"))
    return AppTest.from_file(str(Path(__file__).with_name("ui_instrument_app.py")), default_timeout=90)


def test_streamlit_instrument_page(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.query_params["fiche"] = "OAT"
    at.run()
    assert not at.exception, at.exception
    assert at.segmented_control(key="mm-view").value == "instrument"
    assert at.selectbox(key="mm-fiche-id").value == "OAT"
    text = " ".join(m.value for m in at.markdown)
    assert "Spread contre le Bund 10 ans" in text and "Statistiques" in text
    assert len(at.get("plotly_chart")) == 2 and len(at.get("download_button")) == 2
    at.segmented_control(key="mm-fiche-period").set_value("Max").run()
    assert not at.exception, at.exception
    assert any("ne remonte qu'au 02/01/2012" in c.value for c in at.caption)
    at.selectbox(key="mm-fiche-id").set_value("SPX").run()
    at.segmented_control(key="mm-fiche-mode").set_value("candles").run()
    assert not at.exception, at.exception
    assert len(at.get("plotly_chart")) == 1


def test_overview_is_still_the_default_view(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.run()
    assert not at.exception, at.exception
    assert at.segmented_control(key="mm-view").value == "overview"


# ------------------------------------------------- v1.6.6: spread between any two bonds
def test_pair_spread_between_the_instrument_and_its_overlay(monitor):
    from market_monitor.analytics.instrument import pair_spread, spread_changes

    bund = monitor.instrument_profile("BUND", "1A", AS_OF, overlay="OAT")
    assert bund.pair_spread is not None and bund.spread is None          # the Bund has no Bund spread
    common = bund.bars["last"].index.intersection(bund.overlay.index)
    day = common[-1]
    expected = 100 * (bund.bars["last"].loc[day] - bund.overlay.loc[day])
    assert bund.pair_spread.loc[day] == pytest.approx(expected)
    fig = spread_figure(bund, pair=True)
    assert fig is not None and "Bund 10 ans − OAT 10 ans" in fig.data[0].name
    # a yield against a price: no spread; spreads against spreads: plain bp difference
    assert monitor.instrument_profile("OAT", "1A", AS_OF, overlay="SPX").pair_spread is None
    a = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-10-26", "2026-10-27"]))
    assert pair_spread(a, a / 2, "spread", "spread").tolist() == [0.5, 1.0]
    assert pair_spread(a, a, "yield", "spread") is None
    moves = spread_changes(pd.Series([10.0, 12.0, 15.0], index=pd.bdate_range("2026-10-23", periods=3)),
                           date(2026, 10, 27))
    assert moves["level"] == 15.0 and moves["1J"] == pytest.approx(3.0) and math.isnan(moves["1A"])


def test_streamlit_fiche_shows_the_pair_spread(tmp_path, monkeypatch):
    at = _app(tmp_path, monkeypatch)
    at.query_params["fiche"] = "OAT"
    at.run()
    at.selectbox(key="mm-fiche-overlay").set_value("BUND").run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Écart OAT 10 ans − Bund 10 ans (pb)" in text
    assert "Spread contre le Bund" not in text                          # not shown twice
    assert len(at.get("plotly_chart")) == 2
    at.selectbox(key="mm-fiche-id").set_value("BUND").run()
    at.selectbox(key="mm-fiche-overlay").set_value("OAT").run()
    assert not at.exception, at.exception
    assert "Écart Bund 10 ans − OAT 10 ans (pb)" in " ".join(m.value for m in at.markdown)
