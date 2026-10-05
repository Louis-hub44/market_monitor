from datetime import date, timedelta

import pandas as pd
import pytest

from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError
from tests.conftest import FakeProvider, bday_series


@pytest.fixture
def setup(tmp_path, clock):
    inner = FakeProvider(data={"A": bday_series("2026-01-01", "2026-10-05"),
                               "B": bday_series("2026-01-01", "2026-10-05", 50.0)})
    cached = CachedProvider(inner, ParquetCache(tmp_path), clock=clock,
                            intraday_ttl=timedelta(minutes=15), refresh_lookback_days=5)
    return inner, cached


def _windows(inner):
    return [(c.start, c.end) for c in inner.calls]


def test_second_call_is_served_from_cache(setup):
    inner, cached = setup
    first = cached.get_history(["A", "B"], "2026-06-01", "2026-10-05")
    second = cached.get_history(["A", "B"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 1
    pd.testing.assert_frame_equal(first.data, second.data)
    assert first.sources == {"A": "fake", "B": "fake"}


def test_cache_survives_a_new_instance(setup, tmp_path, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-09-01")
    fresh = CachedProvider(inner, ParquetCache(tmp_path), clock=clock)
    fresh.get_history(["A"], "2026-07-01", "2026-08-01")
    assert len(inner.calls) == 1


def test_closed_history_is_never_refreshed(setup, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-09-01")
    clock.advance(days=3)
    cached.get_history(["A"], "2026-06-01", "2026-09-01")
    assert len(inner.calls) == 1


def test_ttl_expiry_refreshes_only_the_tail_with_lookback(setup, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    clock.advance(minutes=20)
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    # anchor = min(c_e + 1, d_f) = 2026-10-05 ; minus L = 5 days
    assert _windows(inner)[-1] == (date(2026, 9, 30), date(2026, 10, 5))


def test_left_extension_fetches_only_the_gap(setup):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    result = cached.get_history(["A"], "2026-03-02", "2026-10-05")
    assert _windows(inner)[-1] == (date(2026, 3, 2), date(2026, 5, 31))
    assert result.data.index[0] == pd.Timestamp("2026-03-02")


def test_next_day_extends_from_coverage(setup, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-10-02")  # Friday close
    clock.advance(days=3)  # Thursday 8 Oct
    inner.data["A"] = bday_series("2026-01-01", "2026-10-08")
    result = cached.get_history(["A"], "2026-06-01", "2026-10-08")
    assert _windows(inner)[-1] == (date(2026, 9, 28), date(2026, 10, 8))
    assert result.data.index[-1] == pd.Timestamp("2026-10-08")


def test_future_end_is_clipped_to_today(setup):
    inner, cached = setup
    cached.get_history(["A"], "2026-09-01", "2026-12-31")
    assert inner.calls[0].end == date(2026, 10, 5)
    assert cached.get_history(["A"], "2026-11-01", "2026-12-31").errors["A"]


def test_provider_down_serves_stale_cache_with_warning(setup, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    clock.advance(hours=1)
    inner.fail = ProviderUnavailableError("terminal closed")
    result = cached.get_history(["A", "B"], "2026-06-01", "2026-10-05")
    assert not result.data["A"].isna().all()
    assert "serving cached data" in result.warnings["A"]
    assert "terminal closed" in result.errors["B"]
    assert len(inner.calls) == 2  # stopped after the first unavailable error


def test_unknown_ticker_is_not_cached(setup):
    inner, cached = setup
    result = cached.get_history(["ZZZ"], "2026-06-01", "2026-10-05")
    assert result.errors["ZZZ"]
    cached.get_history(["ZZZ"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 2  # retried, not memorised as "empty"


def test_partial_segment_failure_is_a_warning(setup, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    inner.fail = DataProviderError("bad window")
    result = cached.get_history(["A"], "2026-03-02", "2026-10-05")
    assert "bad window" in result.warnings["A"]
    assert result.data.index[0] == pd.Timestamp("2026-06-01")


def test_snapshot_bypasses_cache(setup):
    inner, cached = setup
    today = pd.Timestamp.today().normalize()
    inner.data["A"] = bday_series(str(today - pd.Timedelta(days=30)), str(today))
    snap = cached.get_snapshot(["A"])
    assert snap.data.loc["A", "value"] == inner.data["A"].iloc[-1]
    assert inner.calls  # went straight to the inner provider
