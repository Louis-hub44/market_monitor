"""Market Monitor facade - the only entry point the UI / terminal needs.

    monitor = MarketMonitor.from_settings(load_settings())
    report = monitor.performance("daily_macro")
    report.table            # one row per instrument
    report.by_asset_class() # ordered dict of sub-tables
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from market_monitor.alerts import AlertReport, AlertRule, evaluate, rule_universe
from market_monitor.analytics import correlation as corr
from market_monitor.analytics.comparison import ComparisonResult, compare, period_start
from market_monitor.analytics.correlation import (
    CorrelationResult,
    Frequency,
    MatrixOrder,
    analyse_correlations,
)
from market_monitor.analytics.curves import CurveSet, build_curves, curve_ids
from market_monitor.analytics.history import (
    UniverseBars,
    UniverseHistory,
    load_universe_bars,
    load_universe_history,
    source_label,
)
from market_monitor.analytics.instrument import (
    InstrumentProfile,
    instrument_stats,
    load_start,
    pair_spread,
    spread_vs,
)
from market_monitor.analytics.instrument import period_start as profile_start
from market_monitor.analytics.performance import (
    CHECK_GAP,
    CHECK_OK,
    CHECK_UNAVAILABLE,
    PerformanceEngine,
    required_start,
)
from market_monitor.analytics.quality import CheckResult, cross_check, publication_checks
from market_monitor.config import MANUAL_PROVIDER, AnalyticsSettings, QualitySettings, Settings
from market_monitor.data.base import DataProvider
from market_monitor.data.cached_provider import CachedProvider
from market_monitor.data.factory import build_service
from market_monitor.data.models import DateLike, Field, chain_alternatives, resolve_origin, to_date
from market_monitor.data.providers.manual import ManualProvider, ManualQuotes
from market_monitor.data.service import MarketDataService
from market_monitor.exceptions import MarketMonitorError
from market_monitor.referential import AssetClass, Instrument, Referential, TickerSpec, load_referential

REPORT_EXTRA_COLUMNS = ["source", "proxy", "origin"]
SecondSource = tuple[DataProvider, str, TickerSpec]


@dataclass
class PerformanceReport:
    """Performance table plus data diagnostics for one as-of date."""

    as_of: date
    table: pd.DataFrame
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)
    levels: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    #: ids whose print dated ``as_of`` was dropped: session still open (closing prices only)
    provisional: frozenset[str] = frozenset()

    def checks(self) -> dict[str, list[str]]:
        """Points to check before publishing, per instrument id (see :mod:`.analytics.quality`)."""
        return publication_checks(self.table, self.notes, self.errors)

    def by_asset_class(self) -> dict[AssetClass, pd.DataFrame]:
        """Sub-tables in display order (empty classes omitted)."""
        return {
            ac: self.table[self.table["asset_class"] == ac.value]
            for ac in AssetClass
            if (self.table["asset_class"] == ac.value).any()
        }


class MarketMonitor:
    """Wires the data service, the referential and the performance engine."""

    def __init__(
        self,
        service: MarketDataService,
        referential: Referential,
        analytics: AnalyticsSettings | None = None,
        timezone: str = "Europe/Paris",
        quality: QualitySettings | None = None,
    ) -> None:
        self._service = service
        self._referential = referential
        self._quality = quality or QualitySettings()
        self._engine = PerformanceEngine(analytics, self._quality.suspect_abs_z)
        self._tz = ZoneInfo(timezone)
        self._extended: dict[tuple[str, ...], MarketMonitor] = {}
        self._last_refresh: datetime | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> MarketMonitor:
        referential = load_referential(settings.instruments_path, settings.watchlists_path)
        return cls(build_service(settings), referential, settings.analytics, settings.timezone,
                   settings.quality)

    def with_instruments(self, instruments: Iterable[Instrument]) -> MarketMonitor:
        """Same sources and settings, referential extended with ``instruments`` (index members).

        Memoised per set of ids, so the views' caches keyed on ``id(monitor)`` keep working.
        """
        extra = [i for i in instruments if i.id not in self._referential]
        if not extra:
            return self
        key = tuple(sorted(i.id for i in extra))
        if key not in self._extended:
            twin = MarketMonitor(self._service, self._referential.extended(extra), self._engine.settings,
                                 str(self._tz), self._quality)
            twin._last_refresh = self.last_refresh
            self._extended[key] = twin
        return self._extended[key]

    def mark_refresh(self, at: datetime | None = None) -> datetime:
        """Manual-refresh mode: what is downloaded from now on is kept until the next call.

        Data cached before ``at`` (default: now) is refreshed once more when next needed;
        data downloaded after it is reused, so navigating does not consume the sources' quotas.
        """
        moment = at or datetime.now(UTC)
        for provider in self._service.providers:
            if isinstance(provider, CachedProvider):
                provider.hold_until(moment)
        self._last_refresh = moment
        for twin in self._extended.values():
            twin._last_refresh = moment
        return moment

    def data_time(self) -> datetime:
        """Moment the closing-price rule is applied at (market time zone).

        Manual-refresh mode: the last refresh of the day - data reused since then may date
        from it, so a session still open at that moment keeps its print out ("closes only")
        until the next refresh. Otherwise (or from the next day on): now.
        """
        now = self.now()
        if self._last_refresh is not None:
            loaded = self._last_refresh.astimezone(self._tz)
            if loaded.date() == now.date():
                return min(loaded, now)
        return now

    @property
    def last_refresh(self) -> datetime | None:
        """Moment of the last :meth:`mark_refresh` (``None``: intraday TTL mode)."""
        return self._last_refresh

    @property
    def referential(self) -> Referential:
        return self._referential

    @property
    def service(self) -> MarketDataService:
        return self._service

    @property
    def quality(self) -> QualitySettings:
        return self._quality

    def manual_quotes(self) -> ManualQuotes | None:
        """Store of the manual-quotes provider, when it is active."""
        provider = next((p for p in self._service.providers if isinstance(p, ManualProvider)), None)
        return provider.store if provider is not None else None

    def manual_ids(self, ids: Iterable[str] | None = None) -> tuple[str, ...]:
        """Instruments (among ``ids``, default all) that can be entered manually."""
        pool = self._referential.ids if ids is None else tuple(ids)
        return tuple(i for i in pool if i in self._referential
                     and MANUAL_PROVIDER in self._referential.get(i).tickers)

    @property
    def analytics(self) -> AnalyticsSettings:
        """Performance-engine settings (z-score window... - sets how much history is loaded)."""
        return self._engine.settings

    def today(self) -> date:
        """Current date in the market time zone."""
        return self.now().date()

    def now(self) -> datetime:
        """Current time in the market time zone (Europe/Paris)."""
        return datetime.now(self._tz)

    def is_available(self, instrument_id: str) -> bool:
        """True when an active provider has a ticker for it (all legs, for a derived one).

        Without Bloomberg, Bloomberg-only lines (iTraxx...) are therefore hidden instead of
        being reported as missing; they come back as soon as Bloomberg is reachable.
        """
        inst = self._referential.get(instrument_id)
        if inst.derived:
            return all(self.is_available(leg) for leg, _ in inst.derived.legs)
        return any(name in inst.tickers for name in self._service.provider_names)

    def available(self, ids: Iterable[str]) -> tuple[str, ...]:
        """``ids`` without the instruments no active provider can serve (order kept)."""
        return tuple(i for i in ids if self.is_available(i))

    def select(self, watchlist: str | None = None, ids: Iterable[str] | None = None) -> tuple[str, ...]:
        """Available instrument ids from a watchlist and/or explicit selectors."""
        entries: list[str] = list(self._referential.watchlist(watchlist).instruments) if watchlist else []
        entries.extend(ids or [])
        return self.available(self._referential.resolve(entries or ["*"]))

    def history(self, ids: Iterable[str], start: DateLike, end: DateLike) -> UniverseHistory:
        """Harmonised levels (referential units) for ``ids``."""
        return load_universe_history(self._service, self._referential, ids, start, end)

    def bars(self, ids: Iterable[str], start: DateLike, end: DateLike | None = None) -> UniverseBars:
        """Daily OHLCV bars (referential units) for ``ids``; long windows load in tranches."""
        return load_universe_bars(self._service, self._referential, ids, start,
                                  end if end is not None else self.today())

    def instrument_profile(
        self,
        instrument_id: str,
        period: str = "1A",
        as_of: DateLike | None = None,
        *,
        overlay: str | None = None,
        provisional: Collection[str] = (),
    ) -> InstrumentProfile:
        """Real level history and statistics of one instrument (instrument page).

        The history loaded covers the 5-year statistics and the 200-session average. For a
        window that starts before the history of the source in use (e.g. "Max" with FMP's
        five years), the deepest source among the providers that list the instrument serves
        it (the page names it). ``provisional`` ids have their print dated ``as_of``
        dropped: closes only. A sovereign yield comes with its spread against the Bund of
        the same maturity.
        """
        as_of_date = to_date(as_of) if as_of is not None else self.today()
        inst = self._referential.get(instrument_id)
        display_from = profile_start(period, as_of_date)
        loaded = self._deepest_bars(instrument_id, load_start(period, as_of_date), display_from,
                                    as_of_date)
        bars = loaded.bars[instrument_id]
        dropped = instrument_id in provisional and pd.Timestamp(as_of_date) in bars.index
        if dropped:
            bars = bars.drop(index=pd.Timestamp(as_of_date))
        benchmark = self._referential.benchmark_of(instrument_id)
        extra = [i for i in (benchmark, overlay) if i and i != instrument_id and self.is_available(i)]
        others = pd.DataFrame()
        if extra:
            others = self.history(extra, load_start(period, as_of_date), as_of_date).levels
            drop_session(others, provisional, as_of_date)
        shown = overlay is not None and overlay in others.columns
        start, requested = display_from, None
        if not bars.empty and (bars.index[0].date() - display_from).days > DEPTH_SLACK_DAYS:
            start, requested = bars.index[0].date(), display_from  # window starts with the history
        spread = None
        if benchmark and benchmark in others.columns and not bars.empty:
            spread = spread_vs(bars["last"], others[benchmark].dropna())
        pair = None
        if shown and overlay and not bars.empty:  # OAT against the BTP, the 2 years against the 10...
            pair = pair_spread(bars["last"], others[overlay].dropna(), inst.quote.value,
                               self._referential.get(overlay).quote.value)
        return InstrumentProfile(
            instrument=inst, as_of=as_of_date, period=period, start=start, bars=bars,
            stats=instrument_stats(bars["last"], inst, display_from),
            source=loaded.sources.get(instrument_id), origin=loaded.origins.get(instrument_id),
            proxy=loaded.proxies.get(instrument_id),
            notes=tuple(loaded.notes.get(instrument_id, [])),
            error=loaded.errors.get(instrument_id), warning=loaded.warnings.get(instrument_id),
            depth=self.depth([instrument_id]),
            benchmark_id=benchmark if spread is not None else None, spread=spread,
            overlay_id=overlay if shown else None,
            overlay=others[overlay].dropna() if shown else None,
            overlay_name=self._referential.get(overlay).name if shown and overlay else None,
            overlay_quote=self._referential.get(overlay).quote.value if shown and overlay else None,
            provisional=dropped, requested_start=requested, pair_spread=pair,
        )

    def _deepest_bars(self, instrument_id: str, load_from: date, display_from: date,
                      end: date) -> UniverseBars:
        """Bars from the usual source, or from a deeper one when the window needs it."""
        best = self.bars([instrument_id], load_from, end)
        inst = self._referential.get(instrument_id)
        frame = best.bars[instrument_id]
        if inst.derived or frame.empty or (frame.index[0].date() - display_from).days <= DEPTH_SLACK_DAYS:
            return best
        served_by = best.sources.get(instrument_id)
        for provider in self._service.providers:
            if provider.name == served_by or provider.name not in inst.tickers:
                continue
            try:
                other = load_universe_bars(MarketDataService([provider]), self._referential,
                                           [instrument_id], load_from, end)
            except MarketMonitorError:
                continue
            alt = other.bars[instrument_id]
            current = best.bars[instrument_id]
            if (not alt.empty and alt.index[0] < current.index[0] - pd.Timedelta(days=30)
                    and alt.index[-1] >= current.index[-1] - pd.Timedelta(days=5)):
                best = other
        return best

    def curves(self, as_of: DateLike | None = None, countries: Iterable[str] | None = None, *,
               provisional: Collection[str] = ()) -> CurveSet:
        """Sovereign curves (every ``curve`` point of ``countries``) against J-1, S-1, M-1."""
        as_of_date = to_date(as_of) if as_of is not None else self.today()
        ids = [i for i in curve_ids(self._referential, countries) if self.is_available(i)]
        if not ids:
            empty = build_curves(pd.DataFrame(), self._referential, as_of_date)
            return empty
        report = self.performance(watchlist=None, as_of=as_of_date, ids=ids, provisional=provisional)
        return build_curves(report.table, self._referential, report.as_of, report.provisional,
                            report.errors)

    def depth(self, ids: Iterable[str]) -> pd.DataFrame:
        """History actually held in the cache, per instrument and source (no download).

        ``depth_reached``: the source was asked for more history than it has (the first
        observation is well after the start of the loaded window) - "Max" is reached.
        A derived line is as deep as its shortest leg.
        """
        rows: list[dict[str, object]] = []
        for instrument_id in dict.fromkeys(ids):
            inst = self._referential.get(instrument_id)
            if inst.derived:
                legs = [r for leg, _ in inst.derived.legs for r in self._depth_rows(leg)[:1]]
                if legs:
                    rows.append(_derived_depth(inst, legs))
                continue
            rows.extend(self._depth_rows(instrument_id))
        return pd.DataFrame(rows, columns=DEPTH_COLUMNS)

    def _depth_rows(self, instrument_id: str) -> list[dict[str, object]]:
        inst = self._referential.get(instrument_id)
        rows = []
        for provider in self._service.providers:
            spec = inst.tickers.get(provider.name)
            if spec is None or not isinstance(provider, CachedProvider):
                continue
            entry = provider.coverage(spec.ticker)
            if entry is None or entry.series.empty:
                continue
            first, last = entry.series.index[0].date(), entry.series.index[-1].date()
            origin = entry.origin or chain_alternatives(spec.ticker)[0]
            rows.append({
                "id": instrument_id, "name": inst.name, "provider": provider.name,
                "source": source_label(origin), "origin": origin,
                "loaded_from": entry.covered_start, "first": first, "last": last,
                "observations": len(entry.series),
                "depth_reached": (first - entry.covered_start).days > DEPTH_SLACK_DAYS,
                "ohlc_share": _cached_ohlc_share(provider, spec.ticker, entry.series.index),
            })
        return rows

    def performance(
        self,
        watchlist: str | None = "home",
        as_of: DateLike | None = None,
        ids: Iterable[str] | None = None,
        *,
        provisional: Collection[str] = (),
    ) -> PerformanceReport:
        """Levels, changes and z-scores for a watchlist as of a date (default: today).

        ``provisional``: ids whose print dated ``as_of`` is a live quote of a session not
        closed yet; it is ignored, so the line shows its last close.
        """
        as_of_date = to_date(as_of) if as_of is not None else self.today()
        selected = self.select(watchlist, ids)
        hist = self.history(selected, required_start(as_of_date, self._engine.settings), as_of_date)
        dropped = frozenset(i for i in provisional if i in hist.levels.columns)
        drop_session(hist.levels, dropped, as_of_date)
        instruments = [self._referential.get(i) for i in selected]
        table = self._engine.compute(hist.levels, instruments, as_of_date)
        table["source"] = pd.Series(hist.sources, dtype="object").reindex(table.index)
        table["proxy"] = pd.Series(hist.proxies, dtype="object").reindex(table.index)
        table["origin"] = pd.Series(hist.origins, dtype="object").reindex(table.index)
        return PerformanceReport(as_of_date, table, dict(hist.errors), dict(hist.warnings),
                                 notes={k: list(v) for k, v in hist.notes.items()},
                                 origins=dict(hist.origins), levels=hist.levels,
                                 provisional=dropped)

    # ------------------------------------------------------------ cross-check
    def verify(self, report: PerformanceReport, ids: Iterable[str]) -> PerformanceReport:
        """Cross-check ``ids`` of ``report`` against a second source (in place, and returned).

        A gap marks the line ``suspect``; a derived line (spread, slope) is suspect when one
        of its legs is. Disabled by ``quality.cross_check: false``.
        """
        if not self._quality.cross_check:
            return report
        table = report.table
        targets = [i for i in dict.fromkeys(ids) if i in table.index and pd.notna(table.at[i, "level"])]
        derived = {i: [leg for leg, _ in self._referential.get(i).derived.legs]  # type: ignore[union-attr]
                   for i in targets if self._referential.get(i).is_derived}
        raw = list(dict.fromkeys([i for i in targets if i not in derived]
                                 + [leg for legs in derived.values() for leg in legs]))
        primary = self._primary_levels(report, raw)
        results = {i: self._check_one(i, primary.get(i), report) for i in raw}
        for instrument_id, result in results.items():
            if instrument_id in table.index:
                _apply_check(table, instrument_id, result.status, result.reason)
        for instrument_id, legs in derived.items():
            bad = [self._referential.get(leg).name for leg in legs if results[leg].status == CHECK_GAP]
            status = CHECK_GAP if bad else _combined([results[leg].status for leg in legs])
            _apply_check(table, instrument_id, status,
                         f"jambe à vérifier : {', '.join(bad)}" if bad else "")
        return report

    def _primary_levels(self, report: PerformanceReport, ids: list[str]) -> dict[str, pd.Series]:
        """Levels of ``ids`` as served in ``report`` (legs not in the report are loaded, cached)."""
        found = {i: report.levels[i] for i in ids if i in report.levels.columns}
        missing = [i for i in ids if i not in found]
        if missing:
            start = report.as_of - timedelta(days=CHECK_HISTORY_DAYS)
            hist = self.history(missing, start, report.as_of)
            drop_session(hist.levels, report.provisional, report.as_of)  # same closes as the report
            found.update({i: hist.levels[i] for i in missing if i in hist.levels.columns})
            report.origins.update({i: o for i, o in hist.origins.items() if i not in report.origins})
        return found

    def _check_one(self, instrument_id: str, primary: pd.Series | None,
                   report: PerformanceReport) -> CheckResult:
        if primary is None or primary.dropna().empty:
            return CheckResult(CHECK_UNAVAILABLE)
        inst = self._referential.get(instrument_id)
        t0 = primary.dropna().index[-1]
        for provider, ticker, spec in self._second_sources(inst, report.origins.get(instrument_id)):
            try:
                result = provider.get_history([ticker], t0 - pd.Timedelta(days=CHECK_WINDOW_DAYS), t0)
            except MarketMonitorError:
                continue
            secondary = result.data[ticker].dropna() * spec.scale
            if t0 in secondary.index:
                label = _second_label(provider.name, ticker)
                quality = self._quality
                if _same_benchmark(report.origins.get(instrument_id), ticker):
                    # two quotes of the same bond: a gap beyond a basis point or two is an error
                    quality = replace(quality, cross_check_level_tol_bp=quality.cross_check_quote_tol_bp)
                return cross_check(primary, secondary, inst, quality, label)
        return CheckResult(CHECK_UNAVAILABLE)

    def _second_sources(self, inst: Instrument, origin: str | None) -> list[SecondSource]:
        """Other providers first, then the other alternatives of the serving chain; no proxies."""
        serving = next((p for p in self._service.providers if p.name in inst.tickers and (
            origin == inst.tickers[p.name].ticker
            or origin in chain_alternatives(inst.tickers[p.name].ticker))), None)
        if serving is None:
            return []
        spec = inst.tickers[serving.name]
        used = resolve_origin(spec.ticker, origin)
        if spec.proxy_for(used):
            return []
        others: list[SecondSource] = [
            (p, inst.tickers[p.name].ticker, inst.tickers[p.name]) for p in self._service.providers
            if p is not serving and p.name in inst.tickers and not inst.tickers[p.name].proxy
            and not inst.tickers[p.name].alt_proxies
        ]
        siblings: list[SecondSource] = [
            (serving, alt, spec) for alt in chain_alternatives(spec.ticker)
            if alt != used and not spec.proxy_for(alt)]
        return (others + siblings)[:MAX_SECOND_SOURCES]

    def comparison(
        self,
        ids: Iterable[str],
        period: str = "1A",
        as_of: DateLike | None = None,
        start: DateLike | None = None,
    ) -> ComparisonResult:
        """Rebased histories (base 100 / bp / points) over ``period`` or from ``start``."""
        end = to_date(as_of) if as_of is not None else self.today()
        begin = to_date(start) if start is not None else period_start(period, end)
        selected = self.available(self._referential.resolve(ids))
        hist = self.history(selected, begin, end)
        result = compare(hist.levels, [self._referential.get(i) for i in selected], begin, end)
        result.errors = {**result.errors, **{k: v for k, v in hist.errors.items() if k in result.errors}}
        result.warnings = {**hist.warnings, **result.warnings}
        return result

    def correlation(
        self,
        ids: Iterable[str],
        as_of: DateLike | None = None,
        *,
        window: int = 63,
        frequency: Frequency = Frequency.DAILY,
        lag: int = 21,
        order: MatrixOrder = MatrixOrder.REFERENTIAL,
        history: int = 504,
    ) -> CorrelationResult:
        """Correlation matrix at ``as_of``, ``lag`` periods earlier, and ``history`` of returns."""
        end = to_date(as_of) if as_of is not None else self.today()
        selected = self.available(self._referential.resolve(ids))
        start = corr.required_start(end, window, lag, frequency, history)
        hist = self.history(selected, start, end)
        result = analyse_correlations(
            hist.levels, [self._referential.get(i) for i in selected], end,
            window=window, frequency=frequency, lag=lag, order=order,
        )
        result.errors = {**result.errors, **{k: v for k, v in hist.errors.items() if k in result.errors}}
        result.warnings = dict(hist.warnings)
        return result

    def alerts(self, rules: Sequence[AlertRule], as_of: DateLike | None = None) -> AlertReport:
        """Evaluate ``rules`` on a performance computation over their instruments."""
        as_of_date = to_date(as_of) if as_of is not None else self.today()
        universe = rule_universe(rules)
        if not universe:
            return AlertReport(as_of_date)
        report = self.performance(watchlist=None, as_of=as_of_date, ids=universe)
        return evaluate(rules, report.table, as_of_date)


#: start of the "Max" period of an instrument page (sources stop earlier: see ``depth``)
MAX_HISTORY_START = date(1990, 1, 1)
DEPTH_SLACK_DAYS = 10      # first print later than the loaded window by more: source exhausted
DEPTH_COLUMNS = ["id", "name", "provider", "source", "origin", "loaded_from", "first", "last",
                 "observations", "depth_reached", "ohlc_share"]


def _cached_ohlc_share(provider: CachedProvider, ticker: str, closes: pd.Index) -> float:
    parts = [provider.coverage(ticker, f) for f in (Field.OPEN, Field.HIGH, Field.LOW)]
    if any(p is None or p.series.empty for p in parts) or not len(closes):
        return 0.0
    complete = pd.concat([p.series for p in parts if p is not None], axis=1).dropna().index
    return float(closes.isin(complete).mean())


def _derived_depth(inst: Instrument, legs: list[dict[str, object]]) -> dict[str, object]:
    firsts = [r["first"] for r in legs]
    return {
        "id": inst.id, "name": inst.name, "provider": "derived",
        "source": " / ".join(str(r["source"]) for r in legs), "origin": None,
        "loaded_from": max(r["loaded_from"] for r in legs),  # type: ignore[type-var]
        "first": max(firsts),  # type: ignore[type-var]
        "last": min(r["last"] for r in legs),  # type: ignore[type-var]
        "observations": min(int(r["observations"]) for r in legs),  # type: ignore[call-overload]
        "depth_reached": all(bool(r["depth_reached"]) for r in legs), "ohlc_share": 0.0,
    }


CHECK_HISTORY_DAYS = 420   # enough daily changes for the sigma of a cross-check
CHECK_WINDOW_DAYS = 40     # second source: recent window only
MAX_SECOND_SOURCES = 2     # a firewalled second source must not cost more than two timeouts


def _apply_check(table: pd.DataFrame, instrument_id: str, status: str, reason: str) -> None:
    table.at[instrument_id, "cross_check"] = status
    if status == CHECK_GAP:
        previous = str(table.at[instrument_id, "suspect_reason"] or "")
        table.at[instrument_id, "suspect"] = True
        table.at[instrument_id, "suspect_reason"] = f"{previous} ; {reason}" if previous else reason


def _combined(statuses: list[str]) -> str:
    return CHECK_OK if statuses and all(s == CHECK_OK for s in statuses) else CHECK_UNAVAILABLE


#: prefixes of sources quoting the same benchmark bond (as opposed to a fitted curve: bbk:)
QUOTE_SOURCES = ("stooq:", "cnbc:")


def _same_benchmark(origin: str | None, ticker: str) -> bool:
    return bool(origin) and str(origin).startswith(QUOTE_SOURCES) and ticker.startswith(QUOTE_SOURCES) \
        and str(origin).split(":")[0] != ticker.split(":")[0]


def _second_label(provider: str, ticker: str) -> str:
    if provider == "free":
        return source_label(ticker)
    return {"bloomberg": "Bloomberg", "fmp": "FMP"}.get(provider, provider)


def drop_session(levels: pd.DataFrame, ids: Collection[str], day: date) -> None:
    """Blank (in place) the prints of ``ids`` dated ``day``: an open session is not a close."""
    columns = [i for i in ids if i in levels.columns]
    if columns:
        levels.loc[levels.index == pd.Timestamp(day), columns] = float("nan")
