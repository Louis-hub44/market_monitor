"""Streamlit script used by AppTest: Daily macro view with a manual-quotes provider."""

import os
from datetime import date

import pandas as pd

from market_monitor.config import UiSettings
from market_monitor.data.providers.manual import ManualProvider, ManualQuotes
from market_monitor.data.service import MarketDataService
from market_monitor.export.layout import layout_from_dict
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import referential_from_dicts
from market_monitor.ui.daily_macro_view import render_daily_macro
from tests.conftest import FakeProvider

REF = referential_from_dicts(
    {"defaults": {"equity": {"quote": "price"}}, "instruments": {
        "SX5E": {"name": "Euro Stoxx 50", "class": "equity", "tickers": {"bloomberg": "SX5E Index"}},
        "ITRX_MAIN": {"name": "iTraxx Main", "class": "credit", "quote": "spread", "unit": "bp",
                      "tickers": {"manual": "ITRX_MAIN"}},
        "ITRX_XOVER": {"name": "iTraxx Crossover", "class": "credit", "quote": "spread", "unit": "bp",
                       "tickers": {"manual": "ITRX_XOVER"}},
    }},
    {"watchlists": {"all": {"instruments": ["*"]}}},
)
idx = pd.bdate_range("2025-06-01", "2026-10-06")
bbg = FakeProvider("bloomberg", {"SX5E Index": pd.Series(5000.0, index=idx)})
store = ManualQuotes(os.environ["MM_TEST_QUOTES"])
monitor = MarketMonitor(MarketDataService([bbg, ManualProvider(store)]), REF)
layout = layout_from_dict({"sections": [
    {"title": "Indices", "instruments": ["SX5E"]},
    {"title": "Spreads", "instruments": ["ITRX_MAIN", {"id": "ITRX_XOVER", "label": "Xover"}]},
], "movers": {"universe": "all"}}, REF)
render_daily_macro(monitor, UiSettings(cache_ttl_minutes=0), layout, date(2026, 10, 7), "mm")
