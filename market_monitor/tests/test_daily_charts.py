from datetime import date

import pandas as pd
import pytest

from market_monitor.config import settings_from_dict
from market_monitor.data.providers.public import FredApiClient, parse_fred_observations
from market_monitor.exceptions import DataProviderError
from market_monitor.export import export_daily_macro
from market_monitor.export.charts import (
    ChartData,
    ChartSeries,
    axis_decimals,
    build_charts,
    chart_window,
    charts_png_bytes,
    date_ticks,
    period_extremes,
)
from market_monitor.export.layout import ChartSpec, layout_from_dict, load_layout
from market_monitor.referential import ReferentialError, load_referential
from market_monitor.ui.charts import daily_chart_figure
from market_monitor.ui.daily_macro_view import chart_header_html
from tests.conftest import FakeResponse, FakeSession
from tests.synthetic import synthetic_monitor
from tests.test_config_factory import REPO_CONFIG

CONFIG_DIR = REPO_CONFIG.parent
AS_OF = date(2026, 10, 6)
REF = load_referential(CONFIG_DIR / "instruments.yaml", CONFIG_DIR / "watchlists.yaml")
SECTIONS = [{"title": "Indices", "instruments": ["SX5E"]}]


@pytest.fixture(scope="module")
def monitor():
    return synthetic_monitor(REF, end=AS_OF.isoformat())


# ---------------------------------------------------------------- referential
def test_new_credit_lines():
    hy_ig = REF.get("HY_IG_EUR")
    assert hy_ig.derived.legs == (("EUR_HY_OAS", 1.0), ("EUR_IG_OAS", -1.0)) and hy_ig.unit == "pb"
    assert REF.get("HY_IG_US").derived.legs == (("US_HY_OAS", 1.0), ("US_IG_OAS", -1.0))
    g = REF.get("G_SPREAD_EUR_IG")
    assert g.derived.legs == (("EUR_IG_YLD", 1.0), ("BUND_5Y", -1.0)) and g.derived.multiplier == 100
    us_hy = REF.get("US_HY_OAS").tickers["free"]
    assert us_hy.ticker == "fred:BAMLH0A0HYM2|fredapi:BAMLH0A0HYM2" and us_hy.scale == 100
    assert "free" not in REF.get("EUR_IG_OAS").tickers  # no reliable free source
    for i in ("EUR_IG_OAS", "EUR_IG_YLD", "EUR_HY_OAS", "US_HY_OAS", "US_IG_OAS"):
        assert "manual" in REF.get(i).tickers


def test_repository_layout_has_spreads_and_charts():
    layout = load_layout(CONFIG_DIR / "daily_macro.yaml", REF)
    spreads = next(s for s in layout.sections if s.title == "Spreads")
    assert {"HY_IG_EUR", "HY_IG_US", "G_SPREAD_EUR_IG"} <= set(spreads.instruments)
    assert [c.instruments for c in layout.charts] == [
        ("SX5E",), ("SPX",), ("BUND_10Y", "OAT_10Y"), ("EUR_HY_OAS", "US_HY_OAS"), ("BRENT",), ("VIX",)]
    assert layout.charts[2].labels == ("Bund", "OAT")
    assert layout.charts[3].labels == ("Euro HY", "US HY")
    assert [c.period for c in layout.charts] == ["YTD"] * 5 + ["3M"]


# --------------------------------------------------------------------- layout
def test_chart_options():
    layout = layout_from_dict({"sections": SECTIONS, "charts": [
        {"title": "A", "instruments": "SX5E", "period": "6m"},
        {"title": "B", "instruments": ["HY_IG_EUR", "HY_IG_US"], "start": "2026-03-01",
         "labels": ["Euro"]},
    ]}, REF)
    a, b = layout.charts
    assert a.period == "6M" and a.instruments == ("SX5E",)
    assert b.start == date(2026, 3, 1) and b.label(0, "x") == "Euro" and b.label(1, "US") == "US"
    assert chart_window(a, AS_OF) == (date(2026, 4, 6), AS_OF)
    assert chart_window(b, AS_OF) == (date(2026, 3, 1), AS_OF)
    assert chart_window(ChartSpec("C", ("SX5E",), "YTD"), AS_OF) == (date(2025, 12, 31), AS_OF)


@pytest.mark.parametrize("chart", [
    {"title": "x", "instruments": ["SX5E"], "period": "2S"},
    {"title": "x", "instruments": ["SX5E"], "start": "06/10/2026"},
    {"title": "x", "instruments": ["SX5E"], "colour": "red"},
    {"title": "x", "instruments": ["SX5E", "SPX", "CAC", "DAX", "NKY"]},
    {"title": "x", "instruments": ["SX5E"], "labels": ["a", "b"]},
    {"title": "x", "instruments": ["NOPE"]},
    {"instruments": ["SX5E"]},
])
def test_chart_validation(chart):
    with pytest.raises(ReferentialError):
        layout_from_dict({"sections": SECTIONS, "charts": [chart]}, REF)


