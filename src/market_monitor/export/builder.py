"""Assemble the daily-macro content from one performance computation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from market_monitor.alerts import AlertReport, AlertRule, evaluate, rule_universe
from market_monitor.analytics.performance import Horizon, rank_movers
from market_monitor.data.models import DateLike
from market_monitor.export.display import DEFAULT_LINE, DEFAULT_STYLE, NumberStyle, decorate
from market_monitor.export.layout import DailyMacroLayout, MoversRule, Section
from market_monitor.monitor import MarketMonitor


@dataclass
class DailyMacroData:
    """Everything the Excel / PNG / text renderers need."""

    as_of: date
    title: str
    columns: tuple[str, ...]
    sections: list[tuple[str, pd.DataFrame]]
    movers: pd.DataFrame
    universe: pd.DataFrame  # every row computed (sections first), with ``bp_factor``
    movers_rule: MoversRule
    alerts: AlertReport | None = None
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)
    checks: dict[str, list[str]] = field(default_factory=dict)  # id -> points to check
    style: NumberStyle = DEFAULT_STYLE

    def shown(self) -> pd.DataFrame:
        """Rows that end up in the published bands or movers, once each."""
        frames = [t for _, t in self.sections] + [self.movers]
        shown = pd.concat(frames) if frames else self.universe.iloc[0:0]
        return shown[~shown.index.duplicated()]


def _rows(table: pd.DataFrame, ids: Sequence[str]) -> pd.DataFrame:
    """Rows of ``ids`` present in ``table`` (instruments no active provider serves are absent)."""
    return table.loc[[i for i in ids if i in table.index]]


def build_daily_macro(monitor: MarketMonitor, layout: DailyMacroLayout,
                      as_of: DateLike | None = None,
                      rules: Sequence[AlertRule] = ()) -> DailyMacroData:
    """One ``performance`` call over sections, movers universe and alert rules, then slicing.

    Every line that may be published - the bands and the movers candidates - is
    cross-checked against a second source first, so a bad print is listed under "À
    vérifier avant diffusion" instead of being reported as a move.
    """
    ids = list(dict.fromkeys(layout.section_ids + layout.movers.universe + rule_universe(rules)))
    report = monitor.performance(watchlist=None, as_of=as_of, ids=ids)
    published = list(layout.section_ids) + movers_candidates(
        _rows(report.table, layout.movers.universe), layout.movers)
    monitor.verify(report, published)
    table = report.table.copy()
    ref = monitor.referential
    table["bp_factor"] = [ref.get(str(i)).bp_factor for i in table.index]
    return DailyMacroData(
        as_of=report.as_of,
        title=layout.title,
        columns=layout.columns,
        sections=[(s.title, _band(table, s, layout)) for s in layout.sections],
        movers=_decorated_movers(table, layout),
        universe=table,
        movers_rule=layout.movers,
        alerts=evaluate(rules, table, report.as_of) if rules else None,
        errors=dict(report.errors),
        warnings=dict(report.warnings),
        checks=report.checks(),
        style=layout.style,
    )


def _band(table: pd.DataFrame, section: Section, layout: DailyMacroLayout) -> pd.DataFrame:
    """Rows of a band with their display columns (labels, decimals, units)."""
    rows = _rows(table, section.instruments)
    lines = tuple(section.line(str(i)) for i in rows.index)
    return decorate(rows, lines, layout.columns, layout.style)


def _decorated_movers(table: pd.DataFrame, layout: DailyMacroLayout) -> pd.DataFrame:
    """Movers written like their band line when they have one (same decimals / labels)."""
    movers = select_movers(_rows(table, layout.movers.universe), layout.movers)
    lines = []
    for instrument_id in movers.index:
        home = next((s for s in layout.sections if instrument_id in s.instruments), None)
        lines.append(home.line(str(instrument_id)) if home else DEFAULT_LINE)
    return decorate(movers, tuple(lines), ("chg_1d",), layout.style)


def movers_candidates(table: pd.DataFrame, rule: MoversRule) -> list[str]:
    """Lines that could make the movers list (twice the count, before any exclusion)."""
    z = table["z_1d"].where(~table["stale"]).abs().dropna()
    return [str(i) for i in z.sort_values(ascending=False).index[: 2 * rule.count]]


def select_movers(table: pd.DataFrame, rule: MoversRule) -> pd.DataFrame:
    """Largest |z 1J| above the threshold; stale, suspect and contract-roll rows excluded."""
    ranked = rank_movers(table, Horizon.D1, len(table))
    return ranked[ranked["z_1d"].abs() >= rule.min_abs_z].head(rule.count)
