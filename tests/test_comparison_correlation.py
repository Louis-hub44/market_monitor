import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_monitor.analytics.comparison import compare, max_drawdown, normalise, period_start
from market_monitor.analytics.correlation import (
    Frequency,
    MatrixOrder,
    analyse_correlations,
    cluster_order,
    correlation_matrix,
    required_start,
    rolling_correlation,
    to_returns,
)
from market_monitor.referential import ChangeUnit, referential_from_dicts
from tests.test_referential import BASE

REF = referential_from_dicts(
    {**BASE, "instruments": {
        **BASE["instruments"],
        "A": {"name": "A", "class": "equity", "tickers": {"free": "A"}},
        "B": {"name": "B", "class": "equity", "tickers": {"free": "B"}},
        "C": {"name": "C", "class": "equity", "tickers": {"free": "C"}},
        "D": {"name": "D", "class": "equity", "tickers": {"free": "D"}},
        "VOL": {"name": "Vol", "class": "equity", "quote": "vol", "tickers": {"free": "V"}},
    }}, {})
INST = {i: REF.get(i) for i in ["A", "B", "C", "D", "BUND_10Y", "VOL"]}


# ------------------------------------------------------------------ comparison
def test_period_start():
    as_of = date(2026, 10, 5)
    assert period_start("YTD", as_of) == date(2025, 12, 31)
    assert period_start("1M", as_of) == date(2026, 9, 4)
    assert period_start("1A", as_of) == date(2025, 10, 5)
    with pytest.raises(ValueError):
        period_start("2M", as_of)


def test_normalise_conventions():
    s = pd.Series([2.0, 2.1, 2.05])
    assert normalise(s, ChangeUnit.BP, 100, 2.0).tolist() == pytest.approx([0, 10, 5])
    assert normalise(pd.Series([50.0, 55.0]), ChangeUnit.PCT, None, 50.0).tolist() == [100, 110]
    assert normalise(pd.Series([16.0, 18.5]), ChangeUnit.ABS, None, 16.0).tolist() == [0, 2.5]
    with pytest.raises(ValueError):
        normalise(s, ChangeUnit.PCT, None, 0.0)


def test_compare_common_origin_groups_and_stats():
    idx = pd.to_datetime(["2026-09-01", "2026-09-02", "2026-09-03"])
    levels = pd.DataFrame({"A": [100.0, 110.0, 99.0], "B": [np.nan, 50.0, 55.0],
                           "BUND_10Y": [2.0, 2.1, 2.05]}, index=idx)
    res = compare(levels, [INST["A"], INST["B"], INST["BUND_10Y"]], date(2026, 9, 1), date(2026, 9, 3))
    assert res.base_date == pd.Timestamp("2026-09-02")  # B starts later: everyone rebased there
    assert res.series["A"].dropna().tolist() == pytest.approx([100, 90])
    assert res.series["B"].dropna().tolist() == pytest.approx([100, 110])
    assert res.series["BUND_10Y"].dropna().tolist() == pytest.approx([0, -5])
    assert res.groups == {ChangeUnit.PCT: ["A", "B"], ChangeUnit.BP: ["BUND_10Y"]}
    assert res.stats.loc["A", "change"] == pytest.approx(-10)
    assert res.stats.loc["A", "max_drawdown"] == pytest.approx(-10)
    assert math.isnan(res.stats.loc["BUND_10Y", "max_drawdown"])


def test_compare_reports_missing_and_late_series():
    idx = pd.bdate_range("2026-01-01", "2026-03-31")
    levels = pd.DataFrame({"A": 100.0 + np.arange(len(idx)), "B": np.nan}, index=idx)
    levels.loc[:"2026-02-01", "A"] = np.nan
    res = compare(levels, [INST["A"], INST["B"]], date(2026, 1, 1), date(2026, 3, 31))
    assert "B" in res.errors and "A" in res.warnings


