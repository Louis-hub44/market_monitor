"""Market Monitor facade - the only entry point the UI / terminal needs.

    monitor = MarketMonitor.from_settings(load_settings())
    report = monitor.performance("daily_macro")
    report.table            # one row per instrument
    report.by_asset_class() # ordered dict of sub-tables
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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
from market_monitor.analytics.history import UniverseHistory, load_universe_history
from market_monitor.analytics.performance import PerformanceEngine, required_start
from market_monitor.config import AnalyticsSettings, Settings
from market_monitor.data.factory import build_service
from market_monitor.data.models import DateLike, to_date
from market_monitor.data.service import MarketDataService
from market_monitor.referential import AssetClass, Referential, load_referential

REPORT_EXTRA_COLUMNS = ["source", "proxy"]


@dataclass
class PerformanceReport:
    """Performance table plus data diagnostics for one as-of date."""

    as_of: date
    table: pd.DataFrame
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)

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
    ) -> None:
        self._service = service
        self._referential = referential
        self._engine = PerformanceEngine(analytics)
        self._tz = ZoneInfo(timezone)

    @classmethod
    def from_settings(cls, settings: Settings) -> MarketMonitor:
        referential = load_referential(settings.instruments_path, settings.watchlists_path)
        return cls(build_service(settings), referential, settings.analytics, settings.timezone)

    @property
    def referential(self) -> Referential:
        return self._referential

    @property
    def service(self) -> MarketDataService:
        return self._service

    def today(self) -> date:
        """Current date in the market time zone."""
        return datetime.now(self._tz).date()

    def select(self, watchlist: str | None = None, ids: Iterable[str] | None = None) -> tuple[str, ...]:
        """Instrument ids from a watchlist and/or explicit selectors."""
        entries: list[str] = list(self._referential.watchlist(watchlist).instruments) if watchlist else []
        entries.extend(ids or [])
        return self._referential.resolve(entries or ["*"])

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
        return PerformanceReport(as_of_date, table, dict(hist.errors), dict(hist.warnings))

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
        selected = self._referential.resolve(ids)
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
        selected = self._referential.resolve(ids)
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
