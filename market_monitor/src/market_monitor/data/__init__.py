"""Data layer: provider abstraction, cache and multi-provider service.

Typical use (from the terminal or the Streamlit UI)::

    from market_monitor.config import load_settings
    from market_monitor.data import build_service

    service = build_service(load_settings())
    result = service.get_history(symbols, "2026-01-01", "2026-10-05")
"""

from market_monitor.data.base import DataProvider
from market_monitor.data.cache import CacheEntry, ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.factory import build_provider, build_service, with_cache
from market_monitor.data.models import (
    Field,
    HistoryRequest,
    HistoryResult,
    SnapshotResult,
)
from market_monitor.data.service import MarketDataService, SymbolMap

__all__ = [
    "CacheEntry", "CachedProvider", "DataProvider", "Field", "HistoryRequest", "HistoryResult",
    "MarketDataService", "ParquetCache", "SnapshotResult", "SymbolMap",
    "build_provider", "build_service", "with_cache",
]
