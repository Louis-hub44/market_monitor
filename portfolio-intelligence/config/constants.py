"""Constantes de marché et **hypothèses de modèle**.

Distinction importante :

* les *constantes de marché* (tenors, tickers de courbes, proxys d'indices)
  sont des conventions, pas des données ;
* les *hypothèses de modèle* (§ MODEL_ASSUMPTIONS) sont des paramètres de
  calcul utilisés uniquement en dernier recours, quand la sensibilité n'a pas
  pu être estimée sur des données réelles. Tout résultat qui s'appuie dessus
  est marqué ESTIMATED / confiance LOW et l'hypothèse est affichée à l'écran.

Elles sont surchargeables sans toucher au code via un fichier JSON pointé par
la variable d'environnement `MODEL_ASSUMPTIONS_FILE`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# --------------------------------------------------------------------- #
# Conventions de courbe
# --------------------------------------------------------------------- #
KEY_RATE_TENORS: tuple[str, ...] = ("3M", "1Y", "2Y", "5Y", "10Y", "20Y", "30Y")

TENOR_YEARS: dict[str, float] = {
    "1M": 1 / 12, "3M": 0.25, "6M": 0.5, "1Y": 1.0, "2Y": 2.0, "3Y": 3.0,
    "5Y": 5.0, "7Y": 7.0, "10Y": 10.0, "20Y": 20.0, "30Y": 30.0,
}

# Courbes souveraines suivies. Les tickers sont des *identifiants de série*,
# les niveaux sont toujours récupérés auprès des providers.
SOVEREIGN_CURVES: dict[str, dict[str, str]] = {
    "US": {"name": "US Treasury", "currency": "USD", "fmp_prefix": "US", "yahoo_10y": "^TNX"},
    "DE": {"name": "German Bund", "currency": "EUR", "fmp_prefix": "DE", "yahoo_10y": ""},
    "FR": {"name": "France OAT", "currency": "EUR", "fmp_prefix": "FR", "yahoo_10y": ""},
    "GB": {"name": "UK Gilt", "currency": "GBP", "fmp_prefix": "GB", "yahoo_10y": ""},
    "IT": {"name": "Italy BTP", "currency": "EUR", "fmp_prefix": "IT", "yahoo_10y": ""},
    "JP": {"name": "Japan JGB", "currency": "JPY", "fmp_prefix": "JP", "yahoo_10y": ""},
}

# Proxys utilisés pour estimer les bêtas sur données réelles (pas de valeurs en dur :
# ce sont des tickers, les séries de prix viennent des providers).
MARKET_PROXIES: dict[str, str] = {
    "GLOBAL": "ACWI",
    "US": "SPY",
    "SP500": "SPY",
    "NASDAQ": "QQQ",
    "EUROPE": "IEUR",
    "EUROSTOXX": "FEZ",
    "CAC40": "^FCHI",
    "EM": "EEM",
    "OIL": "USO",
    "GOLD": "GLD",
}

FX_PROXIES: dict[str, str] = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X", "EURCHF": "EURCHF=X",
}

TRADING_DAYS_PER_YEAR = 252

# --------------------------------------------------------------------- #
# Bloomberg — identifiants de séries suivies
# --------------------------------------------------------------------- #
#: Tickers génériques de courbe souveraine. Ce sont des *identifiants*, pas des
#: niveaux : les taux sont toujours récupérés en direct.
BLOOMBERG_CURVE_TICKERS: dict[str, dict[str, str]] = {
    "US": {"3M": "USGG3M Index", "1Y": "USGG12M Index", "2Y": "USGG2YR Index",
           "5Y": "USGG5YR Index", "10Y": "USGG10YR Index", "20Y": "USGG20YR Index",
           "30Y": "USGG30YR Index"},
    "DE": {"2Y": "GTDEM2Y Govt", "5Y": "GTDEM5Y Govt", "10Y": "GTDEM10Y Govt",
           "20Y": "GTDEM20Y Govt", "30Y": "GTDEM30Y Govt"},
    "FR": {"2Y": "GTFRF2Y Govt", "5Y": "GTFRF5Y Govt", "10Y": "GTFRF10Y Govt",
           "20Y": "GTFRF20Y Govt", "30Y": "GTFRF30Y Govt"},
    "IT": {"2Y": "GTITL2Y Govt", "5Y": "GTITL5Y Govt", "10Y": "GTITL10Y Govt",
           "30Y": "GTITL30Y Govt"},
    "GB": {"2Y": "GTGBP2Y Govt", "5Y": "GTGBP5Y Govt", "10Y": "GTGBP10Y Govt",
           "30Y": "GTGBP30Y Govt"},
    "JP": {"2Y": "GTJPY2Y Govt", "5Y": "GTJPY5Y Govt", "10Y": "GTJPY10Y Govt",
           "30Y": "GTJPY30Y Govt"},
}

#: Publications macro suivies par l'Event Radar, par ticker de release Bloomberg.
#: Liste volontairement courte et éditable : chaque ligne coûte une requête.
BLOOMBERG_ECO_TICKERS: dict[str, dict[str, str]] = {
    "FDTR Index": {"label": "Décision Fed (Fed Funds Target)", "country": "US", "factor": "RATES"},
    "EURR002W Index": {"label": "Décision BCE (taux de refi)", "country": "EU", "factor": "RATES"},
    "UKBRBASE Index": {"label": "Décision BoE (Bank Rate)", "country": "GB", "factor": "RATES"},
    "CPI YOY Index": {"label": "CPI Etats-Unis (a/a)", "country": "US", "factor": "INFLATION"},
    "ECCPEMUY Index": {"label": "CPI zone euro (a/a)", "country": "EU", "factor": "INFLATION"},
    "NFP TCH Index": {"label": "Emploi non agricole (NFP)", "country": "US", "factor": "GROWTH"},
    "GDP CQOQ Index": {"label": "PIB Etats-Unis (t/t annualise)", "country": "US", "factor": "GROWTH"},
    "MPMIEZMA Index": {"label": "PMI manufacturier zone euro", "country": "EU", "factor": "GROWTH"},
    "NAPMPMI Index": {"label": "ISM manufacturier", "country": "US", "factor": "GROWTH"},
}

# --------------------------------------------------------------------- #
# Hypothèses de modèle (repli explicite)
# --------------------------------------------------------------------- #
DEFAULT_MODEL_ASSUMPTIONS: dict = {
    # Bêta actions par défaut lorsqu'aucun historique n'est disponible.
    "default_equity_beta": 1.0,

    # Sensibilité d'un secteur actions à un choc pétrole de +1 %.
    # Utilisée uniquement si aucune régression n'a pu être estimée.
    "sector_oil_beta": {
        "Energy": 0.45, "Basic Materials": 0.15, "Industrials": -0.05,
        "Consumer Cyclical": -0.10, "Utilities": -0.05, "Technology": -0.05,
        "Financial Services": 0.00, "Healthcare": 0.00, "Consumer Defensive": -0.05,
        "Communication Services": 0.00, "Real Estate": -0.05,
    },

    # Sensibilité des actions à une hausse de taux de 100 bp, par secteur.
    "sector_rates_beta_100bp": {
        "Utilities": -0.045, "Real Estate": -0.055, "Technology": -0.030,
        "Financial Services": 0.020, "Consumer Defensive": -0.020,
        "Consumer Cyclical": -0.025, "Industrials": -0.015, "Energy": -0.005,
        "Healthcare": -0.015, "Basic Materials": -0.015, "Communication Services": -0.020,
    },

    # Duration de spread par défaut quand elle n'est pas calculable
    # (fraction de la duration modifiée).
    "spread_duration_ratio": 0.95,

    # Bêta d'un choc de spread crédit sur les actions (contagion).
    "equity_credit_beta_100bp": -0.015,

    # Part de la duration attribuée par défaut au segment le plus proche
    # lorsque les KRD ne sont pas calculables.
    "krd_fallback": "nearest_tenor",
}


def load_model_assumptions() -> dict:
    """Hypothèses de modèle, éventuellement surchargées par un fichier JSON."""
    assumptions = json.loads(json.dumps(DEFAULT_MODEL_ASSUMPTIONS))  # copie profonde
    path = os.getenv("MODEL_ASSUMPTIONS_FILE")
    if path and Path(path).exists():
        try:
            override = json.loads(Path(path).read_text(encoding="utf-8"))
            for k, v in override.items():
                if isinstance(v, dict) and isinstance(assumptions.get(k), dict):
                    assumptions[k].update(v)
                else:
                    assumptions[k] = v
        except (OSError, json.JSONDecodeError):
            pass
    return assumptions
