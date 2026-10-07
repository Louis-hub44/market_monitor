"""Daily-macro charts: history of a few instruments over a period, with the session's move.

``build_charts`` loads the data once for every chart (pure, testable); ``charts_png``
draws them for print (white background, matplotlib) and the dashboard draws the same
:class:`ChartData` with Plotly. The last session's move is what a reader looks for, so each
chart states the last level and the 1-day change, and draws the last segment in the
colour of that move.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import date2num
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

from market_monitor.analytics.comparison import period_start
from market_monitor.export.layout import ChartSpec
from market_monitor.monitor import MarketMonitor
from market_monitor.referential import ChangeUnit, Instrument
from market_monitor.text_format import NNBSP, format_change, fr_number, level_decimals

#: series colours, validated (lightness band, CVD separation, contrast) on a white page
SERIES_COLOURS_LIGHT = ("#B26A12", "#2F6DB5", "#5B6B7A", "#7A4FA3")
INK, MUTED, GRID = "#1B2835", "#6B7B8C", "#E3E8ED"
UP, DOWN = "#1E7F6F", "#C4553F"
MONTHS_SHORT = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.",
                "oct.", "nov.", "déc.")
UNIT_SUFFIX = {"yield": "%", "spread": "pb"}


@dataclass(frozen=True)
class ChartSeries:
    id: str
    label: str
    values: pd.Series          # date -> level (referential units), NaN dropped
    instrument: Instrument

    @property
    def last(self) -> float:
        return float(self.values.iloc[-1]) if len(self.values) else math.nan

    @property
    def last_date(self) -> pd.Timestamp | None:
        return self.values.index[-1] if len(self.values) else None

    @property
    def change_1d(self) -> float:
        """Last session's move in the instrument's convention (% / pb / points)."""
        if len(self.values) < 2:
            return math.nan
        prev, last = float(self.values.iloc[-2]), float(self.values.iloc[-1])
        unit = self.instrument.change
        if unit is ChangeUnit.PCT:
            return 100.0 * (last / prev - 1.0) if prev else math.nan
        if unit is ChangeUnit.BP:
            return (self.instrument.bp_factor or 1.0) * (last - prev)
        return last - prev

    def level_text(self) -> str:
        inst = self.instrument
        text = fr_number(self.last, level_decimals(inst.asset_class.value, inst.quote.value, self.last))
        suffix = UNIT_SUFFIX.get(inst.quote.value, "")
        return f"{text}{NNBSP}{suffix}" if suffix and text != "–" else text

    def change_text(self) -> str:
        return format_change(self.change_1d, self.instrument.change.value)


@dataclass(frozen=True)
class ChartData:
    spec: ChartSpec
    start: date
    end: date
    series: tuple[ChartSeries, ...]
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def period_label(self) -> str:
        if self.spec.start is not None or self.spec.period not in PERIOD_LABELS:
            return f"depuis le {self.start:%d/%m/%Y}"
        return PERIOD_LABELS[self.spec.period]

    @property
    def unit_label(self) -> str:
        """Axis unit, shared by every series (one axis per chart)."""
        quotes = {s.instrument.quote.value for s in self.series}
        return UNIT_SUFFIX.get(quotes.pop(), "") if len(quotes) == 1 else ""


PERIOD_LABELS = {"1M": "1 mois", "3M": "3 mois", "6M": "6 mois", "YTD": "YTD",
                 "1A": "1 an", "3A": "3 ans", "5A": "5 ans"}


def chart_window(spec: ChartSpec, as_of: date) -> tuple[date, date]:
    """``[start, as_of]``: the fixed ``start`` when given, else the period's first date."""
    start = spec.start if spec.start is not None else period_start(spec.period, as_of)
    return min(start, as_of), as_of


def build_charts(monitor: MarketMonitor, specs: Sequence[ChartSpec], as_of: date,
                 overrides: Mapping[int, ChartSpec] | None = None) -> list[ChartData]:
    """One history call for every chart (instruments no active provider serves are skipped).

    ``overrides`` replaces chart ``k``'s spec (period / dates changed in the dashboard).
    """
    specs = [(overrides or {}).get(k, spec) for k, spec in enumerate(specs)]
    windows = [chart_window(spec, as_of) for spec in specs]
    ids = list(dict.fromkeys(i for spec in specs for i in monitor.available(spec.instruments)))
    if not ids:
        return [ChartData(spec, *window, ()) for spec, window in zip(specs, windows, strict=True)]
    hist = monitor.history(ids, min(w[0] for w in windows), as_of)
    ref = monitor.referential
    charts = []
    for spec, (start, end) in zip(specs, windows, strict=True):
        series, errors = [], {}
        for k, instrument_id in enumerate(spec.instruments):
            inst = ref.get(instrument_id)
            column = hist.levels.get(instrument_id)
            values = (column.loc[pd.Timestamp(start):pd.Timestamp(end)].dropna()
                      if column is not None else pd.Series(dtype="float64"))
            if values.empty:
                errors[instrument_id] = hist.errors.get(instrument_id, "aucune donnée sur la période")
                continue
            series.append(ChartSeries(instrument_id, spec.label(k, inst.name), values, inst))
        charts.append(ChartData(spec, start, end, tuple(series), errors))
    return charts


