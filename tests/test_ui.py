import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from market_monitor.analytics.performance import Horizon, PerformanceEngine, rank_movers
from market_monitor.text_format import (
    format_change,
    fr_number,
    french_date,
    level_decimals,
)
from market_monitor.ui import theme
from market_monitor.ui.charts import heat_tiles
from market_monitor.ui.formatting import (
    display_table,
    style_table,
)
from tests.test_performance import AS_OF, REF, _levels


def _table():
    table = PerformanceEngine().compute(_levels(), [REF.get("SX5E"), REF.get("BUND_10Y")], AS_OF)
    table["source"] = ["bloomberg", "free"]
    table["proxy"] = [None, "some ETF"]
    return table


# ------------------------------------------------------------------ formatting
def test_french_numbers_and_dates():
    assert fr_number(1234.5, 2) == "1\u202f234,50"
    assert fr_number(-0.5, 1, sign=True) == "−0,5"
    assert fr_number(math.nan, 2) == "–"
    assert format_change(5, "bp") == "+5,0\u202fpb"
    assert format_change(1.8182, "pct") == "+1,82\u202f%"
    assert french_date(date(2026, 10, 5)) == "lundi 5 octobre 2026"


@pytest.mark.parametrize("args,expected", [
    (("rates", "yield", 2.7), 3), (("rates", "spread", 70), 1), (("fx", "price", 1.17), 4),
    (("fx", "price", 147.2), 2), (("crypto", "price", 62000), 0), (("equity", "price", 5600), 2),
])
def test_level_precision(args, expected):
    assert level_decimals(*args) == expected


def test_display_table_units_and_sources():
    table = _table()
    rates = display_table(table.loc[["BUND_10Y"]])
    assert "1J (pb)" in rates.columns and rates.iloc[0]["1J (pb)"] == "+5,0"
    assert rates.iloc[0]["Niveau"] == "2,700" and rates.iloc[0]["Source"] == "Libre (proxy)"
    mixed = display_table(table)  # pct + bp: units move into the cells
    assert "1J" in mixed.columns and mixed.loc["BUND_10Y", "1J"].endswith("pb")


def test_style_table_renders_colours():
    table = _table()
    html = style_table(display_table(table), table).to_html()
    assert theme.UP.lower() in html.lower()  # positive changes in teal


# ---------------------------------------------------------------------- theme
def test_heat_color_scale():
    assert theme.heat_color(0.0) == theme.NEUTRAL_TILE.lower()
    assert theme.heat_color(10.0) == theme.UP.lower()
    assert theme.heat_color(-3.0) == theme.DOWN.lower()
    assert theme.heat_color(math.nan) == theme.NEUTRAL_TILE  # untouched base
    scale = theme.plotly_colorscale()
    assert scale[0][0] == 0 and scale[-1][0] == 1 and scale[5][1] == theme.NEUTRAL_TILE.lower()


# --------------------------------------------------------------------- charts
def test_heat_tiles_layout():
    table = _table()
    fig = heat_tiles(table, Horizon.D1, columns=4)
    assert len(fig.data) == 2  # one panel per asset class (equity, rates)
    assert [a.text for a in fig.layout.annotations] == ["Actions", "Taux"]
    first = fig.data[0]
    assert first.z[0][0] == 0.0  # change without reliable z-score: neutral tile, still shown
    assert "Euro Stoxx 50" in first.text[0][0] and first.z[0][1] is None  # padding
    with pytest.raises(ValueError):
        heat_tiles(table, Horizon.YTD)


def test_rank_movers_excludes_stale_and_missing():
    table = pd.DataFrame({"z_1d": [0.5, -3.0, math.nan, 4.0], "stale": [False, False, False, True]},
                         index=list("ABCD"))
    assert list(rank_movers(table, Horizon.D1, 5).index) == ["B", "A"]
    with pytest.raises(ValueError):
        rank_movers(table, Horizon.MTD)


