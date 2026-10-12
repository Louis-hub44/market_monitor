from datetime import UTC, date, datetime

import pandas as pd
import pytest

from market_monitor.data.cache import CacheEntry, ParquetCache, safe_name
from market_monitor.data.models import Field
from tests.conftest import bday_series


def _entry():
    return CacheEntry(
        series=bday_series("2026-01-01", "2026-01-31"),
        covered_start=date(2026, 1, 1),
        covered_end=date(2026, 1, 31),
        fetched_at=datetime(2026, 2, 1, 9, tzinfo=UTC),
    )


def test_roundtrip_keeps_series_and_coverage(tmp_path):
    cache = ParquetCache(tmp_path)
    entry = _entry()
    assert cache.save("bloomberg", Field.LAST, "SX5E Index", entry)
    loaded = cache.load("bloomberg", Field.LAST, "SX5E Index")
    assert loaded is not None
    assert loaded.series.equals(entry.series.rename_axis("date"))
    assert (loaded.covered_start, loaded.covered_end) == (entry.covered_start, entry.covered_end)
    assert loaded.fetched_at == entry.fetched_at


def test_miss_and_corruption_return_none(tmp_path):
    cache = ParquetCache(tmp_path)
    assert cache.load("fmp", Field.LAST, "^GSPC") is None
    path = cache.path_for("fmp", Field.LAST, "^GSPC")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not parquet")
    assert cache.load("fmp", Field.LAST, "^GSPC") is None


def test_safe_names_are_unique_and_windows_safe():
    names = {safe_name(t) for t in ["A/B", "A B", "A:B", "CON"]}
    assert len(names) == 4
    assert all(not set(n) & set('<>:"/\\|?* ') for n in names)


def test_clear_by_provider(tmp_path):
    cache = ParquetCache(tmp_path)
    cache.save("fmp", Field.LAST, "X", _entry())
    cache.save("free", Field.LAST, "X", _entry())
    cache.clear("fmp")
    assert cache.load("fmp", Field.LAST, "X") is None
    assert cache.load("free", Field.LAST, "X") is not None


def test_entry_validation():
    with pytest.raises(ValueError):
        CacheEntry(bday_series("2026-01-01", "2026-01-02"), date(2026, 2, 1), date(2026, 1, 1),
                   datetime(2026, 2, 1, tzinfo=UTC))


def test_cnbc_series_cached_before_the_date_fix_are_reloaded(tmp_path):
    import json

    import pyarrow.parquet as pq

    from market_monitor.data import cache as cache_module

    cache = ParquetCache(tmp_path)
    entry = CacheEntry(series=pd.Series([3.48], index=pd.to_datetime(["2026-10-09"])),
                       covered_start=date(2026, 10, 1), covered_end=date(2026, 10, 9),
                       fetched_at=datetime(2026, 10, 9, 18, 0, tzinfo=UTC))
    for ticker in ("cnbc:DE10Y-DE|stooq:10dey.b", "^GSPC"):
        assert cache.save("free", Field.LAST, ticker, entry)
        path = cache.path_for("free", Field.LAST, ticker)
        table = pq.read_table(path)
        meta = json.loads(table.schema.metadata[cache_module._META_KEY])
        meta["version"] = 2  # written by v1.5.8
        pq.write_table(table.replace_schema_metadata(
            {**table.schema.metadata, cache_module._META_KEY: json.dumps(meta)}), path)
    assert cache.load("free", Field.LAST, "cnbc:DE10Y-DE|stooq:10dey.b") is None
    assert cache.load("free", Field.LAST, "^GSPC") is not None
