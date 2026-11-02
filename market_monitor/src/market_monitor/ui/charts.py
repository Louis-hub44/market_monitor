"""Plotly figures (pure functions, no Streamlit)."""

from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from market_monitor.analytics.comparison import ComparisonResult
from market_monitor.analytics.curves import COUNTRY_LABELS, SHORT_LABELS, CurveSet
from market_monitor.analytics.instrument import InstrumentProfile, candle_frequency, resample_bars
from market_monitor.analytics.performance import ZSCORE_HORIZONS, Horizon
from market_monitor.analytics.quality import needs_check
from market_monitor.export.charts import (
    ChartData,
    ChartSeries,
    axis_decimals,
    date_ticks,
    period_extremes,
    value_range,
)
from market_monitor.export.layout import ChartSpec
from market_monitor.referential import AssetClass, ChangeUnit, QuoteType
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


def daily_chart_figure(chart: ChartData, height: int = 360) -> go.Figure:
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
        if single:
            _mark_extremes(fig, s)
    if chart.series:
        span = (pd.Timestamp(chart.end) - pd.Timestamp(chart.start)) * 0.02
        fig.update_xaxes(range=[pd.Timestamp(chart.start), pd.Timestamp(chart.end) + span])
        fig.update_yaxes(range=list(value_range(chart)))
    ticks = date_ticks(chart.start, chart.end)
    spikes = {"showspikes": True, "spikemode": "across", "spikesnap": "cursor",
              "spikethickness": -1, "spikedash": "dot", "spikecolor": theme.MUTED}  # -1: no halo
    fig.update_xaxes(showgrid=True, gridcolor=_rgba(theme.RULE, 0.5), color=theme.MUTED,
                     tickvals=[t for t, _ in ticks], ticktext=[label for _, label in ticks],
                     hoverformat="%d/%m/%Y", showline=True, linecolor=theme.RULE, **spikes)
    decimals = axis_decimals(chart) if chart.series else 2
    fig.update_yaxes(side="right", gridcolor=theme.RULE, color=theme.MUTED, zeroline=False, nticks=8,
                     tickformat=f",.{decimals}f",
                     ticksuffix=f" {chart.unit_label}" if chart.unit_label else "", **spikes)
    _apply_layout(fig, height=height)
    fig.update_layout(
        hovermode="x unified", showlegend=not single, spikedistance=-1,
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


def _mark_extremes(fig: go.Figure, series: ChartSeries) -> None:
    """Period high and low: small markers with level and date."""
    decimals = _hover_decimals(series)
    for (when, level), position in zip(period_extremes(series.values), ("top center", "bottom center"),
                                       strict=True):
        fig.add_trace(go.Scatter(
            x=[when], y=[level], mode="markers+text", showlegend=False, hoverinfo="skip",
            marker={"size": 5, "color": theme.MUTED},
            text=[f"{fr_number(level, decimals)} ({when:%d/%m})"], textposition=position,
            textfont={"size": 10, "color": theme.MUTED},
        ))


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


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


# ============================================================ instrument page
MA_STYLES = {"ma50": ("MM 50", theme.CHART_COLORS[1]), "ma200": ("MM 200", theme.CHART_COLORS[3])}


def profile_chart_data(profile: InstrumentProfile) -> ChartData:
    """The page's closes as a daily-macro chart (same style, extremes, last-price tag)."""
    inst = profile.instrument
    window = profile.window["last"].dropna()
    spec = ChartSpec(inst.name, (inst.id,), period=profile.period,
                     start=profile.start if profile.period == "Max" else None)
    series = (ChartSeries(inst.id, inst.name, window, inst),) if not window.empty else ()
    return ChartData(spec, profile.start, profile.as_of, series)


def instrument_figure(profile: InstrumentProfile, *, candles: bool = False, averages: bool = True,
                      height: int = 460) -> go.Figure:
    """Level history of one instrument: area (or candlesticks), 50 / 200-session averages,
    period high and low, last close tagged on the axis, optional second instrument."""
    chart = profile_chart_data(profile)
    fig = daily_chart_figure(chart, height=height)
    if not chart.series:
        return fig
    window = profile.window
    lows, highs = [window["last"].min()], [window["last"].max()]
    if candles:
        bars = window.dropna(subset=["open", "high", "low"])
        frequency = candle_frequency(profile.start, profile.as_of)
        if frequency != "D":
            bars = resample_bars(bars, frequency)
        decimals = _hover_decimals(chart.series[0])
        candle = go.Candlestick(
            x=bars.index, open=bars["open"], high=bars["high"], low=bars["low"], close=bars["last"],
            name=profile.instrument.name, showlegend=False,
            increasing={"line": {"color": theme.UP, "width": 1}, "fillcolor": theme.UP},
            decreasing={"line": {"color": theme.DOWN, "width": 1}, "fillcolor": theme.DOWN},
            hovertext=[f"O {fr_number(o, decimals)} · H {fr_number(h, decimals)} · "
                       f"B {fr_number(lo, decimals)} · C {fr_number(c, decimals)}"
                       for o, h, lo, c in bars[["open", "high", "low", "last"]].to_numpy()],
            hoverinfo="text",
        )
        extremes = [t for t in fig.data if getattr(t, "mode", None) == "markers+text"]
        fig = go.Figure(data=[candle, *(t.to_plotly_json() for t in extremes)], layout=fig.layout)
        lows.append(bars["low"].min())
        highs.append(bars["high"].max())
        fig.update_xaxes(rangeslider_visible=False)
    shown_ma = []
    if averages:
        for column, values in profile.averages().items():
            values = values.dropna()
            if values.empty:
                continue
            label, colour = MA_STYLES[str(column)]
            fig.add_trace(go.Scatter(x=values.index, y=values.to_numpy(), mode="lines", name=label,
                                     line={"width": 1.3, "color": colour, "dash": "dot"},
                                     hovertemplate=f"{label} : %{{y:,.{_hover_decimals(chart.series[0])}f}}"
                                                   "<extra></extra>"))
            lows.append(values.min())
            highs.append(values.max())
            shown_ma.append(label)
    overlay = _add_overlay(fig, profile)
    if overlay and _overlay_same_axis(profile) and profile.overlay is not None:
        shown = profile.overlay.loc[pd.Timestamp(profile.start):pd.Timestamp(profile.as_of)]
        lows.append(shown.min())
        highs.append(shown.max())
    # main axis only: ``update_yaxes`` would also force the overlay's own axis onto this range
    fig.update_layout(yaxis={"range": _padded(min(lows), max(highs))})
    if overlay and not _overlay_same_axis(profile) and profile.overlay is not None:
        shown = profile.overlay.loc[pd.Timestamp(profile.start):pd.Timestamp(profile.as_of)].dropna()
        fig.update_layout(yaxis2={"range": _padded(shown.min(), shown.max())})
    if shown_ma or overlay:
        fig.update_layout(showlegend=True, margin={"t": 28})
    return fig


def _padded(low: float, high: float) -> list[float]:
    low, high = float(low), float(high)
    pad = (high - low) * 0.08 or abs(high) * 0.01 or 1.0
    return [low - pad, high + pad]


def _add_overlay(fig: go.Figure, profile: InstrumentProfile) -> bool:
    """Second instrument: on the same axis when the unit matches, else on a left axis."""
    if profile.overlay is None or profile.overlay_id is None:
        return False
    values = profile.overlay.loc[pd.Timestamp(profile.start):pd.Timestamp(profile.as_of)]
    if values.empty:
        return False
    name = profile.overlay_name or profile.overlay_id
    same_axis = _overlay_same_axis(profile)
    colour = theme.CHART_COLORS[2]
    decimals = 0 if values.abs().max() >= 1_000 else 2
    fig.add_trace(go.Scatter(
        x=values.index, y=values.to_numpy(), mode="lines", name=name, yaxis="y" if same_axis else "y2",
        line={"width": 1.6, "color": colour},
        hovertemplate=f"{name} : %{{y:,.{decimals}f}}<extra></extra>",
    ))
    if not same_axis:
        fig.update_layout(yaxis2={"overlaying": "y", "side": "left", "showgrid": False,
                                  "color": colour, "tickformat": f",.{decimals}f", "zeroline": False})
    return True


def _overlay_same_axis(profile: InstrumentProfile) -> bool:
    """Yields with yields, spreads with spreads share the axis; anything else gets its own."""
    quote = profile.instrument.quote.value
    return profile.overlay_quote == quote and quote in ("yield", "spread")


def spread_figure(profile: InstrumentProfile, height: int = 300, *, pair: bool = False) -> go.Figure | None:
    """Spread (bp) in the daily-macro style: against the Bund of the same maturity, or with
    ``pair`` against the instrument laid over the chart (OAT − BTP, 2 ans − 10 ans...)."""
    series = profile.pair_spread if pair else profile.spread
    if series is None or (profile.overlay_id if pair else profile.benchmark_id) is None:
        return None
    window = series.loc[pd.Timestamp(profile.start):pd.Timestamp(profile.as_of)].dropna()
    if window.empty:
        return None
    inst = profile.instrument
    other = profile.overlay_name if pair else "Bund"
    spread_inst = replace(inst, id=f"{inst.id}_{'PAIR' if pair else 'BUND'}",
                          name=f"{inst.name} − {other}" if pair else f"Spread {inst.name} − Bund",
                          quote=QuoteType.SPREAD, unit="pb", change=ChangeUnit.BP, tickers={},
                          derived=None, curve=None)
    spec = ChartSpec(spread_inst.name, (spread_inst.id,), period=profile.period,
                     start=profile.start if profile.period == "Max" else None)
    chart = ChartData(spec, profile.start, profile.as_of,
                      (ChartSeries(spread_inst.id, spread_inst.name, window, spread_inst),))
    return daily_chart_figure(chart, height=height)


# ================================================================ yield curves
TENOR_TICKS = [2, 5, 10, 30]
COMPARISON_STYLES = {"J-1": ("dot", theme.CHART_COLORS[2]), "S-1": ("dash", theme.CHART_COLORS[1]),
                     "M-1": ("dashdot", theme.CHART_COLORS[3])}


def country_colour(country: str, countries: list[str]) -> str:
    return theme.SERIES_COLORS[countries.index(country) % len(theme.SERIES_COLORS)]


def _tenor_axis(fig: go.Figure, **where: int) -> None:
    fig.update_xaxes(type="log", tickvals=TENOR_TICKS, ticktext=[f"{t} ans" for t in TENOR_TICKS],
                     gridcolor=_rgba(theme.RULE, 0.5), color=theme.MUTED, **where)
    fig.update_yaxes(ticksuffix=" %", tickformat=",.2f", gridcolor=theme.RULE, color=theme.MUTED,
                     zeroline=False, **where)


def curves_today_figure(curves: CurveSet, countries: list[str], height: int = 380) -> go.Figure:
    """Today's curves of every selected country on one chart."""
    fig = go.Figure()
    for country in countries:
        curve = curves.curve(country).dropna()
        if curve.empty:
            continue
        colour = country_colour(country, countries)
        fig.add_trace(go.Scatter(
            x=curve.index, y=curve.to_numpy(), mode="lines+markers", name=SHORT_LABELS.get(country, country),
            line={"width": 2.2, "color": colour}, marker={"size": 7},
            hovertemplate=f"{SHORT_LABELS.get(country, country)} %{{x}} ans : %{{y:.3f}} %<extra></extra>",
        ))
    _tenor_axis(fig)
    _apply_layout(fig, height)
    fig.update_layout(legend={"orientation": "h", "y": 1.04, "x": 0, "yanchor": "bottom"},
                      hovermode="closest", margin={"t": 30})
    return fig


def curves_vs_figure(curves: CurveSet, countries: list[str], comparisons: list[str],
                     height_per_row: int = 260) -> go.Figure:
    """One panel per country: today's curve against J-1 / S-1 / M-1."""
    columns = min(3, max(1, len(countries)))
    rows = math.ceil(len(countries) / columns) if countries else 1
    fig = make_subplots(rows=rows, cols=columns, horizontal_spacing=0.06, vertical_spacing=0.16,
                        subplot_titles=[COUNTRY_LABELS.get(c, c) for c in countries])
    for k, country in enumerate(countries):
        row, col = k // columns + 1, k % columns + 1
        for comparison in comparisons:
            past = curves.curve(country, comparison).dropna()
            dash, colour = COMPARISON_STYLES[comparison]
            fig.add_trace(go.Scatter(
                x=past.index, y=past.to_numpy(), mode="lines+markers", name=comparison,
                legendgroup=comparison, showlegend=k == 0,
                line={"width": 1.4, "color": colour, "dash": dash}, marker={"size": 4},
                hovertemplate=f"{comparison} %{{x}} ans : %{{y:.3f}} %<extra></extra>"),
                row=row, col=col)
        today = curves.curve(country).dropna()
        fig.add_trace(go.Scatter(
            x=today.index, y=today.to_numpy(), mode="lines+markers", name="Aujourd'hui",
            legendgroup="today", showlegend=k == 0,
            line={"width": 2.4, "color": theme.ACCENT}, marker={"size": 6},
            hovertemplate="%{x} ans : %{y:.3f} %<extra></extra>"), row=row, col=col)
    _tenor_axis(fig)
    _apply_layout(fig, height_per_row * rows)
    for k, annotation in enumerate(fig.layout.annotations):  # titles back over their own panel
        axis = fig.layout[f"xaxis{k + 1 if k else ''}"]
        annotation.update(x=axis.domain[0])
    fig.update_layout(legend={"orientation": "h", "y": 1.0 + 0.12 / rows, "x": 0, "yanchor": "bottom"},
                      hovermode="closest", margin={"t": 60})
    return fig


def curve_changes_figure(curves: CurveSet, countries: list[str], comparison: str,
                         height: int = 340) -> go.Figure:
    """Changes by maturity (bp) since ``comparison``, one bar per country and tenor."""
    fig = go.Figure()
    points = curves.points
    for country in countries:
        rows = points[points["country"] == country].sort_values("tenor")
        if rows.empty:
            continue
        values = rows[f"chg_{comparison}"].to_numpy(dtype="float64")
        fig.add_trace(go.Bar(
            x=[f"{t:g} ans" for t in rows["tenor"]], y=values, name=SHORT_LABELS.get(country, country),
            marker={"color": country_colour(country, countries)},
            text=[fr_number(v, 1, sign=True) for v in values], textposition="outside",
            textfont={"size": 10, "color": theme.MUTED},
            hovertemplate=f"{SHORT_LABELS.get(country, country)} %{{x}} : %{{y:+.1f}} pb<extra></extra>",
        ))
    fig.add_hline(y=0, line={"color": theme.MUTED, "width": 1})
    fig.update_yaxes(ticksuffix=" pb", gridcolor=theme.RULE, color=theme.MUTED, zeroline=False)
    fig.update_xaxes(color=theme.MUTED, categoryorder="array",
                     categoryarray=[f"{t} ans" for t in TENOR_TICKS])
    _apply_layout(fig, height)
    fig.update_layout(barmode="group", bargap=0.25,
                      legend={"orientation": "h", "y": 1.04, "x": 0, "yanchor": "bottom"},
                      margin={"t": 30})
    return fig


# ================================================================ top movers
def contributions_figure(table: pd.DataFrame, unit: str = "pts", height: int | None = None) -> go.Figure:
    """Horizontal bars: contribution of each member to the index move (``contribution`` in
    points, or ``contribution_pct`` in % of the index when ``unit`` is ``"%"``)."""
    column = "contribution" if unit == "pts" else "contribution_pct"
    rows = table.assign(_v=pd.to_numeric(table[column], errors="coerce")).dropna(subset=["_v"])
    rows = rows.sort_values("_v")
    values = rows["_v"].to_numpy(dtype="float64")
    decimals = 1 if unit == "pts" else 2
    fig = go.Figure(go.Bar(
        x=values, y=list(rows["name"]), orientation="h",
        marker={"color": [theme.UP if v >= 0 else theme.DOWN for v in values]},
        text=[fr_number(v, decimals, sign=True) for v in values], textposition="outside",
        textfont={"size": 10, "color": theme.MUTED}, cliponaxis=False,
        hovertemplate=f"%{{y}} : %{{x:+.{decimals}f}} {unit}<extra></extra>",
    ))
    fig.add_vline(x=0, line={"color": theme.MUTED, "width": 1})
    fig.update_xaxes(ticksuffix=f" {unit}", gridcolor=theme.RULE, color=theme.MUTED, zeroline=False,
                     range=_padded_range(values))
    fig.update_yaxes(color=theme.TEXT, automargin=True)
    _apply_layout(fig, height or max(220, 24 * len(rows) + 50))
    fig.update_layout(margin={"t": 10, "r": 40}, bargap=0.25)
    return fig


def sectors_figure(sectors: pd.DataFrame, height: int | None = None) -> go.Figure:
    """Average return by sector (equal weight), members counted in the hover."""
    rows = sectors.sort_values("average_pct")
    values = rows["average_pct"].to_numpy(dtype="float64")
    fig = go.Figure(go.Bar(
        x=values, y=list(rows["sector"]), orientation="h",
        marker={"color": [theme.UP if v >= 0 else theme.DOWN for v in values]},
        text=[f"{fr_number(v, 2, sign=True)} %" for v in values], textposition="outside",
        textfont={"size": 10, "color": theme.MUTED}, cliponaxis=False,
        customdata=rows["members"].to_numpy(),
        hovertemplate="%{y} : %{x:+.2f} % (%{customdata} titres)<extra></extra>",
    ))
    fig.add_vline(x=0, line={"color": theme.MUTED, "width": 1})
    fig.update_xaxes(ticksuffix=" %", gridcolor=theme.RULE, color=theme.MUTED, zeroline=False,
                     range=_padded_range(values))
    fig.update_yaxes(color=theme.TEXT, automargin=True)
    _apply_layout(fig, height or max(220, 26 * len(rows) + 50))
    fig.update_layout(margin={"t": 10, "r": 50}, bargap=0.25)
    return fig


def _padded_range(values: object, pad: float = 0.3) -> list[float]:
    """x range of a signed bar chart with room for the outside labels on both sides."""
    import numpy as np
    finite = np.asarray(values, dtype="float64")
    finite = finite[np.isfinite(finite)]
    low, high = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    span = max(high - low, abs(high), abs(low), 1e-9)
    return [min(low, 0.0) - pad * span * (low < 0), max(high, 0.0) + pad * span * (high > 0)]
