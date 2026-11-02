"""Abstract DataProvider interface (template method).

Concrete providers only implement ``_fetch_history`` (and optionally
``_fetch_snapshot``) in their **native ticker namespace**. The public methods validate
inputs and enforce the data contract described in :mod:`market_monitor.data.models`,
so every provider - Bloomberg, FMP, free - is interchangeable for the layers above.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd

from market_monitor.data.models import (
    BAR_FIELDS,
    NO_DATA,
    SNAPSHOT_COLUMNS,
    BarsResult,
    DateLike,
    Field,
    HistoryRequest,
    HistoryResult,
    SnapshotResult,
    empty_bars,
    normalize_tickers,
)
from market_monitor.data.quality import assemble_frame, clean_bars, last_valid, slice_bars, slice_dates
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

SNAPSHOT_LOOKBACK_DAYS = 14


class DataProvider(ABC):
    """Market data source working on provider-native tickers."""

    name: str = "abstract"
    #: bar fields the provider can serve (the default ``_fetch_bars`` asks each of them)
    bar_fields: tuple[Field, ...] = BAR_FIELDS

    def is_available(self) -> bool:
        """Cheap check (no network) that the provider can be used at all."""
        return True

    def get_history(
        self,
        tickers: str | Iterable[str],
        start: DateLike,
        end: DateLike,
        field: Field | str = Field.LAST,
    ) -> HistoryResult:
        """Daily history between ``start`` and ``end`` (inclusive)."""
        request = HistoryRequest.create(tickers, start, end, field)
        return finalize_history(self._fetch_history(request), request, self.name)

    def get_bars(self, tickers: str | Iterable[str], start: DateLike, end: DateLike) -> BarsResult:
        """Daily OHLCV bars between ``start`` and ``end`` (inclusive); close-only sources
        leave open / high / low / volume blank."""
        request = HistoryRequest.create(tickers, start, end)
        return finalize_bars(self._fetch_bars(request), request, self.name)

    def get_snapshot(self, tickers: str | Iterable[str]) -> SnapshotResult:
        """Latest available value per ticker."""
        names = normalize_tickers(tickers)
        return finalize_snapshot(self._fetch_snapshot(names), names, self.name)

    @abstractmethod
    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        """Provider-specific retrieval. May return raw (uncleaned) columns."""

    def _fetch_bars(self, request: HistoryRequest) -> BarsResult:
        """Default bars: one history call per field (close first).

        Providers whose source returns the whole bar at once (Yahoo, FMP, Stooq, CNBC)
        override it with a single call. A field the source refuses is left blank.
        """
        last = self._fetch_history(replace(request, field=Field.LAST))
        columns: dict[str, dict[str, pd.Series]] = {
            t: {"last": last.data[t]} for t in request.tickers if t in last.data.columns}
        for field in self.bar_fields:
            if field is Field.LAST or not columns:
                continue
            try:
                other = self._fetch_history(replace(request, field=field))
            except ProviderUnavailableError:
                break
            except DataProviderError:
                continue
            for ticker, parts in columns.items():
                if ticker in other.data.columns:
                    parts[field.value] = other.data[ticker]
        bars = {t: pd.DataFrame(parts) for t, parts in columns.items()}
        return BarsResult(bars=bars, errors=dict(last.errors), warnings=dict(last.warnings),
                          sources=dict(last.sources), origins=dict(last.origins))

    def _fetch_snapshot(self, tickers: tuple[str, ...]) -> SnapshotResult:
        """Default snapshot: last valid close over the last two weeks."""
        end = date.today()
        hist = self.get_history(tickers, end - timedelta(days=SNAPSHOT_LOOKBACK_DAYS), end)
        return SnapshotResult(
            data=last_valid(hist.data), errors=dict(hist.errors), warnings=dict(hist.warnings)
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


def finalize_history(raw: HistoryResult, request: HistoryRequest, provider: str) -> HistoryResult:
    """Clean, clip to the requested window and reconcile diagnostics."""
    frame = raw.data
    series = {
        t: slice_dates(assemble_frame({t: frame[t]}, [t])[t], request.start, request.end)
        for t in request.tickers
        if t in frame.columns
    }
    data = assemble_frame(series, request.tickers)
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    sources: dict[str, str] = {}
    origins: dict[str, str] = {}
    for ticker in request.tickers:
        if data[ticker].isna().all():
            errors[ticker] = raw.errors.get(ticker, NO_DATA)
            continue
        sources[ticker] = raw.sources.get(ticker, provider)
        origins[ticker] = raw.origins.get(ticker, ticker)
        message = raw.warnings.get(ticker) or raw.errors.get(ticker)
        if message:
            warnings[ticker] = message
    return HistoryResult(data=data, errors=errors, warnings=warnings, sources=sources,
                         origins=origins)


def finalize_bars(raw: BarsResult, request: HistoryRequest, provider: str) -> BarsResult:
    """Clean every ticker's bars, clip to the window and reconcile diagnostics."""
    bars: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    sources: dict[str, str] = {}
    origins: dict[str, str] = {}
    for ticker in request.tickers:
        frame = slice_bars(clean_bars(raw.bars.get(ticker)), request.start, request.end)
        if frame.empty:
            bars[ticker] = empty_bars()
            errors[ticker] = raw.errors.get(ticker, NO_DATA)
            continue
        bars[ticker] = frame
        sources[ticker] = raw.sources.get(ticker, provider)
        origins[ticker] = raw.origins.get(ticker, ticker)
        message = raw.warnings.get(ticker) or raw.errors.get(ticker)
        if message:
            warnings[ticker] = message
    return BarsResult(bars=bars, errors=errors, warnings=warnings, sources=sources, origins=origins)


def finalize_snapshot(raw: SnapshotResult, tickers: tuple[str, ...], provider: str) -> SnapshotResult:
    """Reindex the snapshot on the requested tickers and reconcile diagnostics."""
    data = raw.data.reindex(index=list(tickers), columns=list(SNAPSHOT_COLUMNS))
    data["value"] = pd.to_numeric(data["value"], errors="coerce").astype("float64")
    data["as_of"] = pd.to_datetime(data["as_of"], errors="coerce")
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    sources: dict[str, str] = {}
    for ticker in tickers:
        if pd.isna(data.at[ticker, "value"]):
            errors[ticker] = raw.errors.get(ticker, NO_DATA)
            continue
        sources[ticker] = raw.sources.get(ticker, provider)
        if ticker in raw.warnings:
            warnings[ticker] = raw.warnings[ticker]
    return SnapshotResult(data=data, errors=errors, warnings=warnings, sources=sources)
