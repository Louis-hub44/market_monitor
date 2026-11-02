"""Market-data service: provider chain + parquet cache + missing-data policy.

This is the single entry point the engine (and the other terminal modules)
use to obtain market data::

    svc = build_market_data_service(load_settings())
    quotes = svc.quotes(["EUR.ESTR.OIS.1Y", "EUR.ESTR.OIS.10Y"], as_of=date(2026, 10, 6))

For every request the primary provider is tried first, then each fallback for
the ids still missing. What is still missing at the end is handled according to
the policy: ``raise`` (:class:`MissingDataError`), ``warn`` (log + NaN rows /
columns) or ``drop`` (silently absent). The :class:`DataReport` of the last call
says which provider served each id, which ids are missing, stale or suspicious.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Callable, Sequence

import pandas as pd

from rates_analytics.data.cache import ParquetCache
from rates_analytics.data.provider import (
    BOND_PRICE_COLUMNS,
    BOND_STATIC_COLUMNS,
    QUOTE_COLUMNS,
    Capability,
    DataProvider,
    empty_frame,
    empty_history,
)
from rates_analytics.exceptions import (
    DataNotSupportedError,
    DataProviderError,
    MissingDataError,
)

logger = logging.getLogger(__name__)


class MissingPolicy(str, Enum):
    RAISE = "raise"
    WARN = "warn"
    DROP = "drop"


@dataclass
class DataReport:
    """Diagnostics of the last request."""

    dataset: str = ""
    served_by: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    stale: dict[str, date] = field(default_factory=dict)
    suspicious: dict[str, float] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cache_hits: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.errors)


def _unique(ids: Sequence[str]) -> list[str]:
    if isinstance(ids, str):
        ids = [ids]
    seen, out = set(), []
    for i in ids:
        if not isinstance(i, str) or not i.strip():
            raise ValueError(f"Invalid market data id {i!r}")
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


class MarketDataService:
    """Facade over an ordered chain of providers."""

    def __init__(
        self,
        providers: Sequence[DataProvider],
        cache: ParquetCache | None = None,
        missing_policy: MissingPolicy | str = MissingPolicy.WARN,
        plausible_rate_range: tuple[float, float] = (-0.05, 0.30),
        stale_after_days: int = 5,
        static_ttl: timedelta = timedelta(days=30),
        today: Callable[[], date] = date.today,
    ) -> None:
        if not providers:
            raise ValueError("MarketDataService needs at least one provider")
        self.providers = list(providers)
        self.cache = cache
        self.policy = MissingPolicy(missing_policy)
        self.rate_range = plausible_rate_range
        self.stale_after = timedelta(days=stale_after_days)
        self.static_ttl = static_ttl
        self._today = today
        self.last_report = DataReport()

    @property
    def primary(self) -> DataProvider:
        return self.providers[0]

    # ------------------------------------------------------------- history
    def history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        """Daily history (decimal units), one column per id."""
        ids = _unique(ids)
        if start > end:
            raise ValueError(f"start {start} after end {end}")
        report = DataReport(dataset="history")
        frames: list[pd.Series] = []
        remaining = list(ids)
        for prov in self.providers:
            if not remaining or not prov.supports(Capability.HISTORY):
                continue
            got = self._history_from(prov, remaining, start, end, report)
            for mid, s in got.items():
                frames.append(s.rename(mid))
                report.served_by[mid] = prov.name
            remaining = [i for i in remaining if i not in got]
        out = pd.concat(frames, axis=1) if frames else empty_history()
        out.index.name = "date"
        self._flag_suspicious_history(out, report)
        return self._finalise_columns(out, ids, remaining, report)

    def _history_from(self, prov, ids, start, end, report) -> dict[str, pd.Series]:
        got: dict[str, pd.Series] = {}
        to_fetch = []
        for mid in ids:
            if not prov.serves(mid):
                continue
            cached = self.cache.get_series(prov.name, mid, start, end, self._today()) if self.cache else None
            if cached is not None and not cached.empty:
                got[mid] = cached
                report.cache_hits.append(mid)
            else:
                to_fetch.append(mid)
        if not to_fetch:
            return got
        f_start, f_end = start, end
        if self.cache:  # refetch the union window so that the cached coverage stays contiguous
            for mid in to_fetch:
                meta = self.cache.series_meta(prov.name, mid)
                if meta is not None:
                    f_start, f_end = min(f_start, meta.start), max(f_end, meta.end)
        try:
            df = prov.get_history(to_fetch, f_start, f_end)
        except DataNotSupportedError:
            return got
        except DataProviderError as exc:
            logger.warning("Provider %s failed on history: %s", prov.name, exc)
            for mid in to_fetch:
                report.errors[mid] = f"{prov.name}: {exc}"
            return got
        for mid in to_fetch:
            if mid in df.columns:
                s = self._clean(df[mid])
                if s.empty:
                    continue
                if self.cache:
                    self.cache.put_series(prov.name, mid, s, f_start, min(f_end, self._today()))
                window = s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
                if not window.empty:
                    got[mid] = window
                    report.errors.pop(mid, None)
        return got

    # -------------------------------------------------------------- quotes
    def quotes(self, ids: Sequence[str], as_of: date) -> pd.DataFrame:
        """Last value on or before ``as_of`` per id (columns ``value, obs_date, source``)."""
        ids = _unique(ids)
        report = DataReport(dataset="quotes")
        is_live = as_of >= self._today()
        rows: list[pd.DataFrame] = []
        remaining = list(ids)
        for prov in self.providers:
            if not remaining or not prov.supports(Capability.QUOTES):
                continue
            servable = [i for i in remaining if prov.serves(i)]
            if not servable:
                continue
            partition = f"quotes_{as_of.isoformat()}"
            ttl = self.cache.ttl_live if (self.cache and is_live) else None
            cached = self.cache.get_table(prov.name, "quotes", partition, ttl) if self.cache else None
            hit = cached.loc[cached.index.intersection(servable)] if cached is not None else empty_frame(QUOTE_COLUMNS)
            report.cache_hits += list(hit.index)
            need = [i for i in servable if i not in hit.index]
            fetched = empty_frame(QUOTE_COLUMNS)
            if need:
                try:
                    fetched = prov.get_quotes(need, as_of)
                    fetched = fetched.loc[fetched["value"].map(lambda v: v is not None and math.isfinite(v))]
                    if self.cache and not fetched.empty:
                        self.cache.upsert_table(prov.name, "quotes", partition, fetched)
                except DataProviderError as exc:
                    logger.warning("Provider %s failed on quotes: %s", prov.name, exc)
                    for mid in need:
                        report.errors[mid] = f"{prov.name}: {exc}"
            block = pd.concat([hit, fetched]) if not fetched.empty else hit
            for mid in block.index:
                report.served_by[mid] = prov.name
                report.errors.pop(mid, None)
            rows.append(block)
            remaining = [i for i in remaining if i not in block.index]
        out = pd.concat([r for r in rows if not r.empty]) if any(not r.empty for r in rows) else empty_frame(QUOTE_COLUMNS)
        for mid, r in out.iterrows():
            if as_of - r["obs_date"] > self.stale_after:
                report.stale[mid] = r["obs_date"]
            if not self.rate_range[0] <= r["value"] <= self.rate_range[1]:
                report.suspicious[mid] = float(r["value"])
        return self._finalise_rows(out, ids, remaining, report, QUOTE_COLUMNS)

    # ---------------------------------------------------------------- bonds
    def bond_static(self, isins: Sequence[str]) -> pd.DataFrame:
        """Bond reference data (cached ``static_ttl``)."""
        return self._bond_table(isins, Capability.BOND_STATIC, "bond_static", "all",
                                self.static_ttl, lambda p, need: p.get_bond_static(need),
                                BOND_STATIC_COLUMNS)

    def bond_prices(self, isins: Sequence[str], as_of: date) -> pd.DataFrame:
        """Bond prices on ``as_of`` (live TTL if ``as_of`` is today)."""
        ttl = self.cache.ttl_live if (self.cache and as_of >= self._today()) else None
        out = self._bond_table(isins, Capability.BOND_PRICES, "bond_prices", as_of.isoformat(), ttl,
                               lambda p, need: p.get_bond_prices(need, as_of), BOND_PRICE_COLUMNS)
        for isin, r in out.dropna(subset=["mid"]).iterrows():
            if not 0.0 < r["mid"] < 1000.0:
                self.last_report.suspicious[isin] = float(r["mid"])
        return out

    def _bond_table(self, isins, cap, dataset, partition, ttl, fetch, columns) -> pd.DataFrame:
        isins = _unique(isins)
        report = DataReport(dataset=dataset)
        blocks, remaining = [], list(isins)
        for prov in self.providers:
            if not remaining or not prov.supports(cap):
                continue
            cached = self.cache.get_table(prov.name, dataset, partition, ttl) if self.cache else None
            hit = cached.loc[cached.index.intersection(remaining)] if cached is not None else empty_frame(columns)
            report.cache_hits += list(hit.index)
            need = [i for i in remaining if i not in hit.index]
            fetched = empty_frame(columns)
            if need:
                try:
                    fetched = fetch(prov, need)
                    if self.cache and not fetched.empty:
                        self.cache.upsert_table(prov.name, dataset, partition, fetched)
                except DataProviderError as exc:
                    logger.warning("Provider %s failed on %s: %s", prov.name, dataset, exc)
                    for i in need:
                        report.errors[i] = f"{prov.name}: {exc}"
            block = pd.concat([hit, fetched]) if not fetched.empty else hit
            for i in block.index:
                report.served_by[i] = prov.name
                report.errors.pop(i, None)
            blocks.append(block)
            remaining = [i for i in remaining if i not in block.index]
        non_empty = [b for b in blocks if not b.empty]
        out = pd.concat(non_empty) if non_empty else empty_frame(columns)
        return self._finalise_rows(out, isins, remaining, report, columns)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _clean(s: pd.Series) -> pd.Series:
        s = pd.to_numeric(s, errors="coerce")
        s = s[s.map(lambda v: math.isfinite(v) if v is not None else False)]
        s.index = pd.DatetimeIndex(s.index, name="date")
        return s.sort_index()

    def _flag_suspicious_history(self, df: pd.DataFrame, report: DataReport) -> None:
        lo, hi = self.rate_range
        for col in df.columns:
            bad = df[col].dropna()
            bad = bad[(bad < lo) | (bad > hi)]
            if not bad.empty:
                report.suspicious[col] = float(bad.iloc[-1])

    def _apply_policy(self, missing: list[str], report: DataReport) -> None:
        report.missing = list(missing)
        self.last_report = report
        if not missing:
            return
        if self.policy is MissingPolicy.RAISE:
            raise MissingDataError(missing, report.dataset)
        if self.policy is MissingPolicy.WARN:
            logger.warning("Missing %s for: %s", report.dataset, ", ".join(missing))

    def _finalise_columns(self, df, ids, missing, report) -> pd.DataFrame:
        self._apply_policy(missing, report)
        cols = ids if self.policy is MissingPolicy.WARN else [i for i in ids if i in df.columns]
        return df.reindex(columns=cols).sort_index()

    def _finalise_rows(self, df, ids, missing, report, columns) -> pd.DataFrame:
        self._apply_policy(missing, report)
        rows = ids if self.policy is MissingPolicy.WARN else [i for i in ids if i in df.index]
        out = df.reindex(index=rows, columns=columns)
        out.index.name = "id"
        return out

    def close(self) -> None:
        for p in self.providers:
            p.close()
