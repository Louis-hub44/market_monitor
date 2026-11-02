"""Phase 4 (v1.6): Koyfin-style overview - 1M / 3M / 1A, 52-week range, sparklines, row click."""

import base64
import math
from datetime import date

import pandas as pd
import pytest

from market_monitor.analytics.performance import (
    EXPORT_HORIZONS,
    OVERVIEW_HORIZONS,
    Horizon,
    PerformanceEngine,
    range_52w,
    required_start,
    target_date,
)
from market_monitor.config import AnalyticsSettings
from market_monitor.export.excel import DATA_COLUMNS
from market_monitor.referential import referential_from_dicts
from market_monitor.ui import page
from market_monitor.ui.formatting import (
    FROM_HIGH_COLUMN,
    RANGE_COLUMN,
    SPARK_COLUMN,
    overview_table,
    range_svg,
    sparkline,
    style_overview,
)
from tests.test_referential import BASE

REF = referential_from_dicts(BASE, {})
AS_OF = date(2026, 10, 5)


def _year(values_start: float, step: float) -> pd.Series:
    index = pd.bdate_range("2025-09-01", "2026-10-02")
    return pd.Series(values_start + step * pd.RangeIndex(len(index)), index=index, dtype="float64")


def test_new_horizons_targets_and_changes():
    assert target_date(Horizon.M1, date(2026, 10, 2), AS_OF) == date(2026, 9, 2)
    assert target_date(Horizon.M3, date(2026, 10, 2), AS_OF) == date(2026, 7, 2)
    assert target_date(Horizon.Y1, date(2026, 10, 2), AS_OF) == date(2025, 10, 2)
    assert target_date(Horizon.M1, date(2026, 3, 31), AS_OF) == date(2026, 2, 28)
    bund = _year(2.0, 0.001)                              # +0,1 pb per session
    levels = pd.DataFrame({"BUND_10Y": bund, "SX5E": _year(5000.0, 1.0)})
    table = PerformanceEngine().compute(levels, [REF.get("BUND_10Y"), REF.get("SX5E")], AS_OF)
    row = table.loc["BUND_10Y"]
    sessions = {h: int((bund.index > pd.Timestamp(target_date(h, date(2026, 10, 2), AS_OF))).sum())
                for h in (Horizon.M1, Horizon.M3, Horizon.Y1)}
    for horizon, n in sessions.items():
        assert row[f"chg_{horizon}"] == pytest.approx(0.1 * n)
    sx5e = table.loc["SX5E"]
    ref = levels["SX5E"].loc[:"2025-10-02"].iloc[-1]
    assert sx5e["chg_1y"] == pytest.approx(100 * (levels["SX5E"].iloc[-1] / ref - 1))
    assert sx5e["ref_date_1y"] == pd.Timestamp("2025-10-02")


def test_range_52w_position_and_distance():
    spx = REF.get("SX5E")
    closes = pd.Series([100.0, 150.0, 80.0, 120.0], index=pd.bdate_range("2026-09-01", periods=4))
    out = range_52w(closes, spx)
    assert (out["high_52w"], out["low_52w"]) == (150.0, 80.0)
    assert out["pos_52w"] == pytest.approx(40 / 70) and out["from_high_52w"] == pytest.approx(-20.0)
    bund = range_52w(pd.Series([2.0, 2.5, 2.4], index=pd.bdate_range("2026-09-01", periods=3)),
                     REF.get("BUND_10Y"))
    assert bund["from_high_52w"] == pytest.approx(-10.0)          # in bp
    old = pd.Series([500.0, 100.0], index=pd.to_datetime(["2024-01-02", "2026-10-02"]))
    assert range_52w(old, spx)["high_52w"] == 100.0                # older than 52 weeks: ignored
    flat = range_52w(pd.Series([1.0, 1.0], index=pd.bdate_range("2026-09-01", periods=2)), spx)
    assert math.isnan(flat["pos_52w"])
    assert math.isnan(range_52w(pd.Series(dtype="float64"), spx)["high_52w"])


def test_export_horizons_and_load_window_unchanged_for_the_daily_macro():
    assert EXPORT_HORIZONS == (Horizon.D1, Horizon.W1, Horizon.MTD, Horizon.YTD)
    assert sum(c.startswith("Réf. ") for c in DATA_COLUMNS) == 4
    assert [h.value for h in OVERVIEW_HORIZONS] == ["1d", "1w", "1m", "3m", "ytd", "1y"]
    start = required_start(AS_OF, AnalyticsSettings())
    assert start <= date(2025, 9, 18)                              # 1A reference + gap


def _table():
    levels = pd.DataFrame({"BUND_10Y": _year(2.0, 0.001), "SX5E": _year(5000.0, 1.0)})
    table = PerformanceEngine().compute(levels, [REF.get("BUND_10Y"), REF.get("SX5E")], AS_OF)
    return table, levels


def test_overview_table_columns_and_cells():
    table, levels = _table()
    rates = overview_table(table.loc[["BUND_10Y"]], levels)
    assert list(rates.columns) == ["Groupe", "Instrument", "Dernier", "1J (pb)", "1S (pb)", "1M (pb)",
                                   "3M (pb)", "YTD (pb)", "1A (pb)", SPARK_COLUMN, RANGE_COLUMN,
                                   FROM_HIGH_COLUMN, "z 1J"]
    row = rates.iloc[0]
    assert row["Dernier"].endswith("%") and row["1J (pb)"] == "+0,1" and row[FROM_HIGH_COLUMN] == "+0,0"
    assert isinstance(row[SPARK_COLUMN], list) and 18 <= len(row[SPARK_COLUMN]) <= 24
    svg = base64.b64decode(row[RANGE_COLUMN].split(",", 1)[1]).decode()
    assert svg.startswith("<svg") and "2,284<" in svg               # high of the year, French format
    mixed = overview_table(table, levels)                           # % and pb: units in the cells
    assert "1J" in mixed.columns and mixed.loc["SX5E", "1J"].endswith("%")
    html = style_overview(mixed, table).to_html()
    assert "background-color" in html


def test_stale_rows_show_their_date():
    table, levels = _table()
    table.loc["SX5E", "stale"] = True
    assert overview_table(table, levels).loc["SX5E", "Dernier"].endswith("(02/10)")


def test_sparkline_and_range_svg_edge_cases():
    _, levels = _table()
    assert sparkline(None, "SX5E", pd.Timestamp("2026-10-02")) is None
    assert sparkline(levels, "NONE", pd.Timestamp("2026-10-02")) is None
    assert sparkline(levels, "SX5E", pd.NaT) is None
    assert range_svg(float("nan"), 1.0, 0.5, "equity", "price") is None
    big = base64.b64decode(range_svg(4000.0, 6000.0, 1.0, "equity", "price").split(",", 1)[1]).decode()
    assert "6 000<" in big and "#F0A830" in big                   # thousands, no decimals; amber at the high


def test_row_click_opens_the_instrument_page(monkeypatch):
    state = {"mm-table-rates": {"selection": {"rows": [1]}}}
    monkeypatch.setattr(page.st, "session_state", state)
    page._open_selected("mm", "mm-table-rates", ["BUND_10Y", "OAT_10Y"])
    assert state["mm-view"] == "instrument" and state["mm-fiche-id"] == "OAT_10Y"
    assert "mm-table-rates" not in state                            # same row can open it again
    page._open_selected("mm", "mm-table-rates", ["BUND_10Y"])       # nothing selected: no-op
    assert state["mm-fiche-id"] == "OAT_10Y"
