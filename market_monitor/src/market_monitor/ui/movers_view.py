"""Top movers of the equity indices on their last closed session.

Per index: top / flop 10 (return, close, volume against its 20-session average, sector,
contribution in points), contributions chart, breadth, sectors, members to check. The
Stoxx 600 (~430 more symbols) is loaded only on request, never at start-up.
"""

from __future__ import annotations

import math
from datetime import date
from functools import partial
from html import escape

import pandas as pd
import streamlit as st
from pandas.io.formats.style import Styler

from market_monitor.config import UiSettings
from market_monitor.equities.instruments import stock_id
from market_monitor.equities.movers import IndexMovers, verify_members
from market_monitor.equities.universe import EquityUniverse
from market_monitor.exceptions import MarketMonitorError
from market_monitor.monitor import MarketMonitor
from market_monitor.text_format import MISSING, fr_number, french_date
from market_monitor.ui import theme
from market_monitor.ui.caching import cached, refresh_nonce
from market_monitor.ui.charts import contributions_figure, sectors_figure
from market_monitor.ui.instrument_view import open_instrument

EXTENDED_INDEX = "SXXP"
TOP_N = 10
ROW_HEIGHT_PX = 35
CHART_CONFIG = {"displayModeBar": False}
WEIGHT_LABELS = {"holdings": "poids de l'ETF répliquant", "marketcap": "poids estimés (capitalisation)",
                 "none": "sans poids"}
COLUMNS = ["Titre", "Secteur", "Cours", "Var. %", "Vol./20 j", "Contrib."]
#: sectors shortened for the half-width tables (the sector chart keeps the full names)
SHORT_SECTORS = {"Consommation discrétionnaire": "Conso. discr.", "Consommation de base": "Conso. base",
                 "Services aux collectivités": "Serv. publics", "Technologie": "Techno.",
                 "Télécommunications": "Télécoms", "Matériaux de base": "Matériaux",
                 "Immobilier": "Immobilier", "Services de communication": "Communication"}


def _compute(_universe: EquityUniverse, _monitor: MarketMonitor | None, monitor_id: int, index: str,
             as_of: str, stamp: str, nonce: int) -> IndexMovers:
    return _universe.movers(index, _monitor, date.fromisoformat(as_of))


def load_movers(universe: EquityUniverse, monitor: MarketMonitor | None, ui: UiSettings, index: str,
                as_of: date, key: str) -> IndexMovers:
    """Movers of ``index`` (cached until the stored prices or the refresh nonce change)."""
    compute = cached(_compute, ui, "Calcul des movers…")
    result: IndexMovers = compute(universe, monitor, id(monitor), index, as_of.isoformat(),
                                  universe.panel.stamp(), refresh_nonce(key))
    return result


