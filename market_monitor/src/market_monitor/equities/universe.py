"""Facade of the equity layer: compositions + price panel + loader + calendars."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from market_monitor.config import EquitySettings
from market_monitor.equities.constituents import Compositions, save_composition
from market_monitor.equities.instruments import stock_instrument
from market_monitor.equities.loader import BarsFetcher, EquityLoader, LoadReport, yahoo_equity_fetcher
from market_monitor.equities.movers import IndexMovers, SecondSource, compute_movers, index_session
from market_monitor.equities.panel import EquityPanel
from market_monitor.equities.sources import MarketCaps, RefreshResult, refresh_composition
from market_monitor.exceptions import MarketMonitorError
from market_monitor.referential import Instrument

if TYPE_CHECKING:
    from market_monitor.monitor import MarketMonitor

#: Yahoo FX symbol giving the EUR value of one unit of a member's currency
FX_TO_EUR = {"GBP": "GBPEUR=X", "GBX": "GBPEUR=X", "GBp": "GBPEUR=X", "CHF": "CHFEUR=X",
             "SEK": "SEKEUR=X", "DKK": "DKKEUR=X", "NOK": "NOKEUR=X", "PLN": "PLNEUR=X",
             "USD": "USDEUR=X"}


@dataclass
class EquityUniverse:
    settings: EquitySettings
    compositions: Compositions
    panel: EquityPanel
    fetcher: BarsFetcher

    @classmethod
    def from_settings(cls, settings: EquitySettings, *, session: Any = None,
                      fetcher: BarsFetcher | None = None) -> EquityUniverse:
        return cls(settings, Compositions(settings.constituents_dir), EquityPanel(settings.directory),
                   fetcher or yahoo_equity_fetcher(session))

    def loader(self) -> EquityLoader:
        s = self.settings
        return EquityLoader(self.panel, self.fetcher, chunk_size=s.chunk_size, pause_s=s.pause_s,
                            history_days=s.history_days, lookback_days=s.lookback_days)

    def indices(self, with_extended: bool = False) -> list[str]:
        groups = ("core", "extended") if with_extended else ("core",)
        return self.compositions.indices(groups)

    def symbols(self, indices: Iterable[str]) -> list[str]:
        """Members of ``indices`` plus the FX rates converting foreign listings to the index currency."""
        chosen = list(indices)
        members = self.compositions.tickers(chosen)
        fx = []
        for index in chosen:
            comp = self.compositions.get(index)
            if comp.currency != "EUR":
                continue
            fx += [FX_TO_EUR[m.currency] for m in comp.members if m.currency in FX_TO_EUR]
        return list(dict.fromkeys(members + fx))

    def refresh_prices(self, indices: Iterable[str] | None = None, *, with_extended: bool = False,
                       end: date | None = None,
                       progress: Callable[[int, int], None] | None = None) -> LoadReport:
        chosen = list(indices) if indices is not None else self.indices(with_extended)
        return self.loader().refresh(self.symbols(chosen), end, progress)

    def refresh_composition(self, index: str, *, fetch_text: Callable[[str], str] | None = None,
                            fmp_list: Callable[[str], list[dict[str, Any]]] | None = None,
                            market_caps: MarketCaps | None = None) -> RefreshResult:
        result = refresh_composition(self.compositions.get(index), fetch_text=fetch_text,
                                     fmp_list=fmp_list, market_caps=market_caps)
        if result.composition is not self.compositions.get(index):
            save_composition(result.composition)
            self.compositions.replace(result.composition)
        return result

    def movers(self, index: str, monitor: MarketMonitor | None, as_of: date) -> IndexMovers:
        """Movers of ``index`` on its last closed session on or before ``as_of``.

        The index's own level (``monitor``, same id as the composition) gives the
        contributions in points and the residual check.
        """
        comp = self.compositions.get(index)
        now = monitor.now() if monitor is not None else None
        session = index_session(comp, as_of, now)
        levels = None
        if monitor is not None and index in monitor.referential and monitor.is_available(index):
            try:
                levels = monitor.history([index], session - timedelta(days=15), session).levels.get(index)
            except MarketMonitorError:
                levels = None
        return compute_movers(comp, self.panel, session, index_levels=levels, fx_to_eur=FX_TO_EUR,
                              max_abs_return_pct=self.settings.max_abs_return_pct)

    def second_source(self, monitor: MarketMonitor, index: str) -> SecondSource | None:
        """FMP closes of the members (their ``fmp`` symbol), when FMP is an active source."""
        provider = next((p for p in monitor.service.providers if p.name == "fmp"), None)
        if provider is None:
            return None
        symbols = {m.ticker: m.fmp for m in self.compositions.get(index).members if m.fmp}

        def fetch(tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
            wanted = {t: symbols[t] for t in tickers if t in symbols}
            if not wanted:
                return {}
            data = provider.get_history(list(dict.fromkeys(wanted.values())), start, end).data
            return {t: pd.DataFrame({"last": data[s]}) for t, s in wanted.items() if s in data.columns}
        return fetch

    def instruments(self, indices: Iterable[str] | None = None) -> list[Instrument]:
        """Members as ``EQ:`` instruments (instrument page of a stock)."""
        chosen = list(indices) if indices is not None else [c.index for c in self.compositions]
        seen: dict[str, Instrument] = {}
        for index in chosen:
            for member in self.compositions.get(index).members:
                seen.setdefault(member.ticker, stock_instrument(member))
        return list(seen.values())

    def status(self) -> pd.DataFrame:
        """Per index: members, members with prices, last close, composition date and weights."""
        rows = []
        last = self.panel.field("last")
        for comp in self.compositions:
            loaded = [t for t in comp.tickers if t in last.columns and last[t].notna().any()]
            last_dates = [pd.Timestamp(last[t].dropna().index[-1]) for t in loaded]
            rows.append({
                "index": comp.index, "name": comp.name, "group": comp.group, "members": len(comp.members),
                "loaded": len(loaded), "last_close": max(last_dates).date() if last_dates else None,
                "as_of": comp.as_of, "weights": comp.weights, "source": comp.source,
            })
        return pd.DataFrame(rows)
