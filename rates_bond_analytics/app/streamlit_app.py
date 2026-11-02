"""Rates & Bond Analytics – Streamlit dashboard (GC / YAS / SWPM-like).

Run from the project root:  streamlit run app/streamlit_app.py

Start screen first (data source + corporate SSL policy), then the terminal. The UI only
calls :mod:`app.services` (which calls the engine), :mod:`app.charts` and :mod:`app.theme`.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from app import charts, services, theme  # noqa: E402
from app.connection import (  # noqa: E402
    PROVIDERS,
    back_to_gate,
    render_gate,
    render_sidebar_status,
    render_ssl_rescue,
)
from rates_analytics.config import load_settings  # noqa: E402
from rates_analytics.exceptions import RatesAnalyticsError  # noqa: E402
from rates_analytics.network import NetworkSettings  # noqa: E402
from rates_analytics.scenarios import (  # noqa: E402
    butterfly,
    custom,
    flattener,
    irrbb_scenarios,
    parallel,
    steepener,
)

st.set_page_config(page_title="Rates & Bond Analytics", page_icon="📈", layout="wide")
st.markdown(theme.page_css(), unsafe_allow_html=True)
PCT = "{:.4%}".format
BP = "{:+.1f}".format
EUR = "{:,.0f}".format
SAMPLE_DATE = date(2026, 10, 6)


@st.cache_resource(show_spinner="Construction des courbes et fit des courbes souveraines…")
def get_state(as_of: date, provider: str, interpolation: str, network: NetworkSettings) -> services.MarketState:
    return services.load_market_state(as_of, provider, interpolation, network=network)


@st.cache_resource
def default_settings():
    return load_settings()


# ------------------------------------------------------------ start screen
try:
    base_settings = default_settings()
except RatesAnalyticsError as exc:
    st.error(f"config.yaml invalide : {exc}")
    st.stop()
connection = render_gate(base_settings)
if connection is None:
    st.stop()

# --------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown('<div class="rba-title" style="font-size:1.35rem">Rates &amp; Bond</div>'
                '<div class="rba-side-title" style="margin-top:0">Analytics · GC · YAS · SWPM</div>',
                unsafe_allow_html=True)
    default_date = SAMPLE_DATE if connection.offline else date.today() - timedelta(days=1)
    as_of = st.date_input("Date de valorisation", value=default_date)
    interpolation = st.radio("Interpolation des courbes", ["log_linear", "monotone_convex"],
                             format_func=lambda m: {"log_linear": "Log-linéaire (DF)",
                                                    "monotone_convex": "Monotone convexe"}[m])
    if st.button("Recharger les données", icon=":material/refresh:", width="stretch"):
        get_state.clear()
    render_sidebar_status(connection)
    if connection.offline:
        st.caption("Données d'exemple illustratives (pas des données de marché).")

try:
    state = get_state(as_of, connection.provider, interpolation, connection.network)
except RatesAnalyticsError as exc:
    st.markdown(theme.section("Chargement impossible"), unsafe_allow_html=True)
    st.error(str(exc))
    render_ssl_rescue({"load": str(exc)}, connection)
    if st.button("Revenir à l'écran de connexion", icon=":material/arrow_back:"):
        back_to_gate()
        st.rerun()
    st.stop()

# ---------------------------------------------------------------- top bar
swap_src = state.sources.get("swap curves", "market")
chips = [theme.chip(PROVIDERS.get(connection.provider, connection.provider)),
         theme.chip(f"{state.as_of:%d %b %Y}"),
         theme.chip("Courbes swap · PROXY" if swap_src == "proxy" else "Courbes swap · marché",
                    "warn" if swap_src == "proxy" else "ok")]
gov_src = {state.sources.get(n) for n in state.sovereigns}
if gov_src:
    labels = {"bonds": "obligations", "benchmarks": "taux génériques"}
    chips.append(theme.chip("Souverains · " + (" / ".join(sorted(labels.get(s, s) for s in gov_src if s))),
                            "warn" if "benchmarks" in gov_src else "ok"))
if connection.offline:
    chips.append(theme.chip("Hors ligne · exemple", "warn"))
st.markdown(f'<div class="rba-topbar"><div class="rba-title">Rates &amp; Bond Analytics'
            f'<span>EUR · €STR · EURIBOR 6M · EGB</span></div><div class="rba-chips">{"".join(chips)}</div></div>',
            unsafe_allow_html=True)
render_ssl_rescue(state.data_errors, connection)
if state.proxy.used:
    st.markdown(
        '<div class="rba-note"><b>Courbes de swap reconstruites (proxy).</b> Les sources gratuites ne publient '
        "pas de cotations OIS / IRS en euro : la courbe €STR est la courbe AAA de la BCE (Svensson) recalée sur "
        f"le fixing €STR ({state.proxy.estr_fixing:.3%}) avec un spread OIS−AAA, la courbe EURIBOR 6M ajoute "
        "la basis de <code>config.yaml</code> (<code>proxy_curves</code>). Ordres de grandeur fiables, pas des "
        "niveaux de trading : Bloomberg donne les cotations réelles.</div>", unsafe_allow_html=True)
if "benchmarks" in gov_src:
    st.caption("Courbes souveraines ajustées sur les taux génériques 2/5/10/30 ans (CNBC → Bundesbank → Stooq), "
               "faute de données obligataires titre par titre : rich/cheap non significatif.")
with st.expander(f"Avertissements ({len(state.warnings)})", expanded=False) if state.warnings else st.container():
    for w in state.warnings:
        st.warning(w)

tabs = st.tabs(["Courbes · GC", "Souverains", "Obligation · YAS", "Swap · SWPM", "Scénarios", "Données"])

# ------------------------------------------------------------------ GC
with tabs[0]:
    par = services.par_rates(state.curves)
    tiles = [(f"EUR swap {t}", f"{par.loc[t, 'irs_par']:.3%}", f"€STR {par.loc[t, 'ois_par']:.3%} · basis "
              f"{par.loc[t, 'basis_bp']:.1f} bp") for t in ("2Y", "5Y", "10Y", "30Y")]
    slope = (par.loc["10Y", "irs_par"] - par.loc["2Y", "irs_par"]) * 1e4
    tiles.append(("2s10s", f"{slope:+.1f} bp", "pente EURIBOR 6M"))
    st.markdown(theme.kpi_tiles(tiles), unsafe_allow_html=True)
    grid = services.curve_grid(state.curves)
    left, right = st.columns(2)
    left.plotly_chart(charts.curves_figure(grid, "zero"), width="stretch")
    right.plotly_chart(charts.curves_figure(grid, "fwd"), width="stretch")
    left, right = st.columns([2, 1])
    left.dataframe(par.style.format({"ois_par": PCT, "irs_par": PCT, "basis_bp": "{:.2f}", "estr_zero": PCT}),
                   width="stretch")
    right.plotly_chart(charts.basis_figure(par), width="stretch")
    with st.expander("Bootstrap reports"):
        for name, res in [("€STR", state.curves.discount), *state.curves.forwards.items()]:
            st.markdown(f"**{name}** – max repricing error {res.max_abs_error_bp():.1e} bp")
            st.dataframe(res.report(), width="stretch", hide_index=True)

# ------------------------------------------------------------ sovereigns
with tabs[1]:
    st.markdown(theme.section("Courbes souveraines", "Nelson-Siegel(-Svensson)"), unsafe_allow_html=True)
    if not state.sovereigns:
        st.info("No sovereign universe configured.")
    else:
        st.plotly_chart(charts.sovereign_figure(state.sovereigns), width="stretch")
        left, right = st.columns(2)
        left.markdown("**Fits and par yields**")
        left.dataframe(services.sovereign_table(state).style.format(
            dict.fromkeys(("par_2Y", "par_5Y", "par_10Y", "par_30Y"), PCT) | {"rmse_bp": "{:.2f}"}),
            width="stretch")
        right.markdown("**Zero spreads vs Bund (bp)**")
        right.dataframe(services.sovereign_spreads(state).style.format("{:.1f}"), width="stretch")
        name = st.selectbox("Rich / cheap", list(state.sovereigns))
        fit = state.sovereigns[name]
        st.plotly_chart(charts.rich_cheap_figure(fit), width="stretch")
        st.dataframe(fit.rich_cheap().style.format({"coupon": PCT, "ytm_mkt": PCT, "ytm_model": PCT,
                                                    "rich_cheap_bp": BP}), width="stretch")
        st.caption("NSS parameters: " + ", ".join(f"{k} {v:.4g}" for k, v in fit.params.as_dict().items()
                                                  if v is not None))

# ------------------------------------------------------------------ YAS
with tabs[2]:
    st.markdown(theme.section("Analytics obligataires (YAS)", "rendement, risque, spreads G / I / Z / ASW"), unsafe_allow_html=True)
    issuers = {"All": None, "Germany": "DE", "France": "FR", "Italy": "IT", "Spain": "ES"}
    c1, c2, c3, c4 = st.columns([1, 3, 1, 1])
    issuer = issuers[c1.selectbox("Issuer", list(issuers))]
    choices = services.bond_choices(state, issuer)
    if not choices:
        st.info("No bond available.")
    else:
        isin = c2.selectbox("Bond", choices, format_func=lambda i: f"{state.bonds[i]}  [{i}]")
        clean = c3.number_input("Clean price", value=float(state.prices.get(isin, 100.0)), step=0.01, format="%.3f")
        nominal = c4.number_input("Nominal", value=1_000_000.0, step=100_000.0, format="%.0f")
        reference = st.radio("KRD reference curve (+ Z-spread)", ["ESTR", "EURIBOR6M", *state.sovereigns],
                             horizontal=True)
        try:
            rep, spreads = services.bond_report(state, isin, clean, nominal, reference)
        except RatesAnalyticsError as exc:
            st.error(str(exc))
        else:
            a = rep.analytics
            m = st.columns(6)
            m[0].metric("Yield (street)", PCT(a.ytm))
            m[1].metric("True yield", PCT(a.true_yield))
            m[2].metric("Mod. duration", f"{a.modified:.3f}")
            m[3].metric("Convexity", f"{a.convexity:.2f}")
            m[4].metric("DV01 (nominal)", EUR(rep.amounts["dv01"]))
            m[5].metric("Accrued", f"{a.accrued:.6f}", f"{a.accrued_days} days", delta_color="off")
            s = spreads.as_series()
            m = st.columns(6)
            for col, (label, key) in zip(m, (("G vs Bund", "g_spread"), ("I-spread", "i_spread"),
                                              ("Z vs 6M", "z_spread_swap"), ("Z vs €STR", "z_spread_ois"),
                                              ("ASW 6M", "asw_euribor"), ("ASW €STR", "asw_estr"))):
                col.metric(label, "n/a" if s[key] is None else f"{s[key]:.1f} bp")
            left, right = st.columns([1, 1])
            left.markdown("**Yield analytics**")
            left.dataframe(rep.summary().astype(str), width="stretch")
            right.plotly_chart(charts.krd_figure(rep.curve.krd_dv01(nominal)), width="stretch")
            right.caption(f"Effective duration {rep.curve.effective_duration:.3f} = Σ KRD {rep.curve.krd_sum:.3f}; "
                          f"Z-spread {rep.extra['z_spread'] * 1e4:.1f} bp over {reference}")
            st.markdown("**Cash flows**")
            st.dataframe(rep.cashflows, width="stretch", hide_index=True)
        if issuer:
            st.markdown("**Universe spreads (bp)**")
            st.dataframe(services.universe_spreads(state, issuer), width="stretch")

# ----------------------------------------------------------------- SWPM
with tabs[3]:
    st.markdown(theme.section("Pricer de swaps (SWPM)", "dual curve, fixings, risque bucketé"), unsafe_allow_html=True)
    c = st.columns(6)
    kind = c[0].selectbox("Type", ["IRS", "OIS"], help="IRS: fixed annual 30/360 vs EURIBOR 6M; OIS: vs €STR")
    start = c[1].date_input("Effective", value=state.as_of + timedelta(days=2))
    maturity = c[2].date_input("Maturity", value=date(state.as_of.year + 10, state.as_of.month, 8))
    notional = c[3].number_input("Notional", value=10_000_000.0, step=1_000_000.0, format="%.0f")
    side = c[4].radio("Side", ["Pay fixed", "Receive fixed"])
    try:
        par_k = services.par_rate_for(state, kind, start, maturity)
    except RatesAnalyticsError as exc:
        st.error(str(exc))
        par_k = None
    val = None
    if par_k is not None:
        fixed = c[5].number_input("Fixed rate (%)", value=round(par_k * 100, 4), step=0.01, format="%.4f") / 100
        spread_bp = st.number_input("Float spread (bp)", value=0.0, step=1.0)
        try:
            swap = services.make_swap(state, kind, start, maturity, fixed, notional, side == "Pay fixed",
                                      spread_bp * 1e-4)
            val = services.price_swap(state, swap)
        except RatesAnalyticsError as exc:
            st.error(str(exc))
    if val is not None:
        s = val.summary()
        m = st.columns(6)
        m[0].metric("NPV", EUR(s["npv"]))
        m[1].metric("Clean NPV", EUR(s["clean_npv"]), f"accrued {EUR(s['accrued'])}", delta_color="off")
        m[2].metric("Par rate", PCT(s["par_rate"]))
        m[3].metric("Par spread", f"{s['par_spread_bp']:+.2f} bp")
        m[4].metric("PV01", EUR(s["pv01"]))
        m[5].metric("DV01", EUR(s["dv01"]))
        st.plotly_chart(charts.bucket_figure(val.bucketed.table), width="stretch")
        left, right = st.columns(2)
        left.markdown(f"**Fixed leg** – PV {EUR(val.fixed_leg.pv)}")
        left.dataframe(val.fixed_leg.cashflows, width="stretch", hide_index=True)
        right.markdown(f"**Floating leg** – PV {EUR(val.float_leg.pv)}")
        right.dataframe(val.float_leg.cashflows, width="stretch", hide_index=True)

# ------------------------------------------------------------ scenarios
with tabs[4]:
    st.markdown(theme.section("Scénarios de courbe", "P&L du book, IRRBB"), unsafe_allow_html=True)
    library = {
        "Parallel +25bp": parallel(25), "Parallel -25bp": parallel(-25), "Parallel +100bp": parallel(100),
        "Bear steepener 2s10s 25bp": steepener(25, kind="bear"), "Bull steepener 2s10s 25bp": steepener(25, kind="bull"),
        "Bear flattener 2s10s 25bp": flattener(25, kind="bear"), "Bull flattener 2s10s 25bp": flattener(25, kind="bull"),
        "Butterfly 2s5s10s +15bp": butterfly(15),
        **{s.name: s for s in irrbb_scenarios("EUR")},
    }
    c1, c2, c3 = st.columns([3, 1, 1])
    chosen = c1.multiselect("Scenarios", list(library), default=list(library)[:5])
    mode = c2.radio("Apply to", ["quotes", "zero"], help="quotes: par quotes + re-bootstrap; zero: zero rates")
    target = c3.radio("Curves", ["All", "EURIBOR6M only (basis)"])
    with st.expander("Custom scenario (bp per pillar)"):
        default = pd.DataFrame({"pillar": ["2Y", "5Y", "10Y", "30Y"], "shock_bp": [0.0, 0.0, 0.0, 0.0]})
        edited = st.data_editor(default, num_rows="dynamic", width="stretch")
        rows = edited.dropna()
        if (rows["shock_bp"] != 0).any():
            try:
                cs = custom(dict(zip(rows["pillar"], rows["shock_bp"])), name="Custom")
                library["Custom"] = cs
                chosen = [*chosen, "Custom"]
            except RatesAnalyticsError as exc:
                st.error(str(exc))
    targets = None if target == "All" else ["EURIBOR6M"]
    book = services.default_book(state)
    st.caption("Book: " + " · ".join(p.name for p in book))
    if chosen:
        scenarios = [library[n] for n in chosen]
        pnl, par = services.run_scenarios(state, book, scenarios, mode, targets)
        st.plotly_chart(charts.pnl_figure(pnl), width="stretch")
        st.dataframe(pnl.style.format(EUR), width="stretch")
        left, right = st.columns(2)
        left.markdown("**Par swap rates – base (%) and changes (bp)**")
        par_fmt = par.copy()
        par_fmt["base"] = par_fmt["base"] * 100
        left.dataframe(par_fmt.style.format("{:.3f}"), width="stretch")
        show = right.selectbox("Curve view", chosen)
        right.plotly_chart(charts.scenario_figure(services.scenario_curve_grid(state, library[show], mode, targets),
                                                  show), width="stretch")

# ------------------------------------------------------------------ data
with tabs[5]:
    st.markdown(theme.section("Données de marché", "sources, cache, qualité"), unsafe_allow_html=True)
    st.markdown(f"Chaîne de sources : **{' → '.join(p.name for p in state.service.providers)}** · "
                f"cache : {'actif' if state.service.cache else 'inactif'} · connexion : {connection.network.label}")
    st.dataframe(pd.Series(state.sources, name="source").to_frame(), width="stretch")
    if state.proxy.used:
        st.markdown("**Entrées des courbes proxy**")
        st.json({"estr_fixing": state.proxy.estr_fixing, "ecb_aaa_svensson": state.proxy.aaa_params,
                 "proxied_quotes": len(state.proxy.proxied_ids)})
    if state.data_errors:
        st.markdown(f"**Erreurs de chargement ({len(state.data_errors)})**")
        st.dataframe(pd.Series(state.data_errors, name="erreur").to_frame(), width="stretch")
    st.dataframe(pd.Series(state.curves.quotes, name="quote").to_frame().style.format(PCT), width="stretch")
    rep = state.service.last_report
    st.json({"last_request": rep.dataset, "missing": rep.missing, "stale": {k: str(v) for k, v in rep.stale.items()},
             "suspicious": rep.suspicious, "errors": rep.errors})
