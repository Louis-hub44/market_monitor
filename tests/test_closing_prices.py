"""Closes only, and charts that always agree with the bands (morning review, 12/10/2026)."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from market_monitor.export import build_daily_macro
from market_monitor.export.charts import build_charts
from market_monitor.export.layout import ChartSpec, layout_from_dict
from market_monitor.referential import ReferentialError
from market_monitor.sessions import ClosingRule, closing_rule_from_dict
from tests.synthetic import synthetic_monitor
from tests.test_daily_charts import REF

PARIS = ZoneInfo("Europe/Paris")
MONDAY = date(2026, 10, 12)
FRIDAY = pd.Timestamp("2026-10-09")
LAYOUT = {
    "sections": [{"title": "Indices", "instruments": ["SX5E", "NKY"]},
                 {"title": "Taux", "instruments": ["BUND_10Y", "OAT_10Y", "OAT_BUND_10Y"]}],
    "charts": [{"title": "Euro Stoxx 50", "instruments": ["SX5E"], "period": "YTD"},
               {"title": "Bund et OAT", "instruments": ["BUND_10Y", "OAT_10Y"], "period": "5A"},
               {"title": "VIX", "instruments": ["VIX"], "period": "3M"}],
    "movers": {"universe": ["SX5E"]},
}


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(MONDAY, time(hour, minute), tzinfo=PARIS)


@pytest.fixture
def world():
    monitor = synthetic_monitor(REF, end=MONDAY.isoformat())  # serves a print dated today
    return monitor, monitor.service.providers[0].data, layout_from_dict(LAYOUT, monitor.referential)


def test_morning_review_keeps_closes_only(world):
    monitor, data, layout = world
    review = build_daily_macro(monitor, layout, MONDAY, now=_at(9, 0))
    table = review.universe
    # European session open at 9:00: Friday's close, not the live quote of Monday
    assert table.at["SX5E", "level_date"] == FRIDAY
    assert table.at["SX5E", "level"] == pytest.approx(data["SX5E"][FRIDAY])
    assert table.at["OAT_10Y", "level_date"] == FRIDAY
    assert table.at["OAT_BUND_10Y", "level_date"] == FRIDAY            # spread: legs open
    # Tokyo closed at 8:35: Monday's close is final
    assert table.at["NKY", "level_date"] == pd.Timestamp(MONDAY)
    assert {"SX5E", "OAT_10Y", "BUND_10Y", "OAT_BUND_10Y", "VIX"} <= review.provisional
    assert "NKY" not in review.provisional


def test_evening_review_takes_todays_close(world):
    monitor, _, layout = world
    review = build_daily_macro(monitor, layout, MONDAY, now=_at(18, 5))
    assert review.universe.at["SX5E", "level_date"] == pd.Timestamp(MONDAY)
    assert "SX5E" not in review.provisional and "VIX" in review.provisional  # US still open


def test_past_review_date_is_never_trimmed(world):
    monitor, _, layout = world
    review = build_daily_macro(monitor, layout, date(2026, 10, 9), now=_at(9, 0))
    assert not review.provisional and review.universe.at["SX5E", "level_date"] == FRIDAY


def test_charts_show_the_band_level_even_if_the_source_moved(world):
    """The reported bug: chart 6 222,60 vs band 6 173,37 - two reads at different times."""
    monitor, data, layout = world
    review = build_daily_macro(monitor, layout, MONDAY, now=_at(9, 0))
    data["SX5E"] = data["SX5E"] * 1.008      # the live quote moved before the charts were built
    data["BUND_10Y"] = data["BUND_10Y"] + 0.044
    charts = build_charts(monitor, layout.charts, MONDAY, base=review.levels,
                          provisional=review.provisional)
    sx5e, rates, vix = charts
    assert sx5e.series[0].last == pytest.approx(review.universe.at["SX5E", "level"])
    assert sx5e.series[0].last_date == FRIDAY
    bund = next(s for s in rates.series if s.id == "BUND_10Y")
    assert bund.last == pytest.approx(review.universe.at["BUND_10Y", "level"])
    # 5-year window: the older part is loaded, the recent part is the band's
    assert bund.values.index.min() < review.levels.index.min()
    # VIX is not in the bands: loaded, and its open session dropped too
    assert vix.series[0].last_date == FRIDAY


def test_charts_without_base_still_drop_open_sessions(world):
    monitor, _, _ = world
    (chart,) = build_charts(monitor, [ChartSpec("SX5E", ("SX5E",), "1M")], MONDAY,
                            provisional={"SX5E"})
    assert chart.series[0].last_date == FRIDAY


def test_closing_rule_resolution_and_parsing():
    rule = ClosingRule()
    assert rule.close_time(REF.get("SX5E")) == time(17, 45)
    assert rule.close_time(REF.get("SPX")) == time(23, 5)        # group États-Unis
    assert rule.close_time(REF.get("CSI300")) == time(9, 5)      # instrument
    assert rule.close_time(REF.get("BRENT")) == time(20, 45)     # class
    custom = closing_rule_from_dict({"default": "17:35", "groups": {"Europe": 1060},  # 17:40
                                     "instruments": {"SX5E": "18:00"}})
    assert custom.close_time(REF.get("SX5E")) == time(18, 0)
    assert custom.close_time(REF.get("CAC")) == time(17, 40)
    assert custom.close_time(REF.get("NKY")) == time(8, 35)      # defaults kept
    assert not closing_rule_from_dict(False).open_sessions(REF, ["SX5E"], _at(9, 0))
    assert closing_rule_from_dict(None) == ClosingRule() and hash(custom) != hash(ClosingRule())


@pytest.mark.parametrize("raw", ["yes", {"default": "25:00"}, {"classes": ["x"]}, {"cutoff": "9:00"}])
def test_closing_rule_validation(raw):
    with pytest.raises(ReferentialError):
        closing_rule_from_dict(raw)


def test_repository_layout_enables_closes_only():
    from market_monitor.export.layout import load_layout
    from tests.test_daily_charts import CONFIG_DIR

    layout = load_layout(CONFIG_DIR / "daily_macro.yaml", REF)
    assert layout.closing.enabled and layout.closing.close_time(REF.get("OAT_10Y")) == time(18, 0)
