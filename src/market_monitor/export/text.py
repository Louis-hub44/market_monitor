"""Plain-text daily macro: market-language bullets, ready to paste."""

from __future__ import annotations

import math

import pandas as pd

from market_monitor.alerts import AlertReport
from market_monitor.export.builder import DailyMacroData
from market_monitor.text_format import (
    CHANGE_DECIMALS,
    CHANGE_UNIT_LABELS,
    HORIZON_LABELS,
    NNBSP,
    SOURCE_LABELS,
    format_change,
    format_level_with_unit,
    fr_number,
    french_date,
    short_date,
)

#: (noun up, noun down, masculine?) per quote type, in market French
MOVE_NOUNS = {
    "price": ("hausse", "baisse", False),
    "vol": ("hausse", "baisse", False),
    "yield": ("tension", "détente", False),
    "spread": ("écartement", "resserrement", True),
}


def move_phrase(change: float, change_unit: str, quote: str, z: float, strong_z: float) -> str:
    """``(-4.95, "pct", "price", -4.2, 2.5)`` -> ``forte baisse de 4,95 %``."""
    if math.isnan(change):
        return "variation indisponible"
    if round(change, CHANGE_DECIMALS[change_unit]) == 0:
        return "stable"
    up, down, masculine = MOVE_NOUNS[quote]
    noun = up if change > 0 else down
    if not math.isnan(z) and abs(z) >= strong_z:
        noun = f"{'fort' if masculine else 'forte'} {noun}"
    size = fr_number(abs(change), CHANGE_DECIMALS[change_unit])
    return f"{noun} de {size}{NNBSP}{CHANGE_UNIT_LABELS[change_unit]}"


def mover_line(row: pd.Series, strong_z: float, show_zscore: bool) -> str:
    """``Nikkei 225 : forte baisse de 4,95 % à 38 215,40 (z −4,2)``."""
    phrase = move_phrase(row["chg_1d"], row["change_unit"], row["quote"], row["z_1d"], strong_z)
    level = format_level_with_unit(row["level"], row["asset_class"], row["quote"])
    z = f" (z {fr_number(row['z_1d'], 1, sign=True)})" if show_zscore else ""
    return f"{row['name']} : {phrase} à {level}{z}"


def section_line(row: pd.Series, columns: tuple[str, ...]) -> str:
    """``Euro Stoxx 50 : 5 243,01 (1J −1,12 %, YTD +12,40 %)``."""
    level = format_level_with_unit(row["level"], row["asset_class"], row["quote"])
    parts = [f"{HORIZON_LABELS[c.removeprefix('chg_')]} {format_change(row[c], row['change_unit'])}"
             for c in columns]
    stale = f", cotation du {short_date(row['level_date'])}" if row["stale"] else ""
    return f"{row['name']} : {level} ({', '.join(parts)}{stale})"


def render_text(data: DailyMacroData) -> str:
    """Full text: title, movers, one block per band, points to check."""
    rule = data.movers_rule
    lines = [f"{data.title}, arrêté au {french_date(data.as_of)}", ""]
    if data.alerts is not None:
        lines += alert_lines(data.alerts) + [""]
    lines.append("Mouvements marquants de la dernière séance")
    if data.movers.empty:
        lines.append(f"- Aucun mouvement au-delà de {fr_number(rule.min_abs_z, 1)} écart-type.")
    for _, row in data.movers.iterrows():
        lines.append(f"- {mover_line(row, rule.strong_z, rule.show_zscore)}")
    for title, table in data.sections:
        lines += ["", title]
        lines += [f"- {section_line(row, data.columns)}" for _, row in table.iterrows()]
    checks = _checks(data)
    if checks:
        lines += ["", "À vérifier avant diffusion"] + [f"- {c}" for c in checks]
    lines += ["", f"Sources : {data_sources(data.universe)}."]
    return "\n".join(lines) + "\n"


SEVERITY_LABELS = {"critical": "Critique", "warning": "Attention", "info": "Info"}


def alert_lines(report: AlertReport) -> list[str]:
    """Internal block, kept apart from the publishable bullets."""
    lines = ["Alertes du jour (usage interne)"]
    if not report.alerts:
        return lines + ["- Aucune alerte déclenchée."]
    return lines + [f"- [{SEVERITY_LABELS[a.severity.value]}] {a.message}" for a in report.alerts]


def data_sources(table: pd.DataFrame) -> str:
    """Market data sources actually used (computed lines are not a source)."""
    names = sorted({SOURCE_LABELS.get(s, s) for s in table["source"].dropna() if s != "derived"})
    return ", ".join(names) or "aucune"


def _checks(data: DailyMacroData) -> list[str]:
    shown = pd.concat([t for _, t in data.sections] + [data.movers])
    shown = shown[~shown.index.duplicated()]
    out = []
    for instrument_id, row in shown.iterrows():
        if pd.isna(row["level"]):
            out.append(f"{row['name']} : donnée manquante ({data.errors.get(str(instrument_id), 'n/d')})")
        elif row["stale"]:
            out.append(f"{row['name']} : dernière cotation le {short_date(row['level_date'])}")
        if isinstance(row["proxy"], str):
            out.append(f"{row['name']} : source proxy ({row['proxy']})")
    return out
