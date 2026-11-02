"""Exception hierarchy of the Rates & Bond Analytics engine.

Every error raised on purpose by the engine derives from :class:`RatesAnalyticsError`,
so that the UI (or another terminal module) can catch them in one place.
"""

from __future__ import annotations


class RatesAnalyticsError(Exception):
    """Base class for all engine errors."""


class ConventionError(RatesAnalyticsError, ValueError):
    """Invalid market convention, tenor, day count or calendar input."""


class ScheduleError(RatesAnalyticsError, ValueError):
    """Schedule generation received inconsistent dates or parameters."""


class ConfigurationError(RatesAnalyticsError):
    """Invalid or incomplete configuration (config.yaml / .env)."""


class DataProviderError(RatesAnalyticsError):
    """A data provider failed (connection, authentication, parsing...)."""


class DataNotSupportedError(DataProviderError):
    """The provider does not support the requested dataset."""


class MissingDataError(DataProviderError):
    """Some requested market data could not be obtained from any provider."""

    def __init__(self, missing: list[str], context: str = "") -> None:
        self.missing = list(missing)
        msg = f"Missing market data for {len(self.missing)} id(s): {', '.join(self.missing)}"
        if context:
            msg = f"{msg} ({context})"
        super().__init__(msg)
