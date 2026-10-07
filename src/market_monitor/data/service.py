"""Multi-provider facade with per-instrument fallback.

The service speaks **canonical ids** (``"SX5E"``, ``"BUND_10Y"``...). A symbol map gives,
for each id, the native ticker of each provider that can serve it::

    {"SX5E": {"bloomberg": "SX5E Index", "fmp": "^STOXX50E", "free": "^STOXX50E"}}

Providers are tried in priority order; an id is served by the first provider that
returns at least one valid observation. The YAML instrument referential
builds this map.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from market_monitor.data.base import DataProvider
from market_monitor.data.models import (
    NO_DATA,
    SNAPSHOT_COLUMNS,
    DateLike,
    Field,
    HistoryRequest,
    HistoryResult,
    SnapshotResult,
    normalize_tickers,
)
from market_monitor.data.quality import assemble_frame
from market_monitor.exceptions import ConfigError, DataProviderError, InvalidRequestError

logger = logging.getLogger(__name__)

SymbolMap = Mapping[str, Mapping[str, str]]
# provider, native tickers -> (result object, extractor ticker -> value or None)
_Call = Callable[[DataProvider, list[str]], tuple[Any, Callable[[str], Any]]]


class MarketDataService:
    """Ordered chain of providers resolving canonical ids."""

    def __init__(self, providers: Sequence[DataProvider]) -> None:
        if not providers:
            raise ConfigError("at least one data provider is required")
        names = [p.name for p in providers]
        if len(set(names)) != len(names):
            raise ConfigError(f"duplicate provider names: {names}")
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[DataProvider, ...]:
        return self._providers

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._providers)

    def get_history(
        self,
        symbols: SymbolMap,
        start: DateLike,
        end: DateLike,
        field: Field | str = Field.LAST,
    ) -> HistoryResult:
        """History per canonical id, each id from the best available provider."""
        ids = _validate_symbols(symbols)
        request = HistoryRequest.create(ids, start, end, field)

        origins: dict[str, str] = {}

        def call(provider: DataProvider, tickers: list[str]) -> tuple[Any, Callable[[str], Any]]:
            result = provider.get_history(tickers, request.start, request.end, request.field)

            def extract(ticker: str) -> pd.Series | None:
                series = _non_empty(result.data[ticker].dropna())
                if series is not None:
                    origins[ticker] = result.origins.get(ticker, ticker)
                return series

            return result, extract

        found, sources, errors, warnings = self._resolve(symbols, ids, call)
        by_id = {i: origins.get(symbols[i][sources[i]], symbols[i][sources[i]]) for i in sources}
        return HistoryResult(
            data=assemble_frame(found, ids), errors=errors, warnings=warnings, sources=sources,
            origins=by_id,
        )

    def get_snapshot(self, symbols: SymbolMap) -> SnapshotResult:
        """Latest value per canonical id."""
        ids = _validate_symbols(symbols)

        def call(provider: DataProvider, tickers: list[str]) -> tuple[Any, Callable[[str], Any]]:
            result = provider.get_snapshot(tickers)
            return result, lambda t: (
                result.data.loc[t] if pd.notna(result.data.at[t, "value"]) else None
            )

        found, sources, errors, warnings = self._resolve(symbols, ids, call)
        frame = pd.DataFrame(
            [found[i] for i in found], index=list(found), columns=list(SNAPSHOT_COLUMNS)
        ).reindex(list(ids))
        return SnapshotResult(data=frame, errors=errors, warnings=warnings, sources=sources)

    def _resolve(
        self, symbols: SymbolMap, ids: tuple[str, ...], call: _Call
    ) -> tuple[dict[str, Any], dict[str, str], dict[str, str], dict[str, str]]:
        found: dict[str, Any] = {}
        sources: dict[str, str] = {}
        warnings: dict[str, str] = {}
        trail: dict[str, list[str]] = defaultdict(list)
        for provider in self._providers:
            pending = {i: symbols[i][provider.name] for i in ids
                       if i not in found and provider.name in symbols[i]}
            if not pending:
                continue
            try:
                result, extract = call(provider, list(dict.fromkeys(pending.values())))
            except DataProviderError as exc:
                logger.warning("provider %s failed: %s", provider.name, exc)
                for i in pending:
                    trail[i].append(f"{provider.name}: {exc}")
                continue
            for i, ticker in pending.items():
                value = extract(ticker)
                if value is None:
                    trail[i].append(f"{provider.name}: {result.errors.get(ticker, NO_DATA)}")
                    continue
                found[i] = value
                sources[i] = provider.name
                if ticker in result.warnings:
                    warnings[i] = f"{provider.name}: {result.warnings[ticker]}"
        errors = {
            i: " | ".join(trail[i]) or f"no ticker mapped for providers {list(self.provider_names)}"
            for i in ids if i not in found
        }
        return found, sources, errors, warnings


def _non_empty(series: pd.Series) -> pd.Series | None:
    return None if series.empty else series


def _validate_symbols(symbols: SymbolMap) -> tuple[str, ...]:
    if not isinstance(symbols, Mapping) or not symbols:
        raise InvalidRequestError("symbols must be a non-empty mapping id -> {provider: ticker}")
    ids = tuple(symbols)
    if normalize_tickers(ids) != ids:
        raise InvalidRequestError("canonical ids must be unique, non-blank and stripped")
    for sid in ids:
        mapping = symbols[sid]
        if not isinstance(mapping, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() and v == v.strip()
            for k, v in mapping.items()
        ):
            raise InvalidRequestError(f"invalid provider mapping for {sid!r}: {mapping!r}")
    return ids
