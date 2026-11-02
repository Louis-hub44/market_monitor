"""Historical index fixings (€STR, EURIBOR) for seasoned instruments.

Fixings are keyed by **rate date** (the fixing date of the period for EURIBOR,
the business day the overnight rate applies to for €STR), in decimal units.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date

import numpy as np
import pandas as pd

from rates_analytics.conventions.dateutils import to_date
from rates_analytics.instruments.common import InstrumentError


class MissingFixingError(InstrumentError):
    """A past fixing needed to value a seasoned instrument is not available."""


class Fixings(Mapping[date, float]):
    """Read-only mapping ``date -> fixing`` built from a dict or a ``pandas.Series``."""

    def __init__(self, data: Mapping | pd.Series | None = None, name: str = "") -> None:
        self.name = name
        items: dict[date, float] = {}
        if data is not None:
            pairs = data.items() if not isinstance(data, pd.Series) else data.dropna().items()
            for k, v in pairs:
                if v is None or not np.isfinite(float(v)):
                    continue
                items[to_date(k)] = float(v)
        self._data = items

    @classmethod
    def coerce(cls, data: Fixings | Mapping | pd.Series | None, name: str = "") -> Fixings:
        return data if isinstance(data, Fixings) else cls(data, name)

    def __getitem__(self, d: date) -> float:
        return self._data[d]

    def __iter__(self) -> Iterator[date]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def require(self, d: date) -> float:
        """Fixing for ``d`` or :class:`MissingFixingError`."""
        try:
            return self._data[d]
        except KeyError as exc:
            raise MissingFixingError(f"Missing {self.name or 'index'} fixing for {d}") from exc


def fixings_from_market(service, market_id: str, start: date, end: date, name: str = "") -> Fixings:
    """Fixings history from a :class:`~rates_analytics.data.MarketDataService` (e.g. ``EUR.ESTR.FIX``)."""
    hist = service.history([market_id], start, end)
    series = hist[market_id] if market_id in hist else pd.Series(dtype=float)
    return Fixings(series, name or market_id)
