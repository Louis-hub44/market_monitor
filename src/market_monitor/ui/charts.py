"""Plotly figures (pure functions, no Streamlit)."""

from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from market_monitor.analytics.comparison import ComparisonResult
from market_monitor.analytics.performance import ZSCORE_HORIZONS, Horizon
from market_monitor.analytics.quality import needs_check
from market_monitor.export.charts import MONTHS_SHORT, ChartData, ChartSeries, value_range
from market_monitor.referential import AssetClass, ChangeUnit
from market_monitor.text_format import (
    ASSET_CLASS_LABELS,
    HORIZON_LABELS,
    format_change,
    fr_number,
)
from market_monitor.ui import theme
from market_monitor.ui.formatting import (
    CHECK_MARK,
    tile_label,
)

TILE_HEIGHT_PX = 72
TITLE_HEIGHT_PX = 44


def _tile_grid(rows: pd.DataFrame, horizon: Horizon, columns: int) -> tuple[list, list, list]:
    """Lay one asset class out as a ``ceil(n / columns) x columns`` grid of tiles."""
    n_rows = math.ceil(len(rows) / columns)
    z: list[list[float | None]] = [[None] * columns for _ in range(n_rows)]
    text: list[list[str]] = [[""] * columns for _ in range(n_rows)]
    hover: list[list[str]] = [[""] * columns for _ in range(n_rows)]
    for k, (_, row) in enumerate(rows.iterrows()):
        r, c = divmod(k, columns)
        chg = row[f"chg_{horizon}"]
        zval = row[f"z_{horizon}"]
        change = format_change(chg, row["change_unit"])
        # a tile with a change but no reliable z-score is drawn neutral (z = 0), not hidden
        check = needs_check(row) and horizon is Horizon.D1
        # a suspect print or a roll jump is drawn neutral: it is not a market move
        z[r][c] = 0.0 if check else (zval if not math.isnan(zval) else (0.0 if not math.isnan(chg) else None))
        mark = f" {CHECK_MARK}" if check else ""
        text[r][c] = f"<b>{tile_label(row['name'])}{mark}</b><br>{change}"
        hover[r][c] = (f"{row['name']}<br>{HORIZON_LABELS[horizon.value]} : {change}"
                       f"<br>z : {fr_number(zval, 2, sign=True)}")
    return z, text, hover


def heat_tiles(
    table: pd.DataFrame, horizon: Horizon = Horizon.D1, *, columns: int = 8, clip: float = 3.0
) -> go.Figure:
    """Heatmap of tiles grouped by asset class, coloured by the z-score of ``horizon``."""
    if horizon not in ZSCORE_HORIZONS:
        raise ValueError(f"no z-score for {horizon}")
    classes = [ac for ac in AssetClass if (table["asset_class"] == ac.value).any()]
    if not classes:
        return go.Figure()
    grids = [_tile_grid(table[table["asset_class"] == ac.value], horizon, columns) for ac in classes]
    heights = [len(g[0]) for g in grids]
    fig = make_subplots(
        rows=len(classes), cols=1, row_heights=heights, vertical_spacing=0.12 / len(classes),
        subplot_titles=[ASSET_CLASS_LABELS[ac.value] for ac in classes],
    )
    for i, (z, text, hover) in enumerate(grids, start=1):
        fig.add_trace(
            go.Heatmap(
                z=z, text=text, texttemplate="%{text}", hovertext=hover, hoverinfo="text",
                zmin=-clip, zmax=clip, colorscale=theme.plotly_colorscale(), xgap=3, ygap=3,
                showscale=(i == 1), textfont={"size": 11, "color": theme.TEXT},
                colorbar={"title": {"text": "z-score"}, "thickness": 10, "len": 0.5, "y": 1,
                          "yanchor": "top", "tickfont": {"color": theme.MUTED}},
            ),
            row=i, col=1,
        )
        fig.update_xaxes(visible=False, row=i, col=1)
        fig.update_yaxes(visible=False, autorange="reversed", row=i, col=1)
    _apply_layout(fig, height=sum(heights) * TILE_HEIGHT_PX + len(classes) * TITLE_HEIGHT_PX)
    return fig


GROUP_TITLES = {
    ChangeUnit.PCT: "Prix, base 100",
    ChangeUnit.BP: "Taux et spreads, variation cumulée (pb)",
    ChangeUnit.ABS: "Volatilité, variation cumulée (pts)",
}
GROUP_REFERENCE = {ChangeUnit.PCT: 100.0, ChangeUnit.BP: 0.0, ChangeUnit.ABS: 0.0}
HOVER_FORMAT = {ChangeUnit.PCT: ".1f", ChangeUnit.BP: "+.1f", ChangeUnit.ABS: "+.2f"}


