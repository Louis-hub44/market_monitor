"""Table preparation and styling for the dashboard (pure, no Streamlit)."""

from __future__ import annotations

import base64
import textwrap
from typing import Any

import pandas as pd
from pandas.io.formats.style import Styler

from market_monitor.analytics.quality import needs_check
from market_monitor.data.quality import to_float
from market_monitor.text_format import (  # re-exported for the UI
    CHANGE_COLUMNS,
    CHANGE_UNIT_LABELS,
    HORIZON_LABELS,
    Z_FOR_CHANGE,
    format_change,
    format_level,
    format_level_with_unit,
    format_source,
    fr_number,
    level_decimals,
    short_date,
)
from market_monitor.ui import theme

#: mark of a line whose 1J must be checked (suspect print, contract roll) - same as the PNG
CHECK_MARK = "\u2020"


def tile_label(name: str, width: int = 17) -> str:
    """Wrap a name on at most two lines for heatmap tiles (``<br>`` separated)."""
    lines = textwrap.wrap(name, width=width) or [""]
    if len(lines) > 2:
        lines = [lines[0], textwrap.shorten(" ".join(lines[1:]), width=width, placeholder="…")]
    return "<br>".join(lines)


def display_table(table: pd.DataFrame) -> pd.DataFrame:
    """Performance rows of one asset class -> French display strings.

    When every row shares the same change unit, the unit goes into the column headers
    (``1J (pb)``) and cells stay bare; otherwise each cell carries its unit.
    """
    units = set(table["change_unit"])
    shared = units.pop() if len(units) == 1 else None
    suffix = f" ({CHANGE_UNIT_LABELS[shared]})" if shared else ""
    groups = table["group"].where(table["group"] != table["group"].shift(), "")
    out = pd.DataFrame(index=table.index)
    out["Groupe"] = groups
    out["Instrument"] = [f"{name} {CHECK_MARK}" if needs_check(row) else name
                         for name, (_, row) in zip(table["name"], table.iterrows(), strict=True)]
    out["Niveau"] = table.apply(format_level, axis=1)
    out["Date"] = table["level_date"].map(short_date)
    for col in CHANGE_COLUMNS:
        label = HORIZON_LABELS[col.removeprefix("chg_")] + suffix
        out[label] = [format_change(x, u, with_unit=shared is None)
                      for x, u in zip(table[col], table["change_unit"], strict=True)]
    for col in ("z_1d", "z_1w"):
        label = f"z {HORIZON_LABELS[col.removeprefix('z_')]}"
        out[label] = [fr_number(z, 1, sign=True) for z in table[col]]
    out["Source"] = [format_source(s, p) for s, p in zip(table["source"], table["proxy"], strict=True)]
    return out


def style_table(display: pd.DataFrame, table: pd.DataFrame, clip: float = 3.0) -> Styler:
    """Colour change cells (text by sign, 1J / 1S background by z) and mute stale rows."""
    change_labels = [c for c in display.columns if c.split(" ")[0] in HORIZON_LABELS.values()]

    def cell_styles(_: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=display.index, columns=display.columns)
        for label, col in zip(change_labels, CHANGE_COLUMNS, strict=True):
            for idx in display.index:
                if col in Z_FOR_CHANGE:  # background carries the sign: keep text readable
                    z = to_float(table.at[idx, Z_FOR_CHANGE[col]])
                    css = (f"color: {theme.TEXT}; font-weight: 600; "
                           f"background-color: {theme.heat_color(z, clip, base=theme.PANEL)};")
                else:
                    css = f"color: {theme.sign_color(to_float(table.at[idx, col]))};"
                styles.at[idx, label] = css
        stale = table["stale"].reindex(display.index).fillna(True).astype(bool)
        styles.loc[stale] = styles.loc[stale] + f" color: {theme.MUTED}; font-style: italic;"
        one_day = change_labels[0] if change_labels else None
        for idx in display.index:  # suspect move or contract roll: amber, not a market colour
            if one_day and needs_check(table.loc[idx]):
                styles.at[idx, one_day] = (f"color: {theme.TEXT}; font-weight: 600; "
                                           f"background-color: {theme.CHECK_BG};")
        return styles

    return display.style.apply(cell_styles, axis=None)


# ================================================================ overview v1.6
#: overview columns, Koyfin order (``chg_<h>``)
OVERVIEW_CHANGES = ["chg_1d", "chg_1w", "chg_1m", "chg_3m", "chg_ytd", "chg_1y"]
SPARK_COLUMN, RANGE_COLUMN, FROM_HIGH_COLUMN = "1 mois", "52 semaines", "vs + haut"
SPARK_DAYS = 31


def sparkline(levels: pd.DataFrame | None, instrument_id: str,
              end: pd.Timestamp | None) -> list[float] | None:
    """Closes of the last month (list for ``LineChartColumn``), ``None`` if too short."""
    if levels is None or instrument_id not in levels.columns or end is None or pd.isna(end):
        return None
    window = levels[instrument_id].loc[end - pd.Timedelta(days=SPARK_DAYS):end].dropna()
    return [float(v) for v in window] if len(window) >= 2 else None


