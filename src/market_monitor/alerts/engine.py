"""Evaluate alert rules on a performance table (pure, no I/O)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import cast

import pandas as pd

from market_monitor.alerts.rules import AlertRule, Direction, RuleKind, Severity
from market_monitor.text_format import (
    CHANGE_DECIMALS,
    CHANGE_UNIT_LABELS,
    HORIZON_LABELS,
    NNBSP,
    format_change,
    format_level_with_unit,
    fr_number,
    short_date,
)

ALERT_COLUMNS = ["severity", "rule", "instrument", "message", "value", "threshold", "level_date",
                 "rule_id", "instrument_id", "kind"]


@dataclass(frozen=True)
class Alert:
    rule_id: str
    rule: str
    kind: RuleKind
    severity: Severity
    instrument_id: str
    instrument: str
    message: str
    value: float
    threshold: float | None
    level_date: pd.Timestamp | None


@dataclass
class AlertReport:
    as_of: date
    alerts: list[Alert] = field(default_factory=list)
    not_evaluated: dict[str, list[str]] = field(default_factory=dict)  # rule id -> instrument ids

    def count(self, severity: Severity | None = None) -> int:
        return sum(1 for a in self.alerts if severity is None or a.severity is severity)

    @property
    def max_severity(self) -> Severity | None:
        return max((a.severity for a in self.alerts), key=lambda s: s.rank, default=None)

    def frame(self) -> pd.DataFrame:
        rows = [{"severity": a.severity.value, "rule": a.rule, "instrument": a.instrument,
                 "message": a.message, "value": a.value, "threshold": a.threshold,
                 "level_date": a.level_date, "rule_id": a.rule_id,
                 "instrument_id": a.instrument_id, "kind": a.kind.value} for a in self.alerts]
        return pd.DataFrame(rows, columns=ALERT_COLUMNS)


def evaluate(rules: Sequence[AlertRule], table: pd.DataFrame, as_of: date) -> AlertReport:
    """All triggered alerts, most severe first (rule order, then |value|)."""
    report = AlertReport(as_of)
    for rule in rules:
        for instrument_id in rule.instruments:
            if instrument_id not in table.index or pd.isna(table.at[instrument_id, "level"]):
                report.not_evaluated.setdefault(rule.id, []).append(instrument_id)
                continue
            alert = _check(rule, instrument_id, cast(pd.Series, table.loc[instrument_id]))
            if alert is not None:
                report.alerts.append(alert)
    rank = {r.id: k for k, r in enumerate(rules)}
    report.alerts.sort(key=lambda a: (-a.severity.rank, rank[a.rule_id], -abs(_finite(a.value))))
    return report


def _check(rule: AlertRule, instrument_id: str, row: pd.Series) -> Alert | None:
    checker = {RuleKind.ZSCORE: _zscore, RuleKind.LEVEL: _level,
               RuleKind.CHANGE: _change, RuleKind.STALE: _stale}[rule.kind]
    found = checker(rule, row)
    if found is None:
        return None
    severity, value, message = found
    return Alert(rule.id, rule.name, rule.kind, severity, instrument_id, str(row["name"]),
                 f"{row['name']} : {message}", value, rule.threshold, row["level_date"])


Found = tuple[Severity, float, str] | None


def _zscore(rule: AlertRule, row: pd.Series) -> Found:
    if row["stale"]:
        return None
    z, change = row[f"z_{rule.horizon}"], row[f"chg_{rule.horizon}"]
    if pd.isna(z) or abs(z) < (rule.min_abs_z or math.inf) or not _direction_ok(rule.direction, z):
        return None
    critical = rule.critical_abs_z is not None and abs(z) >= rule.critical_abs_z
    period = "la séance" if rule.horizon.value == "1d" else "la semaine"
    message = (f"mouvement de {fr_number(abs(z), 1)} écarts-types sur {period} "
               f"({format_change(change, row['change_unit'])})")
    return (Severity.CRITICAL if critical else rule.severity), float(z), message


def _level(rule: AlertRule, row: pd.Series) -> Found:
    level, threshold = float(row["level"]), float(rule.threshold or 0.0)
    above = rule.condition == "above"
    hit = level > threshold if above else level < threshold
    if not hit:
        return None
    shown = format_level_with_unit(threshold, row["asset_class"], row["quote"])
    current = format_level_with_unit(level, row["asset_class"], row["quote"])
    if rule.cross:
        previous = row["ref_1d"]
        if pd.isna(previous) or row["stale"] or (previous > threshold if above else previous < threshold):
            return None  # already beyond the threshold before the last session
        way = "à la hausse" if above else "à la baisse"
        return rule.severity, level, f"franchit {shown} {way} ({current})"
    side = "au-dessus de" if above else "en dessous de"
    stale = f", cotation du {short_date(row['level_date'])}" if row["stale"] else ""
    return rule.severity, level, f"{side} {shown} ({current}{stale})"


def _change(rule: AlertRule, row: pd.Series) -> Found:
    if row["stale"]:
        return None
    change = row[f"chg_{rule.horizon}"]
    threshold = float(rule.threshold or 0.0)
    if pd.isna(change):
        return None
    if rule.condition == "above":
        hit = change > threshold
    elif rule.condition == "below":
        hit = change < threshold
    else:
        hit = abs(change) > threshold
    if not hit:
        return None
    unit = row["change_unit"]
    limit = (f"±{fr_number(threshold, CHANGE_DECIMALS[unit])}{NNBSP}{CHANGE_UNIT_LABELS[unit]}"
             if rule.condition == "abs_above" else format_change(threshold, unit))
    message = (f"{format_change(change, unit)} sur {HORIZON_LABELS[rule.horizon.value]} "
               f"(seuil {limit})")
    return rule.severity, float(change), message


def _stale(rule: AlertRule, row: pd.Series) -> Found:
    if not row["stale"]:
        return None
    return rule.severity, math.nan, f"dernière cotation le {short_date(row['level_date'])}"


def _direction_ok(direction: Direction, z: float) -> bool:
    return direction is Direction.BOTH or (z > 0) == (direction is Direction.UP)


def _finite(x: float) -> float:
    return 0.0 if math.isnan(x) else x


def rule_universe(rules: Iterable[AlertRule]) -> tuple[str, ...]:
    """Every instrument any rule looks at, in rule order."""
    return tuple(dict.fromkeys(i for r in rules for i in r.instruments))
