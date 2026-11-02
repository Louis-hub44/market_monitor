"""Safeguards for a published review: suspect moves, second-source check, contract rolls,
legs out of sync, and the "to check" list."""

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_monitor.analytics.performance import (
    CHECK_GAP,
    CHECK_OK,
    CHECK_UNAVAILABLE,
    Horizon,
    PerformanceEngine,
    rank_movers,
)
from market_monitor.analytics.quality import cross_check, daily_sigma, publication_checks
from market_monitor.config import QualitySettings
from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import ReferentialError, referential_from_dicts
from market_monitor.rolls import roll_days, rolled_between
from tests.conftest import FakeProvider

IDX = pd.bdate_range("2025-06-02", "2026-10-02")
AS_OF = date(2026, 10, 5)
RAW = {
    "defaults": {"rates": {"quote": "yield", "unit": "%"}, "equity": {"quote": "price"},
                 "commodities": {"quote": "price"}, "fx": {"quote": "price"}},
    "instruments": {
        "SPX": {"name": "S&P 500", "class": "equity",
                "tickers": {"fmp": "^GSPC", "free": "^GSPC"}},
        "EEMX": {"name": "MSCI EM", "class": "equity",
                 "tickers": {"fmp": "MXEF", "free": {"ticker": "EEM", "proxy": "ETF"}}},
        "EURUSD": {"name": "EUR/USD", "class": "fx", "tickers": {"fmp": "EURUSD", "free": "EURUSD=X"}},
        "BRENT": {"name": "Brent", "class": "commodities", "roll": "brent",
                  "tickers": {"free": "BZ=F"}},
        "UST_2Y": {"name": "UST 2 ans", "class": "rates", "tickers": {"free": "cnbc:US2Y|fred:DGS2"}},
        "UST_10Y": {"name": "UST 10 ans", "class": "rates", "tickers": {"free": "cnbc:US10Y|^TNX"}},
        "UST_2S10S": {"name": "Pente UST 2s10s", "class": "rates", "quote": "spread", "unit": "pb",
                      "derived": {"multiplier": 100, "legs": {"UST_10Y": 1, "UST_2Y": -1}}},
    },
}
REF = referential_from_dicts(RAW, {})


def walk(base, vol, seed, idx=IDX, log=True):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, vol, len(idx))
    return pd.Series(base * np.exp(np.cumsum(steps)) if log else base + np.cumsum(steps), index=idx)


