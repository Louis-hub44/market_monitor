"""Normalised request / result objects shared by every DataProvider.

Data contract (every provider, every result):
    * index   : ``DatetimeIndex`` named ``date``, tz-naive, normalised to midnight,
                sorted, unique - the *local trading date* of the print;
    * columns : one per requested ticker, in request order;
    * values  : ``float64`` in the provider's native quotation unit (price, yield in %,
                spread in bp...). Unit harmonisation is done by the instrument
                referential, never by the providers;
    * missing : ``NaN`` - never 0, never forward-filled at this layer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime
from enum import StrEnum
from typing import TypeAlias

import pandas as pd

from market_monitor.exceptions import InvalidRequestError

DateLike: TypeAlias = str | date | datetime | pd.Timestamp
NO_DATA = "no data returned"
#: fallback chain separator in native tickers: ``"cnbc:DE10Y-DE|bbk:..."``
CHAIN_SEP = "|"
INDEX_NAME = "date"
SNAPSHOT_COLUMNS = ("value", "as_of")


class Field(StrEnum):
    """Daily bar fields supported by the providers."""

    LAST = "last"
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    VOLUME = "volume"


def to_date(value: DateLike) -> date:
    """Coerce an ISO string / date / datetime / Timestamp into a ``date``."""
    if isinstance(value, datetime):  # pd.Timestamp is a datetime subclass
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise InvalidRequestError(f"invalid date {value!r} (expected YYYY-MM-DD)") from exc
    raise InvalidRequestError(f"unsupported date type: {type(value).__name__}")


def to_field(value: Field | str) -> Field:
    """Coerce a string into a :class:`Field`."""
    try:
        return Field(value)
    except ValueError as exc:
        allowed = ", ".join(f.value for f in Field)
        raise InvalidRequestError(f"unknown field {value!r} (allowed: {allowed})") from exc


def normalize_tickers(tickers: str | Iterable[str]) -> tuple[str, ...]:
    """Validate tickers, strip them and de-duplicate while keeping order.

    A bare string is treated as a single ticker (avoids iterating over characters).
    """
    items = [tickers] if isinstance(tickers, str) else list(tickers)
    cleaned: list[str] = []
    for ticker in items:
        if not isinstance(ticker, str) or not ticker.strip():
            raise InvalidRequestError(f"invalid ticker {ticker!r}")
        cleaned.append(ticker.strip())
    if not cleaned:
        raise InvalidRequestError("at least one ticker is required")
    return tuple(dict.fromkeys(cleaned))


@dataclass(frozen=True)
class HistoryRequest:
    """Validated daily-history request (dates inclusive)."""

    tickers: tuple[str, ...]
    start: date
    end: date
    field: Field = Field.LAST

    def __post_init__(self) -> None:
        if not self.tickers:
            raise InvalidRequestError("at least one ticker is required")
        if self.start > self.end:
            raise InvalidRequestError(f"start {self.start} is after end {self.end}")

    @classmethod
    def create(
        cls,
        tickers: str | Iterable[str],
        start: DateLike,
        end: DateLike,
        field: Field | str = Field.LAST,
    ) -> HistoryRequest:
        """Build a request from loosely-typed inputs."""
        return cls(normalize_tickers(tickers), to_date(start), to_date(end), to_field(field))


def chain_alternatives(ticker: str) -> list[str]:
    """Alternatives of a fallback chain ``a|b|c`` (a plain ticker is a one-element chain)."""
    return [a.strip() for a in ticker.split(CHAIN_SEP) if a.strip()] or [ticker]


def is_chain(ticker: str) -> bool:
    return len(chain_alternatives(ticker)) > 1


def resolve_origin(ticker: str, origin: str | None) -> str:
    """The alternative of ``ticker`` that served; the preferred one when unknown.

    A provider that does not resolve chains reports the chain itself (or nothing): the
    first alternative is then assumed.
    """
    alternatives = chain_alternatives(ticker)
    return origin if origin in alternatives else alternatives[0]


def fallback_origin(ticker: str, origin: str | None) -> str | None:
    """``origin`` when it is a fallback (not the preferred, first) alternative of ``ticker``."""
    if origin is None or not is_chain(ticker):
        return None
    resolved = resolve_origin(ticker, origin)
    return None if resolved == chain_alternatives(ticker)[0] else resolved


def empty_frame(columns: Iterable[str] = ()) -> pd.DataFrame:
    """Empty history frame respecting the data contract."""
    return pd.DataFrame(
        index=pd.DatetimeIndex([], name=INDEX_NAME), columns=list(columns), dtype="float64"
    )


@dataclass
class HistoryResult:
    """Daily history plus per-ticker diagnostics.

    Attributes:
        data: frame following the module-level data contract.
        errors: ticker -> reason, for tickers with **no** data at all.
        warnings: ticker -> message, for tickers whose data is degraded
            (stale cache, partial refresh...).
        sources: ticker -> name of the provider that actually served it.
        origins: ticker -> native ticker that actually served it: the alternative of a
            fallback chain ``a|b`` (defaults to the ticker itself). A series is never
            made of two origins - see :mod:`market_monitor.data.cached_provider`.
    """

    data: pd.DataFrame
    errors: dict[str, str] = dc_field(default_factory=dict)
    warnings: dict[str, str] = dc_field(default_factory=dict)
    sources: dict[str, str] = dc_field(default_factory=dict)
    origins: dict[str, str] = dc_field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        """Tickers without a single valid observation."""
        return [c for c in self.data.columns if self.data[c].isna().all()]


@dataclass
class SnapshotResult:
    """Latest value per ticker: index = tickers, columns = ``value`` / ``as_of``.

    ``as_of`` is a tz-naive Timestamp: the trading date for EOD fallbacks, the UTC
    time of the last update for real-time quotes.
    """

    data: pd.DataFrame
    errors: dict[str, str] = dc_field(default_factory=dict)
    warnings: dict[str, str] = dc_field(default_factory=dict)
    sources: dict[str, str] = dc_field(default_factory=dict)
