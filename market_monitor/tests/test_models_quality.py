from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_monitor.data.models import Field, HistoryRequest, normalize_tickers, to_date
from market_monitor.data.quality import (
    align_calendars,
    assemble_frame,
    clean_series,
    last_valid,
    stale_columns,
)
from market_monitor.exceptions import InvalidRequestError


def test_request_coerces_and_dedupes():
    req = HistoryRequest.create(["A", " B ", "A"], "2026-01-02", pd.Timestamp("2026-02-01"), "last")
    assert req.tickers == ("A", "B")
    assert req.start == date(2026, 1, 2) and req.end == date(2026, 2, 1)
    assert req.field is Field.LAST


def test_single_string_is_one_ticker():
    assert normalize_tickers("SX5E Index") == ("SX5E Index",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tickers": [], "start": "2026-01-01", "end": "2026-01-02"},
        {"tickers": ["A", ""], "start": "2026-01-01", "end": "2026-01-02"},
        {"tickers": ["A"], "start": "2026-02-01", "end": "2026-01-01"},
        {"tickers": ["A"], "start": "01/02/2026", "end": "2026-01-01"},
        {"tickers": ["A"], "start": "2026-01-01", "end": "2026-01-02", "field": "bid"},
    ],
)
def test_invalid_requests(kwargs):
    with pytest.raises(InvalidRequestError):
        HistoryRequest.create(**kwargs)


def test_to_date_rejects_numbers():
    with pytest.raises(InvalidRequestError):
        to_date(20260101)


def test_clean_series_handles_junk():
    raw = pd.Series(
        ["1.5", None, "x", np.inf, 2.0, 3.0],
        index=["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "not a date", "2026-01-05"],
    )
    out = clean_series(raw)
    assert list(out.index) == [pd.Timestamp("2026-01-05")]
    assert out.iloc[0] == 3.0  # last duplicate wins
    assert out.dtype == "float64" and out.index.name == "date"


def test_clean_series_drops_timezone_keeps_local_date():
    idx = pd.DatetimeIndex(["2026-01-05 23:30"], tz="America/New_York")
    out = clean_series(pd.Series([1.0], index=idx))
    assert out.index[0] == pd.Timestamp("2026-01-05")


def test_assemble_frame_order_and_missing():
    s = pd.Series([1.0], index=pd.to_datetime(["2026-01-05"]))
    frame = assemble_frame({"B": s}, ["A", "B"])
    assert list(frame.columns) == ["A", "B"]
    assert frame["A"].isna().all() and frame.dtypes.eq("float64").all()


def test_last_valid_and_staleness():
    frame = pd.DataFrame(
        {"A": [1.0, 2.0, np.nan], "B": [np.nan, np.nan, np.nan]},
        index=pd.to_datetime(["2026-09-30", "2026-10-01", "2026-10-02"]),
    )
    snap = last_valid(frame)
    assert snap.loc["A", "value"] == 2.0 and snap.loc["A", "as_of"] == pd.Timestamp("2026-10-01")
    assert np.isnan(snap.loc["B", "value"])
    # Monday 5 Oct: Thursday 1 Oct is 2 business days old
    assert stale_columns(frame, "2026-10-05", max_age_bdays=1) == ["A", "B"]
    assert stale_columns(frame, "2026-10-05", max_age_bdays=2) == ["B"]


def test_align_calendars_is_bounded():
    frame = pd.DataFrame({"A": [1.0] + [np.nan] * 4})
    assert align_calendars(frame, max_fill_days=2)["A"].isna().sum() == 2
    with pytest.raises(ValueError):
        align_calendars(frame, -1)
