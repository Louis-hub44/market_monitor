"""Streamlit script used by AppTest: the real page wired to in-memory providers."""

import numpy as np
import pandas as pd
import streamlit as st

from market_monitor.alerts import rules_from_dict
from market_monitor.config import UiSettings
from market_monitor.data.service import MarketDataService
from market_monitor.export.layout import layout_from_dict
from market_monitor.monitor import MarketMonitor
from market_monitor.ui import theme
from market_monitor.ui.page import render_market_monitor
from tests.conftest import FakeProvider
from tests.test_history_monitor import REF

today = pd.Timestamp.today().normalize()
idx = pd.bdate_range(today - pd.Timedelta(days=500), today)
rng = np.random.default_rng(1)


def walk(base: float, vol: float) -> pd.Series:
    return pd.Series(base + np.cumsum(rng.normal(0, vol, len(idx))), index=idx)


bbg = FakeProvider("bloomberg", {"SX5E Index": walk(5000, 30), "GFRN10 Index": walk(3.4, 0.03),
                                 "GDBR10 Index": walk(2.7, 0.03)})
free = FakeProvider("free", {"ETF": walk(40, 0.5)})
monitor = MarketMonitor(MarketDataService([bbg, free]), REF)
st.markdown(theme.page_css(), unsafe_allow_html=True)
layout = layout_from_dict(
    {"sections": [{"title": "Indices", "instruments": ["SX5E"]},
                  {"title": "Taux", "instruments": ["OAT_BUND", "BUND_10Y"]}],
     "movers": {"universe": "all", "min_abs_z": 0.0}}, REF)
rules = rules_from_dict({"rules": [
    {"id": "lvl", "name": "OAT-Bund > 50 pb", "type": "level", "instrument": "OAT_BUND", "above": 50,
     "severity": "critical"},
    {"id": "z", "type": "zscore", "instruments": ["*"], "min_abs_z": 0.1},
]}, REF)
render_market_monitor(monitor, UiSettings(default_watchlist="macro", cache_ttl_minutes=0),
                      layout=layout, rules=rules)