# ====================================================================== axes
def date_ticks(start: date, end: date, max_ticks: int = 7) -> list[tuple[pd.Timestamp, str]]:
    """French date ticks scaled to the window, shared by the dashboard and the PNG.

    Years beyond two years, months beyond ~7 months, Mondays (every 1-2 weeks) on 1-6
    months - a 3-month VIX reads week by week - and days below two weeks.
    """
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    days = (last - first).days
    if days > 730:
        years = _thin(pd.date_range(first + pd.Timedelta(days=1), last, freq="YS"), max_ticks)
        return [(t, str(t.year)) for t in years]
    if days > 200:
        months = _thin(pd.date_range(first + pd.Timedelta(days=1), last, freq="MS"), max_ticks)
        return [(t, MONTHS_SHORT[t.month - 1] + (f" {t.year}" if t.month == 1 else "")) for t in months]
    if days > 14:
        dates = _thin(pd.date_range(first + pd.Timedelta(days=1), last, freq="W-MON"), max_ticks)
    else:
        dates = _thin(pd.bdate_range(first, last), max_ticks)
    return [(t, f"{t.day} {MONTHS_SHORT[t.month - 1]}") for t in dates]


def _thin(ticks: pd.DatetimeIndex, max_ticks: int) -> list[pd.Timestamp]:
    step = max(1, -(-len(ticks) // max_ticks))
    return list(ticks[::step])


def period_extremes(series: pd.Series) -> tuple[tuple[pd.Timestamp, float], tuple[pd.Timestamp, float]]:
    """``((date, high), (date, low))`` of the window - the levels a reader looks for first."""
    high, low = pd.Timestamp(series.idxmax()), pd.Timestamp(series.idxmin())  # type: ignore[arg-type]
    return (high, float(series.max())), (low, float(series.min()))


def axis_decimals(chart: ChartData, ticks: int = 7) -> int:
    """Decimals that keep neighbouring axis labels distinct (VIX 15,5 / 16,0 over 3 months)."""
    span = max(float(s.values.max() - s.values.min()) for s in chart.series) or 1.0
    step = span / ticks
    return 0 if step >= 5 else (1 if step >= 0.25 else (2 if step >= 0.025 else 3))


# ====================================================================== PNG
COLUMNS, CELL_W_IN, CELL_H_IN = 2, 4.0, 2.7
RIGHT_MARGIN = 0.03  # share of the window left blank after the last date


def _draw(ax: Axes, chart: ChartData) -> None:
    ax.set_facecolor("white")
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.tick_params(colors=MUTED, labelsize=7, length=0)
    ax.set_title(f"{chart.title} · {chart.period_label}", loc="left", fontsize=9,
                 fontweight="bold", color=INK, pad=16)
    if not chart.series:
        ax.text(0.5, 0.5, "Pas de donnée", ha="center", va="center", color=MUTED,
                transform=ax.transAxes, fontsize=8)
        ax.set_xticks([])
        ax.set_yticks([])
        return
    single = len(chart.series) == 1
    low, high = value_range(chart)
    subtitle = []
    for k, s in enumerate(chart.series):
        colour = SERIES_COLOURS_LIGHT[k % len(SERIES_COLOURS_LIGHT)]
        ax.plot(s.values.index, s.values.to_numpy(), color=colour, linewidth=1.4,
                label=s.label, solid_capstyle="round")
        if single:  # Investing-style area under the price line
            ax.fill_between(s.values.index, s.values.to_numpy(), low, color=colour, alpha=0.10,
                            linewidth=0)
        move = s.change_1d
        tone = MUTED if math.isnan(move) or round(move, 2) == 0 else (UP if move > 0 else DOWN)
        if len(s.values) >= 2:  # the session's move: last segment and last point
            ax.plot(s.values.index[-2:], s.values.to_numpy()[-2:], color=tone, linewidth=2.4,
                    solid_capstyle="round")
        ax.plot([s.values.index[-1]], [s.last], "o", color=tone, markersize=4.5,
                markeredgecolor="white", markeredgewidth=1.0)
        tag = tone if single else colour
        ax.axhline(s.last, color=tag, linewidth=0.7, linestyle=(0, (1.5, 2)))
        ax.annotate(  # last-price tag on the right axis
            s.level_text().replace(NNBSP, "\u00a0"), xy=(1.0, s.last),
            xycoords=("axes fraction", "data"), xytext=(2, 0), textcoords="offset points",
            ha="left", va="center", fontsize=6.5, fontweight="bold", color="white",
            bbox={"boxstyle": "square,pad=0.25", "facecolor": tag, "edgecolor": tag},
            annotation_clip=False, zorder=5,
        )
        if single:
            _mark_extremes(ax, s)
        prefix = f"{s.label} " if len(chart.series) > 1 else ""
        subtitle.append((f"{prefix}{s.level_text()}", f" {s.change_text()}", tone))
    ax.set_ylim(low, high)
    _subtitle(ax, subtitle)
    left, right = float(date2num(chart.start)), float(date2num(chart.end))
    ax.set_xlim(left, right + RIGHT_MARGIN * (right - left))  # room for the last point
    ticks = date_ticks(chart.start, chart.end)
    ax.set_xticks([float(date2num(t)) for t, _ in ticks], [label for _, label in ticks])
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]))
    decimals = axis_decimals(chart)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: fr_number(v, decimals)
                                               .replace(NNBSP, " ")))
    ax.yaxis.tick_right()
    if len(chart.series) > 1:
        ax.legend(loc="best", fontsize=7, frameon=True, facecolor="white", edgecolor="white",
                  framealpha=0.85, labelcolor=INK)