# ----------------------------------------------------------------------- data
def test_build_charts_windows_and_daily_moves(monitor):
    specs = [ChartSpec("SX5E", ("SX5E",), "YTD"), ChartSpec("Bund", ("BUND_10Y",), "6M"),
             ChartSpec("HY-IG", ("HY_IG_EUR", "HY_IG_US"), "1M", labels=("Euro", "US"))]
    sx5e, bund, hy_ig = build_charts(monitor, specs, AS_OF)
    first = sx5e.series[0]
    assert first.values.index.min() >= pd.Timestamp("2025-12-31")
    assert first.values.index.max() <= pd.Timestamp(AS_OF)
    prev, last = first.values.iloc[-2:]
    assert first.change_1d == pytest.approx(100 * (last / prev - 1))       # price: %
    b = bund.series[0]
    assert b.change_1d == pytest.approx(100 * (b.values.iloc[-1] - b.values.iloc[-2]))  # yield: bp
    assert b.change_text().endswith("pb") and b.level_text().endswith("%")
    assert [s.label for s in hy_ig.series] == ["Euro", "US"] and hy_ig.unit_label == "pb"
    assert hy_ig.series[0].values.index.min() >= pd.Timestamp("2026-09-06")
    assert sx5e.period_label == "YTD" and bund.period_label == "6 mois"


def test_build_charts_overrides_and_missing_data(monitor):
    specs = [ChartSpec("SX5E", ("SX5E",), "YTD")]
    (chart,) = build_charts(monitor, specs, AS_OF, {0: ChartSpec("SX5E", ("SX5E",), "1M")})
    assert chart.start == date(2026, 9, 5)
    empty = ChartData(ChartSpec("x", ("SX5E",), start=date(2026, 1, 1)), date(2026, 1, 1), AS_OF, ())
    assert empty.period_label == "depuis le 01/01/2026" and empty.unit_label == ""


def test_png_and_plotly(monitor):
    charts = build_charts(monitor, [ChartSpec("SX5E", ("SX5E",)),
                                    ChartSpec("HY-IG", ("HY_IG_EUR", "HY_IG_US"), "6M")], AS_OF)
    png = charts_png_bytes(charts, dpi=60)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    fig = daily_chart_figure(charts[1])
    assert len(fig.data) == 4  # two lines + two "last session" overlays
    assert [t.name for t in fig.data if t.showlegend is not False] == [
        "Spread HY-IG Euro", "Spread HY-IG US"]
    assert fig.layout.showlegend is True
    tags = [a for a in fig.layout.annotations if a.x == 1 and a.xanchor == "left"]
    assert len(tags) == 2 and all("pb" in a.text for a in tags)  # last level on the right axis
    assert len(fig.layout.shapes) == 2  # dotted line at each last level
    single = daily_chart_figure(charts[0])
    assert single.data[0].fill == "tozeroy" and single.layout.showlegend is False
    low, high = single.layout.yaxis.range
    values = charts[0].series[0].values
    assert low < values.min() and high > values.max() and low > 0  # fitted, not from zero
    html = chart_header_html(charts[1])
    assert "Spread HY-IG Euro" in html and "pb" in html and "6 mois" in html


def test_date_ticks_follow_the_window():
    def labels(start, end):
        return [label for _, label in date_ticks(date.fromisoformat(start), date.fromisoformat(end))]

    assert labels("2025-12-31", "2026-10-06")[0] == "janv. 2026"            # YTD: months
    three_months = date_ticks(date(2026, 7, 6), date(2026, 10, 6))         # 3M: Mondays
    assert 5 <= len(three_months) <= 7 and all(t.weekday() == 0 for t, _ in three_months)
    assert three_months[0][1] == "13 juil."
    one_month = labels("2026-09-06", "2026-10-06")
    assert one_month == ["7 sept.", "14 sept.", "21 sept.", "28 sept.", "5 oct."]
    assert labels("2026-09-28", "2026-10-06")[-1] == "6 oct."             # days
    assert labels("2021-10-06", "2026-10-06") == ["2022", "2023", "2024", "2025", "2026"]


