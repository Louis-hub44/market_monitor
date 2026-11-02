"""Alertes: compact strip on the overview and a full view."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from html import escape

import pandas as pd
import streamlit as st

from market_monitor.alerts import AlertReport, AlertRule, RuleKind, Severity
from market_monitor.config import UiSettings
from market_monitor.exceptions import MarketMonitorError
from market_monitor.export.text import SEVERITY_LABELS
from market_monitor.monitor import MarketMonitor
from market_monitor.text_format import HORIZON_LABELS
from market_monitor.ui import theme
from market_monitor.ui.caching import cached, refresh_nonce

STRIP_SIZE = 5
KIND_LABELS = {"zscore": "z-score", "level": "Niveau", "change": "Variation", "stale": "Donnée périmée",
               "suspect": "Donnée suspecte"}


def _compute(_monitor: MarketMonitor, _rules: Sequence[AlertRule], monitor_id: int, rules_hash: int,
             as_of: str, nonce: int) -> AlertReport:
    return _monitor.alerts(_rules, as_of)


def load_alerts(monitor: MarketMonitor, rules: Sequence[AlertRule] | None, ui: UiSettings,
                as_of: date, key: str) -> AlertReport | None:
    if not rules:
        return None
    compute = cached(_compute, ui, "Évaluation des alertes…")
    try:
        return compute(monitor, tuple(rules), id(monitor), hash(tuple(rules)), as_of.isoformat(),
                       refresh_nonce(key))
    except MarketMonitorError as exc:
        st.error(f"Évaluation des alertes impossible : {exc}")
        return None


def alerts_strip(report: AlertReport | None) -> None:
    """Most severe alerts above the overview (nothing when there is none)."""
    if report is None or not report.alerts:
        return
    critical = report.count(Severity.CRITICAL)
    title = f"{len(report.alerts)} alerte(s)" + (f", dont {critical} critique(s)" if critical else "")
    st.markdown(f"**{title}**")
    # threshold / quality alerts first: unusual moves are already in "Mouvements marquants"
    shown = sorted(report.alerts, key=lambda a: (a.kind is RuleKind.ZSCORE, -a.severity.rank))
    st.markdown("".join(_alert_html(a.severity.value, a.message) for a in shown[:STRIP_SIZE]),
                unsafe_allow_html=True)
    if len(report.alerts) > STRIP_SIZE:
        st.caption("Liste complète dans la vue Alertes.")


def render_alerts(monitor: MarketMonitor, rules: Sequence[AlertRule] | None, ui: UiSettings,
                  as_of: date, key: str) -> None:
    if not rules:
        st.info("Aucune règle d'alerte n'est configurée (config/alerts.yaml).")
        return
    report = load_alerts(monitor, rules, ui, as_of, key)
    if report is None:
        return
    cols = st.columns(3)
    for col, severity in zip(cols, (Severity.CRITICAL, Severity.WARNING, Severity.INFO), strict=True):
        col.metric(SEVERITY_LABELS[severity.value], report.count(severity))
    if report.alerts:
        st.markdown("".join(_alert_html(a.severity.value, a.message) for a in report.alerts),
                    unsafe_allow_html=True)
    else:
        st.success("Aucune alerte déclenchée.")
    st.markdown("**Règles actives**")
    st.dataframe(rules_table(rules, report), hide_index=True, width="stretch")


def rules_table(rules: Sequence[AlertRule], report: AlertReport) -> pd.DataFrame:
    """One row per rule: condition in words, scope, number of alerts, lines not evaluated."""
    fired = pd.Series([a.rule_id for a in report.alerts], dtype="object").value_counts()
    return pd.DataFrame([{
        "Règle": r.name,
        "Type": KIND_LABELS[r.kind.value],
        "Condition": describe_condition(r),
        "Instruments": len(r.instruments),
        "Gravité": SEVERITY_LABELS[r.severity.value],
        "Déclenchées": int(fired.get(r.id, 0)),
        "Sans données": len(report.not_evaluated.get(r.id, [])),
    } for r in rules])


def describe_condition(rule: AlertRule) -> str:
    horizon = HORIZON_LABELS[rule.horizon.value]
    if rule.kind.value == "zscore":
        text = f"|z {horizon}| ≥ {rule.min_abs_z:g}"
        if rule.critical_abs_z is not None:
            text += f" (critique ≥ {rule.critical_abs_z:g})"
        if rule.direction.value == "both":
            return text
        return f"{text}, {'hausse' if rule.direction.value == 'up' else 'baisse'} seulement"
    if rule.kind.value == "suspect":
        return "mouvement suspect ou écart entre sources"
    if rule.kind.value == "stale":
        return "cotation périmée"
    symbol = {"above": ">", "below": "<", "abs_above": "|·| >"}[rule.condition or "above"]
    subject = "niveau" if rule.kind.value == "level" else f"variation {horizon}"
    text = f"{subject} {symbol} {rule.threshold:g}"
    return f"{text}, franchissement" if rule.cross else text


def _alert_html(severity: str, message: str) -> str:
    colour = theme.SEVERITY_COLORS[severity]
    return (f'<div class="mm-alert" style="border-color:{colour}">'
            f'<span class="sev" style="color:{colour}">{SEVERITY_LABELS[severity]}</span>'
            f"{escape(message)}</div>")

