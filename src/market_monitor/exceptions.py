"""Exception hierarchy of the Market Monitor package."""

from __future__ import annotations


class MarketMonitorError(Exception):
    """Base class for every error raised by the package."""


class ConfigError(MarketMonitorError):
    """Invalid or missing configuration."""


class InvalidRequestError(MarketMonitorError, ValueError):
    """The caller's request is malformed (dates, tickers, field...)."""


class DataProviderError(MarketMonitorError):
    """A data provider could not serve (part of) a request."""


class ProviderUnavailableError(DataProviderError):
    """The provider as a whole is unreachable (no Terminal, network down, bad key, quota).

    Callers must stop hitting this provider for the current request and fall back.
    """