def loaded_indices(universe: EquityUniverse) -> list[str]:
    """Core indices, plus the Stoxx 600 once its members have prices."""
    core = universe.indices()
    last = universe.panel.field("last")
    extended = [i for i in universe.indices(with_extended=True) if i not in core]
    shown = [i for i in extended
             if sum(t in last.columns for t in universe.compositions.get(i).tickers) >=
             max(1, len(universe.compositions.get(i).members) // 2)]
    return core + shown


def render_movers(monitor: MarketMonitor, universe: EquityUniverse | None, ui: UiSettings, as_of: date,
                  key: str) -> None:
    if universe is None:
        st.info("Couche actions non configurée (section equities de config.yaml).")
        return
    universe.panel.stamp()  # reload the stored prices if another process wrote them
    indices = loaded_indices(universe)
    if not indices:
        st.info("Aucune composition d'indice : config/constituents/*.yaml.")
        return
    names = {c.index: c.name for c in universe.compositions}
    controls = st.columns([4, 3, 3], vertical_alignment="bottom")
    index = controls[0].selectbox("Indice", indices, format_func=lambda i: names.get(i, i),
                                  key=f"{key}-movers-index")
    if controls[1].button("Actualiser les cours des titres", key=f"{key}-movers-refresh",
                          icon=":material/refresh:", help="Indices principaux (~700 titres, Yahoo)"):
        _refresh(universe, None, "Cours des titres")
    if EXTENDED_INDEX in names and EXTENDED_INDEX not in indices:
        if controls[2].button("Charger le Stoxx 600", key=f"{key}-movers-sxxp", icon=":material/download:",
                              help="~430 titres de plus : quelques minutes au premier chargement"):
            _refresh(universe, [EXTENDED_INDEX], "Stoxx 600")
            st.rerun()
    elif EXTENDED_INDEX in indices and controls[2].button(
            "Actualiser le Stoxx 600", key=f"{key}-movers-sxxp-refresh", icon=":material/refresh:"):
        _refresh(universe, [EXTENDED_INDEX], "Stoxx 600")
    try:
        movers = load_movers(universe, monitor, ui, index, as_of, key)
    except MarketMonitorError as exc:
        st.error(f"Movers indisponibles : {exc}")
        return
    if movers.session is None or movers.counted.empty:
        for note in movers.notes or ["aucun titre comptable sur la séance"]:
            st.warning(note)
        return
    _apply_checks(movers, key)
    _header(movers)
    unit = "pts" if not math.isnan(movers.contributions_total) else "%"
    left, right = st.columns(2, gap="large")
    with left:
        _section("Plus fortes hausses")
        _table(movers.top(TOP_N), unit, key, f"{key}-movers-top-{index}")
    with right:
        _section("Plus fortes baisses")
        _table(movers.flop(TOP_N), unit, key, f"{key}-movers-flop-{index}")
    _verify_button(universe, monitor, movers, key)
    left, right = st.columns(2, gap="large")
    with left:
        scale = "points" if unit == "pts" else "% de l'indice"
        _section(f"Contributions à la variation de l'indice ({scale})")
        contributors = movers.contributors(TOP_N)
        if contributors.empty:
            st.caption("Pas de poids : contributions non calculées.")
        else:
            st.plotly_chart(contributions_figure(contributors, unit), width="stretch", config=CHART_CONFIG,
                            key=f"{key}-movers-contrib")
    with right:
        _section("Performance moyenne par secteur (équipondérée)")
        sectors = movers.sectors()
        st.plotly_chart(sectors_figure(sectors), width="stretch", config=CHART_CONFIG,
                        key=f"{key}-movers-sectors")
    _breadth(movers)
    _checks(movers)


# ---------------------------------------------------------------------- blocks
def _section(title: str) -> None:
    st.markdown(f'<div class="mm-section">{escape(title)}</div>', unsafe_allow_html=True)


def _refresh(universe: EquityUniverse, indices: list[str] | None, label: str) -> None:
    bar = st.progress(0.0, text=f"{label} : chargement…")

    def progress(done: int, total: int) -> None:
        bar.progress(min(done / max(total, 1), 1.0), text=f"{label} : {done} / {total} titres")
    try:
        report = universe.refresh_prices(indices, progress=progress)
    except MarketMonitorError as exc:
        bar.empty()
        st.error(f"{label} : chargement impossible ({exc})")
        return
    bar.empty()
    (st.warning if report.stopped or report.failed else st.success)(f"{label} : {report.summary()}")


def _header(movers: IndexMovers) -> None:
    b = movers.breadth()
    change = (f"{fr_number(movers.index_change_pct, 2, sign=True)} %"
              if not math.isnan(movers.index_change_pct) else MISSING)
    points = (f"{fr_number(movers.index_change_pts, 1, sign=True)} pts à {fr_number(movers.index_level, 2)}"
              if not math.isnan(movers.index_change_pts) else "niveau de l'indice indisponible")
    comp = (f"composition du {movers.composition_as_of:%d/%m/%Y}" if movers.composition_as_of
            else "composition non datée")
    tiles = [
        (movers.name, change, points, theme.sign_color(movers.index_change_pct)),
        ("Séance", french_date(movers.session) if movers.session else MISSING,
         f"contre le {movers.previous:%d/%m}" if movers.previous else "", theme.TEXT),
        ("Hausses / baisses", f"{b['advancers']} / {b['decliners']}",
         f"{b['counted']} titres comptés sur {b['members']}", theme.TEXT),
        ("Composition", WEIGHT_LABELS.get(movers.weights, movers.weights), comp, theme.TEXT),
    ]
    html = "".join(f'<div class="mm-kpi"><div class="lbl">{escape(label)}</div>'
                   f'<div class="val" style="color:{colour};font-size:1.05rem">{escape(value)}</div>'
                   f'<div style="color:{theme.MUTED};font-size:.8rem">{escape(sub)}</div></div>'
                   for label, value, sub, colour in tiles)
    st.markdown(f'<div class="mm-kpis" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">'
                f'{html}</div>', unsafe_allow_html=True)


def movers_table(rows: pd.DataFrame, unit: str = "pts") -> pd.DataFrame:
    """Display frame of a top / flop table (French formatting)."""
    column = "contribution" if unit == "pts" else "contribution_pct"
    decimals = 1 if unit == "pts" else 2
    return pd.DataFrame({
        "Titre": list(rows["name"]),
        "Secteur": [SHORT_SECTORS.get(s, s) or "Autres" for s in rows["sector"]],
        "Cours": [fr_number(v, 2) for v in rows["close"]],
        "Var. %": [f"{fr_number(v, 2, sign=True)} %" for v in rows["return_pct"]],
        "Vol./20 j": [f"{fr_number(v, 1)}×" if not pd.isna(v) else MISSING
                           for v in rows["volume_ratio"]],
        "Contrib.": [fr_number(v, decimals, sign=True) for v in pd.to_numeric(rows[column], errors="coerce")],
    })


def style_movers(display: pd.DataFrame, rows: pd.DataFrame, unit: str = "pts") -> Styler:
    column = "contribution" if unit == "pts" else "contribution_pct"

    def cells(_: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=display.index, columns=display.columns)
        for k, (ret, contrib, ratio) in enumerate(zip(rows["return_pct"], pd.to_numeric(rows[column],
                                                      errors="coerce"), rows["volume_ratio"], strict=True)):
            styles.iat[k, 3] = f"color: {theme.sign_color(float(ret))}; font-weight: 600;"
            styles.iat[k, 5] = f"color: {theme.sign_color(float(contrib))};"
            if not pd.isna(ratio) and float(ratio) >= 2.0:
                styles.iat[k, 4] = f"color: {theme.ACCENT}; font-weight: 600;"
            styles.iat[k, 1] = f"color: {theme.MUTED};"
        return styles
    return display.style.apply(cells, axis=None)


def _table(rows: pd.DataFrame, unit: str, key: str, table_key: str) -> None:
    if rows.empty:
        st.caption("Aucun titre.")
        return
    display = movers_table(rows, unit)
    st.dataframe(
        style_movers(display, rows, unit), hide_index=True, width="stretch",
        height=(len(display) + 1) * ROW_HEIGHT_PX + 3, key=table_key, selection_mode="single-row",
        on_select=partial(_open_selected, key, table_key, [stock_id(str(t)) for t in rows["ticker"]]),
        column_config={
            "Titre": st.column_config.TextColumn(width=120),
            "Secteur": st.column_config.TextColumn(width=95),
            "Cours": st.column_config.TextColumn(width=62),
            "Var. %": st.column_config.TextColumn(width=64),
            "Vol./20 j": st.column_config.TextColumn(width=52, help="Volume du jour / moyenne 20 séances"),
            "Contrib.": st.column_config.TextColumn(width=55, help="Contribution à la variation de l'indice, "
                                                    + ("en points" if unit == "pts" else "en % de l'indice")),
        },
    )


def _open_selected(key: str, table_key: str, ids: list[str]) -> None:
    state = st.session_state.get(table_key)
    rows = list(state["selection"]["rows"]) if state else []
    if rows and rows[0] < len(ids):
        open_instrument(key, ids[rows[0]])
        st.session_state.pop(table_key, None)


def _verified_key(key: str, movers: IndexMovers) -> str:
    return f"{key}-movers-verified-{movers.index}-{movers.session}"


def _apply_checks(movers: IndexMovers, key: str) -> None:
    """Re-apply a second-source check run earlier on this index and session."""
    for ticker, verdict in st.session_state.get(_verified_key(key, movers), {}).items():
        if verdict.startswith("écart") and ticker in movers.members.index:
            movers.members.at[ticker, "status"] = "à vérifier"
            movers.members.at[ticker, "reason"] = verdict
            movers.checks.append(f"{movers.members.at[ticker, 'name']} ({ticker}) : {verdict}")


def _verify_button(universe: EquityUniverse, monitor: MarketMonitor, movers: IndexMovers, key: str) -> None:
    done = st.session_state.get(_verified_key(key, movers))
    second = universe.second_source(monitor, movers.index)
    if done:
        gaps = sum(v.startswith("écart") for v in done.values())
        missing = sum(v == "indisponible" for v in done.values())
        ok = len(done) - gaps - missing
        st.caption(f"Contrôle 2e source (FMP) : {ok} concordant(s), {gaps} écart(s) passé(s) en "
                   f"« À vérifier », {missing} indisponible(s).")
        return
    if second is None:
        st.caption("Contrôle 2e source : FMP non actif. Cours Yahoo, à vérifier avant diffusion.")
        return
    tickers = list(dict.fromkeys([*movers.top(TOP_N)["ticker"], *movers.flop(TOP_N)["ticker"]]))
    if st.button(f"Contrôler les {len(tickers)} titres affichés avec FMP", key=f"{key}-movers-verify",
                 icon=":material/fact_check:", help="Un appel FMP par titre (plan gratuit : 250 par jour)"):
        st.session_state[_verified_key(key, movers)] = verify_members(movers, tickers, second)
        st.rerun()


def _breadth(movers: IndexMovers) -> None:
    b = movers.breadth()

    def pct(v: float) -> str:
        return f"{fr_number(v, 0)} %" if not math.isnan(v) else MISSING
    tiles = [("Hausses", str(b["advancers"]), theme.UP), ("Baisses", str(b["decliners"]), theme.DOWN),
             ("Inchangés", str(b["unchanged"]), theme.TEXT),
             ("Au-dessus de la MM 50", pct(b["above_ma50"]), theme.TEXT),
             ("Au-dessus de la MM 200", pct(b["above_ma200"]), theme.TEXT),
             ("Plus hauts 52 sem.", str(b["new_highs"]), theme.UP),
             ("Plus bas 52 sem.", str(b["new_lows"]), theme.DOWN)]
    html = "".join(f'<div class="mm-kpi"><div class="lbl">{escape(label)}</div>'
                   f'<div class="val" style="color:{colour}">{escape(value)}</div></div>'
                   for label, value, colour in tiles)
    st.markdown('<div class="mm-section">Largeur de marché</div>'
                '<div class="mm-kpis mm-kpis-tight" '
                f'style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">{html}</div>',
                unsafe_allow_html=True)


def _checks(movers: IndexMovers) -> None:
    if movers.checks:
        _section(f"À vérifier ({len(movers.checks)}) - exclus des movers, de la largeur et des contributions")
        for check in movers.checks:
            st.markdown(f'<div class="mm-alert">{escape(check)}</div>', unsafe_allow_html=True)
    for note in movers.notes:
        st.caption(note)
    st.caption("Clôtures Yahoo, en devise de l'indice ; un titre non coté sur la séance n'est pas compté. "
               "Contribution = poids de la veille (dérivé des cours depuis la date de composition) "
               "× rendement × niveau de la veille. Sélectionner une ligne ouvre la fiche du titre.")