def _mark_extremes(ax: Axes, series: ChartSeries) -> None:
    """Small labels at the period's high and low (level and date)."""
    decimals = level_decimals(series.instrument.asset_class.value, series.instrument.quote.value,
                              series.last)
    for (when, level), va, dy in zip(period_extremes(series.values), ("bottom", "top"), (3, -3),
                                     strict=True):
        ax.plot([float(date2num(when))], [level], "o", color=MUTED, markersize=2.5)
        ax.annotate(f"{fr_number(level, decimals)} ({when:%d/%m})".replace(NNBSP, "\u00a0"),
                    xy=(when, level), xytext=(0, dy), textcoords="offset points", ha="center",
                    va=va, fontsize=6, color=MUTED)


def value_range(chart: ChartData, pad: float = 0.10) -> tuple[float, float]:
    """Y range fitted to the data (not to zero), with a margin: moves stay readable."""
    low = min(float(s.values.min()) for s in chart.series)
    high = max(float(s.values.max()) for s in chart.series)
    margin = (high - low) * pad or abs(high) * 0.01 or 1.0
    return low - margin, high + margin


def _subtitle(ax: Axes, parts: list[tuple[str, str, str]]) -> None:
    """``level`` in ink then the 1-day change in its sign colour, under the title."""
    x = 0.0
    fig = ax.figure
    renderer = fig.canvas.get_renderer()  # type: ignore[attr-defined]
    for level, change, tone in parts:
        for text, colour in ((level, INK), (change + "   ", tone)):
            artist = ax.text(x, 1.02, text.replace(NNBSP, " "), transform=ax.transAxes,
                             fontsize=7.5, color=colour, va="bottom", ha="left")
            box = artist.get_window_extent(renderer=renderer)
            x += box.width / ax.get_window_extent(renderer=renderer).width


def render_charts_png(charts: Sequence[ChartData], target: str | Path | BinaryIO,
                      *, dpi: int = 200) -> None:
    """Grid of charts, two per row, print-friendly."""
    rows = max(1, math.ceil(len(charts) / COLUMNS))
    fig = Figure(figsize=(COLUMNS * CELL_W_IN, rows * CELL_H_IN), facecolor="white")
    FigureCanvasAgg(fig)
    axes = fig.subplots(rows, COLUMNS, squeeze=False)
    for k, ax in enumerate(axes.flat):
        if k < len(charts):
            _draw(ax, charts[k])
        else:
            ax.set_visible(False)
    fig.tight_layout(h_pad=1.6, w_pad=3.2)
    fig.savefig(target, format="png", dpi=dpi, facecolor="white")


def charts_png_bytes(charts: Sequence[ChartData], *, dpi: int = 200) -> bytes:
    buffer = BytesIO()
    render_charts_png(charts, buffer, dpi=dpi)
    return buffer.getvalue()
