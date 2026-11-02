"""In-memory / CSV provider: offline demos, manual snapshots and tests.

CSV layout of a snapshot directory (all files optional)::

    history.csv      date,<id1>,<id2>,...        (decimal units)
    bond_static.csv  isin,name,issuer,currency,coupon,frequency,day_count,issue_date,
                     accrual_start,first_coupon_date,maturity
    bond_prices.csv  date,isin,bid,ask,mid,yield_mid
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd

from rates_analytics.data.provider import (
    BOND_PRICE_COLUMNS,
    BOND_STATIC_COLUMNS,
    Capability,
    DataProvider,
    empty_frame,
    empty_history,
)
from rates_analytics.exceptions import ConfigurationError

_DATE_COLS = ("issue_date", "accrual_start", "first_coupon_date", "maturity")


class StaticProvider(DataProvider):
    """Serves data held in memory (every id present in the frames is servable)."""

    name = "static"
    capabilities = frozenset(Capability)

    def __init__(
        self,
        history: pd.DataFrame | None = None,
        bond_static: pd.DataFrame | None = None,
        bond_prices: pd.DataFrame | None = None,
        quote_lookback_days: int = 10,
    ) -> None:
        super().__init__(None, quote_lookback_days)
        self._history = self._prep_history(history)
        self._static = bond_static if bond_static is not None else empty_frame(BOND_STATIC_COLUMNS)
        self._prices = bond_prices  # long format: date, isin, bid, ask, mid, yield_mid

    @staticmethod
    def _prep_history(h: pd.DataFrame | None) -> pd.DataFrame:
        if h is None or h.empty:
            return empty_history()
        h = h.copy()
        h.index = pd.DatetimeIndex(pd.to_datetime(h.index), name="date")
        return h.sort_index().apply(pd.to_numeric, errors="coerce")

    @classmethod
    def from_directory(cls, directory: str | Path, quote_lookback_days: int = 10) -> "StaticProvider":
        """Load ``history.csv`` / ``bond_static.csv`` / ``bond_prices.csv`` from a folder."""
        d = Path(directory)
        if not d.is_dir():
            raise ConfigurationError(f"Static data directory not found: {d}")
        hist = pd.read_csv(d / "history.csv", index_col=0) if (d / "history.csv").is_file() else None
        static = None
        if (d / "bond_static.csv").is_file():
            static = pd.read_csv(d / "bond_static.csv", index_col="isin")
            for c in _DATE_COLS:
                if c in static:
                    static[c] = pd.to_datetime(static[c]).dt.date
        prices = None
        if (d / "bond_prices.csv").is_file():
            prices = pd.read_csv(d / "bond_prices.csv", parse_dates=["date"])
        return cls(hist, static, prices, quote_lookback_days)

    def serves(self, market_id: str) -> bool:
        return market_id in self._history.columns

    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        cols = [i for i in ids if i in self._history.columns]
        h = self._history.loc[(self._history.index >= pd.Timestamp(start))
                              & (self._history.index <= pd.Timestamp(end)), cols]
        return h.dropna(how="all", axis=1)

    def get_bond_static(self, isins: Sequence[str]) -> pd.DataFrame:
        out = self._static.loc[self._static.index.intersection(list(isins))].copy()
        out["source"] = self.name
        out.index.name = "id"
        return out.reindex(columns=BOND_STATIC_COLUMNS)

    def get_bond_prices(self, isins: Sequence[str], as_of: date) -> pd.DataFrame:
        if self._prices is None or self._prices.empty:
            return empty_frame(BOND_PRICE_COLUMNS)
        p = self._prices
        p = p.loc[p["isin"].isin(list(isins)) & (p["date"] <= pd.Timestamp(as_of))]
        p = p.sort_values("date").groupby("isin").tail(1).set_index("isin")
        p["obs_date"] = p["date"].dt.date
        p["source"] = self.name
        if "mid" not in p or p["mid"].isna().any():
            p["mid"] = p.get("mid", pd.Series(index=p.index, dtype=float)).fillna((p["bid"] + p["ask"]) / 2)
        p.index.name = "id"
        return p.reindex(columns=BOND_PRICE_COLUMNS)
