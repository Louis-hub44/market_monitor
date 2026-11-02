"""Abstract market-data provider interface.

Every provider returns pandas objects in **engine units** (rates as decimals,
prices in % of par) with a common schema, so the curve / bond engines never
know where data came from:

* ``get_history(ids, start, end)`` -> ``DataFrame`` indexed by ``date`` (daily,
  ``DatetimeIndex``), one column per *available* logical id;
* ``get_quotes(ids, as_of)`` -> ``DataFrame`` indexed by id with columns
  ``value``, ``obs_date`` (last observation on or before ``as_of``), ``source``;
* ``get_bond_static(isins)`` -> ``DataFrame`` indexed by ISIN, columns
  :data:`BOND_STATIC_COLUMNS`;
* ``get_bond_prices(isins, as_of)`` -> ``DataFrame`` indexed by ISIN, columns
  :data:`BOND_PRICE_COLUMNS`.

Ids/ISINs a provider cannot serve are simply *absent* from the result; the
:class:`~rates_analytics.data.service.MarketDataService` handles fallbacks and
the missing-data policy.
"""

from __future__ import annotations

import logging
from abc import ABC
from datetime import date, timedelta
from enum import Enum
from typing import ClassVar, Sequence

import pandas as pd

from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.exceptions import DataNotSupportedError

logger = logging.getLogger(__name__)


class Capability(str, Enum):
    """Datasets a provider may serve."""

    HISTORY = "history"
    QUOTES = "quotes"
    BOND_STATIC = "bond_static"
    BOND_PRICES = "bond_prices"


QUOTE_COLUMNS = ["value", "obs_date", "source"]
BOND_STATIC_COLUMNS = [
    "name", "issuer", "currency", "coupon", "frequency", "day_count",
    "issue_date", "accrual_start", "first_coupon_date", "maturity", "source",
]
BOND_PRICE_COLUMNS = ["bid", "ask", "mid", "yield_mid", "obs_date", "source"]


def empty_history() -> pd.DataFrame:
    return pd.DataFrame(index=pd.DatetimeIndex([], name="date"))


def empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns, index=pd.Index([], name="id"))


class DataProvider(ABC):
    """Base class of all market-data providers."""

    name: ClassVar[str] = "abstract"
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init__(self, mapping: MarketDataMap | None = None, quote_lookback_days: int = 10) -> None:
        self.mapping = mapping or MarketDataMap()
        self.quote_lookback_days = quote_lookback_days

    # ----------------------------------------------------------------- helpers
    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def _require(self, capability: Capability) -> None:
        if not self.supports(capability):
            raise DataNotSupportedError(f"Provider {self.name!r} does not serve {capability.value}")

    def serves(self, market_id: str) -> bool:
        """True if the provider has a mapping for ``market_id``."""
        return self.mapping.spec(market_id, self.name) is not None

    # ------------------------------------------------------------ interface
    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        """Daily history of logical ids (decimal units)."""
        self._require(Capability.HISTORY)
        raise NotImplementedError

    def get_quotes(self, ids: Sequence[str], as_of: date) -> pd.DataFrame:
        """Latest value on or before ``as_of`` for each id.

        Default implementation: last valid point of :meth:`get_history` over a
        ``quote_lookback_days`` calendar-day window (handles holidays and
        publication lags).
        """
        self._require(Capability.QUOTES)
        start = as_of - timedelta(days=self.quote_lookback_days)
        hist = self.get_history(ids, start, as_of)
        return last_valid_quotes(hist, as_of, self.name)

    def get_bond_static(self, isins: Sequence[str]) -> pd.DataFrame:
        """Bond reference data."""
        self._require(Capability.BOND_STATIC)
        raise NotImplementedError

    def get_bond_prices(self, isins: Sequence[str], as_of: date) -> pd.DataFrame:
        """Clean prices (% of par) and yields (decimal) on ``as_of``."""
        self._require(Capability.BOND_PRICES)
        raise NotImplementedError

    def close(self) -> None:
        """Release connections (sessions, sockets)."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


def last_valid_quotes(history: pd.DataFrame, as_of: date, source: str) -> pd.DataFrame:
    """Turn a history frame into a quote frame (last valid value <= ``as_of``)."""
    rows = {}
    if not history.empty:
        h = history.loc[history.index <= pd.Timestamp(as_of)]
        for col in h.columns:
            s = h[col].dropna()
            if not s.empty:
                rows[col] = {"value": float(s.iloc[-1]), "obs_date": s.index[-1].date(), "source": source}
    if not rows:
        return empty_frame(QUOTE_COLUMNS)
    df = pd.DataFrame.from_dict(rows, orient="index")[QUOTE_COLUMNS]
    df.index.name = "id"
    return df
