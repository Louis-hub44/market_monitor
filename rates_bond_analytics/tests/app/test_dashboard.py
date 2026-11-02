"""Dashboard tests: service layer (pure Python) and a headless Streamlit smoke run."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app import charts, services
from rates_analytics.scenarios import parallel, steepener

AS_OF = date(2026, 10, 6)
APP = Path(__file__).resolve().parents[2] / "app" / "streamlit_app.py"


@pytest.fixture(scope="module")
def state():
    return services.load_market_state(AS_OF)


def test_market_state(state):
    assert not state.warnings and set(state.sovereigns) == {"BUND", "OAT", "BTP"}
    assert not state.proxy.used and state.sources["swap curves"] == "market" and state.sources["BUND"] == "bonds"
    assert len(state.bonds) == 109 and set(state.fixings) == {"ESTR", "EURIBOR6M"}


def test_tables_and_figures(state):
    par = services.par_rates(state.curves)
    assert list(par.index) == list(services.CURVE_TENORS) and (par["basis_bp"] > 0).all()
    grid = services.curve_grid(state.curves)
    assert {"estr_zero", "e6m_fwd6m"} <= set(grid.columns)
    spreads = services.sovereign_spreads(state)
    assert spreads.loc[10, "BTP"] > spreads.loc[10, "OAT"]
    for fig in (charts.curves_figure(grid), charts.curves_figure(grid, "fwd"), charts.basis_figure(par),
                charts.sovereign_figure(state.sovereigns), charts.rich_cheap_figure(state.sovereigns["BTP"])):
        assert len(fig.data) >= 1


def test_bond_and_swap_services(state):
    rep, spreads = services.bond_report(state, "SMPL-IT-20350401", reference="BTP")
    assert rep.curve.model_dirty == pytest.approx(rep.analytics.dirty, abs=1e-8)
    assert spreads.g_spread is not None
    swap = services.make_swap(state, "IRS", date(2025, 3, 12), date(2032, 3, 12), 0.0245, 25e6, False)
    val = services.price_swap(state, swap)
    assert val.npv == pytest.approx(253101.47, abs=1.0)
    k = services.par_rate_for(state, "OIS", date(2026, 10, 8), date(2031, 10, 8))
    assert k == pytest.approx(0.02278, abs=1e-6)
    assert len(charts.bucket_figure(val.bucketed.table).data) == 1


def test_scenario_services(state):
    book = services.default_book(state)
    pnl, par = services.run_scenarios(state, book, [parallel(25), steepener(25)], "quotes")
    assert "TOTAL" in pnl.index and par.loc["10Y", "Parallel +25bp"] == pytest.approx(25.0, abs=1e-6)
    grid = services.scenario_curve_grid(state, steepener(25), "zero")
    assert grid["shock_bp"].iloc[-1] == pytest.approx(25.0)
    assert len(charts.pnl_figure(pnl).data) == 1


def test_start_screen_sends_no_request_before_launch():
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 0 and {"rba-test", "rba-start"} <= {b.key for b in at.button}
    assert any("Rates &amp; Bond Analytics" in m.value for m in at.markdown)


def test_streamlit_app_runs_headless_after_launch():
    testing = pytest.importorskip("streamlit.testing.v1")
    from app.connection import Connection
    from rates_analytics.network import NetworkSettings

    at = testing.AppTest.from_file(str(APP), default_timeout=300)
    at.session_state["rba-connection"] = Connection("static", NetworkSettings())
    at.run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 6
    assert any("EUR swap 10Y" in m.value for m in at.markdown)            # KPI tiles
    assert any("Courbes swap · marché" in m.value for m in at.markdown)   # source chips


def test_connection_helpers():
    from app.connection import diagnostic_html, network_from
    from app.theme import chip, kpi_tiles

    html = diagnostic_html({"ECB": {"ok": True, "kind": "ok", "status": 200, "latency_ms": 42, "message": "m"},
                            "CNBC": {"ok": False, "kind": "ssl", "status": None, "latency_ms": 9, "message": "<x>"}})
    assert 'rba-probe ok' in html and "42 ms" in html and "Certificat" in html and "&lt;x&gt;" in html
    assert network_from("insecure", "").mode == "auto" and network_from("ca", "").mode == "auto"
    assert network_from("ca", "C:/corp.pem").ca_bundle.name == "corp.pem"
    assert 'class="rba-chip warn"' in chip("PROXY", "warn") and kpi_tiles([("a", "1", "<b>")]).count("rba-kpi") == 2
