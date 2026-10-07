"""A cached series is never spliced from two alternatives of a fallback chain."""

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_monitor.data.cache import CacheEntry, ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.models import Field, chain_alternatives, fallback_origin
from market_monitor.data.providers.free import ECBClient, FreeProvider, YahooClient
from market_monitor.data.service import MarketDataService
from market_monitor.exceptions import ProviderUnavailableError

CHAIN = "cnbc:DE10Y-DE|bbk:BUND"
IDX = pd.bdate_range("2026-03-02", "2026-10-05")


class Source:
    def __init__(self, level: float, start: str = "2026-03-02") -> None:
        self.series = pd.Series(level, index=IDX[IDX >= start])
        self.down = False
        self.calls: list[tuple[str, date, date]] = []

    def fetch(self, key, start, end):
        self.calls.append((key, start, end))
        if self.down:
            raise ProviderUnavailableError("blocked by proxy")
        return self.series.loc[pd.Timestamp(start):pd.Timestamp(end)]


@pytest.fixture
def world(tmp_path):
    cnbc, bbk = Source(2.70, start="2026-06-01"), Source(2.62)  # CNBC: shorter history
    clock = {"now": datetime(2026, 10, 5, 7, 0, tzinfo=UTC)}
    # the source circuit breaker (10 min) follows the same simulated clock
    free = FreeProvider(ECBClient(), YahooClient(lambda *a: None), sources={"cnbc:": cnbc, "bbk:": bbk},
                        clock=lambda: clock["now"].timestamp())
    cached = CachedProvider(free, ParquetCache(tmp_path), clock=lambda: clock["now"])
    return cnbc, bbk, cached, clock


def _later(clock, hours=1):
    clock["now"] += timedelta(hours=hours)


def test_chain_helpers():
    assert chain_alternatives(CHAIN) == ["cnbc:DE10Y-DE", "bbk:BUND"]
    assert chain_alternatives("^VIX") == ["^VIX"]
    assert fallback_origin(CHAIN, "bbk:BUND") == "bbk:BUND"
    assert fallback_origin(CHAIN, "cnbc:DE10Y-DE") is None
    assert fallback_origin("^VIX", "^VIX") is None


def test_fallback_reloads_the_whole_window_instead_of_splicing(world):
    cnbc, bbk, cached, clock = world
    first = cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    assert first.origins == {CHAIN: "cnbc:DE10Y-DE"} and not first.warnings
    cnbc.down = True
    _later(clock)
    second = cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    series = second.data[CHAIN]
    assert series.nunique() == 1 and series.iloc[0] == 2.62            # no -8 bp seam
    assert series.index[0] == pd.Timestamp("2026-06-01")              # full window reloaded
    assert second.origins == {CHAIN: "bbk:BUND"}
    assert second.warnings[CHAIN] == "served by fallback bbk:BUND"


def test_primary_source_comes_back(world):
    cnbc, bbk, cached, clock = world
    cnbc.down = True
    assert cached.get_history([CHAIN], "2026-06-01", "2026-10-05").origins[CHAIN] == "bbk:BUND"
    cnbc.down = False
    _later(clock)
    result = cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    assert result.data[CHAIN].nunique() == 1 and result.data[CHAIN].iloc[-1] == 2.70
    assert result.origins[CHAIN] == "cnbc:DE10Y-DE" and not result.warnings


def test_left_extension_asks_the_cached_origin(world):
    cnbc, bbk, cached, clock = world
    cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    result = cached.get_history([CHAIN], "2026-03-02", "2026-10-05")
    # CNBC has nothing before June: the gap stays empty, Bundesbank is not spliced in
    assert result.data[CHAIN].dropna().nunique() == 1
    assert bbk.calls == []
    assert cnbc.calls[-1] == ("DE10Y-DE", date(2026, 3, 2), date(2026, 5, 31))


def test_switch_keeps_cache_when_the_fallback_has_no_full_history(world):
    cnbc, bbk, cached, clock = world
    cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    cnbc.down = True
    bbk.series = bbk.series.loc["2026-10-01":]  # fallback only knows the last days
    _later(clock)
    original_fetch = bbk.fetch

    def short_then_none(key, start, end):
        data = original_fetch(key, start, end)
        return data if (end - start).days < 10 else data.iloc[0:0]

    bbk.fetch = short_then_none
    result = cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    assert result.data[CHAIN].dropna().nunique() == 1 and result.data[CHAIN].iloc[-1] == 2.70
    assert "full history is unavailable" in result.warnings[CHAIN]


def test_legacy_chain_cache_is_rebuilt_once(world, tmp_path):
    cnbc, bbk, cached, clock = world
    # a v1 file (no origin) that mixes two sources, as written by v1.2.x
    spliced = pd.concat([pd.Series(2.70, index=IDX[IDX < "2026-09-28"]),
                         pd.Series(2.62, index=IDX[IDX >= "2026-09-28"])])
    cache = ParquetCache(tmp_path)
    cache.save("free", Field.LAST, CHAIN, CacheEntry(
        spliced.loc["2026-06-01":], date(2026, 6, 1), date(2026, 10, 5),
        datetime(2026, 10, 5, 6, 0, tzinfo=UTC)))
    path = cache.path_for("free", Field.LAST, CHAIN)
    table = pq.read_table(path)
    meta = json.loads(table.schema.metadata[b"market_monitor"])
    meta.update(version=1)
    meta.pop("origin")
    pq.write_table(table.replace_schema_metadata({b"market_monitor": json.dumps(meta).encode()}), path)
    assert cache.load("free", Field.LAST, CHAIN).origin is None
    _later(clock)
    result = cached.get_history([CHAIN], "2026-06-01", "2026-10-05")
    assert result.data[CHAIN].nunique() == 1 and result.origins[CHAIN] == "cnbc:DE10Y-DE"


def test_plain_tickers_keep_their_behaviour(world):
    cnbc, bbk, cached, clock = world
    result = cached.get_history(["bbk:BUND"], "2026-06-01", "2026-10-05")
    assert result.origins == {"bbk:BUND": "bbk:BUND"} and not result.warnings


def test_service_reports_origins_per_instrument(world):
    cnbc, bbk, cached, clock = world
    cnbc.down = True
    service = MarketDataService([cached])
    result = service.get_history({"BUND_10Y": {"free": CHAIN}}, "2026-06-01", "2026-10-05")
    assert result.origins == {"BUND_10Y": "bbk:BUND"}
    assert "fallback" in result.warnings["BUND_10Y"]


def test_cache_entry_roundtrip_keeps_origin(tmp_path):
    cache = ParquetCache(tmp_path)
    entry = CacheEntry(pd.Series([1.0], index=pd.to_datetime(["2026-10-01"])), date(2026, 10, 1),
                       date(2026, 10, 1), datetime(2026, 10, 1, tzinfo=UTC), "bbk:BUND")
    cache.save("free", Field.LAST, CHAIN, entry)
    assert cache.load("free", Field.LAST, CHAIN).origin == "bbk:BUND"
    assert pa  # pyarrow imported for the legacy test
