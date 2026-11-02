"""AppTest script: the dashboard on a cached in-memory market (instrument page)."""

import os
from datetime import UTC, datetime

from market_monitor.config import UiSettings
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from tests.test_instrument_page import REF, market

cached = CachedProvider(market(), ParquetCache(os.environ["MM_TEST_CACHE"]),
                        clock=lambda: datetime(2026, 10, 27, 8, 0, tzinfo=UTC))
monitor = MarketMonitor(MarketDataService([cached]), REF)

from market_monitor.ui.page import render_market_monitor  # noqa: E402

render_market_monitor(monitor, UiSettings(default_watchlist="all", cache_ttl_minutes=0))
