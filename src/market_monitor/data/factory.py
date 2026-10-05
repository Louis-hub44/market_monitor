"""Build providers and the :class:`MarketDataService` from :class:`Settings`."""

from __future__ import annotations

import logging
from datetime import timedelta

import requests

from market_monitor.config import Settings
from market_monitor.data.base import DataProvider
from market_monitor.data.cache import ParquetCache
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.providers import (
    BloombergProvider,
    ECBClient,
    FMPProvider,
    FreeProvider,
    YahooClient,
)
from market_monitor.data.service import MarketDataService
from market_monitor.exceptions import ConfigError
from market_monitor.network import build_session, configure_session

logger = logging.getLogger(__name__)


def rest_session(settings: Settings) -> requests.Session | None:
    """``requests`` session carrying the corporate SSL policy, ``None`` when not customised."""
    net = settings.network
    if not net.customised:
        return None
    return configure_session(requests.Session(), net.insecure_ssl, net.ca_bundle)


def build_provider(name: str, settings: Settings) -> DataProvider:
    """Instantiate one raw (uncached) provider by name."""
    if name == "bloomberg":
        bbg = settings.bloomberg
        return BloombergProvider(bbg.host, bbg.port, bbg.timeout_ms)
    if name == "fmp":
        fmp = settings.fmp
        return FMPProvider(
            fmp.api_key, base_url=fmp.base_url, timeout_s=fmp.timeout_s, max_retries=fmp.max_retries,
            session=rest_session(settings),
        )
    if name == "free":
        free = settings.free
        net = settings.network
        return FreeProvider(
            ECBClient(free.ecb_base_url, timeout_s=free.timeout_s, session=rest_session(settings)),
            YahooClient(session=build_session(net.insecure_ssl, net.ca_bundle)),
        )
    raise ConfigError(f"unknown provider {name!r}")


def with_cache(provider: DataProvider, settings: Settings) -> DataProvider:
    """Wrap ``provider`` with the parquet cache when enabled."""
    if not settings.cache.enabled:
        return provider
    return CachedProvider(
        provider,
        ParquetCache(settings.cache.directory),
        intraday_ttl=timedelta(minutes=settings.cache.intraday_ttl_minutes),
        refresh_lookback_days=settings.cache.refresh_lookback_days,
        timezone=settings.timezone,
    )


def build_service(settings: Settings) -> MarketDataService:
    """Providers in priority order, unavailable ones skipped, each wrapped by the cache."""
    providers: list[DataProvider] = []
    for name in settings.priority:
        provider = build_provider(name, settings)
        if not provider.is_available():
            logger.warning("provider %r unavailable (missing library or API key) - skipped", name)
            continue
        providers.append(with_cache(provider, settings))
    if not providers:
        raise ConfigError(f"no available provider among {list(settings.priority)}")
    logger.info("data providers: %s", [p.name for p in providers])
    return MarketDataService(providers)
