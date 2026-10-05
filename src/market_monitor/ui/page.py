"""Streamlit rendering of the Market Monitor home screen.

``render_market_monitor`` draws everything inside the current page and can be called
from the terminal's multipage app; ``main`` is the standalone entry point.
Widget keys are prefixed with ``key`` so the page can coexist with other modules.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from html import escape
from typing import Any

import pandas as pd
import streamlit as st

from market_monitor.alerts import AlertRule, load_rules
from market_monitor.analytics.performance import Horizon, rank_movers
from market_monitor.config import Settings, UiSettings, configure_logging, load_settings
from market_monitor.exceptions import MarketMonitorError
from market_monitor.export import DailyMacroLayout, load_layout
from market_monitor.monitor import MarketMonitor, PerformanceReport
from market_monitor.text_format import (
    ASSET_CLASS_LABELS,
    HORIZON_LABELS,
    SOURCE_LABELS,
    format_change,
    fr_number,
    french_date,
    short_date,
)
from market_monitor.ui import theme
from market_monitor.ui.alerts_view import alerts_strip, load_alerts, render_alerts
from market_monitor.ui.caching import bump_refresh_nonce, cached, refresh_nonce
from market_monitor.ui.charts import heat_tiles
from market_monitor.ui.correlation_view import render_correlations
from market_monitor.ui.daily_macro_view import render_daily_macro
from market_monitor.ui.formatting import (
    display_table,
    style_table,
)
from market_monitor.ui.history_view import render_history

FULL_WIDTH_ROWS = 8   # classes with more rows than this get the full page width
ROW_HEIGHT_PX = 35


# ====================================================================== public
VIEWS = {"overview": "Vue d'ensemble", "history": "Historique", "correlations": "Corrélations",
         "alerts": "Alertes", "daily": "Daily macro"}


def render_market_monitor(monitor: MarketMonitor, ui: UiSettings, *, key: str = "mm",
                          layout: DailyMacroLayout | None = None,
                          rules: Sequence[AlertRule] | None = None) -> None:
    """Draw the dashboard for ``monitor`` in the current Streamlit container."""
    watchlist, as_of = _sidebar(monitor, ui, key)
    st.markdown('<div class="mm-title">Market Monitor</div>', unsafe_allow_html=True)
    view = st.segmented_control("Vue", list(VIEWS), default="overview", required=True,
                                format_func=lambda v: VIEWS[v], label_visibility="collapsed",
                                key=f"{key}-view") or "overview"
    if view == "history":
        render_history(monitor, ui, watchlist, as_of, key)
    elif view == "correlations":
        render_correlations(monitor, ui, watchlist, as_of, key)
    elif view == "alerts":
        render_alerts(monitor, rules, ui, as_of, key)
    elif view == "daily":
        render_daily_macro(monitor, ui, layout, as_of, key, rules)
    else:
        _overview(monitor, ui, watchlist, as_of, key, rules)


def _overview(monitor: MarketMonitor, ui: UiSettings, watchlist: str, as_of: date, key: str,
              rules: Sequence[AlertRule] | None) -> None:
    report = _load_report(monitor, watchlist, as_of, ui, key)
    if report is None:
        return
    _header(report)
    alerts_strip(load_alerts(monitor, rules, ui, as_of, key))
    if report.table["level"].isna().all():
        st.warning("Aucune donnée pour cette sélection. Le panneau Qualité des données détaille les sources.")
        _diagnostics(report)
        return
    _movers(report, ui)
    horizon = st.segmented_control(
        "Couleur des tuiles", options=[Horizon.D1, Horizon.W1], default=Horizon.D1, required=True,
        format_func=lambda h: f"z-score {HORIZON_LABELS[h.value]}", key=f"{key}-horizon",
    ) or Horizon.D1
    st.plotly_chart(
        heat_tiles(report.table, horizon, columns=ui.heatmap_columns, clip=ui.zscore_clip),
        width="stretch", config={"displayModeBar": False}, key=f"{key}-heat",
    )
    _tables(report, ui)
    _diagnostics(report)


def main() -> None:
    """Standalone app: ``streamlit run app.py``."""
    st.set_page_config(page_title="Market Monitor", layout="wide")
    st.markdown(theme.page_css(), unsafe_allow_html=True)
    try:
        settings = _settings()
        configure_logging(settings)
        monitor = _monitor(settings)
        layout = _layout(settings, monitor)
        rules = _rules(settings, monitor)
    except MarketMonitorError as exc:
        st.error(f"Configuration invalide : {exc}")
        st.stop()
    render_market_monitor(monitor, settings.ui, layout=layout, rules=rules)


# ===================================================================== loading
@st.cache_resource(show_spinner=False)
def _settings() -> Settings:
    return load_settings()


@st.cache_resource(show_spinner="Connexion aux sources de données…")
def _monitor(_settings: Settings) -> MarketMonitor:
    return MarketMonitor.from_settings(_settings)


@st.cache_resource(show_spinner=False)
def _layout(_settings: Settings, _monitor: MarketMonitor) -> DailyMacroLayout | None:
    if not _settings.daily_macro_path.is_file():
        return None
    return load_layout(_settings.daily_macro_path, _monitor.referential)


@st.cache_resource(show_spinner=False)
def _rules(_settings: Settings, _monitor: MarketMonitor) -> tuple[AlertRule, ...]:
    if not _settings.alerts_path.is_file():
        return ()
    return load_rules(_settings.alerts_path, _monitor.referential)


def _compute_report(_monitor: MarketMonitor, monitor_id: int, watchlist: str, as_of: str,
                    nonce: int) -> PerformanceReport:
    return _monitor.performance(watchlist, as_of)


def _load_report(monitor: MarketMonitor, watchlist: str, as_of: date, ui: UiSettings,
                 key: str) -> PerformanceReport | None:
    compute = cached(_compute_report, ui, "Chargement des marchés…")
    try:
        return compute(monitor, id(monitor), watchlist, as_of.isoformat(), refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Chargement impossible : {exc}")
        return None


# ===================================================================== sidebar
def _sidebar(monitor: MarketMonitor, ui: UiSettings, key: str) -> tuple[str, date]:
    ref = monitor.referential
    names = list(ref.watchlist_names) or ["*"]
    default = names.index(ui.default_watchlist) if ui.default_watchlist in names else 0
    with st.sidebar:
        watchlist = st.selectbox(
            "Watchlist", names, index=default, key=f"{key}-watchlist",
            format_func=lambda n: ref.watchlist(n).description or n if n in ref.watchlist_names else n,
        )
        today = monitor.today()
        as_of = st.date_input("Date d'arrêté", value=today, max_value=today, format="DD/MM/YYYY",
                              key=f"{key}-asof")
        if st.button("Actualiser les données", key=f"{key}-refresh"):
            bump_refresh_nonce(key)
        chain = ", ".join(SOURCE_LABELS.get(n, n) for n in monitor.service.provider_names)
        st.caption(f"Sources par ordre de priorité : {chain}.")
    return watchlist, as_of if isinstance(as_of, date) else today


# ====================================================================== blocks
def _header(report: PerformanceReport) -> None:
    table = report.table
    last = table["level_date"].max()
    stale = int(table["stale"].sum())
    sentence = f"Arrêté au {french_date(report.as_of)}. {len(table)} instruments"
    if pd.notna(last):
        sentence += f", dernière cotation le {short_date(last)}"
    st.markdown(f'<div class="mm-asof">{escape(sentence)}.</div>', unsafe_allow_html=True)
    if stale:
        st.caption(f"{stale} ligne(s) sans cotation récente, affichées en grisé.")


def _movers(report: PerformanceReport, ui: UiSettings) -> None:
    movers = rank_movers(report.table, Horizon.D1, ui.top_movers)
    if movers.empty:
        return
    items = []
    for _, row in movers.iterrows():
        color = theme.sign_color(row["chg_1d"])
        change = format_change(row["chg_1d"], row["change_unit"])
        items.append(
            f'<span class="mm-mover"><span class="name">{escape(row["name"])}</span>'
            f'<span class="chg" style="color:{color}">{change}</span>'
            f'<span class="z">z {fr_number(row["z_1d"], 1, sign=True)}</span></span>'
        )
    st.markdown("**Mouvements marquants sur la séance**", unsafe_allow_html=False)
    st.markdown(f'<div class="mm-movers">{"".join(items)}</div>', unsafe_allow_html=True)


def _tables(report: PerformanceReport, ui: UiSettings) -> None:
    sections = list(report.by_asset_class().items())
    wide = [(ac, t) for ac, t in sections if len(t) > FULL_WIDTH_ROWS]
    narrow = [(ac, t) for ac, t in sections if len(t) <= FULL_WIDTH_ROWS]
    for asset_class, table in wide:
        _table_block(asset_class.value, table, ui)
    for i in range(0, len(narrow), 2):
        columns = st.columns(2, gap="large")
        for column, (asset_class, table) in zip(columns, narrow[i:i + 2], strict=False):
            with column:
                _table_block(asset_class.value, table, ui)


def _table_block(asset_class: str, table: pd.DataFrame, ui: UiSettings) -> None:
    st.markdown(f'<h3 class="mm-class">{ASSET_CLASS_LABELS[asset_class]}</h3>', unsafe_allow_html=True)
    display = display_table(table)
    st.dataframe(
        style_table(display, table, ui.zscore_clip), hide_index=True, width="stretch",
        height=(len(display) + 1) * ROW_HEIGHT_PX + 3,
        column_config={"Groupe": st.column_config.TextColumn(width="medium"),
                       "Instrument": st.column_config.TextColumn(width="medium")},
    )


def _diagnostics(report: PerformanceReport) -> None:
    table = report.table
    proxies: dict[str, Any] = {str(k): v for k, v in table["proxy"].dropna().items()}
    n_issues = len(report.errors) + len(report.warnings) + len(proxies)
    with st.expander(f"Qualité des données ({n_issues} point(s) d'attention)", expanded=False):
        sources = Counter(SOURCE_LABELS.get(s, s) for s in table["source"].dropna())
        st.caption("Lignes servies par source : "
                   + (", ".join(f"{k} {v}" for k, v in sources.items()) or "aucune") + ".")
        sections: list[tuple[str, dict[str, Any]]] = [
            ("Données manquantes", dict(report.errors)),
            ("Avertissements", dict(report.warnings)),
            ("Proxies utilisés", proxies),
        ]
        for title, issues in sections:
            if issues:
                st.markdown(f"**{title}**")
                st.dataframe(_issues_frame(issues, table), hide_index=True, width="stretch")


def _issues_frame(issues: dict[str, Any], table: pd.DataFrame) -> pd.DataFrame:
    names = table["name"].reindex(list(issues)).fillna("")
    return pd.DataFrame({"Instrument": names.to_numpy(), "Code": list(issues),
                         "Détail": [str(v) for v in issues.values()]})
