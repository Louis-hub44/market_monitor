"""Missing-data handling and normalisation helpers.

Policy: providers return raw observations only; nothing is filled at the data layer.
Calendar alignment (e.g. EUR vs US holidays) is an explicit, bounded step done by the
analytics layer via :func:`align_calendars`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from market_monitor.data.models import INDEX_NAME, SNAPSHOT_COLUMNS, DateLike, to_date


def empty_series(name: str | None = None) -> pd.Series:
    """Empty float series with a ``date`` DatetimeIndex."""
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([], name=INDEX_NAME), name=name)


def _to_naive_dates(index: pd.Index) -> pd.DatetimeIndex:
    """Parse an index into tz-naive dates (local wall-clock date is kept)."""
    idx = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce"))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def clean_series(series: pd.Series | None) -> pd.Series:
    """Coerce to float64, drop NaN / inf / unparseable dates, sort and de-duplicate.

    On duplicated dates the **last** observation wins (latest print / revision).
    """
    if series is None or len(series) == 0:
        return empty_series(getattr(series, "name", None))
    values = pd.to_numeric(pd.Series(series), errors="coerce").to_numpy(
        dtype="float64", na_value=np.nan
    )
    out = pd.Series(values, index=_to_naive_dates(series.index), name=series.name)
    out = out[out.index.notna()]
    out = out.replace([np.inf, -np.inf], np.nan).dropna()
    out = out.sort_index(kind="stable")
    out = out[~out.index.duplicated(keep="last")]
    out.index.name = INDEX_NAME
    return out


def assemble_frame(series_by_col: Mapping[str, pd.Series], columns: Sequence[str]) -> pd.DataFrame:
    """Outer-join cleaned series into a frame with exactly ``columns`` (in order)."""
    cleaned = {c: clean_series(s) for c, s in series_by_col.items()}
    non_empty = {c: s for c, s in cleaned.items() if not s.empty}
    if non_empty:
        frame = pd.concat(non_empty, axis=1).sort_index()
    else:
        frame = pd.DataFrame(index=pd.DatetimeIndex([], name=INDEX_NAME))
    frame = frame.reindex(columns=list(columns)).astype("float64")
    frame.index = pd.DatetimeIndex(frame.index, name=INDEX_NAME)
    frame.columns.name = None
    return frame


def slice_dates(series: pd.Series, start: date, end: date) -> pd.Series:
    """Inclusive date slice of a sorted series."""
    return series.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def last_valid(frame: pd.DataFrame) -> pd.DataFrame:
    """Last valid observation per column -> frame indexed by column, ``value``/``as_of``."""
    rows: dict[str, tuple[float, Any]] = {}
    for col in frame.columns:
        valid = frame[col].dropna()
        rows[col] = (float(valid.iloc[-1]), valid.index[-1]) if not valid.empty else (np.nan, pd.NaT)
    out = pd.DataFrame.from_dict(rows, orient="index", columns=list(SNAPSHOT_COLUMNS))
    out["value"] = out["value"].astype("float64")
    out["as_of"] = pd.to_datetime(out["as_of"])
    return out


def align_calendars(frame: pd.DataFrame, max_fill_days: int = 3) -> pd.DataFrame:
    """Forward-fill holiday gaps across markets, bounded to ``max_fill_days`` rows.

    Needed before cross-asset operations (correlations, spreads EUR vs US) so that a
    US holiday does not create a NaN return on every EUR asset - but bounded so that a
    dead series never looks alive.
    """
    if max_fill_days < 0:
        raise ValueError("max_fill_days must be >= 0")
    return frame.ffill(limit=max_fill_days) if max_fill_days else frame.copy()


def stale_columns(frame: pd.DataFrame, as_of: DateLike, max_age_bdays: int = 1) -> list[str]:
    """Columns whose last valid print is older than ``max_age_bdays`` business days."""
    ref = np.datetime64(to_date(as_of), "D")
    stale: list[str] = []
    for col in frame.columns:
        valid = frame[col].dropna()
        if valid.empty:
            stale.append(col)
            continue
        last = np.datetime64(valid.index[-1].date(), "D")
        if np.busday_count(last, ref) > max_age_bdays:
            stale.append(col)
    return stale


def to_float(value: object) -> float:
    """Best-effort float conversion; ``NaN`` for ``None`` / unparseable values."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
