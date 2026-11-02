"""PNG of the daily-macro bands, print-friendly (white background), via matplotlib.

Matplotlib (Agg canvas, no pyplot state) renders offline and deterministically - no
headless browser needed, which matters for an unattended 8 a.m. run.
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from market_monitor.analytics.quality import needs_check
from market_monitor.export.builder import DailyMacroData
from market_monitor.export.text import data_sources
from market_monitor.text_format import (
    CHANGE_DECIMALS,
    HORIZON_LABELS,
    NNBSP,
    format_change,
    format_level_with_unit,
    french_date,
)

NAVY, MUTED, RULE, ACCENT = "#1B2835", "#6B7B8C", "#D5DCE3", "#F0A830"
UP, DOWN = "#1E7F6F", "#C4553F"
CHECK_MARK, CHECK_COLOUR = "\u2020", "#B7791F"  # dagger, dark amber: "to check"
FONT_CANDIDATES = ("Arial", "Liberation Sans", "DejaVu Sans")
WIDTH_IN = 8.0          # fits an A4 page with margins
TITLE_IN, BAND_TITLE_IN, BAND_IN, FOOTER_IN = 0.42, 0.27, 0.8, 0.25
EXTRA_COLUMN_IN = 0.12  # tile height of each change column beyond the first


def _band_in(columns: tuple[str, ...]) -> float:
    return BAND_IN - EXTRA_COLUMN_IN * (2 - max(len(columns), 1))


@lru_cache(maxsize=1)
def _font() -> str:
    """First installed candidate (Arial on Windows) without matplotlib fallback warnings."""
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((f for f in FONT_CANDIDATES if f in installed), "DejaVu Sans")


def _t(text: str) -> str:
    """Narrow no-break space is missing from some fonts: use a regular no-break space."""
    return text.replace(NNBSP, "\u00a0")


def _colour(x: float, change_unit: str, decimals: int | None = None) -> str:
    """Sign colour of the change *as displayed* (a +0,04 pb move shown "+0,0" stays neutral)."""
    shown = CHANGE_DECIMALS[change_unit] if decimals is None else decimals
    if pd.isna(x) or round(x, shown) == 0:
        return MUTED
    return UP if x > 0 else DOWN


def render_png(data: DailyMacroData, target: str | Path | BinaryIO, *, dpi: int = 200) -> None:
    """Title, one band of tiles per section, footer with sources and stale marks."""
    band_in = _band_in(data.columns)
    height = TITLE_IN + len(data.sections) * (BAND_TITLE_IN + band_in) + FOOTER_IN
    fig = Figure(figsize=(WIDTH_IN, height), dpi=dpi, facecolor="white")
    FigureCanvasAgg(fig)
    fig.text(0.0, 1.0, _t(f"{data.title}, arrêté au {french_date(data.as_of)}"), ha="left",
             va="top", fontsize=12, fontweight="bold", color=NAVY, family=_font(),
             transform=fig.transFigure)
    top = 1.0 - TITLE_IN / height
    stale_seen = check_seen = False
    slots = max((len(t) for _, t in data.sections), default=1)  # same tile width in every band
    for title, table in data.sections:
        stale, check = _band(fig, title, table, data.columns, top, height, slots)
        stale_seen, check_seen = stale_seen or stale, check_seen or check
        top -= (BAND_TITLE_IN + band_in) / height
    footer = f"Sources : {data_sources(data.universe)}."
    if stale_seen:
        footer += " * Dernière cotation antérieure de plus de deux jours ouvrés."
    if check_seen:
        footer += f" {CHECK_MARK} Variation à vérifier avant diffusion."
    fig.text(0.0, 0.0, _t(footer), ha="left", va="bottom", fontsize=6.5, color=MUTED, family=_font())
    fig.savefig(target, format="png", dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.15)


def png_bytes(data: DailyMacroData, *, dpi: int = 200) -> bytes:
    buffer = BytesIO()
    render_png(data, buffer, dpi=dpi)
    return buffer.getvalue()


def _band(fig: Figure, title: str, table: pd.DataFrame, columns: tuple[str, ...],
          top: float, height: float, slots: int = 0) -> tuple[bool, bool]:
    """Draw one band; returns whether a stale row and a row to check were marked."""
    fig.text(0.0, top, _t(title), ha="left", va="top", fontsize=9.5, fontweight="bold",
             color=NAVY, family=_font())
    rule_y = top - 0.21 / height
    fig.add_artist(_hline(fig, rule_y, ACCENT, 1.2))
    n = max(len(table), slots, 1)
    tile_top = rule_y - 0.08 / height
    stale_seen = check_seen = False
    for k, (_, row) in enumerate(table.iterrows()):
        x = k / n
        check = needs_check(row)
        label, level, texts, first, decimals = _tile_values(row, columns)
        name = label + (" *" if row["stale"] else "") + (f" {CHECK_MARK}" if check else "")
        stale_seen |= bool(row["stale"])
        check_seen |= check
        unit = row.get("display_unit", row["change_unit"])
        lines = [
            (name, 7, "normal", MUTED),
            (level, 10.5, "bold", NAVY),
            (texts[0], 9, "bold", CHECK_COLOUR if check else _colour(first, unit, decimals)),
        ]
        lines += [(f"{HORIZON_LABELS[c.removeprefix('chg_')]} {text}", 7, "normal", MUTED)
                  for c, text in zip(columns[1:], texts[1:], strict=True)]
        y = tile_top
        for text, size, weight, colour in lines:
            fig.text(x + 0.008, y, _t(text), ha="left", va="top", fontsize=size,
                     fontweight=weight, color=colour, family=_font())
            y -= (size / 72 + 0.06) / height
        if k:
            bottom = tile_top - (_band_in(columns) - 0.12) / height
            fig.add_artist(_vline(fig, x, tile_top + 0.02 / height, bottom))
    return stale_seen, check_seen


def _tile_values(row: pd.Series, columns: tuple[str, ...]
                 ) -> tuple[str, str, list[str], float, int | None]:
    """Label, level, change texts, first displayed change and its decimals."""
    if "level_text" in row:
        return (row["label"], row["level_text"], [row[f"text_{c}"] for c in columns],
                row[f"disp_{columns[0]}"], int(row["change_decimals"]))
    level = format_level_with_unit(row["level"], row["asset_class"], row["quote"])
    return (row["name"], level, [format_change(row[c], row["change_unit"]) for c in columns],
            row[columns[0]], None)


def _hline(fig: Figure, y: float, colour: str, width: float) -> Line2D:
    return Line2D([0, 1], [y, y], transform=fig.transFigure, color=colour, linewidth=width)


def _vline(fig: Figure, x: float, y0: float, y1: float) -> Line2D:
    return Line2D([x, x], [y0, y1], transform=fig.transFigure, color=RULE, linewidth=0.6)
