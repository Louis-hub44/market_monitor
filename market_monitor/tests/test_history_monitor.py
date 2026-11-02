from datetime import date

import pandas as pd
import pytest

from market_monitor.analytics.history import DERIVED_SOURCE, load_universe_history
from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import AssetClass, referential_from_dicts
from tests.conftest import FakeProvider, bday_series
from tests.test_referential import BASE

REF = referential_from_dicts(
    {**BASE, "instruments": {
        **BASE["instruments"],
        "PROXY": {"name": "proxied", "class": "equity",
                  "tickers": {"free": {"ticker": "ETF", "proxy": "some ETF"}}},
    }},
    {"watchlists": {"macro": {"instruments": ["SX5E", "OAT_BUND"]},
                    "all": {"description": "Tout", "instruments": ["*"]}}},
)


def _flat(value):
    return bday_series("2025-01-01", "2026-10-02") * 0 + value


def _providers():
    bbg = FakeProvider("bloomberg", {
        "SX5E Index": bday_series("2025-01-01", "2026-10-02", 5000.0),
        "GFRN10 Index": _flat(3.40),
        "GDBR10 Index": _flat(2.70),
    })
    free = FakeProvider("free", {"X": _flat(27.0),
                                 "ETF": bday_series("2025-01-01", "2026-10-02", 40.0)})
    return bbg, free


def test_derived_spread_in_bp_and_legs_hidden():
    bbg, free = _providers()
    service = MarketDataService([bbg, free])
    hist = load_universe_history(service, REF, ["OAT_BUND"], "2026-09-01", "2026-10-02")
    assert list(hist.levels.columns) == ["OAT_BUND"]
    assert hist.levels["OAT_BUND"].iloc[-1] == pytest.approx(70.0)  # (3.40 - 2.70) x 100
    assert hist.sources == {"OAT_BUND": DERIVED_SOURCE}


def test_scale_proxy_and_sanity_check():
    bbg, free = _providers()
    bbg.data.pop("GDBR10 Index")  # Bund falls back to "free", quoted x10 with scale 0.1
    hist = load_universe_history(MarketDataService([bbg, free]), REF,
                                 ["BUND_10Y", "PROXY"], "2026-09-01", "2026-10-02")
    assert hist.levels["BUND_10Y"].iloc[-1] == pytest.approx(2.7)
    assert hist.sources == {"BUND_10Y": "free", "PROXY": "free"}
    assert hist.proxies == {"PROXY": "some ETF"}
    assert "BUND_10Y" not in hist.warnings  # 2.7 % is plausible


def test_missing_leg_and_implausible_level():
    bbg, free = _providers()
    bbg.data.pop("GFRN10 Index")
    bbg.data["GDBR10 Index"] = bbg.data["GDBR10 Index"] * 100  # wrong unit: 270 % -> implausible
    hist = load_universe_history(MarketDataService([bbg, free]), REF,
                                 ["OAT_BUND", "BUND_10Y"], "2026-09-01", "2026-10-02")
    assert "missing leg" in hist.errors["OAT_BUND"] and "OAT_10Y" in hist.errors["OAT_BUND"]
    assert "implausible level" in hist.warnings["BUND_10Y"]
    assert hist.levels["OAT_BUND"].isna().all()


def test_monitor_end_to_end():
    bbg, free = _providers()
    monitor = MarketMonitor(MarketDataService([bbg, free]), REF)
    report = monitor.performance("macro", as_of=date(2026, 10, 5))
    table = report.table
    assert list(table.index) == ["SX5E", "OAT_BUND"]
    assert table.loc["SX5E", "source"] == "bloomberg"
    assert table.loc["OAT_BUND", "chg_1d"] == pytest.approx(0.0)
    level = table.loc["SX5E", "level"]  # fake series grows by 1 point per day
    assert table.loc["SX5E", "chg_1d"] == pytest.approx(100 * (level / (level - 1) - 1))
    assert not pd.isna(table.loc["SX5E", "z_1d"])
    assert list(report.by_asset_class()) == [AssetClass.EQUITY, AssetClass.RATES]
    # history reached back to the previous year-end for YTD
    first_call = bbg.calls[0]
    assert first_call.start <= date(2025, 12, 31)


def test_monitor_selection():
    bbg, free = _providers()
    monitor = MarketMonitor(MarketDataService([bbg, free]), REF)
    assert monitor.select("macro", ["class:rates"]) == ("SX5E", "OAT_BUND", "OAT_10Y", "BUND_10Y")
    assert monitor.select() == REF.ids


def test_monitor_comparison_and_correlation():
    bbg, free = _providers()
    monitor = MarketMonitor(MarketDataService([bbg, free]), REF)
    comp = monitor.comparison(["SX5E", "OAT_BUND"], "3M", as_of=date(2026, 10, 2))
    assert comp.series["SX5E"].iloc[0] == pytest.approx(100.0)
    assert comp.series["OAT_BUND"].abs().max() == pytest.approx(0.0)  # flat spread
    corr = monitor.correlation(["SX5E", "PROXY"], as_of=date(2026, 10, 2), window=21, lag=5)
    assert corr.matrix.loc["SX5E", "PROXY"] == pytest.approx(1.0, abs=1e-3)  # both grow linearly
    assert corr.previous is not None
