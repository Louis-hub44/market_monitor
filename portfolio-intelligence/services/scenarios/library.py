"""Bibliothèque de scénarios.

Un scénario est une *hypothèse de gestion*, pas une donnée de marché : les
amplitudes ci-dessous sont des conventions paramétrables, elles ne prétendent
à aucune valeur prédictive. Le gérant peut en créer d'autres via
`custom_scenario()`.
"""

from __future__ import annotations

from models.enums import ShockType
from models.scenario import Scenario, Shock
from services.fixed_income.curves import CURVE_SHAPES, CURVE_SHAPE_LABELS


def _rates(bp: float, target: str = "ALL") -> Shock:
    return Shock(ShockType.RATES_PARALLEL, bp, target)


def _equity(pct: float, target: str = "ALL") -> Shock:
    return Shock(ShockType.EQUITY, pct, target)


def _credit(bp: float) -> Shock:
    return Shock(ShockType.CREDIT_SPREAD, bp, "CREDIT")


def _fx(pct: float, ccy: str) -> Shock:
    return Shock(ShockType.FX, pct, ccy)


def _oil(pct: float) -> Shock:
    return Shock(ShockType.COMMODITY, pct, "OIL")


# --------------------------------------------------------------------- #
# Scénarios de taux
# --------------------------------------------------------------------- #
def rates_scenarios() -> list[Scenario]:
    out = [
        Scenario(f"rates_{bp:+d}", f"Taux {bp:+d} bp (translation)", (_rates(bp),), family="RATES")
        for bp in (-100, -50, -25, 25, 50, 100)
    ]
    out += [
        Scenario(f"curve_{key.lower()}", CURVE_SHAPE_LABELS[key],
                 (Shock(ShockType.RATES_CURVE, 0.0, "ALL", curve=shape),), family="RATES",
                 narrative="Déformation non parallèle appliquée via les key rate durations.")
        for key, shape in CURVE_SHAPES.items()
    ]
    return out


# --------------------------------------------------------------------- #
# Scénarios actions / change / matières premières
# --------------------------------------------------------------------- #
def equity_scenarios() -> list[Scenario]:
    out = [
        Scenario(f"equity_{int(p*100)}", f"Marchés actions {p:+.0%}", (_equity(p),), family="EQUITY")
        for p in (-0.20, -0.10, -0.05, 0.05)
    ]
    out += [
        Scenario("nasdaq_-10", "Nasdaq -10 %", (_equity(-0.10, "NASDAQ"),), family="EQUITY"),
        Scenario("sp500_-10", "S&P 500 -10 %", (_equity(-0.10, "SP500"),), family="EQUITY"),
        Scenario("cac40_-10", "CAC 40 -10 %", (_equity(-0.10, "CAC40"),), family="EQUITY"),
    ]
    return out


def fx_scenarios(base_currency: str = "EUR") -> list[Scenario]:
    return [
        Scenario("eur_+10", f"{base_currency} +10 % (appréciation devise de base)",
                 (_fx(0.10, base_currency),), family="FX"),
        Scenario("eur_+5", f"{base_currency} +5 %", (_fx(0.05, base_currency),), family="FX"),
        Scenario("usd_+10", "USD +10 %", (_fx(0.10, "USD"),), family="FX"),
        Scenario("usd_-10", "USD -10 %", (_fx(-0.10, "USD"),), family="FX"),
    ]


def commodity_scenarios() -> list[Scenario]:
    return [
        Scenario("oil_+20", "Pétrole +20 %", (_oil(0.20),), family="COMMODITY"),
        Scenario("oil_-20", "Pétrole -20 %", (_oil(-0.20),), family="COMMODITY"),
    ]


def credit_scenarios() -> list[Scenario]:
    return [
        Scenario(f"credit_{bp:+d}", f"Spreads de crédit {bp:+d} bp", (_credit(bp),), family="CREDIT")
        for bp in (50, 100, 150, 300)
    ]


# --------------------------------------------------------------------- #
# Scénarios macro composites (§15)
# --------------------------------------------------------------------- #
def macro_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "inflation_shock", "Choc inflationniste",
            (Shock(ShockType.INFLATION, 0.02, "CPI", description="Inflation +2 pts"),
             _rates(150, "USD"), _rates(100, "EUR"), _oil(0.30), _equity(-0.07)),
            family="MACRO",
            narrative="Surprise d'inflation entraînant un resserrement monétaire des deux côtés de l'Atlantique.",
        ),
        Scenario(
            "recession", "Récession",
            (_equity(-0.15), _oil(-0.20), _rates(-100), _credit(150)),
            family="MACRO",
            narrative="Contraction de l'activité : baisse des actions et des taux, écartement du crédit.",
        ),
        Scenario(
            "deflation", "Déflation",
            (Shock(ShockType.INFLATION, -0.01, "CPI"), _rates(-100), _equity(-0.10), _credit(80)),
            family="MACRO",
        ),
        Scenario(
            "growth_shock", "Choc de croissance",
            (Shock(ShockType.GDP, -0.02, "GDP"), _equity(-0.15), _credit(100), _oil(-0.20)),
            family="MACRO",
        ),
        Scenario(
            "usd_appreciation", "Appréciation du dollar",
            (_fx(0.10, "USD"), _equity(-0.03, "EM"), _oil(-0.05)),
            family="MACRO",
        ),
        Scenario(
            "stagflation", "Stagflation",
            (Shock(ShockType.INFLATION, 0.015, "CPI"), _equity(-0.12), _rates(75), _oil(0.25), _credit(120)),
            family="MACRO",
        ),
    ]


def all_scenarios(base_currency: str = "EUR") -> list[Scenario]:
    return (rates_scenarios() + equity_scenarios() + credit_scenarios()
            + fx_scenarios(base_currency) + commodity_scenarios() + macro_scenarios())


def stress_battery(base_currency: str = "EUR") -> list[Scenario]:
    """Batterie testée automatiquement par « What can hurt my portfolio ? » (§17)."""
    keep = {
        "rates_+100", "rates_+50", "rates_-100", "curve_bear_steepener", "curve_bear_flattener",
        "equity_-20", "equity_-10", "credit_+150", "credit_+300",
        f"{base_currency.lower()}_+10", "usd_-10", "oil_+20", "oil_-20",
        "recession", "inflation_shock", "stagflation", "growth_shock", "usd_appreciation",
    }
    return [s for s in all_scenarios(base_currency) if s.key in keep or s.key.startswith("eur_+10")]


def custom_scenario(name: str, *, equity_pct: float = 0.0, rates_bp: float = 0.0,
                    credit_bp: float = 0.0, oil_pct: float = 0.0,
                    fx_pct: float = 0.0, fx_currency: str = "USD",
                    curve_shape: dict[str, float] | None = None) -> Scenario:
    """Construit un scénario sur mesure à partir des curseurs de l'interface."""
    shocks: list[Shock] = []
    if equity_pct:
        shocks.append(_equity(equity_pct))
    if curve_shape:
        shocks.append(Shock(ShockType.RATES_CURVE, 0.0, "ALL", curve=curve_shape))
    elif rates_bp:
        shocks.append(_rates(rates_bp))
    if credit_bp:
        shocks.append(_credit(credit_bp))
    if oil_pct:
        shocks.append(_oil(oil_pct))
    if fx_pct:
        shocks.append(_fx(fx_pct, fx_currency))
    return Scenario("custom", name or "Scénario personnalisé", tuple(shocks), family="CUSTOM")
