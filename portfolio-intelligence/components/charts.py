"""Graphiques Plotly de la plateforme — point d'entrée unique du style.

Deux règles suivies partout :

1. **La palette est résolue à l'appel**, jamais à l'import : un changement de
   thème n'exige donc aucun redémarrage.
2. **Catégoriel ≠ séquentiel.** Les répartitions par secteur, pays ou devise
   reçoivent des teintes franchement distinctes (`categorical_colors`) ; les
   dégradés sont réservés aux intensités, et l'échelle divergente rouge/bleu
   aux impacts signés autour de zéro.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config.theme import categorical_colors, palette, plotly_template


def _fig(height: int = 320, title: str = "") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(template=plotly_template(), height=height, title=title)
    return fig


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


# --------------------------------------------------------------------- #
# Répartitions
# --------------------------------------------------------------------- #
def bar_breakdown(data: dict[str, float], title: str = "", percent: bool = True,
                  height: int = 320, horizontal: bool = True) -> go.Figure:
    """Histogramme de répartition, une couleur distincte par catégorie."""
    fig = _fig(height, title)
    keys, values = list(data.keys()), list(data.values())
    fmt_axis = ".0%" if percent else ",.2f"
    fig.add_trace(
        go.Bar(
            x=values if horizontal else keys,
            y=keys if horizontal else values,
            orientation="h" if horizontal else "v",
            marker={"color": categorical_colors(keys),
                    "line": {"color": palette().surface, "width": 1}},
            text=[f"{v:.1%}" if percent else f"{v:,.2f}" for v in values],
            textposition="auto", textfont={"size": 10.5},
            hovertemplate=("%{y}: %{x:.2%}<extra></extra>" if percent
                           else "%{y}: %{x:,.2f}<extra></extra>"),
        )
    )
    fig.update_layout(**{("xaxis" if horizontal else "yaxis"): {"tickformat": fmt_axis}})
    if horizontal:
        fig.update_yaxes(autorange="reversed")
    return fig


def allocation_bar(data: dict[str, float], height: int = 74) -> go.Figure:
    """Barre empilée unique : la composition du portefeuille en une ligne.

    Plus lisible qu'un camembert pour comparer des poids, et bien plus compacte
    qu'un histogramme quand l'axe des catégories n'apporte rien.
    """
    p = palette()
    colors = categorical_colors(data.keys())
    fig = go.Figure()
    for (key, value), color in zip(data.items(), colors):
        if value <= 0:
            continue
        fig.add_trace(
            go.Bar(x=[value], y=[""], orientation="h", name=key, marker_color=color,
                   marker_line={"color": p.paper, "width": 1.5},
                   text=[f"{value:.0%}" if value >= 0.07 else ""],
                   textposition="inside", insidetextanchor="middle",
                   textfont={"size": 11, "color": p.paper if not p.is_dark else "#0F1319"},
                   hovertemplate=f"{key}: %{{x:.2%}}<extra></extra>")
        )
    total = sum(v for v in data.values() if v > 0) or 1.0
    fig.update_layout(template=plotly_template(), height=height, barmode="stack",
                      showlegend=False, margin={"l": 0, "r": 0, "t": 4, "b": 0},
                      xaxis={"visible": False, "range": [0, total]},
                      yaxis={"visible": False})
    return fig


def donut(data: dict[str, float], title: str = "", height: int = 300,
          center_label: str = "") -> go.Figure:
    """Anneau — réservé aux répartitions de faible cardinalité (≤ 8 postes)."""
    p = palette()
    keys = list(data.keys())
    fig = _fig(height, title)
    fig.add_trace(
        go.Pie(labels=keys, values=[data[k] for k in keys], hole=0.64, sort=False,
               marker={"colors": categorical_colors(keys),
                       "line": {"color": p.surface if p.is_dark else "#FFFFFF", "width": 1.5}},
               textinfo="none",
               hovertemplate="%{label}: %{percent}<extra></extra>")
    )
    if center_label:
        fig.add_annotation(text=center_label, showarrow=False, font={"size": 13, "color": p.ink_soft})
    fig.update_layout(showlegend=False, margin={"l": 8, "r": 8, "t": 30, "b": 8})
    return fig


# --------------------------------------------------------------------- #
# Impacts
# --------------------------------------------------------------------- #
def impact_bars(data: dict[str, float], title: str = "", height: int = 340,
                percent: bool = True) -> go.Figure:
    """Barres signées : rouge pour les impacts négatifs, vert pour les positifs."""
    p = palette()
    fig = _fig(height, title)
    keys, values = list(data.keys()), list(data.values())
    fig.add_trace(
        go.Bar(
            x=values, y=keys, orientation="h",
            marker_color=[p.negative if v < 0 else p.positive for v in values],
            text=[f"{v:+.2%}" if percent else f"{v:.2f}" for v in values],
            textposition="outside", textfont={"size": 10.5}, cliponaxis=False,
            hovertemplate=("%{y}: %{x:+.2%}<extra></extra>" if percent
                           else "%{y}: %{x:.2f}<extra></extra>"),
        )
    )
    span = max((abs(v) for v in values), default=0.01) or 0.01
    fig.update_layout(
        xaxis={"tickformat": "+.1%" if percent else ".2f",
               "range": [min(0, min(values, default=0)) - span * 0.35,
                         max(0, max(values, default=0)) + span * 0.35]},
        margin={"l": 4, "r": 40, "t": 34 if title else 10, "b": 28},
    )
    fig.update_yaxes(autorange="reversed")
    return fig


def ranked_impacts(rows: list[tuple[str, float]], height: int = 320,
                   max_items: int = 8) -> go.Figure:
    """Classement d'impacts, du pire au meilleur, valeurs lisibles sans survol."""
    p = palette()
    rows = sorted(rows, key=lambda kv: kv[1])[:max_items]
    labels = [name for name, _ in rows]
    values = [value for _, value in rows]
    fig = _fig(height)
    fig.add_trace(
        go.Bar(x=values, y=labels, orientation="h",
               marker={"color": [p.negative if v < 0 else p.positive for v in values],
                       "line": {"color": p.rule, "width": 0.5}},
               text=[f"{v:+.2%}" for v in values], textposition="outside",
               textfont={"size": 11}, cliponaxis=False,
               hovertemplate="%{y}: %{x:+.2%}<extra></extra>")
    )
    span = max((abs(v) for v in values), default=0.01) or 0.01
    fig.update_layout(
        xaxis={"tickformat": "+.0%", "range": [-span * 1.4, span * 0.6], "zeroline": True},
        margin={"l": 4, "r": 46, "t": 8, "b": 28}, showlegend=False,
    )
    fig.update_yaxes(autorange="reversed", tickfont={"size": 11.5})
    return fig