# ------------------------------------------------------------------- full page
def test_streamlit_page_renders():
    at = AppTest.from_file(str(Path(__file__).with_name("ui_fake_app.py")), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Market Monitor" in text and "Arrêté au" in text
    assert len(at.dataframe) >= 2  # equity and rates tables
    at.sidebar.selectbox[0].select("Tout").run()
    assert not at.exception
    assert "Mouvements marquants" in " ".join(m.value for m in at.markdown)


# ------------------------------------------------------------ phase 4 charts
def test_comparison_figure_panels():
    from market_monitor.analytics.comparison import compare
    from market_monitor.ui.charts import comparison_figure

    levels = _levels()
    res = compare(levels, [REF.get("SX5E"), REF.get("BUND_10Y")], date(2025, 12, 1), AS_OF)
    fig = comparison_figure(res, {"SX5E": "Euro Stoxx 50", "BUND_10Y": "Bund 10 ans"})
    assert [a.text for a in fig.layout.annotations] == [
        "Prix, base 100", "Taux et spreads, variation cumulée (pb)"]
    assert [t.name for t in fig.data] == ["Euro Stoxx 50", "Bund 10 ans"]
    assert fig.data[0].y[0] == pytest.approx(100.0) and fig.data[1].y[0] == 0.0


def test_correlation_heatmap_blank_diagonal():
    from market_monitor.ui.charts import correlation_heatmap, rolling_correlation_figure

    m = pd.DataFrame([[1.0, 0.5], [0.5, 1.0]], index=["A", "B"], columns=["A", "B"])
    fig = correlation_heatmap(m, {"A": "Actif A"})
    heat = fig.data[0]
    assert math.isnan(heat.z[0][0]) and heat.text[0][1] == "0,50"
    assert list(heat.x) == ["Actif A", "B"] and heat.zmin == -1
    change = correlation_heatmap(m - 0.6, {}, change=True).data[0]
    assert change.zmax == 0.5 and change.text[0][1] == "−0,10"
    roll = rolling_correlation_figure(pd.Series([0.1, None, 0.3]), "A / B")
    assert len(roll.data[0].y) == 2


def test_stats_table_formatting():
    from market_monitor.analytics.comparison import compare
    from market_monitor.ui.history_view import stats_table

    res = compare(_levels(), [REF.get("SX5E"), REF.get("BUND_10Y")], date(2025, 12, 1), AS_OF)
    table = stats_table(res, REF)
    assert table.loc[0, "Variation"] == "+14,29\u202f%"
    assert table.loc[1, "Variation"] == "+20,0\u202fpb" and table.loc[1, "Perte max."] == "–"


def test_streamlit_history_and_correlation_views():
    at = AppTest.from_file(str(Path(__file__).with_name("ui_fake_app.py")), default_timeout=60)
    at.run()
    at.segmented_control(key="mm-view").set_value("history").run()
    assert not at.exception, at.exception
    assert any("Origine commune" in c.value for c in at.caption)
    assert len(at.dataframe) == 1  # period statistics
    at.segmented_control(key="mm-view").set_value("correlations").run()
    assert not at.exception, at.exception
    assert any("Corrélations de Pearson" in c.value for c in at.caption)
    at.toggle(key="mm-corr-change").set_value(True).run()
    assert not at.exception, at.exception


def test_streamlit_daily_macro_view():
    at = AppTest.from_file(str(Path(__file__).with_name("ui_fake_app.py")), default_timeout=60)
    at.run()
    at.segmented_control(key="mm-view").set_value("daily").run()
    assert not at.exception, at.exception
    assert any("Texte prêt à coller" in m.value for m in at.markdown)
    assert len(at.get("download_button")) == 3
    assert at.code[0].value.startswith("Revue de marché, arrêté au")


def test_streamlit_alerts_strip_and_view():
    at = AppTest.from_file(str(Path(__file__).with_name("ui_fake_app.py")), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    page_text = " ".join(m.value for m in at.markdown)
    assert "dont 1 critique" in page_text and "au-dessus de 50,0" in page_text
    at.segmented_control(key="mm-view").set_value("alerts").run()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric] == ["Critique", "Attention", "Info"]
    assert at.metric[0].value == "1"
    rules = at.dataframe[0].value
    assert list(rules["Règle"]) == ["OAT-Bund > 50 pb", "z"]


def test_describe_condition():
    from market_monitor.alerts import rules_from_dict
    from market_monitor.ui.alerts_view import describe_condition
    from tests.test_history_monitor import REF as FULL_REF

    rules = rules_from_dict({"rules": [
        {"id": "a", "type": "zscore", "instrument": "SX5E", "min_abs_z": 2, "critical_abs_z": 3,
         "direction": "down"},
        {"id": "b", "type": "level", "instrument": "OAT_BUND", "below": 0, "cross": True},
        {"id": "c", "type": "change", "instrument": "SX5E", "horizon": "mtd", "abs_above": 5},
        {"id": "d", "type": "stale", "instrument": "SX5E"},
    ]}, FULL_REF)
    assert [describe_condition(r) for r in rules] == [
        "|z 1J| ≥ 2 (critique ≥ 3), baisse seulement", "niveau < 0, franchissement",
        "variation MTD |·| > 5", "cotation périmée"]


def test_rows_to_check_are_marked():
    table = _table()
    table["suspect"] = [True, False]
    table["roll_1d"] = [False, False]
    shown = display_table(table)
    assert shown.loc["SX5E", "Instrument"] == "Euro Stoxx 50 †"
    assert shown.loc["BUND_10Y", "Instrument"] == "Bund 10Y"
    html = style_table(shown, table).to_html()
    assert theme.CHECK_BG.lower() in html.lower()
    fig = heat_tiles(table, Horizon.D1, columns=4)
    assert "†" in fig.data[0].text[0][0] and fig.data[0].z[0][0] == 0.0
