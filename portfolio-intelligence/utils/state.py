"""État applicatif partagé entre les pages.

Le portefeuille courant, les analytiques dérivées et le service de données
vivent ici. Les calculs coûteux (durations, bêtas, séries longues) sont mis
en cache et invalidés uniquement quand le portefeuille change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st

from config.constants import BLOOMBERG_CURVE_TICKERS
from config.settings import get_settings
from data.market_data import MarketDataService, get_market_data
from models.portfolio import Portfolio
from services.equity.factors import estimate_betas
from services.fixed_income.analytics import FixedIncomeAnalytics, analyse_portfolio
from services.fixed_income.curves import build_curves
from services.portfolio.enrichment import enrich_bond_risks
from services.scenarios.engine import ScenarioEngine, SensitivitySet

PORTFOLIO_KEY = "pi_portfolio"
REPORT_KEY = "pi_import_report"


@st.cache_resource(show_spinner=False)
def market_data() -> MarketDataService:
    return get_market_data()


def settings():
    return get_settings()


# --------------------------------------------------------------------- #
def set_portfolio(portfolio: Portfolio, report: Any | None = None) -> None:
    st.session_state[PORTFOLIO_KEY] = portfolio
    st.session_state[REPORT_KEY] = report
    for key in list(st.session_state.keys()):
        if key.startswith("pi_cache_"):
            del st.session_state[key]


def current_portfolio() -> Portfolio | None:
    return st.session_state.get(PORTFOLIO_KEY)


def import_report() -> Any | None:
    return st.session_state.get(REPORT_KEY)


def require_portfolio() -> Portfolio | None:
    portfolio = current_portfolio()
    if portfolio is None:
        st.info(
            "Aucun portefeuille chargé. Utilisez le panneau latéral pour importer "
            "un fichier CSV ou Excel, ou charger un modèle d'exemple."
        )
        return None
    return portfolio


# --------------------------------------------------------------------- #
def _memo(key: str, builder):
    cache_key = f"pi_cache_{key}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = builder()
    return st.session_state[cache_key]


def curves() -> dict:
    """Courbes souveraines disponibles.

    Avec Bloomberg, les six courbes déclarées sont récupérées ; sans lui, seule
    la courbe US est atteignable — les positions en euro retombent alors sur les
    sensibilités analytiques.
    """
    def build():
        md = market_data()
        countries = ["US"]
        if md.bloomberg.is_configured:
            countries = list(BLOOMBERG_CURVE_TICKERS)
        return build_curves({code: md.treasury_curve(code) for code in countries})
    return _memo("curves", build)


def bloomberg_enrichment(portfolio: Portfolio):
    """Applique une fois par portefeuille les sensibilités Bloomberg."""
    return _memo(f"bbg_{id(portfolio)}", lambda: enrich_bond_risks(portfolio, market_data()))


def fixed_income_analytics(portfolio: Portfolio) -> FixedIncomeAnalytics:
    def build():
        bloomberg_enrichment(portfolio)      # enrichit les positions sur place
        return analyse_portfolio(portfolio, curves())
    return _memo(f"fi_{id(portfolio)}", build)


def betas(portfolio: Portfolio, proxy: str = "GLOBAL") -> dict:
    return _memo(f"betas_{id(portfolio)}_{proxy}",
                 lambda: estimate_betas(portfolio, market_data(), proxy_key=proxy))


def scenario_engine(portfolio: Portfolio) -> ScenarioEngine:
    def build():
        sens = SensitivitySet.from_fixed_income(fixed_income_analytics(portfolio))
        estimates = st.session_state.get(f"pi_cache_betas_{id(portfolio)}_GLOBAL", {})
        sens.equity_betas = {k: v.beta for k, v in estimates.items()}
        return ScenarioEngine(portfolio, sens)
    return _memo(f"engine_{id(portfolio)}", build)


def clear_derived() -> None:
    for key in list(st.session_state.keys()):
        if key.startswith("pi_cache_"):
            del st.session_state[key]