def waterfall(labels: list[str], values: list[float], title: str = "",
              height: int = 360) -> go.Figure:
    p = palette()
    fig = _fig(height, title)
    fig.add_trace(
        go.Waterfall(
            orientation="v",
            measure=["relative"] * len(values) + ["total"],
            x=labels + ["Total"], y=values + [0],
            connector={"line": {"color": p.neutral, "width": 1, "dash": "dot"}},
            decreasing={"marker": {"color": p.negative}},
            increasing={"marker": {"color": p.positive}},
            totals={"marker": {"color": p.accent}},
            textposition="outside", texttemplate="%{y:+.2%}",
            textfont={"size": 10.5},
            hovertemplate="%{x}: %{y:+.2%}<extra></extra>",
        )
    )
    fig.update_layout(yaxis={"tickformat": "+.1%"}, margin={"l": 50, "r": 20, "t": 34, "b": 60})
    return fig


# --------------------------------------------------------------------- #
# Séries et courbes
# --------------------------------------------------------------------- #
def line_series(series: dict[str, pd.Series], title: str = "", height: int = 340,
                y_format: str = ",.2f") -> go.Figure:
    fig = _fig(height, title)
    colors = categorical_colors(series.keys())
    for (name, s), color in zip(series.items(), colors):
        if s is None or len(s) == 0:
            continue
        fig.add_trace(go.Scatter(x=s.index, y=s.values, name=name, mode="lines",
                                 line={"width": 1.9, "color": color}))
    fig.update_layout(yaxis={"tickformat": y_format},
                      showlegend=len(series) > 1)
    return fig


