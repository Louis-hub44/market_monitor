"""Index members as ad-hoc instruments, so a click on a stock opens its instrument page.

A member becomes ``EQ:<Yahoo symbol>`` (equity, price, change in %), served by Yahoo
(``free``) and, when the composition gives one, by FMP (deeper history, cross-check).
They are never added to the main referential: the overview and the watchlists keep their
84 lines; :meth:`MarketMonitor.with_instruments` builds a monitor that knows them too.
"""

from __future__ import annotations

from market_monitor.equities.constituents import Member
from market_monitor.referential import AssetClass, ChangeUnit, Instrument, QuoteType, TickerSpec

STOCK_PREFIX = "EQ:"


def stock_id(ticker: str) -> str:
    return f"{STOCK_PREFIX}{ticker}"


def is_stock_id(instrument_id: str | None) -> bool:
    return bool(instrument_id) and str(instrument_id).startswith(STOCK_PREFIX)


def stock_ticker(instrument_id: str) -> str:
    return instrument_id.removeprefix(STOCK_PREFIX)


def stock_instrument(member: Member) -> Instrument:
    tickers = {"free": TickerSpec(member.ticker)}
    if member.fmp:
        tickers["fmp"] = TickerSpec(member.fmp)
    return Instrument(id=stock_id(member.ticker), name=member.name, asset_class=AssetClass.EQUITY,
                      group=member.sector or "Actions", quote=QuoteType.PRICE,
                      unit=member.currency or "", change=ChangeUnit.PCT, tickers=tickers)