def test_max_drawdown_and_annualised_vol():
    assert max_drawdown(pd.Series([100.0, 120.0, 90.0, 130.0])) == pytest.approx(-25)
    idx = pd.bdate_range("2026-01-01", periods=21)
    yields = pd.Series(2.0 + np.cumsum([0.0] + [0.01, -0.01] * 10), index=idx)  # +-1 bp
    res = compare(pd.DataFrame({"BUND_10Y": yields}), [INST["BUND_10Y"]],
                  date(2026, 1, 1), date(2026, 2, 1))
    expected = np.std([1.0, -1.0] * 10, ddof=1) * math.sqrt(252)
    assert res.stats.loc["BUND_10Y", "ann_vol"] == pytest.approx(expected)


# ----------------------------------------------------------------- correlation
def _correlated_levels(n=300, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    a, d = rng.normal(0, 1, n), rng.normal(0, 1, n)
    price = lambda r: 100 * np.cumprod(1 + r / 100)  # noqa: E731
    return pd.DataFrame({
        "A": price(a), "B": price(2 * a), "C": price(-a), "D": price(d),
        "BUND_10Y": 2.0 + np.cumsum(a) / 100,  # yield moves 1 bp per 1 % of A
    }, index=idx)


def test_returns_follow_conventions():
    levels = _correlated_levels()
    rets = to_returns(levels, [INST["A"], INST["BUND_10Y"]], Frequency.DAILY)
    assert rets["A"].corr(rets["BUND_10Y"]) == pytest.approx(1.0)
    y = levels["BUND_10Y"]
    assert rets["BUND_10Y"].iloc[0] == pytest.approx(100 * (y.iloc[1] - y.iloc[0]))  # bp


def test_holiday_gives_zero_change_then_catches_up():
    idx = pd.bdate_range("2026-03-02", periods=5)
    levels = pd.DataFrame({"A": [100.0, 101.0, np.nan, 103.0, 104.0]}, index=idx)
    rets = to_returns(levels, [INST["A"]], Frequency.DAILY)["A"]
    assert rets.iloc[1] == 0.0 and rets.iloc[2] == pytest.approx(100 * (103 / 101 - 1))


def test_weekly_resampling():
    levels = _correlated_levels(60)
    rets = to_returns(levels, [INST["A"]], Frequency.WEEKLY)
    assert (rets.index.dayofweek == 4).all()
    assert len(rets) == len(levels.resample("W-FRI").last()) - 1


def test_matrix_signs_and_cluster_order():
    levels = _correlated_levels()
    insts = [INST[i] for i in ["A", "D", "C", "B"]]
    rets = to_returns(levels, insts, Frequency.DAILY)
    m = correlation_matrix(rets, 63)
    assert m.loc["A", "B"] == pytest.approx(1.0) and m.loc["A", "C"] == pytest.approx(-1.0)
    assert abs(m.loc["A", "D"]) < 0.4
    order = cluster_order(m)
    assert abs(order.index("A") - order.index("B")) == 1
    with pytest.raises(ValueError):
        correlation_matrix(rets, 2)


def test_analyse_with_lag_order_and_exclusions():
    levels = _correlated_levels()
    levels["D"] = np.nan  # no data: excluded
    insts = [INST[i] for i in ["A", "B", "C", "D"]]
    res = analyse_correlations(levels, insts, date(2026, 12, 31), window=63, lag=21,
                               order=MatrixOrder.CLUSTER)
    assert "D" in res.errors and "D" not in res.matrix.index
    assert res.previous is not None and res.change is not None
    assert res.change.abs().max().max() == pytest.approx(0.0, abs=1e-9)  # perfect links stay perfect
    assert res.end == levels.index[-1]
    series = rolling_correlation(res.returns, "A", "C", 63)
    assert series.dropna().iloc[-1] == pytest.approx(-1.0)


def test_analyse_needs_two_instruments():
    levels = _correlated_levels()
    res = analyse_correlations(levels, [INST["A"]], date(2026, 12, 31), window=63)
    assert res.matrix.shape == (1, 1) and res.previous is None


def test_required_start_is_long_enough():
    as_of = date(2026, 10, 5)
    start = required_start(as_of, 63, 21, Frequency.DAILY, 504)
    assert len(pd.bdate_range(start, as_of)) > 63 + 21 + 504
    weekly = required_start(as_of, 52, 4, Frequency.WEEKLY)
    assert len(pd.date_range(weekly, as_of, freq="W-FRI")) > 56
