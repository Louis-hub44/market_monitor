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
history extended to the left is asked from the cached origin itself. A series served by a
fallback is re-asked in full from the chain at each refresh, so the preferred source takes
it back as soon as it answers.

Bars (:meth:`get_bars`) use the same machinery over several fields: the close is the
primary field (segments, origin), every field is filled by the same call, and a field the
source does not publish is cached empty over the same coverage. A segment longer than
``tranche_days`` is downloaded in tranches - backwards from the most recent one for a first
load or a left extension, stopping where the source's history ends - so a "Max" window is
asked only once and coverage stays one interval.
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
    BAR_COLUMNS,
    NO_DATA,
    BarsResult,
    Field,
    HistoryRequest,
    HistoryResult,
    SnapshotResult,
    empty_bars,
    empty_frame,
    fallback_origin,
    is_chain,
)
from market_monitor.data.quality import assemble_frame, empty_series, slice_dates
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)

Segment = tuple[date, date]
ONE_DAY = timedelta(days=1)
#: a longer window is downloaded in tranches of this size (most recent first)
DEFAULT_TRANCHE_DAYS = 1830


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
        tranche_days: int = DEFAULT_TRANCHE_DAYS,
    ) -> None:
        if intraday_ttl < timedelta(0):
            raise ValueError("intraday_ttl must be >= 0")
        if refresh_lookback_days < 0:
            raise ValueError("refresh_lookback_days must be >= 0")
        if tranche_days < 0:
            raise ValueError("tranche_days must be >= 0 (0 = no tranches)")
        self.name = inner.name
        self._inner = inner
        self._cache = cache
        self._ttl = intraday_ttl
        self._lookback = timedelta(days=refresh_lookback_days)
        self._tz = ZoneInfo(timezone)
        self._clock = clock
        self._tranche = timedelta(days=tranche_days)
        self._floor: datetime | None = None

    @property
    def refresh_floor(self) -> datetime | None:
        """Manual-refresh mode: entries downloaded since this moment never expire (see below)."""
        return self._floor

    def hold_until(self, floor: datetime | None) -> None:
        """Manual refresh: today's data downloaded after ``floor`` is reused whatever its age,
        today's data downloaded before it is downloaded again when next needed.

        The dashboard sets it at start-up and on "Actualiser les données": moving between
        views then never asks the sources again (FMP quota). From the next day on, and with
        ``None``, the intraday TTL applies.
        """
        self._floor = floor

    @property
    def inner(self) -> DataProvider:
        return self._inner

    def is_available(self) -> bool:
        return self._inner.is_available()

    def _fetch_snapshot(self, tickers: tuple[str, ...]) -> SnapshotResult:
        return self._inner.get_snapshot(tickers)

    # ------------------------------------------------------------------ history
    def coverage(self, ticker: str, field: Field = Field.LAST) -> CacheEntry | None:
        """Cached entry of ``ticker`` (coverage window, origin, observations), if any."""
        return self._cache.load(self.name, field, ticker)

    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        field = request.field

        def fetch(tickers: list[str], start: date, end: date) -> dict[Field, HistoryResult]:
            return {field: self._inner.get_history(tickers, start, end, field)}

        run = self._run(request, (field,), fetch)
        if run is None:  # purely future window
            return HistoryResult(data=empty_frame(request.tickers))
        entries, failures, end = run
        return self._build_result(entries[field], failures[field], request, end)

    def _fetch_bars(self, request: HistoryRequest) -> BarsResult:
        """Bars through the cache: one file per field, every field filled by one call.

        The close is the primary field (segments, origin); a field the source does not
        publish is cached as empty over the same coverage, so it is not asked again.
        """
        fields = (Field.LAST, *[f for f in self._inner.bar_fields if f is not Field.LAST])

        def fetch(tickers: list[str], start: date, end: date) -> dict[Field, HistoryResult]:
            bars = self._inner.get_bars(tickers, start, end)
            return {f: HistoryResult(data=bars.field(f), errors=dict(bars.errors),
                                     warnings=dict(bars.warnings), sources=dict(bars.sources),
                                     origins=dict(bars.origins)) for f in fields}

        run = self._run(request, fields, fetch)
        if run is None:
            return BarsResult(bars={t: empty_bars() for t in request.tickers})
        entries, failures, end = run
        last = self._build_result(entries[Field.LAST], failures[Field.LAST], request, end)
        bars: dict[str, pd.DataFrame] = {}
        for ticker in request.tickers:
            columns = {}
            for f in fields:
                entry = entries[f][ticker]
                columns[f.value] = (slice_dates(entry.series, request.start, end) if entry is not None
                                    else empty_series())
            frame = pd.DataFrame(columns).reindex(columns=list(BAR_COLUMNS))
            bars[ticker] = frame[frame["last"].notna()] if not frame.empty else empty_bars()
        return BarsResult(bars=bars, errors=last.errors, warnings=last.warnings, origins=last.origins)

    def _run(
        self, request: HistoryRequest, fields: tuple[Field, ...], fetch: _Fetch
    ) -> tuple[_Entries, dict[Field, dict[str, str]], date] | None:
        """Plan and download what the cache lacks for ``fields``; ``None`` for a future window."""
        now = self._clock()
        end = min(request.end, now.astimezone(self._tz).date())
        if request.start > end:
            return None
        primary = fields[0]
        entries: _Entries = {
            f: {t: self._cache.load(self.name, f, t) for t in request.tickers} for f in fields}
        plan: dict[tuple[Segment, ...], list[str]] = defaultdict(list)
        for ticker in request.tickers:
            entry = entries[primary][ticker]
            segments = _merge_segments([seg for f in fields for seg in self._missing_segments(
                entries[f][ticker], request.start, end, now)])
            if segments and entry is not None and fallback_origin(ticker, _origin(entry, ticker)):
                # served by a fallback: each refresh gives the preferred source the whole
                # window again, so a transient failure does not pin the series on the fallback
                segments = [(min(request.start, entry.covered_start), max(end, entry.covered_end))]
            if segments:
                plan[tuple(segments)].append(ticker)
        failures = self._execute_plan(plan, entries, fields, _memoised(fetch), now)
        return entries, failures, end

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
        if self._floor is not None and fetched_day == now.astimezone(self._tz).date():
            # manual refresh, same day: stale only if fetched before the last refresh
            stale = entry.fetched_at < self._floor
        else:  # intraday TTL (also from the next day on: yesterday's print was not its close)
            stale = now - entry.fetched_at > self._ttl
        provisional_expired = end >= fetched_day and stale
        if end > entry.covered_end or provisional_expired:
            anchor = min(entry.covered_end + ONE_DAY, fetched_day)
            right_start = max(entry.covered_start, anchor - self._lookback)
            segments.append((right_start, max(end, right_start)))
        return _merge_segments(segments)

    def _tranches(self, segment: Segment, entry: CacheEntry | None) -> tuple[list[Segment], bool]:
        """A long segment cut into tranches, ordered so that coverage stays one interval.

        History to the left of the cache (or a first load) is walked back from the most
        recent tranche, so the search stops where the source's history ends; a segment
        extending the cache to the right is walked forward.
        """
        start, end = segment
        backwards = entry is None or end < entry.covered_start
        if not self._tranche or (end - start).days < self._tranche.days:
            return [segment], backwards
        pieces: list[Segment] = []
        cursor = end
        while cursor >= start:
            first = max(start, cursor - self._tranche + ONE_DAY)
            pieces.append((first, cursor))
            cursor = first - ONE_DAY
        return (pieces if backwards else pieces[::-1]), backwards

    def _execute_plan(
        self,
        plan: dict[tuple[Segment, ...], list[str]],
        entries: _Entries,
        fields: tuple[Field, ...],
        fetch: _Fetch,
        now: datetime,
    ) -> dict[Field, dict[str, str]]:
        """Download every planned segment, update entries/cache, return failures per field."""
        primary = fields[0]
        failures: dict[Field, dict[str, str]] = {f: {} for f in fields}
        for segments, tickers in plan.items():
            for segment in segments:
                stopped: set[str] = set()  # failed or past the source's history: no older tranche
                tranches, backwards = self._tranches(segment, entries[primary][tickers[0]])
                for tranche in tranches:
                    active = [t for t in tickers if t not in stopped]
                    if not active:
                        break
                    # history extended to the left is asked from the cached origin itself, so an
                    # alternative with a shorter history cannot take over the whole series
                    fetch_as = {t: self._fetch_ticker(t, entries[primary][t], tranche) for t in active}
                    try:
                        fetched = fetch(list(dict.fromkeys(fetch_as.values())), tranche[0], tranche[1])
                    except DataProviderError as exc:
                        for ticker in active:
                            failures[primary][ticker] = f"fetch failed: {exc}"
                        stopped.update(active)
                        if isinstance(exc, ProviderUnavailableError):
                            logger.warning("%s unavailable, serving cache only: %s", self.name, exc)
                            for other in (t for group in plan.values() for t in group):
                                failures[primary].setdefault(other, f"fetch failed: {exc}")
                            return failures
                        continue
                    for ticker in active:
                        status = self._absorb_fields(ticker, fetched, fetch_as[ticker], tranche,
                                                     entries, fields, fetch, now, failures)
                        if status == _FAILED:
                            stopped.add(ticker)
                        elif status == _EXHAUSTED and backwards and len(tranches) > 1:
                            # nothing in this tranche: the source's history ends here; the
                            # older ones are recorded as covered without being asked for
                            if segment[0] < tranche[0]:
                                rest = (segment[0], tranche[0] - ONE_DAY)
                                empty = {f: HistoryResult(data=empty_frame([ticker])) for f in fields}
                                self._absorb_fields(ticker, empty, ticker, rest, entries, fields,
                                                    fetch, now, failures)
                            stopped.add(ticker)
        return failures

    def _absorb_fields(
        self,
        ticker: str,
        fetched: dict[Field, HistoryResult],
        fetched_as: str,
        segment: Segment,
        entries: _Entries,
        fields: tuple[Field, ...],
        fetch: _Fetch,
        now: datetime,
        failures: dict[Field, dict[str, str]],
    ) -> str:
        """Absorb every field of one ticker: :data:`_OK`, :data:`_EXHAUSTED` (a known series
        without data in the segment) or :data:`_FAILED`."""
        primary = fields[0]
        head = _Piece.of(fetched[primary], fetched_as)
        known = entries[primary][ticker] is not None
        self._absorb(ticker, head, segment, entries[primary], primary, fetch, now, failures[primary])
        for field in fields[1:]:
            piece = _Piece.of(fetched[field], fetched_as)
            if piece.series.empty and not head.series.empty:  # field not published by the source
                piece = _Piece(piece.series, None, head.origin)
            self._absorb(ticker, piece, segment, entries[field], field, fetch, now, failures[field],
                         trust_empty=not head.series.empty)
            self._align(ticker, entries, primary, field, now)
        if head.error not in (None, NO_DATA):
            return _FAILED
        if head.series.empty:
            return _EXHAUSTED if known else _FAILED
        return _OK

    def _align(self, ticker: str, entries: _Entries, primary: Field, field: Field, now: datetime) -> None:
        """Open / high / low never come from another source than the close they go with."""
        main, other = entries[primary][ticker], entries[field][ticker]
        if main is None or other is None or other.origin == main.origin:
            return
        blank = CacheEntry(empty_series(), main.covered_start, main.covered_end, now, main.origin)
        entries[field][ticker] = blank
        self._cache.save(self.name, field, ticker, blank)

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
        fetch: _Fetch,
        now: datetime,
        failures: dict[str, str],
        *,
        trust_empty: bool = False,
    ) -> None:
        """Merge one ticker's fetched segment into its cache entry (never across origins)."""
        entry = entries[ticker]
        new, error, origin = piece.series, piece.error, piece.origin
        # An empty answer is trusted (holidays, pre-inception) only for a ticker the
        # provider already knows; otherwise it may be a typo or a transient failure.
        if new.empty and not trust_empty and (entry is None or error not in (None, NO_DATA)):
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
                self._switch_origin(ticker, origin, segment, entry, entries, field, fetch, now, failures)
                return
        merged = pd.concat([kept, new]).sort_index() if not kept.empty else new.sort_index()
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
        fetch: _Fetch,
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
            full = _Piece.of(fetch([source], start, end)[field], source)
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


_OK, _EXHAUSTED, _FAILED = "ok", "exhausted", "failed"
_Fetch = Callable[[list[str], date, date], dict[Field, HistoryResult]]
_Entries = dict[Field, dict[str, CacheEntry | None]]


def _memoised(fetch: _Fetch) -> _Fetch:
    """Same call within one run answered once (an origin switch reloads every field)."""
    seen: dict[tuple[tuple[str, ...], date, date], dict[Field, HistoryResult]] = {}

    def call(tickers: list[str], start: date, end: date) -> dict[Field, HistoryResult]:
        key = (tuple(tickers), start, end)
        if key not in seen:
            seen[key] = fetch(tickers, start, end)
        return seen[key]

    return call


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

