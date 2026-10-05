"""Table preparation and styling for the dashboard (pure, no Streamlit)."""

from __future__ import annotations

import textwrap

import pandas as pd
from pandas.io.formats.style import Styler

from market_monitor.data.quality import to_float
from market_monitor.text_format import (  # re-exported for the UI
    CHANGE_COLUMNS,
    CHANGE_UNIT_LABELS,
    HORIZON_LABELS,
    Z_FOR_CHANGE,
    format_change,
    format_level,
    format_source,
    fr_number,
    short_date,
)
from market_monitor.ui import theme


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
    out["Instrument"] = table["name"]
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
        return styles

    return display.style.apply(cell_styles, axis=None)
