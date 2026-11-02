"""v1.6.2: manual refresh - navigating between views never asks the sources again."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.config import load_settings
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.service import MarketDataService
from market_monitor.exceptions import ConfigError
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import referential_from_dicts
from tests.conftest import FakeProvider, bday_series

ROOT = Path(__file__).resolve().parents[1]
#: every window the dashboard's source was asked for (filled by ui_refresh_app.py)
CALLS: list[object] = []


@pytest.fixture
def setup(tmp_path, clock):
    inner = FakeProvider(data={"A": bday_series("2026-01-01", "2026-10-05")})
    cached = CachedProvider(inner, ParquetCache(tmp_path), clock=clock,
                            intraday_ttl=timedelta(minutes=15), refresh_lookback_days=5)
    return inner, cached


def test_held_data_is_reused_whatever_its_age(setup, clock):
    inner, cached = setup
    cached.hold_until(clock())
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    clock.advance(minutes=90)                                    # beyond the 15-minute TTL
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 1
    cached.hold_until(clock())                                   # "Actualiser les données"
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 2                                 # downloaded again at once
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 2


def test_held_data_expires_the_next_day(setup, clock):
    inner, cached = setup
    cached.hold_until(clock())
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    clock.advance(days=1)
    cached.get_history(["A"], "2026-06-01", "2026-10-05")       # yesterday's intraday print
    assert len(inner.calls) == 2


def test_without_floor_the_ttl_applies(setup, clock):
    inner, cached = setup
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    clock.advance(minutes=20)
    cached.get_history(["A"], "2026-06-01", "2026-10-05")
    assert len(inner.calls) == 2


def test_monitor_marks_every_cached_source_and_dates_the_closing_rule(setup, clock):
    inner, cached = setup
    ref = referential_from_dicts({"instruments": {"A": {"name": "A", "class": "equity", "quote": "price",
                                                        "tickers": {"free": "A"}}}}, {})
    monitor = MarketMonitor(MarketDataService([cached]), ref)
    assert monitor.last_refresh is None and monitor.data_time() <= monitor.now()
    at = monitor.mark_refresh()
    assert cached.refresh_floor == at == monitor.last_refresh
    assert monitor.data_time() == at.astimezone(monitor.now().tzinfo)     # same day: last refresh
    monitor.mark_refresh(datetime(2020, 1, 2, 9, 0, tzinfo=UTC))
    assert monitor.data_time().date() == monitor.today()                   # another day: now


def test_refresh_setting(tmp_path):
    assert load_settings(ROOT / "config" / "config.yaml").ui.refresh == "manual"
    bad = tmp_path / "config.yaml"
    bad.write_text("ui:\n  refresh: sometimes\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="ui.refresh"):
        load_settings(bad)


# ------------------------------------------------------------------------ UI
def test_switching_views_does_not_download_again(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_TEST_CACHE", str(tmp_path / "cache"))
    CALLS.clear()
    at = AppTest.from_file(str(Path(__file__).with_name("ui_refresh_app.py")), default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    for view in ("overview", "curves", "instrument", "history"):
        at.segmented_control(key="mm-view").set_value(view).run()
        assert not at.exception, (view, at.exception)
    first_tour = len(CALLS)
    assert first_tour > 0
    for view in ("overview", "curves", "instrument", "history", "seance", "overview"):
        at.segmented_control(key="mm-view").set_value(view).run()
    assert len(CALLS) == first_tour                    # nothing asked again
    assert any("changer de vue ne réinterroge pas" in c.value for c in at.sidebar.caption)
    at.sidebar.button(key="mm-refresh").click().run()
    assert len(CALLS) > first_tour                     # the button reloads
