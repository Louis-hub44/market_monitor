"""Synthetic but plausible market data for every instrument of a referential (tests only)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from market_monitor.data.service import MarketDataService
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import Referential, TickerSpec
from tests.conftest import FakeProvider

BASE = {"price": 100.0, "yield": 3.0, "spread": 60.0, "vol": 17.0}
DAILY_VOL = {"price": 0.011, "yield": 0.035, "spread": 1.5, "vol": 0.8}


def synthetic_monitor(referential: Referential, end: str | None = None, seed: int = 7) -> MarketMonitor:
    """Monitor on a copy of ``referential`` where one fake provider serves one-factor random walks.

    Every raw instrument is re-mapped to the ticker ``<id>`` of a single "bloomberg" fake,
    so the whole referential (derived instruments, watchlists) works offline.
    """
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    idx = pd.bdate_range(end_ts - pd.Timedelta(days=600), end_ts)
    rng = np.random.default_rng(seed)
    factor = rng.normal(0, 1, len(idx))
    instruments, data = [], {}
    for instrument_id in referential.ids:
        inst = referential.get(instrument_id)
        if not inst.is_derived:
            quote = inst.quote.value
            steps = DAILY_VOL[quote] * (0.6 * factor + 0.8 * rng.normal(0, 1, len(idx)))
            path = (BASE[quote] * np.exp(np.cumsum(steps)) if quote == "price"
                    else BASE[quote] + np.cumsum(steps))
            data[instrument_id] = pd.Series(path, index=idx)
            inst = replace(inst, tickers={"bloomberg": TickerSpec(instrument_id)})
        instruments.append(inst)
    watchlists = [referential.watchlist(n) for n in referential.watchlist_names]
    fake_ref = Referential(instruments, watchlists)
    return MarketMonitor(MarketDataService([FakeProvider("bloomberg", data)]), fake_ref)
