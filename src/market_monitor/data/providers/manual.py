"""Manual quotes: levels typed by the user, for what no reachable source publishes.

Typical use: iTraxx Europe Main / Crossover 5Y without Bloomberg (Markit data are licensed,
no free daily source exists). Each morning the closing spread read on a screen or in a
broker note is entered once - in the *Daily macro* view or with
``market-monitor quote set ITRX_XOVER 287.5`` - and the history builds up from there
(1J after two entries, z-scores after about three months).

Storage: one local CSV ``date,ticker,value`` (``config/../data/manual_quotes.csv``), one
row per (date, ticker), never cached (a correction must show immediately).
"""

from __future__ import annotations

import io
import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd

from market_monitor.data.base import DataProvider
from market_monitor.data.models import NO_DATA, HistoryRequest, HistoryResult, to_date
from market_monitor.data.quality import assemble_frame, clean_series
from market_monitor.exceptions import DataProviderError, InvalidRequestError

logger = logging.getLogger(__name__)

COLUMNS = ["date", "ticker", "value"]


def previous_business_day(reference: date | str | None = None) -> date:
    """Default date of an entry: the last close before ``reference`` (default today)."""
    ref = pd.Timestamp(to_date(reference) if reference is not None else date.today())
    return (ref - pd.offsets.BDay()).date()
NOT_ENTERED = "aucune saisie manuelle"


class ManualQuotes:
    """CSV store of manual quotes (atomic writes, tolerant reads)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def frame(self) -> pd.DataFrame:
        """All quotes, sorted by ticker then date (empty frame when the file is absent)."""
        if not self._path.is_file():
            return _empty()
        try:
            frame = pd.read_csv(self._path, dtype={"ticker": str})
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise DataProviderError(f"unreadable manual quotes file {self._path}: {exc}") from exc
        return _normalise(frame)

    def series(self, ticker: str) -> pd.Series:
        frame = self.frame()
        rows = frame[frame["ticker"] == ticker]
        return clean_series(pd.Series(rows["value"].to_numpy(), index=rows["date"], name=ticker))

    def set(self, ticker: str, day: date | str, value: float) -> None:
        """Insert or replace the quote of ``ticker`` on ``day``."""
        if not isinstance(value, (int, float)) or value != value:  # NaN check
            raise InvalidRequestError(f"invalid value {value!r} for {ticker}")
        stamp = pd.Timestamp(to_date(day))
        frame = self.frame()
        frame = frame[~((frame["ticker"] == ticker) & (frame["date"] == stamp))]
        row = pd.DataFrame({"date": [stamp], "ticker": [ticker], "value": [float(value)]})
        self._write(pd.concat([frame, row], ignore_index=True))

    def delete(self, ticker: str, day: date | str) -> bool:
        stamp = pd.Timestamp(to_date(day))
        frame = self.frame()
        keep = ~((frame["ticker"] == ticker) & (frame["date"] == stamp))
        if keep.all():
            return False
        self._write(frame[keep])
        return True

    def import_csv(self, text: str) -> int:
        """Merge a history: ``date,ticker,value`` or ``date,<ticker1>,<ticker2>...``.

        Separator ``,`` or ``;`` and decimal comma are accepted (Excel exports in French).
        Imported rows replace existing ones on the same (date, ticker).
        """
        raw = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str)
        raw.columns = [str(c).strip() for c in raw.columns]
        if "date" not in raw.columns:
            raise InvalidRequestError("the file needs a 'date' column")
        if not {"ticker", "value"} <= set(raw.columns):
            raw = raw.melt(id_vars="date", var_name="ticker", value_name="value")
        imported = _normalise(raw[["date", "ticker", "value"]])
        if imported.empty:
            return 0
        current = self.frame()
        keys = set(zip(imported["date"], imported["ticker"], strict=True))
        pairs = zip(current["date"], current["ticker"], strict=True)
        current = current[[(d, t) not in keys for d, t in pairs]]
        self._write(pd.concat([current, imported], ignore_index=True))
        return len(imported)

    def _write(self, frame: pd.DataFrame) -> None:
        frame = frame.sort_values(["ticker", "date"]).reset_index(drop=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        out = frame.assign(date=pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d"))
        out.to_csv(tmp, index=False, columns=COLUMNS, float_format="%.6g")
        os.replace(tmp, self._path)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"date": pd.Series(dtype="datetime64[ns]"), "ticker": pd.Series(dtype=str),
                         "value": pd.Series(dtype=float)})


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Typed, de-duplicated (last wins) quotes; unparseable rows dropped."""
    if frame.empty:
        return _empty()
    values = frame["value"].astype(str).str.replace(" ", "").str.replace(" ", "")
    values = values.str.replace(",", ".", regex=False)
    out = pd.DataFrame({
        "date": pd.to_datetime(frame["date"].astype(str).str.strip(), errors="coerce", dayfirst=False),
        "ticker": frame["ticker"].astype(str).str.strip(),
        "value": pd.to_numeric(values, errors="coerce"),
    }).dropna()
    out = out[out["ticker"] != ""]
    return out.drop_duplicates(["date", "ticker"], keep="last").sort_values(["ticker", "date"])


class ManualProvider(DataProvider):
    """Serves the manual quotes; always available (an empty file simply serves nothing)."""

    name = "manual"

    def __init__(self, store: ManualQuotes) -> None:
        self._store = store

    @property
    def store(self) -> ManualQuotes:
        return self._store

    def _fetch_history(self, request: HistoryRequest) -> HistoryResult:
        frame = self._store.frame()
        series: dict[str, pd.Series] = {}
        errors: dict[str, str] = {}
        for ticker in request.tickers:
            rows = frame[frame["ticker"] == ticker]
            if rows.empty:
                errors[ticker] = NOT_ENTERED
                continue
            series[ticker] = pd.Series(rows["value"].to_numpy(), index=rows["date"])
        result = HistoryResult(data=assemble_frame(series, request.tickers), errors=errors)
        for ticker in request.tickers:  # entries exist but none in the window
            if ticker not in errors and result.data[ticker].isna().all():
                errors[ticker] = NO_DATA
        return result
