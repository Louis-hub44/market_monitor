"""Assemble the daily-macro content from one performance computation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from market_monitor.alerts import AlertReport, AlertRule, evaluate, rule_universe
from market_monitor.analytics.performance import Horizon, rank_movers
from market_monitor.data.models import DateLike
from market_monitor.export.layout import DailyMacroLayout, MoversRule
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


def _rows(table: pd.DataFrame, ids: Sequence[str]) -> pd.DataFrame:
    """Rows of ``ids`` present in ``table`` (instruments no active provider serves are absent)."""
    return table.loc[[i for i in ids if i in table.index]]


def build_daily_macro(monitor: MarketMonitor, layout: DailyMacroLayout,
                      as_of: DateLike | None = None,
                      rules: Sequence[AlertRule] = ()) -> DailyMacroData:
    """One ``performance`` call over sections, movers universe and alert rules, then slicing."""
    ids = list(dict.fromkeys(layout.section_ids + layout.movers.universe + rule_universe(rules)))
    report = monitor.performance(watchlist=None, as_of=as_of, ids=ids)
    table = report.table.copy()
    ref = monitor.referential
    table["bp_factor"] = [ref.get(str(i)).bp_factor for i in table.index]
    return DailyMacroData(
        as_of=report.as_of,
        title=layout.title,
        columns=layout.columns,
        sections=[(s.title, _rows(table, s.instruments)) for s in layout.sections],
        movers=select_movers(_rows(table, layout.movers.universe), layout.movers),
        universe=table,
        movers_rule=layout.movers,
        alerts=evaluate(rules, table, report.as_of) if rules else None,
        errors=dict(report.errors),
        warnings=dict(report.warnings),
    )


def select_movers(table: pd.DataFrame, rule: MoversRule) -> pd.DataFrame:
    """Largest |z 1J| above the threshold, stale rows excluded."""
    ranked = rank_movers(table, Horizon.D1, len(table))
    return ranked[ranked["z_1d"].abs() >= rule.min_abs_z].head(rule.count)