# ----------------------------------------------------------------------- rolls
def test_roll_calendars():
    assert [d.strftime("%d/%m") for d in roll_days("brent", "2026-10-01", "2026-11-30")] == [
        "01/10", "02/10", "02/11", "03/11"]
    # WTI: 25 Oct 2026 is a Sunday -> last trading day Tue 20 Oct, roll Wed 21 Oct
    assert roll_days("wti", "2026-10-01", "2026-10-31")[0] == pd.Timestamp("2026-10-21")
    assert roll_days("ttf", "2026-10-01", "2026-10-31")[-1] == pd.Timestamp("2026-10-30")
    assert rolled_between("brent", pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-01"))
    assert not rolled_between("brent", pd.Timestamp("2026-10-05"), pd.Timestamp("2026-10-06"))
    assert not rolled_between(None, pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-01"))
    with pytest.raises(ValueError):
        roll_days("gold", "2026-10-01", "2026-10-31")


def test_referential_rejects_unknown_roll_rule():
    raw = {**RAW, "instruments": {"X": {"name": "x", "class": "commodities", "roll": "gold",
                                        "tickers": {"free": "GC=F"}}}}
    with pytest.raises(ReferentialError, match="roll"):
        referential_from_dicts(raw, {})


def test_roll_day_is_flagged_and_kept_out_of_sigma_and_movers():
    idx = IDX[IDX <= "2026-10-01"]
    brent = walk(70, 0.015, 1, idx)
    jumped = brent.copy()
    roll = roll_days("brent", idx[0], idx[-1])
    for day in roll[::2]:  # a +6 % calendar-spread jump at every effective roll day
        jumped.loc[day:] *= 1.06
    table = PerformanceEngine().compute(pd.DataFrame({"BRENT": jumped}), [REF.get("BRENT")],
                                        date(2026, 10, 1))
    row = table.loc["BRENT"]
    assert row["roll_1d"] and row["roll_1w"] and not row["suspect"]
    assert abs(row["z_1d"]) > 2                     # the jump is still visible...
    assert rank_movers(table, Horizon.D1).empty     # ...but not reported as a market move
    clean = PerformanceEngine().compute(pd.DataFrame({"BRENT": brent}), [REF.get("BRENT")],
                                        date(2026, 10, 1))
    # roll jumps excluded from sigma: same z on the day after, with or without the jumps
    later = PerformanceEngine().compute(pd.DataFrame({"BRENT": jumped.loc[:"2026-09-29"]}),
                                        [REF.get("BRENT")], date(2026, 9, 29))
    base = PerformanceEngine().compute(pd.DataFrame({"BRENT": brent.loc[:"2026-09-29"]}),
                                       [REF.get("BRENT")], date(2026, 9, 29))
    assert later.loc["BRENT", "z_1d"] == pytest.approx(base.loc["BRENT", "z_1d"], rel=0.02)
    assert not clean.loc["BRENT", "suspect"]


# ------------------------------------------------------------- suspect moves
def test_bad_print_is_suspect_and_not_a_mover():
    spx = walk(6000, 0.01, 2)
    spx.iloc[-1] = spx.iloc[-2] * 1.25  # +25 % in one session: a bad tick
    table = PerformanceEngine(suspect_abs_z=8).compute(pd.DataFrame({"SPX": spx}), [REF.get("SPX")], AS_OF)
    row = table.loc["SPX"]
    assert row["suspect"] and "écarts-types" in row["suspect_reason"]
    assert rank_movers(table, Horizon.D1).empty
    normal = PerformanceEngine(suspect_abs_z=50).compute(pd.DataFrame({"SPX": spx}), [REF.get("SPX")], AS_OF)
    assert not normal.loc["SPX", "suspect"]
    with pytest.raises(ValueError):
        PerformanceEngine(suspect_abs_z=0)


# --------------------------------------------------------------- cross-check
QS = QualitySettings()


def test_cross_check_ok_level_gap_and_move_gap():
    a = walk(6000, 0.01, 3)
    assert cross_check(a, a * 1.001, REF.get("SPX"), QS, "Yahoo").status == CHECK_OK
    level = cross_check(a, a * 1.10, REF.get("SPX"), QS, "Yahoo")
    assert level.status == CHECK_GAP and level.reason.startswith("niveau ")
    b = a.copy()
    b.iloc[-1] = b.iloc[-2] * 1.001
    a2 = a.copy()
    a2.iloc[-1] = a2.iloc[-2] * 1.045  # +4.5 % on one source, +0.1 % on the other
    move = cross_check(a2, b, REF.get("SPX"), QS, "Yahoo")
    assert move.status == CHECK_GAP and move.reason.startswith("1J +4,50") and "Yahoo" in move.reason


def test_cross_check_in_bp_and_unavailable_cases():
    y = walk(4.1, 0.03, 4, log=False)
    assert cross_check(y, y + 0.05, REF.get("UST_10Y"), QS, "FRED").status == CHECK_OK     # 5 bp
    assert cross_check(y, y + 0.40, REF.get("UST_10Y"), QS, "FRED").status == CHECK_GAP    # 40 bp
    assert cross_check(y, y.iloc[:-1], REF.get("UST_10Y"), QS, "FRED").status == CHECK_UNAVAILABLE
    assert cross_check(y, y.iloc[0:0], REF.get("UST_10Y"), QS, "FRED").status == CHECK_UNAVAILABLE
    assert math.isnan(daily_sigma(y.iloc[:5], REF.get("UST_10Y")))


# -------------------------------------------------------- monitor.verify
def _monitor(fmp_spx=None, quality=QS):
    spx, fx = walk(6000, 0.01, 5), walk(1.17, 0.004, 6)
    us2, us10 = walk(3.5, 0.03, 7, log=False), walk(4.1, 0.03, 8, log=False)
    free = FakeProvider("free", {"^GSPC": spx, "EURUSD=X": fx, "EEM": walk(45, 0.01, 9),
                                 "cnbc:US2Y|fred:DGS2": us2, "fred:DGS2": us2 + 0.01,
                                 "cnbc:US10Y|^TNX": us10, "^TNX": us10.iloc[:-3]})
    fmp = FakeProvider("fmp", {"^GSPC": spx if fmp_spx is None else fmp_spx, "EURUSD": fx * 1.0005,
                               "MXEF": walk(1300, 0.01, 10)})
    return MarketMonitor(MarketDataService([free, fmp]), REF, quality=quality), free, fmp


def test_verify_marks_gaps_and_skips_proxies():
    bad = walk(6000, 0.01, 5)
    bad.iloc[-1] *= 1.05                           # FMP disagrees by 5 % on the last session
    monitor, free, fmp = _monitor(fmp_spx=bad)
    report = monitor.performance(None, AS_OF, ["SPX", "EURUSD", "EEMX"])
    monitor.verify(report, ["SPX", "EURUSD", "EEMX"])
    table = report.table
    assert table.loc["SPX", "cross_check"] == CHECK_GAP and table.loc["SPX", "suspect"]
    assert "FMP" in table.loc["SPX", "suspect_reason"]
    assert table.loc["EURUSD", "cross_check"] == CHECK_OK and not table.loc["EURUSD", "suspect"]
    assert table.loc["EEMX", "cross_check"] == CHECK_UNAVAILABLE   # proxy: never compared
    assert "SPX" not in rank_movers(table, Horizon.D1).index


def test_verify_uses_the_other_chain_alternative_and_propagates_to_spreads():
    monitor, free, fmp = _monitor()
    report = monitor.performance(None, AS_OF, ["UST_2S10S"])
    monitor.verify(report, ["UST_2S10S"])
    # UST 2Y checked against FRED (1 bp off: ok); UST 10Y's ^TNX lags: unverifiable
    assert report.table.loc["UST_2S10S", "cross_check"] == CHECK_UNAVAILABLE
    assert not report.table.loc["UST_2S10S", "suspect"]
    free.data["fred:DGS2"] = free.data["fred:DGS2"].copy()
    free.data["fred:DGS2"].iloc[-1] += 0.5      # FRED now 50 bp away on the last day
    report = monitor.performance(None, AS_OF, ["UST_2S10S"])
    monitor.verify(report, ["UST_2S10S"])
    assert report.table.loc["UST_2S10S", "suspect"]
    assert report.table.loc["UST_2S10S", "suspect_reason"] == "jambe à vérifier : UST 2 ans"


def test_verify_can_be_disabled():
    monitor, free, fmp = _monitor(quality=QualitySettings(cross_check=False))
    report = monitor.verify(monitor.performance(None, AS_OF, ["SPX"]), ["SPX"])
    assert report.table.loc["SPX", "cross_check"] == ""
    assert not fmp.calls


# --------------------------------------------- legs out of sync, checks list
def test_legs_out_of_sync_and_fallback_are_noted():
    us2 = walk(3.5, 0.03, 7, log=False).iloc[:-1]     # 2Y one day late (FRED-like)
    us10 = walk(4.1, 0.03, 8, log=False)
    free = FakeProvider("free", {"cnbc:US2Y|fred:DGS2": us2, "cnbc:US10Y|^TNX": us10})
    monitor = MarketMonitor(MarketDataService([free]), REF)
    report = monitor.performance(None, AS_OF, ["UST_2S10S"])
    notes = report.checks()["UST_2S10S"]
    assert notes == ["jambes décalées : dernière date commune 01/10 (UST 10 ans au 02/10)"]


def test_publication_checks_gathers_everything():
    table = pd.DataFrame({
        "level": [1.0, np.nan, 2.0], "stale": [True, False, False],
        "level_date": pd.to_datetime(["2026-09-25", None, "2026-10-02"]),
        "suspect": [False, False, True], "suspect_reason": ["", "", "écart"],
        "roll_1d": [False, False, True], "roll_1w": [False, False, True],
        "proxy": ["ETF", None, None],
    }, index=["A", "B", "C"])
    checks = publication_checks(table, {"C": ["source de secours (Bundesbank)"]}, {"B": "timeout"})
    assert checks["A"] == ["dernière cotation le 25/09", "source proxy (ETF)"]
    assert checks["B"] == ["donnée manquante (timeout)"]
    assert checks["C"] == ["mouvement suspect : écart",
                           "1J affecté par un changement de contrat (échéance du front-month)",
                           "source de secours (Bundesbank)"]
