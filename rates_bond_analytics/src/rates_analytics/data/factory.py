"""Build the market-data service from :class:`~rates_analytics.config.Settings`.

``data.provider`` selects the primary source, ``data.fallback`` the ordered
chain used for missing ids:

* ``bloomberg`` -> :class:`BloombergProvider` (Desktop API, reference source)
* ``fmp``       -> :class:`FmpProvider` (needs ``FMP_API_KEY``)
* ``free``      -> :class:`EcbProvider` (ECB Data Portal, no key)
* ``public``    -> :class:`PublicProvider` (CNBC / Bundesbank / Stooq sovereign yields, no key)
* ``static``    -> :class:`StaticProvider` loaded from ``providers.static.directory``

Every REST provider shares one :class:`requests.Session` configured with the SSL
policy of :mod:`rates_analytics.network` (corporate proxy support).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from datetime import timedelta
from pathlib import Path

from rates_analytics.config import PROJECT_ROOT, Settings
from rates_analytics.data.cache import ParquetCache
from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.provider import DataProvider
from rates_analytics.data.providers.bloomberg import BloombergProvider
from rates_analytics.data.providers.ecb import EcbProvider
from rates_analytics.data.providers.fmp import FmpProvider
from rates_analytics.data.providers.public import PublicProvider
from rates_analytics.data.providers.static import StaticProvider
from rates_analytics.data.service import MarketDataService
from rates_analytics.exceptions import ConfigurationError
from rates_analytics.network import NetworkSettings, build_session

logger = logging.getLogger(__name__)


def build_provider(name: str, settings: Settings, mapping: MarketDataMap, session: Any | None = None) -> DataProvider:
    """Instantiate one provider by configuration name."""
    lookback = settings.data.quote_lookback_days
    if name == "bloomberg":
        o = settings.provider_options("bloomberg")
        return BloombergProvider(mapping, host=o.get("host", "localhost"), port=int(o.get("port", 8194)),
                                 timeout_ms=int(o.get("timeout_ms", 30000)),
                                 default_field=o.get("field", "PX_LAST"), quote_lookback_days=lookback)
    if name == "fmp":
        o = settings.provider_options("fmp")
        key = o.get("api_key") or os.environ.get("FMP_API_KEY", "")
        return FmpProvider(mapping, api_key=key, base_url=o.get("base_url", "https://financialmodelingprep.com/stable"),
                           timeout_s=float(o.get("timeout_s", 20)), max_days_per_call=int(o.get("max_days_per_call", 90)),
                           quote_lookback_days=lookback, session=session)
    if name == "free":
        o = settings.provider_options("ecb")
        return EcbProvider(mapping, base_url=o.get("base_url", "https://data-api.ecb.europa.eu/service/data"),
                           timeout_s=float(o.get("timeout_s", 30)), quote_lookback_days=lookback, session=session)
    if name == "public":
        o = settings.provider_options("public")
        return PublicProvider(mapping, timeout_s=float(o.get("timeout_s", 20)), session=session,
                              quote_lookback_days=lookback)
    if name == "static":
        o = settings.provider_options("static")
        directory = Path(o.get("directory", "data/sample"))
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        return StaticProvider.from_directory(directory, lookback)
    raise ConfigurationError(f"Unknown provider {name!r}")


def build_market_data_service(settings: Settings, mapping: MarketDataMap | None = None,
                              network: NetworkSettings | None = None) -> MarketDataService:
    """Primary provider + fallbacks + cache, as configured.

    A fallback that cannot be built (e.g. FMP without key) is skipped with a
    warning; a primary that cannot be built raises :class:`ConfigurationError`.
    """
    mapping = mapping or MarketDataMap(settings.market_data)
    chain = [settings.data.provider, *settings.data.fallback]
    session = build_session(network or settings.network) if set(chain) & {"fmp", "free", "public"} else None
    providers = [build_provider(settings.data.provider, settings, mapping, session)]
    for name in settings.data.fallback:
        try:
            providers.append(build_provider(name, settings, mapping, session))
        except ConfigurationError as exc:
            logger.warning("Fallback provider %s disabled: %s", name, exc)
    c = settings.data.cache
    cache = ParquetCache(c.directory, c.ttl_hours_live) if c.enabled else None
    return MarketDataService(providers, cache=cache, missing_policy=settings.data.missing_policy,
                             plausible_rate_range=settings.data.plausible_rate_range,
                             static_ttl=timedelta(days=c.ttl_days_static))
