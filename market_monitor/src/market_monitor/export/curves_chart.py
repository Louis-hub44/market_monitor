"""Print version of the yield curves (white background, matplotlib), for the daily macro.

One panel per country - today's curve against J-1 / S-1 / M-1 - and a last panel with the
change of every point (bp) since the chosen comparison.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from market_monitor.analytics.curves import COUNTRY_LABELS, SHORT_LABELS, CurveSet
from market_monitor.export.charts import GRID, INK, MUTED
from market_monitor.text_format import NNBSP, fr_number, french_date

TODAY_COLOUR = "#B26A12"
COMPARISON_STYLES = {"J-1": ((0, (1, 1.6)), "#5B6B7A"), "S-1": ((0, (4, 2)), "#2F6DB5"),
                     "M-1": ((0, (5, 1.5, 1, 1.5)), "#7A4FA3")}
COUNTRY_COLOURS = ("#B26A12", "#2F6DB5", "#5B6B7A", "#7A4FA3", "#2E8B57", "#B23A48", "#8C6D1F")
TENORS = (2.0, 5.0, 10.0, 30.0)
COLUMNS, CELL_W_IN, CELL_H_IN = 3, 3.4, 2.5


def _style(ax: Axes, title: str) -> None:
    ax.set_facecolor("white")
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.tick_params(colors=MUTED, labelsize=7, length=0)
    ax.set_title(title, loc="left", fontsize=8.5, fontweight="bold", color=INK, pad=8)


def _curve_panel(ax: Axes, curves: CurveSet, country: str, comparisons: Sequence[str]) -> None:
    _style(ax, COUNTRY_LABELS.get(country, country))
    for comparison in comparisons:
        past = curves.curve(country, comparison).dropna()
        style, colour = COMPARISON_STYLES[comparison]
        ax.plot(past.index, past.to_numpy(), linestyle=style, color=colour, linewidth=1.1,
                marker="o", markersize=2.5, label=comparison)
    today = curves.curve(country).dropna()
    ax.plot(today.index, today.to_numpy(), color=TODAY_COLOUR, linewidth=1.8, marker="o",
            markersize=3.5, label="Aujourd'hui")
    for tenor, level in today.items():
        ax.annotate(fr_number(level, 2), (tenor, level), xytext=(0, 5), textcoords="offset points",
                    ha="center", fontsize=6, color=INK)
    ax.set_xscale("log")
    ax.set_xticks(list(TENORS), [f"{t:g} ans" for t in TENORS])
    ax.minorticks_off()
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{fr_number(v, 2)} %".replace(NNBSP, " ")))


def _changes_panel(ax: Axes, curves: CurveSet, countries: Sequence[str], comparison: str) -> None:
    _style(ax, f"Variations depuis {comparison} (pb)")
    width = 0.8 / max(len(countries), 1)
    x = np.arange(len(TENORS))
    for k, country in enumerate(countries):
        rows = curves.points[curves.points["country"] == country]
        by_tenor = dict(zip(rows["tenor"], rows[f"chg_{comparison}"].astype(float), strict=True))
        values = [by_tenor.get(t, math.nan) for t in TENORS]
        ax.bar(x + (k - (len(countries) - 1) / 2) * width, values, width=width * 0.92,
               color=COUNTRY_COLOURS[k % len(COUNTRY_COLOURS)], label=SHORT_LABELS.get(country, country))
    ax.axhline(0, color=MUTED, linewidth=0.7)
    ax.set_xticks(x, [f"{t:g} ans" for t in TENORS])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: fr_number(v, 0, sign=True)))
    ax.legend(fontsize=6, frameon=False, ncols=min(len(countries), 5), loc="upper left",
              bbox_to_anchor=(0.0, -0.12), labelcolor=INK)


def render_curves_png(curves: CurveSet, countries: Sequence[str], target: str | Path | BinaryIO, *,
                      comparisons: Sequence[str] = ("J-1", "S-1", "M-1"), changes_since: str = "J-1",
                      dpi: int = 200) -> None:
    """Curves of ``countries`` and their changes, as a print-ready PNG."""
    shown = [c for c in countries if not curves.curve(c).dropna().empty]
    panels = len(shown) + 1
    rows = math.ceil(panels / COLUMNS)
    fig = Figure(figsize=(COLUMNS * CELL_W_IN, rows * CELL_H_IN + 0.35), facecolor="white")
    FigureCanvasAgg(fig)
    axes = fig.subplots(rows, COLUMNS, squeeze=False)
    for k, ax in enumerate(axes.flat):
        if k < len(shown):
            _curve_panel(ax, curves, shown[k], comparisons)
        elif k == len(shown):
            _changes_panel(ax, curves, shown, changes_since)
        else:
            ax.set_visible(False)
    if shown:
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper right", ncols=len(labels), fontsize=7, frameon=False,
                   labelcolor=INK, bbox_to_anchor=(0.99, 1.0))
    fig.suptitle(f"Courbes de taux souverains, arrêtées au {french_date(curves.as_of)}", x=0.01,
                 ha="left", fontsize=10, fontweight="bold", color=INK)
    fig.tight_layout(h_pad=1.4, w_pad=2.0)
    fig.savefig(target, format="png", dpi=dpi, facecolor="white")


def curves_png_bytes(curves: CurveSet, countries: Sequence[str], *,
                     comparisons: Sequence[str] = ("J-1", "S-1", "M-1"), changes_since: str = "J-1",
                     dpi: int = 200) -> bytes:
    buffer = BytesIO()
    render_curves_png(curves, countries, buffer, comparisons=comparisons, changes_since=changes_since,
                      dpi=dpi)
    return buffer.getvalue()