def test_axis_decimals_and_extremes():
    vix = REF.get("VIX")
    values = pd.Series([15.2, 22.4, 14.1, 16.0],
                       index=pd.to_datetime(["2026-09-01", "2026-09-12", "2026-09-20", "2026-10-06"]))
    chart = ChartData(ChartSpec("VIX", ("VIX",), "3M"), date(2026, 7, 6), date(2026, 10, 6),
                      (ChartSeries("VIX", "VIX", values, vix),))
    assert axis_decimals(chart) == 1                       # 8,3 points over the window
    (high_day, high), (low_day, low) = period_extremes(values)
    assert (high_day, high, low_day, low) == (pd.Timestamp("2026-09-12"), 22.4,
                                              pd.Timestamp("2026-09-20"), 14.1)
    fig = daily_chart_figure(chart)
    texts = [t.text[0] for t in fig.data if t.mode == "markers+text"]
    assert texts == ["22,40 (12/09)", "14,10 (20/09)"]
    assert fig.layout.yaxis.tickformat == ",.1f" and fig.layout.xaxis.showspikes


def test_export_writes_charts_png(monitor, tmp_path):
    layout = load_layout(CONFIG_DIR / "daily_macro.yaml", REF)
    result = export_daily_macro(monitor, layout, tmp_path, AS_OF)
    assert result.charts is not None and result.charts.name.endswith("_graphiques.png")
    assert result.charts.read_bytes()[:4] == b"\x89PNG"


def test_chart_series_change_conventions():
    vix = REF.get("VIX")
    series = ChartSeries("VIX", "VIX", pd.Series([20.0, 18.5], index=pd.to_datetime(["2026-10-05",
                                                                                     "2026-10-06"])), vix)
    assert series.change_1d == pytest.approx(-1.5)  # vol: points
    single = ChartSeries("VIX", "VIX", series.values.iloc[-1:], vix)
    assert pd.isna(single.change_1d)


# ------------------------------------------------------------------ FRED API
def test_fred_api_needs_a_key_and_hides_it():
    with pytest.raises(DataProviderError, match="FRED_API_KEY"):
        FredApiClient(api_key="", session=FakeSession([])).fetch("X", AS_OF, AS_OF)
    payload = {"observations": [{"date": "2026-10-05", "value": "3.05"},
                                {"date": "2026-10-06", "value": "."}]}
    session = FakeSession([FakeResponse(200, payload=payload)])
    series = FredApiClient(api_key="s3cr3t", session=session).fetch(
        "BAMLH0A0HYM2", date(2026, 10, 1), AS_OF)
    assert series.tolist() == [3.05]
    assert session.calls[0]["params"]["api_key"] == "s3cr3t"
    assert parse_fred_observations({"observations": []}).empty
    with pytest.raises(DataProviderError):
        parse_fred_observations({"error_message": "Bad Request"})
    with pytest.raises(DataProviderError, match="HTTP 400"):
        FredApiClient(api_key="k", session=FakeSession([FakeResponse(400)]), max_retries=0).fetch(
            "X", AS_OF, AS_OF)


def test_fred_api_key_read_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FRED_API_KEY", " k3y ")
    assert FredApiClient()._api_key == "k3y"
    from market_monitor.data.factory import build_provider

    provider = build_provider("free", settings_from_dict({}, base_dir=tmp_path, env={}))
    assert "fredapi:" in provider._sources


# --------------------------------------------------------------- manual entry
def test_manual_candidates_skip_legs_served_by_another_source(tmp_path):
    from market_monitor.data.providers.manual import ManualProvider, ManualQuotes
    from market_monitor.data.service import MarketDataService
    from market_monitor.export.layout import layout_from_dict
    from market_monitor.monitor import MarketMonitor
    from market_monitor.ui.daily_macro_view import _manual_candidates
    from tests.conftest import FakeProvider, bday_series

    oas = bday_series("2026-09-01", "2026-10-06", 3.0)
    free = FakeProvider("free", {"fred:BAMLH0A0HYM2|fredapi:BAMLH0A0HYM2": oas,
                                 "fred:BAMLC0A0CM|fredapi:BAMLC0A0CM": oas})
    manual = ManualProvider(ManualQuotes(tmp_path / "quotes.csv"))
    monitor = MarketMonitor(MarketDataService([free, manual]), REF)
    layout = layout_from_dict({"sections": [{"title": "Spreads", "instruments": [
        "HY_IG_US", "HY_IG_EUR", "G_SPREAD_EUR_IG", "ITRX_MAIN"]}]}, REF)
    ids = _manual_candidates(monitor, layout, AS_OF)
    # US legs come from FRED; Euro HY has no data here; Euro IG / IG yield / iTraxx are manual-only
    assert set(ids) == {"EUR_HY_OAS", "EUR_IG_OAS", "EUR_IG_YLD", "ITRX_MAIN"}
