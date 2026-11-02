"""AppTest script: the dashboard with an index and its members (top movers, stock pages)."""

import os
from datetime import UTC, datetime

import streamlit as st

from market_monitor.config import UiSettings
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from tests.test_movers import demo_market


@st.cache_resource
def _market(directory: str):
    monitor, universe = demo_market(directory)
    cached = CachedProvider(monitor.service.providers[0], ParquetCache(os.path.join(directory, "cache")),
                            clock=lambda: datetime(2026, 10, 27, 8, 0, tzinfo=UTC))
    providers = [cached]
    if os.environ.get("MM_TEST_FMP"):  # second source: same closes, one member 3 % off
        from tests.test_bars_history import BarsFake
        data = {t: b.copy() for t, b in monitor.service.providers[0].data.items()}
        data["MC.PA"].iloc[-1, data["MC.PA"].columns.get_loc("last")] *= 1.03
        providers.append(BarsFake(data, name="fmp"))
    return MarketMonitor(MarketDataService(providers), monitor.referential), universe


from market_monitor.ui.page import render_market_monitor  # noqa: E402

monitor, universe = _market(os.environ["MM_TEST_DIR"])
ui = UiSettings(default_watchlist="all", cache_ttl_minutes=0,
                default_view=os.environ.get("MM_TEST_VIEW", "overview"))
render_market_monitor(monitor, ui, equities=universe)