def comparison_figure(result: ComparisonResult, names: dict[str, str]) -> go.Figure:
    """One panel per change convention, shared time axis, consistent colours."""
    groups = list(result.groups.items())
    if not groups:
        return go.Figure()
    order = [i for _, ids in groups for i in ids]
    colors = {i: theme.SERIES_COLORS[k % len(theme.SERIES_COLORS)] for k, i in enumerate(order)}
    fig = make_subplots(rows=len(groups), cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=[GROUP_TITLES[u] for u, _ in groups],
                        row_heights=[2 if u is ChangeUnit.PCT else 1.4 for u, _ in groups])
    for row, (unit, ids) in enumerate(groups, start=1):
        for instrument_id in ids:
            series = result.series[instrument_id].dropna()
            name = names.get(instrument_id, instrument_id)
            fig.add_trace(go.Scatter(
                x=series.index, y=series.to_numpy(), name=name, mode="lines",
                line={"width": 1.8, "color": colors[instrument_id]},
                hovertemplate=f"%{{y:{HOVER_FORMAT[unit]}}}<extra>{name}</extra>",
            ), row=row, col=1)
        fig.add_hline(y=GROUP_REFERENCE[unit], line={"color": theme.RULE, "width": 1, "dash": "dot"},
                      row=row, col=1)
    fig.update_xaxes(showgrid=False, color=theme.MUTED)
    fig.update_yaxes(gridcolor=theme.RULE, zeroline=False, color=theme.MUTED)
    _apply_layout(fig, height=260 + 190 * len(groups))
    fig.update_layout(hovermode="x unified", legend={"orientation": "h", "y": -0.08, "x": 0})
    return fig


def correlation_heatmap(matrix: pd.DataFrame, names: dict[str, str], *, change: bool = False) -> go.Figure:
    """Annotated correlation (or correlation-change) matrix, diagonal left blank."""
    labels = [names.get(i, i) for i in matrix.index]
    values = matrix.to_numpy(dtype="float64", copy=True)
    for k in range(len(values)):
        values[k, k] = float("nan")
    limit = 0.5 if change else 1.0
    text = [[("" if math.isnan(v) else fr_number(v, 2, sign=change)) for v in row] for row in values]
    fig = go.Figure(go.Heatmap(
        z=values, x=labels, y=labels, text=text, texttemplate="%{text}", zmin=-limit, zmax=limit,
        colorscale=theme.plotly_colorscale(), xgap=1, ygap=1, hoverongaps=False,
        hovertemplate="%{y} / %{x} : %{text}<extra></extra>",
        textfont={"size": 10 if len(labels) <= 20 else 8, "color": theme.TEXT},
        colorbar={"thickness": 10, "tickfont": {"color": theme.MUTED},
                  "title": {"text": "Δ ρ" if change else "ρ"}},
    ))
    fig.update_yaxes(autorange="reversed", tickfont={"color": theme.TEXT})
    fig.update_xaxes(side="top", tickangle=-45, tickfont={"color": theme.TEXT})
    _apply_layout(fig, height=140 + 30 * len(labels))
    fig.update_layout(margin={"l": 0, "r": 0, "t": 120, "b": 0})
    return fig


def rolling_correlation_figure(series: pd.Series, label: str) -> go.Figure:
    """Rolling correlation of one pair, bounded to [-1, 1]."""
    data = series.dropna()
    fig = go.Figure(go.Scatter(
        x=data.index, y=data.to_numpy(), mode="lines", name=label,
        line={"width": 1.8, "color": theme.ACCENT}, hovertemplate="%{y:+.2f}<extra></extra>",
    ))
    fig.add_hline(y=0, line={"color": theme.RULE, "width": 1, "dash": "dot"})
    fig.update_yaxes(range=[-1, 1], gridcolor=theme.RULE, color=theme.MUTED)
    fig.update_xaxes(showgrid=False, color=theme.MUTED)
    _apply_layout(fig, height=300)
    return fig


