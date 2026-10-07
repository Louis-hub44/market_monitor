"""Display conventions of the daily-macro bands (layout ``format`` and per-line options)."""

import math

import pandas as pd
import pytest

from market_monitor.export import build_daily_macro, excel_bytes, png_bytes, render_text
from market_monitor.export.display import (
    DEFAULT_STYLE,
    LineFormat,
    NumberStyle,
    decorate,
    display_change,
    format_change,
    format_level,
)
from market_monitor.export.excel import band_change_format, band_level_format, number_format
from market_monitor.export.layout import layout_from_dict
from market_monitor.referential import ReferentialError
from market_monitor.text_format import format_change as house_change
from market_monitor.text_format import format_level_with_unit
from tests.test_export import AS_OF, LAYOUT_RAW, REF, _monitor

REVIEW = NumberStyle(thousands_separator=False, compact_units=True, bp_unit="bps")


def _row(**kw):
    base = {"name": "X", "level": 6272.95, "asset_class": "equity", "quote": "price",
            "change_unit": "pct", "chg_1d": 0.48, "ref_1d": 6243.0, "bp_factor": 100.0}
    return pd.Series({**base, **kw})


def test_defaults_reproduce_house_style():
    cases = [(6272.95, "equity", "price"), (2.7, "rates", "yield"), (72.1, "rates", "spread")]
    for level, cls, quote in cases:
        row = _row(level=level, asset_class=cls, quote=quote)
        assert format_level(row) == format_level_with_unit(level, cls, quote)
    for x, unit in [(0.48, "pct"), (-5.0, "bp"), (1.2, "abs")]:
        assert format_change(x, unit) == house_change(x, unit)


def test_review_style_levels_and_units():
    assert format_level(_row(), style=REVIEW) == "6272,95"
    assert format_level(_row(level=70192.4), LineFormat(decimals=0), REVIEW) == "70192"
    assert format_level(_row(level=5.27, quote="yield"), LineFormat(decimals=2), REVIEW) == "5,27%"
    assert format_level(_row(level=100.58), LineFormat(suffix=" $"), REVIEW) == "100,58 $"
    assert format_level(_row(level=127.0, quote="spread"), LineFormat(decimals=0), REVIEW) == "127bps"
    assert format_change(-5.0, "bp", 0, REVIEW) == "−5bps"
    assert format_change(-1.0, "bp", 0, REVIEW) == "−1bp"   # singular at |x| <= 1
    assert format_change(-0.4, "bp", 1, REVIEW) == "−0,4bp"
    assert format_change(0.3, "pct", 1, REVIEW) == "+0,3%"


def test_vol_index_change_recomputed_in_percent():
    row = _row(level=15.01, quote="vol", asset_class="volatility", change_unit="abs",
               chg_1d=-0.51, ref_1d=15.52)
    value = display_change(row, "chg_1d", LineFormat(change_unit="pct"))
    assert value == pytest.approx(100 * (15.01 / 15.52 - 1))
    no_ref = _row(change_unit="abs", ref_1d=float("nan"))
    assert math.isnan(display_change(no_ref, "chg_1d", LineFormat(change_unit="pct")))
    assert display_change(row, "chg_1d") == -0.51


def test_decorate_adds_display_columns():
    table = pd.DataFrame([_row(name="Nikkei 225", level=70192.0, chg_1d=-0.7)], index=["NKY"])
    out = decorate(table, (LineFormat(label="Nikkei", decimals=0),), ("chg_1d",), REVIEW)
    row = out.loc["NKY"]
    assert (row["label"], row["level_text"], row["text_chg_1d"]) == ("Nikkei", "70192", "−0,70%")
    assert (row["level_decimals"], row["level_suffix"], row["change_decimals"]) == (0, "", 2)


def test_excel_number_formats():
    assert number_format(2, " $", thousands=False) == '0.00" $"'
    assert number_format(0, thousands=True) == "#,##0"
    assert number_format(1, thousands=False, sign=True, percent=True) == "+0.0%;-0.0%;0.0%"
    row = pd.Series({"level_decimals": 2, "level_suffix": "%", "display_unit": "bp", "change_decimals": 0})
    assert band_level_format(row, REVIEW) == '0.00"%"'
    assert band_change_format(row, REVIEW) == '+0"bps";-0"bps";0"bps"'
    assert band_change_format(row, DEFAULT_STYLE) == '+0" pb";-0" pb";0" pb"'


def test_layout_line_options_and_validation():
    raw = {**LAYOUT_RAW, "format": {"thousands_separator": False, "compact_units": True, "bp_unit": "bps"},
           "sections": [{"title": "Taux", "decimals": 2, "change_decimals": 0,
                         "instruments": [{"id": "OAT_10Y", "label": "France"}, "BUND_10Y",
                                         {"id": "OAT_BUND", "decimals": 0}]}]}
    layout = layout_from_dict(raw, REF)
    section = layout.sections[0]
    assert section.instruments == ("OAT_10Y", "BUND_10Y", "OAT_BUND")
    assert section.line("OAT_10Y") == LineFormat(label="France", decimals=2, change_decimals=0)
    assert section.line("OAT_BUND").decimals == 0 and section.line("SX5E") == LineFormat()
    assert layout.style == REVIEW
    hash(layout)  # the UI caches on it
    bad = [
        {"sections": [{"title": "T", "instruments": [{"label": "no id"}]}]},
        {"sections": [{"title": "T", "instruments": [{"id": "SX5E", "color": "red"}]}]},
        {"sections": [{"title": "T", "instruments": [{"id": "*"}]}]},
        {"sections": [{"title": "T", "decimals": -1, "instruments": ["SX5E"]}]},
        {"sections": [{"title": "T", "instruments": [{"id": "SX5E", "change": "x"}]}]},
        {"sections": [{"title": "T", "instruments": ["SX5E"]}], "format": {"bp_unit": ""}},
        {"sections": [{"title": "T", "instruments": ["SX5E"]}], "format": {"commas": True}},
    ]
    for patch in bad:
        with pytest.raises(ReferentialError):
            layout_from_dict({**LAYOUT_RAW, **patch}, REF)


def test_review_format_end_to_end():
    raw = {**LAYOUT_RAW, "columns": ["chg_1d"],
           "format": {"thousands_separator": False, "compact_units": True, "bp_unit": "bps"},
           "sections": [{"title": "Indices", "instruments": [{"id": "SX5E", "decimals": 0}]},
                        {"title": "Taux 10 ans", "decimals": 2, "change_decimals": 0,
                         "instruments": [{"id": "OAT_10Y", "label": "France"}]}]}
    data = build_daily_macro(_monitor(), layout_from_dict(raw, REF), AS_OF)
    text = render_text(data)
    france = next(line for line in text.splitlines() if line.startswith("- France : "))
    assert france.endswith("bps)") or france.endswith("bp)")
    assert "%" in france and " %" not in france and "," in france
    assert png_bytes(data, dpi=60)[:4] == b"\x89PNG" and excel_bytes(data)[:2] == b"PK"