def nav_area(series: pd.Series, height: int = 320, label: str = "NAV (base 100)") -> go.Figure:
    """Courbe de NAV avec aire, et sous-graphique de drawdown.

    Le niveau seul cache l'amplitude des replis ; les deux lectures partagent
    l'axe temporel et se lisent d'un seul regard.
    """
    p = palette()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28],
                        vertical_spacing=0.05)
    fig.add_trace(
        go.Scatter(x=series.index, y=series.values, name=label, mode="lines",
                   line={"width": 2, "color": p.accent}, fill="tozeroy",
                   fillcolor=_rgba(p.accent, 0.14),
                   hovertemplate="%{x|%d %b %Y} · %{y:.1f}<extra></extra>"),
        row=1, col=1,
    )
    drawdown = series / series.cummax() - 1.0
    fig.add_trace(
        go.Scatter(x=drawdown.index, y=drawdown.values, name="Drawdown", mode="lines",
                   line={"width": 1, "color": p.negative}, fill="tozeroy",
                   fillcolor=_rgba(p.negative, 0.20),
                   hovertemplate="%{x|%d %b %Y} · %{y:.2%}<extra></extra>"),
        row=2, col=1,
    )
    if len(drawdown):
        trough = drawdown.idxmin()
        fig.add_annotation(x=trough, y=float(drawdown.min()), row=2, col=1,
                           text=f"{drawdown.min():.1%}", showarrow=False, yshift=-11,
                           font={"size": 10, "color": p.negative})
    fig.update_layout(template=plotly_template(), height=height, showlegend=False,
                      margin={"l": 56, "r": 18, "t": 10, "b": 28})
    fig.update_yaxes(tickformat=".0f", row=1, col=1)
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    fig.update_xaxes(showgrid=False, row=1, col=1)
    return fig


def yield_curve(curves: dict[str, dict[str, float]], title: str = "Courbes des taux",
                height: int = 360) -> go.Figure:
    fig = _fig(height, title)
    colors = categorical_colors(curves.keys())
    for (name, points), color in zip(curves.items(), colors):
        fig.add_trace(go.Scatter(x=list(points.keys()), y=list(points.values()), name=name,
                                 mode="lines+markers", line={"width": 2.2, "color": color},
                                 marker={"size": 6},
                                 hovertemplate=f"{name} %{{x}} : %{{y:.2%}}<extra></extra>"))
    fig.update_layout(yaxis={"tickformat": ".2%"}, xaxis={"title": "Maturité"},
                      showlegend=len(curves) > 1)
    return fig


def krd_bars(krd: dict[str, float], title: str = "Key Rate Duration",
             height: int = 320) -> go.Figure:
    """KRD par segment : dégradé séquentiel, car la maturité est une grandeur ordonnée."""
    p = palette()
    keys = list(krd.keys())
    shades = [p.sequential[min(len(p.sequential) - 1, 1 + i * (len(p.sequential) - 2) // max(len(keys) - 1, 1))]
              for i in range(len(keys))]
    fig = _fig(height, title)
    fig.add_trace(
        go.Bar(x=keys, y=list(krd.values()), marker_color=shades,
               text=[f"{v:.2f}" for v in krd.values()], textposition="outside",
               textfont={"size": 10.5}, cliponaxis=False,
               hovertemplate="%{x}: %{y:.2f} an(s)<extra></extra>")
    )
    fig.update_layout(yaxis={"title": "années", "tickformat": ".2f"})
    return fig


def heatmap(frame: pd.DataFrame, title: str = "", percent: bool = True,
            diverging: bool = True, height: int = 420) -> go.Figure:
    p = palette()
    fig = _fig(height, title)
    fig.add_trace(
        go.Heatmap(
            z=frame.values, x=list(frame.columns), y=list(frame.index),
            colorscale=list(p.diverging if diverging else p.sequential),
            zmid=0 if diverging else None,
            texttemplate="%{z:+.2%}" if percent else "%{z:.2f}",
            textfont={"size": 10.5},
            xgap=1, ygap=1,
            hovertemplate=("%{y} / %{x}: %{z:+.2%}<extra></extra>" if percent
                           else "%{y} / %{x}: %{z:.2f}<extra></extra>"),
            colorbar={"tickformat": "+.0%" if percent else ".2f", "thickness": 12,
                      "outlinewidth": 0},
        )
    )
    return fig


def valuation_history_chart(series: pd.Series, current: float, mean: float,
                            label: str, height: int = 300) -> go.Figure:
    p = palette()
    fig = _fig(height, f"{label} — historique")
    fig.add_trace(go.Scatter(x=list(series.index), y=list(series.values), mode="lines+markers",
                             name=label, line={"width": 2.2, "color": p.accent},
                             marker={"size": 6}, fill="tozeroy",
                             fillcolor=_rgba(p.accent, 0.10)))
    fig.add_hline(y=mean, line_dash="dash", line_color=p.neutral,
                  annotation_text=f"moyenne {mean:.1f}x",
                  annotation_font={"size": 10, "color": p.ink_soft})
    fig.add_hline(y=current, line_color=p.negative if current < mean else p.positive,
                  annotation_text=f"actuel {current:.1f}x",
                  annotation_font={"size": 10})
    return fig
