r"""Read-through cache decorator for any :class:`DataProvider`.

For a cached series with coverage :math:`[c_s, c_e]` fetched at :math:`t_f` (local
date :math:`d_f`), a request :math:`[s, e]` (with :math:`e` clipped to today) triggers:

* a **left** segment :math:`[s, c_s - 1]` if :math:`s < c_s`;
* a **right** segment :math:`[\max(c_s, \min(c_e + 1, d_f) - L), e]` if
  :math:`e > c_e`, or if :math:`e \ge d_f` and :math:`t - t_f > \tau`
  (provisional "today" prints older than the TTL :math:`\tau`). The look-back
  :math:`L` re-pulls the last days to catch revisions and late prints.

Segments are always contiguous with the coverage, so coverage stays one interval.

A series is never made of two sources. Each entry records its ``origin`` (the
alternative of a ``a|b`` fallback chain that served it); when a refresh is answered by
another alternative, the whole window is reloaded from it instead of being spliced, and
history extended to the left is asked from the cached origin itself.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from market_monitor.data.base import DataProvider
from market_monitor.data.cache import CacheEntry, ParquetCache
from market_monitor.data.models import (
    NO_DATA,
    Field,
    HistoryRequest,
    HistoryResult,
    SnapshotResult,
    empty_frame,
    fallback_origin,
    is_chain,
)
from market_monitor.data.quality import assemble_frame, empty_series, slice_dates
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)

Segment = tuple[date, date]
ONE_DAY = timedelta(days=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CachedProvider(DataProvider):
    """Wraps a provider with the parquet cache (history only; snapshots are live)."""

    def __init__(
        self,
        inner: DataProvider,
        cache: ParquetCache,
        *,
        intraday_ttl: timedelta = timedelta(minutes=15),
        refresh_lookback_days: int = 5,
        timezone: str = "Europe/Paris",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if intraday_ttl < timedelta(0):
            raise ValueError("intraday_ttl must be >= 0")
        if refresh_lookback_days < 0:
            raise ValueError("refresh_lookback_days must be >= 0")
        self.name = inner.name
        self._inner = inner
        self._cache = cache
        self._ttl = intraday_ttl
        self._lookback = timedelta(days=refresh_lookback_days)
        self._tz = ZoneInfo(timezone)
        self._clock = clock

    @property
    def inner(self) -> DataProvider:
        return self._inner

    def is_available(self) -> bool:
        return self._inner.is_available()

    def _fetch_snapshot(self, tickers: tuple[str, ...]) -> SnapshotResult:
        return self._inner.get_snapshot(tickers)

    # ------------------------------------------------------------------ history
    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        now = self._clock()
        end = min(request.end, now.astimezone(self._tz).date())
        if request.start > end:  # purely future window
            return HistoryResult(data=empty_frame(request.tickers))

        entries = {t: self._cache.load(self.name, request.field, t) for t in request.tickers}
        plan: dict[tuple[Segment, ...], list[str]] = defaultdict(list)
        for ticker, entry in entries.items():
            segments = self._missing_segments(entry, request.start, end, now)
            if segments:
                plan[tuple(segments)].append(ticker)

        failures = self._execute_plan(plan, entries, request.field, now)
        return self._build_result(entries, failures, request, end)

    def _missing_segments(
        self, entry: CacheEntry | None, start: date, end: date, now: datetime
    ) -> list[Segment]:
        """Segments to download so that ``[start, end]`` is fully and freshly covered."""
        if entry is None:
            return [(start, end)]
        segments: list[Segment] = []
        if start < entry.covered_start:
            segments.append((start, entry.covered_start - ONE_DAY))
        fetched_day = entry.fetched_at.astimezone(self._tz).date()
        provisional_expired = end >= fetched_day and now - entry.fetched_at > self._ttl
        if end > entry.covered_end or provisional_expired:
            anchor = min(entry.covered_end + ONE_DAY, fetched_day)
            right_start = max(entry.covered_start, anchor - self._lookback)
            segments.append((right_start, max(end, right_start)))
        return _merge_segments(segments)

    def _execute_plan(
        self,
        plan: dict[tuple[Segment, ...], list[str]],
        entries: dict[str, CacheEntry | None],
        field: Field,
        now: datetime,
    ) -> dict[str, str]:
        """Download every planned segment, update entries/cache, return failures."""
        failures: dict[str, str] = {}
        for segments, tickers in plan.items():
            for segment in segments:
                # history extended to the left is asked from the cached origin itself, so an
                # alternative with a shorter history cannot take over the whole series
                fetch_as = {t: self._fetch_ticker(t, entries[t], segment) for t in tickers}
                try:
                    fetched = self._inner.get_history(
                        list(dict.fromkeys(fetch_as.values())), segment[0], segment[1], field)
                except DataProviderError as exc:
                    for ticker in tickers:
                        failures[ticker] = f"fetch failed: {exc}"
                    if isinstance(exc, ProviderUnavailableError):
                        logger.warning("%s unavailable, serving cache only: %s", self.name, exc)
                        for other in (t for group in plan.values() for t in group):
                            failures.setdefault(other, f"fetch failed: {exc}")
                        return failures
                    continue
                for ticker in tickers:
                    piece = _Piece.of(fetched, fetch_as[ticker])
                    self._absorb(ticker, piece, segment, entries, field, now, failures)
        return failures

    @staticmethod
    def _fetch_ticker(ticker: str, entry: CacheEntry | None, segment: Segment) -> str:
        origin = _origin(entry, ticker)
        left_only = entry is not None and segment[1] < entry.covered_start
        return origin if left_only and origin is not None else ticker

    def _absorb(
        self,
        ticker: str,
        piece: _Piece,
        segment: Segment,
        entries: dict[str, CacheEntry | None],
        field: Field,
        now: datetime,
        failures: dict[str, str],
    ) -> None:
        """Merge one ticker's fetched segment into its cache entry (never across origins)."""
        entry = entries[ticker]
        new, error, origin = piece.series, piece.error, piece.origin
        # An empty answer is trusted (holidays, pre-inception) only for a ticker the
        # provider already knows; otherwise it may be a typo or a transient failure.
        if new.empty and (entry is None or error not in (None, NO_DATA)):
            failures[ticker] = error or NO_DATA
            return
        if error and error != NO_DATA:
            failures[ticker] = error
        seg_start, seg_end = segment
        old = entry.series if entry is not None else empty_series()
        kept = old[(old.index < pd.Timestamp(seg_start)) | (old.index > pd.Timestamp(seg_end))]
        if entry is not None:
            origin = origin if not new.empty else _origin(entry, ticker)
            if not kept.empty and origin != _origin(entry, ticker):
                self._switch_origin(ticker, origin, segment, entry, entries, field, now, failures)
                return
        merged = pd.concat([kept, new]).sort_index()
        if entry is None:
            updated = CacheEntry(merged, seg_start, seg_end, now, origin)
        else:
            updated = CacheEntry(
                series=merged,
                covered_start=min(seg_start, entry.covered_start),
                covered_end=max(seg_end, entry.covered_end),
                # only a right-edge refresh makes the provisional tail fresh again
                fetched_at=now if seg_end >= entry.covered_end else entry.fetched_at,
                origin=origin,
            )
        entries[ticker] = updated
        self._cache.save(self.name, field, ticker, updated)

    def _switch_origin(
        self,
        ticker: str,
        origin: str | None,
        segment: Segment,
        entry: CacheEntry,
        entries: dict[str, CacheEntry | None],
        field: Field,
        now: datetime,
        failures: dict[str, str],
    ) -> None:
        """Another alternative answered: reload the whole window from it instead of splicing.

        Two sources of the "same" instrument differ in level (e.g. CNBC generic vs
        Bundesbank zero-coupon Bund): a spliced series would show a fake move at the seam.
        """
        start, end = min(segment[0], entry.covered_start), max(segment[1], entry.covered_end)
        source = origin or ticker
        try:
            full = _Piece.of(self._inner.get_history([source], start, end, field), source)
        except DataProviderError as exc:
            full = _Piece(empty_series(), str(exc), None)
        if full.series.empty:
            failures[ticker] = (f"{source} answered but its full history is unavailable "
                                f"({full.error or NO_DATA}); cached series kept")
            return
        logger.info("%s: source switched to %s, history reloaded %s -> %s", ticker, source, start, end)
        updated = CacheEntry(full.series, start, end, now, source)
        entries[ticker] = updated
        self._cache.save(self.name, field, ticker, updated)

    def _build_result(
        self,
        entries: dict[str, CacheEntry | None],
        failures: dict[str, str],
        request: HistoryRequest,
        end: date,
    ) -> HistoryResult:
        series = {
            t: slice_dates(e.series, request.start, end) if e else empty_series()
            for t, e in entries.items()
        }
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        origins: dict[str, str] = {}
        for ticker, entry in entries.items():
            origin = _origin(entry, ticker)
            if entry is None or origin is None:
                continue
            origins[ticker] = origin
            if fallback_origin(ticker, origin):
                warnings[ticker] = f"served by fallback {origin}"
        for ticker, message in failures.items():
            if series[ticker].empty:
                errors[ticker] = message
            else:
                previous = warnings.get(ticker)
                current = f"{message} - serving cached data"
                warnings[ticker] = f"{previous}; {current}" if previous else current
        return HistoryResult(
            data=assemble_frame(series, request.tickers), errors=errors, warnings=warnings,
            origins=origins,
        )


@dataclass(frozen=True)
class _Piece:
    """One ticker's slice of a fetched result."""

    series: pd.Series
    error: str | None
    origin: str | None

    @classmethod
    def of(cls, result: HistoryResult, ticker: str) -> _Piece:
        series = result.data[ticker].dropna() if ticker in result.data else empty_series()
        origin = result.origins.get(ticker) if not series.empty else None
        return cls(series, result.errors.get(ticker), origin or (ticker if not series.empty else None))


def _origin(entry: CacheEntry | None, ticker: str) -> str | None:
    """Origin of a cached series; unknown (``None``) for a chain cached before v1.3."""
    if entry is None:
        return None
    if entry.origin:
        return entry.origin
    return None if is_chain(ticker) else ticker


def _merge_segments(segments: list[Segment]) -> list[Segment]:
    """Merge overlapping / adjacent segments (input is at most two, ordered)."""
    merged: list[Segment] = []
    for seg in sorted(segments):
        if merged and seg[0] <= merged[-1][1] + ONE_DAY:
            merged[-1] = (merged[-1][0], max(merged[-1][1], seg[1]))
        else:
            merged.append(seg)
    return merged

