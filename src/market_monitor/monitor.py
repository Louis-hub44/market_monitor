"""Market Monitor facade - the only entry point the UI / terminal needs.

    monitor = MarketMonitor.from_settings(load_settings())
    report = monitor.performance("daily_macro")
    report.table            # one row per instrument
    report.by_asset_class() # ordered dict of sub-tables
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
from market_monitor.analytics.history import UniverseHistory, load_universe_history, source_label
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
from market_monitor.data.factory import build_service
from market_monitor.data.models import DateLike, chain_alternatives, resolve_origin, to_date
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

    @classmethod
    def from_settings(cls, settings: Settings) -> MarketMonitor:
        referential = load_referential(settings.instruments_path, settings.watchlists_path)
        return cls(build_service(settings), referential, settings.analytics, settings.timezone,
                   settings.quality)

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

    def today(self) -> date:
        """Current date in the market time zone."""
        return datetime.now(self._tz).date()

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

    def performance(
        self,
        watchlist: str | None = "home",
        as_of: DateLike | None = None,
        ids: Iterable[str] | None = None,
    ) -> PerformanceReport:
        """Levels, changes and z-scores for a watchlist as of a date (default: today)."""
        as_of_date = to_date(as_of) if as_of is not None else self.today()
        selected = self.select(watchlist, ids)
        hist = self.history(selected, required_start(as_of_date, self._engine.settings), as_of_date)
        instruments = [self._referential.get(i) for i in selected]
        table = self._engine.compute(hist.levels, instruments, as_of_date)
        table["source"] = pd.Series(hist.sources, dtype="object").reindex(table.index)
        table["proxy"] = pd.Series(hist.proxies, dtype="object").reindex(table.index)
        table["origin"] = pd.Series(hist.origins, dtype="object").reindex(table.index)
        return PerformanceReport(as_of_date, table, dict(hist.errors), dict(hist.warnings),
                                 notes={k: list(v) for k, v in hist.notes.items()},
                                 origins=dict(hist.origins), levels=hist.levels)

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
                return cross_check(primary, secondary, inst, self._quality, label)
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


def _second_label(provider: str, ticker: str) -> str:
    if provider == "free":
        return source_label(ticker)
    return {"bloomberg": "Bloomberg", "fmp": "FMP"}.get(provider, provider)