def daily_chart_figure(chart: ChartData, height: int = 320) -> go.Figure:
    """Price history, Investing-style: area under a single line, last price tagged on the axis.

    The last session stays visible: last segment and point in the colour of the move, dotted
    line at the last level, and a tag on the right axis (green / red for a single series,
    series colour when several share the chart).
    """
    fig = go.Figure()
    single = len(chart.series) == 1
    tags: list[tuple[float, str, str]] = []
    for k, s in enumerate(chart.series):
        colour = theme.CHART_COLORS[k % len(theme.CHART_COLORS)]
        fig.add_trace(go.Scatter(
            x=s.values.index, y=s.values.to_numpy(), mode="lines", name=s.label,
            line={"width": 2, "color": colour},
            fill="tozeroy" if single else None, fillcolor=_rgba(colour, 0.14),
            hovertemplate=f"{s.label} : %{{y:,.{_hover_decimals(s)}f}}<extra></extra>",
        ))
        move = s.change_1d
        tone = theme.sign_color(round(move, 2) if not math.isnan(move) else move)
        tail = s.values.iloc[-2:]
        fig.add_trace(go.Scatter(  # last session: segment + point, coloured by the move
            x=tail.index, y=tail.to_numpy(), mode="lines+markers", showlegend=False,
            line={"width": 3, "color": tone},
            marker={"size": [0, 8][-len(tail):], "color": tone,
                    "line": {"width": 2, "color": theme.BACKGROUND}},
            hoverinfo="skip",
        ))
        tags.append((s.last, s.level_text(), tone if single else colour))
    if chart.series:
        span = (pd.Timestamp(chart.end) - pd.Timestamp(chart.start)) * 0.02
        fig.update_xaxes(range=[pd.Timestamp(chart.start), pd.Timestamp(chart.end) + span])
        fig.update_yaxes(range=list(value_range(chart)))
    tick_values, tick_text = french_date_ticks(pd.Timestamp(chart.start), pd.Timestamp(chart.end))
    fig.update_xaxes(showgrid=False, color=theme.MUTED, tickvals=tick_values, ticktext=tick_text,
                     hoverformat="%d/%m/%Y", showline=True, linecolor=theme.RULE)
    fig.update_yaxes(side="right", gridcolor=theme.RULE, color=theme.MUTED, zeroline=False,
                     tickformat=",~r", ticksuffix=f" {chart.unit_label}" if chart.unit_label else "")
    _apply_layout(fig, height=height)
    fig.update_layout(
        hovermode="x unified", showlegend=not single,
        legend={"orientation": "h", "y": 1.02, "x": 0, "yanchor": "bottom", "font": {"color": theme.TEXT}},
        margin={"l": 0, "r": 78, "t": 8 if single else 28, "b": 0},
    )
    for level, text, colour in tags:  # after _apply_layout, which restyles annotations as titles
        fig.add_hline(y=level, line={"color": colour, "width": 1, "dash": "dot"})
        fig.add_annotation(  # last-price tag on the right axis
            x=1, xref="paper", xanchor="left", y=level, yref="y", showarrow=False,
            text=f"<b>{text}</b>", bgcolor=colour, borderpad=3,
            font={"color": "#FFFFFF", "size": 11},
        )
    return fig


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


def french_date_ticks(start: pd.Timestamp, end: pd.Timestamp,
                      max_ticks: int = 6) -> tuple[list[pd.Timestamp], list[str]]:
    """French axis ticks: month starts (``janv.``), or days (``14 sept.``) on short windows."""
    if (end - start).days > 730:
        years = pd.date_range(start + pd.Timedelta(days=1), end, freq="YS")
        return list(years), [str(t.year) for t in years]
    if (end - start).days > 70:
        months = pd.date_range(start + pd.Timedelta(days=1), end, freq="MS")
        step = max(1, -(-len(months) // max_ticks))
        ticks = list(months[::step])
        text = [MONTHS_SHORT[t.month - 1] + (f" {t.year}" if t.month == 1 and step < 12 else "")
                for t in ticks]
        return ticks, text
    days = pd.date_range(start, end, periods=min(max_ticks, max((end - start).days, 1) + 1)).normalize()
    return list(days), [f"{t.day} {MONTHS_SHORT[t.month - 1]}" for t in days]


def _hover_decimals(series: ChartSeries) -> int:
    quote = series.instrument.quote.value
    return 3 if quote == "yield" else (1 if quote == "spread" else 2)


def _apply_layout(fig: go.Figure, height: int) -> None:
    fig.update_layout(
        separators=",\u202f",
        height=height, margin={"l": 0, "r": 0, "t": 30, "b": 0},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"family": theme.FONT_FAMILY, "color": theme.TEXT},
        hoverlabel={"bgcolor": theme.PANEL, "font": {"family": theme.FONT_FAMILY}},
    )
    for annotation in fig.layout.annotations:  # subplot titles: left-aligned, muted
        annotation.update(x=0, xanchor="left", font={"size": 13, "color": theme.MUTED})
