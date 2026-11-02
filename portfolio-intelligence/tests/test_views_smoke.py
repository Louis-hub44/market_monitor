"""Tests de fumée des pages Streamlit.

Objectif : garantir qu'aucune page ne lève d'exception **en mode dégradé**,
c'est-à-dire sans réseau et sans clé API — la situation la plus fréquente en
production quand un provider tombe. Les pages doivent afficher un message
explicite, pas une trace d'erreur.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["OFFLINE_MODE"] = "true"
os.environ.pop("FMP_API_KEY", None)

from streamlit.testing.v1 import AppTest  # noqa: E402

from services.portfolio.loader import load_portfolio  # noqa: E402
from utils.state import PORTFOLIO_KEY, REPORT_KEY  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ["dashboard", "portfolio_view", "equity", "fixed_income", "diversified", "events", "data_quality"]


@pytest.fixture(scope="module")
def multi_asset():
    path = ROOT / "samples" / "sample_multi_asset_portfolio.csv"
    portfolio, report = load_portfolio(path, file_name=path.name)
    assert report.ok
    return portfolio, report


def _run(view: str, portfolio=None, report=None) -> AppTest:
    app = AppTest.from_file(str(ROOT / "views" / f"{view}.py"), default_timeout=60)
    if portfolio is not None:
        app.session_state[PORTFOLIO_KEY] = portfolio
        app.session_state[REPORT_KEY] = report
    app.run()
    return app


@pytest.mark.parametrize("view", VIEWS)
def test_view_renders_without_portfolio(view):
    """Sans portefeuille chargé, chaque page invite à en importer un."""
    app = _run(view)
    assert not app.exception


@pytest.mark.parametrize("view", VIEWS)
def test_view_renders_offline_with_portfolio(view, multi_asset):
    """Avec un portefeuille mais sans réseau, aucune page ne doit planter."""
    portfolio, report = multi_asset
    app = _run(view, portfolio, report)
    assert not app.exception, f"{view} : {app.exception}"


def test_dashboard_shows_top_risks(multi_asset):
    portfolio, report = multi_asset
    app = _run("dashboard", portfolio, report)
    text = " ".join(str(m.value) for m in app.markdown)
    assert "Principaux risques" in text


def test_fixed_income_page_reports_duration(multi_asset):
    portfolio, report = multi_asset
    app = _run("fixed_income", portfolio, report)
    text = " ".join(str(m.value) for m in app.markdown)
    assert "Duration modifiée" in text


@pytest.mark.parametrize("sample", [
    "sample_equity_portfolio.csv",
    "sample_fixed_income_portfolio.csv",
    "sample_multi_asset_portfolio.csv",
])
def test_dashboard_renders_for_every_sample(sample):
    """Le dashboard doit tenir les trois profils de portefeuille livrés."""
    path = ROOT / "samples" / sample
    portfolio, report = load_portfolio(path, file_name=path.name)
    app = _run("dashboard", portfolio, report)
    assert not app.exception, f"{sample} : {app.exception}"


def test_dashboard_shows_a_readable_nav(multi_asset):
    """Régression : la bande d'identité affichait « NAV 1 » sur un fichier en poids."""
    portfolio, report = multi_asset
    app = _run("dashboard", portfolio, report)
    band = next(m.value for m in app.markdown if "pi-band" in str(m.value))
    assert "NAV notionnelle" in band
    assert "1 000 000" in band


def test_no_deprecated_width_argument_remains():
    """`use_container_width` est déprécié côté Streamlit : on ne l'utilise plus."""
    offenders = [
        path.name for path in list((ROOT / "views").glob("*.py")) + [ROOT / "app.py"]
        if "use_container_width" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
