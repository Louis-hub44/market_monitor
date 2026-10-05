"""Corrélations: rolling correlation matrix, its change, and a pair drill-down."""

from __future__ import annotations

from datetime import date

import streamlit as st

from market_monitor.analytics.correlation import (
    CorrelationResult,
    Frequency,
    MatrixOrder,
    rolling_correlation,
)
from market_monitor.config import UiSettings
from market_monitor.exceptions import MarketMonitorError
from market_monitor.monitor import MarketMonitor
from market_monitor.text_format import (
    fr_number,
    short_date,
)
from market_monitor.ui.caching import cached, refresh_nonce
from market_monitor.ui.charts import correlation_heatmap, rolling_correlation_figure

WINDOWS = {
    Frequency.DAILY: {"1M": 21, "3M": 63, "6M": 126, "1A": 252},
    Frequency.WEEKLY: {"6M": 26, "1A": 52, "2A": 104},
}
DEFAULT_WINDOW = {Frequency.DAILY: "3M", Frequency.WEEKLY: "1A"}
LAG = {Frequency.DAILY: 21, Frequency.WEEKLY: 4}  # about one month
PERIOD_NAMES = {Frequency.DAILY: "séances", Frequency.WEEKLY: "semaines"}
FREQUENCY_LABELS = {Frequency.DAILY: "Quotidienne", Frequency.WEEKLY: "Hebdomadaire"}
ORDER_LABELS = {MatrixOrder.REFERENTIAL: "Référentiel", MatrixOrder.CLUSTER: "Regroupement"}
MAX_INSTRUMENTS = 30
DEFAULT_BASKET = "cross_asset"


def _compute(_monitor: MarketMonitor, monitor_id: int, ids: tuple[str, ...], as_of: str,
             window: int, frequency: Frequency, lag: int, order: MatrixOrder,
             nonce: int) -> CorrelationResult:
    return _monitor.correlation(ids, as_of, window=window, frequency=frequency, lag=lag, order=order)


def render_correlations(monitor: MarketMonitor, ui: UiSettings, watchlist: str, as_of: date,
                        key: str) -> None:
    ref = monitor.referential
    basket = DEFAULT_BASKET if DEFAULT_BASKET in ref.watchlist_names else watchlist
    default = list(ref.resolve(ref.watchlist(basket).instruments))[:MAX_INSTRUMENTS]
    ids = st.multiselect(
        "Instruments", options=list(ref.ids), default=default, max_selections=MAX_INSTRUMENTS,
        format_func=lambda i: ref.get(i).name, key=f"{key}-corr-ids",
    )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        frequency = st.segmented_control(
            "Fréquence", list(Frequency), default=Frequency.DAILY, required=True,
            format_func=lambda f: FREQUENCY_LABELS[f], key=f"{key}-corr-freq") or Frequency.DAILY
    windows = WINDOWS[frequency]
    with c2:
        window_label = st.segmented_control(
            "Fenêtre", list(windows), default=DEFAULT_WINDOW[frequency], required=True,
            key=f"{key}-corr-window-{frequency}") or DEFAULT_WINDOW[frequency]
    with c3:
        order = st.segmented_control(
            "Ordre", list(MatrixOrder), default=MatrixOrder.CLUSTER, required=True,
            format_func=lambda o: ORDER_LABELS[o], key=f"{key}-corr-order") or MatrixOrder.CLUSTER
    with c4:
        show_change = st.toggle("Variation sur un mois", key=f"{key}-corr-change")
    if len(ids) < 2:
        st.info("Choisissez au moins deux instruments.")
        return
    window = windows[window_label]
    compute = cached(_compute, ui, "Calcul des corrélations…")
    try:
        result = compute(monitor, id(monitor), tuple(ids), as_of.isoformat(), window, frequency,
                         LAG[frequency], order, refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Calcul impossible : {exc}")
        return
    _matrix_block(result, monitor, show_change, key)
    _pair_block(result, monitor, key)


def _matrix_block(result: CorrelationResult, monitor: MarketMonitor, show_change: bool, key: str) -> None:
    ref = monitor.referential
    if result.matrix.shape[0] < 2:
        st.warning("Pas assez de données communes pour calculer une matrice.")
        return
    names = {i: ref.get(i).name for i in result.matrix.index}
    unit = PERIOD_NAMES[result.frequency]
    end = short_date(result.end)
    st.caption(
        f"Corrélations de Pearson sur {result.window} {unit} jusqu'au {end}. Rendements en % pour "
        "les prix, variations en pb pour les taux et spreads : une corrélation positive entre une "
        "action et un taux signifie que le taux monte quand l'action monte."
    )
    matrix = result.change if show_change and result.change is not None else result.matrix
    if show_change and result.change is None:
        st.caption("Historique insuffisant pour la variation : la matrice actuelle est affichée.")
    st.plotly_chart(correlation_heatmap(matrix, names, change=matrix is result.change),
                    width="stretch", config={"displayModeBar": False}, key=f"{key}-corr-heat")
    excluded = [ref.get(i).name for i in result.errors if i in ref]
    if excluded:
        st.caption("Exclus faute de données suffisantes : " + ", ".join(excluded) + ".")


def _pair_block(result: CorrelationResult, monitor: MarketMonitor, key: str) -> None:
    labels = list(result.matrix.index)
    if len(labels) < 2:
        return
    ref = monitor.referential
    st.markdown("**Corrélation glissante d'une paire**")
    c1, c2 = st.columns(2)
    a = c1.selectbox("Premier instrument", labels, index=0, format_func=lambda i: ref.get(i).name,
                     key=f"{key}-pair-a")
    b = c2.selectbox("Second instrument", labels, index=1, format_func=lambda i: ref.get(i).name,
                     key=f"{key}-pair-b")
    if a == b:
        st.info("Choisissez deux instruments différents.")
        return
    series = rolling_correlation(result.returns, a, b, result.window)
    label = f"{ref.get(a).name} / {ref.get(b).name}"
    st.plotly_chart(rolling_correlation_figure(series, label), width="stretch",
                    config={"displayModeBar": False}, key=f"{key}-pair-chart")
    current = series.dropna()
    if not current.empty:
        st.caption(f"{label} : {fr_number(current.iloc[-1], 2, sign=True)} sur la dernière fenêtre, "
                   f"entre {fr_number(current.min(), 2, sign=True)} et "
                   f"{fr_number(current.max(), 2, sign=True)} sur l'historique affiché.")
