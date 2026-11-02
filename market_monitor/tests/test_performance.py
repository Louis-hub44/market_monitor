import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_monitor.analytics.performance import (
    Horizon,
    PerformanceEngine,
    change,
    daily_changes,
    required_start,
    target_date,
    zscore,
)
from market_monitor.config import AnalyticsSettings
from market_monitor.referential import ChangeUnit, referential_from_dicts
from tests.test_referential import BASE

AS_OF = date(2026, 10, 5)  # Monday
REF = referential_from_dicts(BASE, {})


BUND = {"2025-12-31": 2.50, "2026-09-25": 2.55, "2026-09-30": 2.60,
        "2026-10-01": 2.65, "2026-10-02": 2.70}
SX5E = {"2025-12-31": 4900.0, "2026-09-25": 5450.0, "2026-09-30": 5480.0,
        "2026-10-01": 5500.0, "2026-10-02": 5600.0}


def _levels():
    return pd.DataFrame({"BUND_10Y": BUND, "SX5E": SX5E}).set_axis(pd.to_datetime(list(BUND)))


def test_hand_computed_example():
    table = PerformanceEngine().compute(_levels(), [REF.get("BUND_10Y"), REF.get("SX5E")], AS_OF)
    bund, sx5e = table.loc["BUND_10Y"], table.loc["SX5E"]
    assert bund["level"] == 2.70 and bund["level_date"] == pd.Timestamp("2026-10-02")
    assert bund[["chg_1d", "chg_1w", "chg_mtd", "chg_ytd"]].tolist() == pytest.approx([5, 15, 10, 20])
    assert sx5e["chg_1d"] == pytest.approx(100 * (5600 / 5500 - 1))   # +1.818 %
    assert sx5e["chg_ytd"] == pytest.approx(100 * (5600 / 4900 - 1))  # +14.286 %
    assert not bund["stale"] and bund["change_unit"] == "bp"
    assert math.isnan(bund["z_1d"])  # not enough history for a z-score


def test_target_dates():
    assert target_date(Horizon.W1, date(2026, 10, 2), AS_OF) == date(2026, 9, 25)
    assert target_date(Horizon.MTD, date(2026, 10, 2), AS_OF) == date(2026, 9, 30)
    assert target_date(Horizon.YTD, date(2026, 10, 2), AS_OF) == date(2025, 12, 31)
    with pytest.raises(ValueError):
        target_date(Horizon.D1, date(2026, 10, 2), AS_OF)


def test_mtd_is_zero_on_first_day_before_any_new_print():
    table = PerformanceEngine().compute(_levels(), [REF.get("BUND_10Y")], date(2026, 10, 1))
    # as of 1 Oct the Oct-1 print is used (<= as_of), MTD vs 30 Sep
    assert table.loc["BUND_10Y", "chg_mtd"] == pytest.approx(5)
    levels = _levels().loc[:"2026-09-30"]
    table = PerformanceEngine().compute(levels, [REF.get("BUND_10Y")], date(2026, 10, 1))
    assert table.loc["BUND_10Y", "chg_mtd"] == 0.0


def test_reference_gap_guard_and_staleness():
    levels = _levels().rename(index={pd.Timestamp("2025-12-31"): pd.Timestamp("2025-12-15")})
    table = PerformanceEngine().compute(levels, [REF.get("BUND_10Y")], date(2026, 10, 20))
    row = table.loc["BUND_10Y"]
    assert math.isnan(row["chg_ytd"])  # last 2025 print is 16 days before 31 Dec
    assert row["stale"]                # last print 12 business days old


def test_missing_instrument_row():
    table = PerformanceEngine().compute(pd.DataFrame(), [REF.get("SX5E")], AS_OF)
    assert table.loc["SX5E", "stale"] and math.isnan(table.loc["SX5E", "level"])


def test_change_conventions():
    assert change(110, 100, ChangeUnit.PCT, None) == pytest.approx(10)
    assert math.isnan(change(1, -1, ChangeUnit.PCT, None))
    assert change(2.75, 2.70, ChangeUnit.BP, 100) == pytest.approx(5)
    assert change(80, 75, ChangeUnit.BP, 1) == 5
    assert change(18.5, 16.0, ChangeUnit.ABS, None) == 2.5
    with pytest.raises(ValueError):
        change(1, 2, ChangeUnit.BP, None)
    s = pd.Series([100.0, 110.0, 99.0])
    assert daily_changes(s, ChangeUnit.PCT, None).tolist() == pytest.approx([10, -10])


def test_zscore_formula_and_scaling():
    history = pd.Series([1.0, -1.0] * 50)  # mu = 0, sigma = sqrt(100/99)
    sigma = math.sqrt(100 / 99)
    settings = AnalyticsSettings(zscore_window=252, zscore_min_obs=60)
    assert zscore(3.0, 1, history, settings) == pytest.approx(3 / sigma)
    assert zscore(4.0, 4, history, settings) == pytest.approx(4 / (sigma * 2))
    assert math.isnan(zscore(3.0, 1, history.head(10), settings))
    assert math.isnan(zscore(3.0, 1, pd.Series([1.0] * 100), settings))  # sigma = 0
    drift = history + 1.0
    demeaned = zscore(3.0, 1, drift, settings)
    raw = zscore(3.0, 1, drift, AnalyticsSettings(zscore_demean=False))
    assert demeaned == pytest.approx(2 / sigma)  # (x - mu) / sigma, mu = 1
    assert raw == pytest.approx(3 / sigma)       # sigma is shift-invariant


def test_zscore_is_out_of_sample():
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2025-06-02", "2026-10-02")
    bp = np.cumsum(rng.normal(0, 3, len(idx))) / 100 + 2.5   # ~3 bp daily vol
    bp[-1] = bp[-2] + 0.30                                   # +30 bp shock on the last day
    levels = pd.DataFrame({"BUND_10Y": bp}, index=idx)
    row = PerformanceEngine().compute(levels, [REF.get("BUND_10Y")], AS_OF).loc["BUND_10Y"]
    assert row["chg_1d"] == pytest.approx(30)
    assert row["z_1d"] > 8  # the shock does not inflate its own sigma


def test_required_start_covers_ytd_and_window():
    start = required_start(AS_OF, AnalyticsSettings())
    assert start <= date(2025, 12, 24)
    assert len(pd.bdate_range(start, AS_OF)) > 252 + 5
