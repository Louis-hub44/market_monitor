"""Concrete market-data providers."""

from rates_analytics.data.providers.ecb import EcbProvider
from rates_analytics.data.providers.fmp import FmpProvider
from rates_analytics.data.providers.public import PublicProvider
from rates_analytics.data.providers.static import StaticProvider

__all__ = ["EcbProvider", "FmpProvider", "PublicProvider", "StaticProvider"]
