"""AppTest script: dashboard in manual-refresh mode, counting the source's downloads."""

import os

import streamlit as st

from market_monitor.config import UiSettings
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from tests.test_instrument_page import REF, market
from tests.test_manual_refresh import CALLS


@st.cache_resource
def _monitor(cache: str) -> MarketMonitor:
    source = market()
    source.calls = CALLS
    monitor = MarketMonitor(MarketDataService([CachedProvider(source, ParquetCache(cache))]), REF)
    monitor.mark_refresh()
    return monitor


from market_monitor.ui.page import render_market_monitor  # noqa: E402

render_market_monitor(_monitor(os.environ["MM_TEST_CACHE"]),
                      UiSettings(default_watchlist="all", cache_ttl_minutes=5, refresh="manual"))