def range_svg(low: float, high: float, position: float, asset_class: str, quote: str) -> str | None:
    """52-week range bar as an inline SVG data URL: low and high under a track, a dot at
    the last close (amber near the high or low, where a breakout is in play)."""
    if any(pd.isna(v) for v in (low, high, position)):
        return None
    left, right, width = 4.0, 136.0, 132.0
    x = left + width * min(max(position, 0.0), 1.0)
    edge = position >= 0.95 or position <= 0.05
    dot = theme.ACCENT if edge else theme.TEXT

    def label(value: float) -> str:
        decimals = 0 if quote == "price" and abs(value) >= 1000 else level_decimals(asset_class, quote, value)
        return fr_number(value, decimals)

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="140" height="30" viewBox="0 0 140 30">'
           f'<rect x="{left}" y="7" width="{width}" height="3" rx="1.5" fill="{theme.RULE}"/>'
           f'<rect x="{left}" y="7" width="{x - left:.1f}" height="3" rx="1.5" fill="{theme.MUTED}" '
           f'opacity="0.55"/>'
           f'<circle cx="{x:.1f}" cy="8.5" r="4" fill="{dot}" stroke="{theme.BACKGROUND}" '
           f'stroke-width="1.2"/>'
           f'<text x="{left}" y="25" font-size="9" fill="{theme.MUTED}" font-family="Arial,sans-serif">'
           f'{label(low)}</text>'
           f'<text x="{right}" y="25" font-size="9" fill="{theme.MUTED}" text-anchor="end" '
           f'font-family="Arial,sans-serif">{label(high)}</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def overview_table(table: pd.DataFrame, levels: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rows of one asset class for the Koyfin-style overview (display values).

    Last close (yields in %), 1J / 1S / 1M / 3M / YTD / 1A in the class convention (% or
    pb, the unit in the header when shared), a 1-month sparkline, the 52-week range bar,
    the distance from the 52-week high and the 1J z-score.
    """
    units = set(table["change_unit"])
    shared = units.pop() if len(units) == 1 else None
    suffix = f" ({CHANGE_UNIT_LABELS[shared]})" if shared else ""
    out = pd.DataFrame(index=table.index)
    out["Groupe"] = table["group"].where(table["group"] != table["group"].shift(), "")
    out["Instrument"] = [f"{name} {CHECK_MARK}" if needs_check(row) else name
                         for name, (_, row) in zip(table["name"], table.iterrows(), strict=True)]
    # the date only when the print is old: the table stays readable at full width
    out["Dernier"] = [format_level_with_unit(r["level"], r["asset_class"], r["quote"]) + _old_date(r)
                      for _, r in table.iterrows()]
    for col in OVERVIEW_CHANGES:
        label = HORIZON_LABELS[col.removeprefix("chg_")] + suffix
        out[label] = [format_change(x, u, with_unit=shared is None)
                      for x, u in zip(table[col], table["change_unit"], strict=True)]
    out[SPARK_COLUMN] = pd.Series([sparkline(levels, str(i), r["level_date"]) for i, r in table.iterrows()],
                                  index=table.index, dtype="object")
    out[RANGE_COLUMN] = [range_svg(r["low_52w"], r["high_52w"], r["pos_52w"], r["asset_class"], r["quote"])
                         for _, r in table.iterrows()]
    out[FROM_HIGH_COLUMN] = [format_change(x, u, with_unit=shared is None)
                             for x, u in zip(table["from_high_52w"], table["change_unit"], strict=True)]
    out["z 1J"] = [fr_number(z, 1, sign=True) for z in table["z_1d"]]
    return out


def _old_date(row: pd.Series) -> str:
    return f" ({short_date(row['level_date'])})" if row["stale"] and pd.notna(row["level_date"]) else ""


def overview_column_config(display: pd.DataFrame) -> dict[str, Any]:
    """Pixel widths so the 13 columns fit a laptop screen without horizontal scrolling."""
    import streamlit as st  # the formatting module stays importable without a script context

    config: dict[str, Any] = {
        "Groupe": st.column_config.TextColumn(width=84),
        "Instrument": st.column_config.TextColumn(width=165),
        "Dernier": st.column_config.TextColumn(width=82),
        SPARK_COLUMN: st.column_config.LineChartColumn(SPARK_COLUMN, width=78),
        RANGE_COLUMN: st.column_config.ImageColumn(RANGE_COLUMN, width=148),
        FROM_HIGH_COLUMN: st.column_config.TextColumn(width=66),
        "z 1J": st.column_config.TextColumn(width=48),
    }
    for label in display.columns[3:3 + len(OVERVIEW_CHANGES)]:
        config[label] = st.column_config.TextColumn(width=56)
    return config


def style_overview(display: pd.DataFrame, table: pd.DataFrame, clip: float = 3.0) -> Styler:
    """Change cells coloured by sign (1J background by z), stale rows muted, checks amber."""
    labels = list(display.columns[3:3 + len(OVERVIEW_CHANGES)])

    def cell_styles(_: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=display.index, columns=display.columns)
        for label, col in zip(labels, OVERVIEW_CHANGES, strict=True):
            for idx in display.index:
                if col == "chg_1d":
                    background = theme.heat_color(to_float(table.at[idx, "z_1d"]), clip, base=theme.PANEL)
                    styles.at[idx, label] = (f"color: {theme.TEXT}; font-weight: 600; "
                                             f"background-color: {background};")
                else:
                    styles.at[idx, label] = f"color: {theme.sign_color(to_float(table.at[idx, col]))};"
        for idx in display.index:
            styles.at[idx, FROM_HIGH_COLUMN] = f"color: {theme.MUTED};"
            if needs_check(table.loc[idx]):
                styles.at[idx, labels[0]] = (f"color: {theme.TEXT}; font-weight: 600; "
                                             f"background-color: {theme.CHECK_BG};")
        stale = table["stale"].reindex(display.index).fillna(True).astype(bool)
        styles.loc[stale] = styles.loc[stale] + f" color: {theme.MUTED}; font-style: italic;"
        return styles

    return display.style.apply(cell_styles, axis=None)
