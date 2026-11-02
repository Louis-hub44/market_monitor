"""v1.6.5: Brent quoted on the most active contract (next one 5 business days before expiry)."""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from market_monitor.data.models import Field
from market_monitor.data.providers.free import FreeProvider, FuturesClient
from market_monitor.exceptions import DataProviderError
from market_monitor.referential import ReferentialError, load_referential, referential_from_dicts
from market_monitor.rolls import (
    contract_symbol,
    front_contract,
    last_trading_day,
    roll_days,
    rolled_between,
    switch_day,
)

ROOT = Path(__file__).resolve().parents[1]
KEY = "brent/BZ=F/BZ{code}{yy}.NYM/5"


def test_contract_calendar():
    assert last_trading_day("brent", 2026, 12) == pd.Timestamp("2026-10-30")   # Friday
    assert last_trading_day("brent", 2027, 1) == pd.Timestamp("2026-11-30")
    assert last_trading_day("wti", 2026, 12) == pd.Timestamp("2026-11-20")
    assert last_trading_day("ttf", 2026, 12) == pd.Timestamp("2026-11-27")
    assert front_contract("brent", date(2026, 10, 29)) == (2026, 12)          # the generic
    assert front_contract("brent", date(2026, 10, 29), 5) == (2027, 1)        # most active
    assert front_contract("brent", date(2026, 10, 23), 5) == (2026, 12)
    assert front_contract("brent", date(2026, 10, 26), 5) == (2027, 1)
    assert switch_day("brent", 2026, 12, 5) == pd.Timestamp("2026-10-26")
    assert contract_symbol("BZ{code}{yy}.NYM", 2027, 1) == "BZF27.NYM"
    assert contract_symbol("BZ{code}{yy}.NYM", 2026, 12) == "BZZ26.NYM"


def test_roll_days_with_an_early_roll():
    days = roll_days("brent", "2026-10-01", "2026-11-30", ahead=5)
    for day in ("2026-10-26", "2026-10-27", "2026-11-02"):    # early switch, tolerance, expiry roll
        assert pd.Timestamp(day) in days
    assert pd.Timestamp("2026-10-26") not in roll_days("brent", "2026-10-01", "2026-11-30")
    assert rolled_between("brent", pd.Timestamp("2026-10-23"), pd.Timestamp("2026-10-26"), 5)
    assert not rolled_between("brent", pd.Timestamp("2026-10-23"), pd.Timestamp("2026-10-26"))


class FakeYahoo:
    def __init__(self, data):
        self.data, self.calls = data, []

    def fetch(self, tickers, start, end, field):
        self.calls.append(tuple(tickers))
        return {t: self.data[t].loc[pd.Timestamp(start):pd.Timestamp(end)] for t in tickers if t in self.data}

    def fetch_bars(self, tickers, start, end):
        return {t: pd.DataFrame({"open": s, "high": s, "low": s, "last": s})
                for t, s in self.fetch(tickers, start, end, Field.LAST).items()}


DAYS = pd.bdate_range("2026-10-01", "2026-10-29")
GENERIC = pd.Series(110.0, index=DAYS)
GENERIC.loc["2026-10-29"] = 115.32              # December, the day before its expiry
JANUARY = pd.Series(108.0, index=DAYS)
JANUARY.loc["2026-10-29"] = 111.64


def test_futures_client_quotes_the_next_contract_in_the_roll_window():
    yahoo = FakeYahoo({"BZ=F": GENERIC, "BZF27.NYM": JANUARY.drop(pd.Timestamp("2026-10-27"))})
    series = FuturesClient(yahoo).fetch(KEY, date(2026, 10, 1), date(2026, 10, 29))
    assert series.loc["2026-10-29"] == pytest.approx(111.64)
    assert series.loc["2026-10-26"] == pytest.approx(108.0)
    assert series.loc["2026-10-27"] == pytest.approx(110.0)       # no January print: the generic
    assert series.loc["2026-10-23"] == pytest.approx(110.0)       # before the window: the generic
    assert ("BZF27.NYM",) in yahoo.calls
    bars = FuturesClient(yahoo).fetch_bars(KEY, date(2026, 10, 1), date(2026, 10, 29))
    assert bars.loc["2026-10-29", "last"] == pytest.approx(111.64)


def test_futures_client_outside_a_window_and_failures():
    yahoo = FakeYahoo({"BZ=F": GENERIC})
    quiet = FuturesClient(yahoo).fetch(KEY, date(2026, 10, 1), date(2026, 10, 20))
    assert quiet.equals(GENERIC.loc[:"2026-10-20"]) and yahoo.calls == [("BZ=F",)]

    class Down(FakeYahoo):
        def fetch(self, tickers, start, end, field):
            if tickers != ["BZ=F"]:
                raise DataProviderError("contract unknown")
            return super().fetch(tickers, start, end, field)
    kept = FuturesClient(Down({"BZ=F": GENERIC})).fetch(KEY, date(2026, 10, 1), date(2026, 10, 29))
    assert kept.loc["2026-10-29"] == pytest.approx(115.32)
    with pytest.raises(DataProviderError, match="expected"):
        FuturesClient.parse("brent/BZ=F")


def test_free_provider_routes_fut_tickers():
    provider = FreeProvider(yahoo=FakeYahoo({"BZ=F": GENERIC, "BZF27.NYM": JANUARY}))
    result = provider.get_history([f"fut:{KEY}|BZ=F"], "2026-10-20", "2026-10-29")
    assert result.data[f"fut:{KEY}|BZ=F"].iloc[-1] == pytest.approx(111.64)


def test_repository_brent_and_roll_ahead_validation():
    ref = load_referential(ROOT / "config" / "instruments.yaml", ROOT / "config" / "watchlists.yaml")
    brent = ref.get("BRENT")
    assert brent.roll == "brent" and brent.roll_ahead == 5 and "fmp" not in brent.tickers
    assert brent.tickers["free"].ticker.startswith("fut:brent/BZ=F/")
    with pytest.raises(ReferentialError, match="roll_ahead"):
        referential_from_dicts({"instruments": {"X": {"name": "X", "class": "commodities", "quote": "price",
                                                      "roll_ahead": 3, "tickers": {"free": "X"}}}}, {})
