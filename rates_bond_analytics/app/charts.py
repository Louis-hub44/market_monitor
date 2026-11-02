"""Plotly figure builders (no Streamlit, no engine calls: they only format DataFrames)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from app.theme import ACCENT, DOWN, ESTR_COLOR, IBOR_COLOR, INFO, MUTED, RULE, SERIES, UP, plotly_layout

GREY = MUTED


def _layout(fig: go.Figure, title: str, y_title: str = "%", height: int = 360) -> go.Figure:
    fig.update_layout(**plotly_layout(title, y_title, height))
    return fig


def curves_figure(grid: pd.DataFrame, kind: str = "zero") -> go.Figure:
    fig = go.Figure()
    cols = [("estr_zero", "€STR zero"), ("e6m_zero", "EURIBOR 6M zero")] if kind == "zero" else \
        [("estr_fwd6m", "€STR 6M fwd"), ("e6m_fwd6m", "EURIBOR 6M fwd")]
    for (c, name), color in zip(cols, (ESTR_COLOR, IBOR_COLOR)):
        fig.add_scatter(x=grid["years"], y=grid[c] * 100, name=name, line={"color": color, "width": 2},
                        hovertemplate="%{y:.3f}%<extra>" + name + "</extra>")
    fig.update_xaxes(title_text="maturity (years)")
    return _layout(fig, "Zero rates · continuous, ACT/365F" if kind == "zero" else "6M forwards · ACT/360")


def basis_figure(par: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Bar(x=list(par.index), y=par["basis_bp"], marker_color=ACCENT, marker_line_width=0,
                           hovertemplate="%{x}: %{y:.1f} bp<extra></extra>"))
    return _layout(fig, "€STR / EURIBOR 6M basis", "bp", 300)


def sovereign_figure(fits: dict, horizon: int = 30) -> go.Figure:
    t = np.linspace(0.25, horizon, 240)
    fig = go.Figure()
    for (name, fit), color in zip(fits.items(), SERIES):
        fig.add_scatter(x=t, y=fit.curve.zero_rate_cc(t) * 100, name=f"{name}", line={"color": color, "width": 2},
                        hovertemplate="%{y:.3f}%<extra>" + name + "</extra>")
        used = fit.bonds
        fig.add_scatter(x=used["residual_years"], y=used["ytm_mkt"] * 100, mode="markers", name=f"{name} bonds",
                        showlegend=False,
                        marker={"color": color, "size": 6, "line": {"width": 0},
                                "symbol": ["circle" if u else "x" for u in used["used"]]},
                        text=used["name"], hovertemplate="%{text}<br>%{y:.3f}%<extra></extra>")
    fig.update_xaxes(title_text="maturity (years)")
    return _layout(fig, "Sovereign zero curves (NS / NSS) and bond yields", height=430)


def rich_cheap_figure(fit) -> go.Figure:
    b = fit.bonds.sort_values("residual_years")
    colors = [RULE if not u else (UP if v > 0 else DOWN) for u, v in zip(b["used"], b["rich_cheap_bp"])]
    fig = go.Figure(go.Bar(x=b["residual_years"], y=b["rich_cheap_bp"], marker_color=colors, marker_line_width=0,
                           text=b["name"], hovertemplate="%{text}<br>%{y:+.1f} bp<extra></extra>"))
    return _layout(fig, f"{fit.name} · rich (−) / cheap (+) vs fitted curve", "bp", 310)


def krd_figure(krd_dv01: pd.Series) -> go.Figure:
    fig = go.Figure(go.Bar(x=[f"{k:g}Y" for k in krd_dv01.index], y=krd_dv01.values, marker_color=INFO,
                           marker_line_width=0, hovertemplate="%{x}: %{y:,.0f} EUR<extra></extra>"))
    return _layout(fig, "Key-rate DV01 · EUR / bp", "EUR", 300)


def bucket_figure(table: pd.DataFrame) -> go.Figure:
    t = table.sort_values("pillar")
    if len(t) and t["dv01"].abs().max() > 0:   # hide the quotes the trade has no sensitivity to
        t = t[t["dv01"].abs() >= 1e-3 * t["dv01"].abs().max()]
    colors = [ESTR_COLOR if c == "ESTR" else IBOR_COLOR for c in t["curve"]]
    fig = go.Figure(go.Bar(x=t["instrument"] + " (" + t["curve"] + ")", y=t["dv01"], marker_color=colors,
                           marker_line_width=0, hovertemplate="%{x}: %{y:,.0f} EUR<extra></extra>"))
    return _layout(fig, "Bucketed DV01 per market quote · EUR / +1bp", "EUR", 330)


def scenario_figure(grid: pd.DataFrame, name: str) -> go.Figure:
    fig = go.Figure()
    fig.add_scatter(x=grid["years"], y=grid["e6m_zero"] * 100, name="EURIBOR 6M base", line={"color": GREY})
    fig.add_scatter(x=grid["years"], y=grid["e6m_zero_scen"] * 100, name=f"EURIBOR 6M {name}",
                    line={"color": ACCENT, "width": 2})
    fig.add_scatter(x=grid["years"], y=grid["shock_bp"], name="shock (bp, right)", yaxis="y2",
                    line={"color": INFO, "dash": "dot"})
    _layout(fig, f"Scenario · {name}", height=370)
    fig.update_layout(yaxis2={"overlaying": "y", "side": "right", "title": {"text": "bp"}, "showgrid": False,
                              "tickfont": {"color": MUTED}})
    return fig


def pnl_figure(pnl: pd.DataFrame) -> go.Figure:
    total = pnl.loc["TOTAL"].drop("base_value")
    fig = go.Figure(go.Bar(x=list(total.index), y=total.values, marker_line_width=0,
                           marker_color=[UP if v >= 0 else DOWN for v in total.values],
                           hovertemplate="%{x}: %{y:,.0f} EUR<extra></extra>"))
    return _layout(fig, "Book P&L by scenario · EUR", "EUR", 330)
