r"""Publication safeguards: second-source cross-check and the "to check" list.

Cross-check of a line served by source :math:`A`, against a second source :math:`B` (another
provider, or another alternative of the same fallback chain), on the last date :math:`t_0`
of :math:`A`:

* **level** gap: :math:`|L_A / L_B - 1| > \text{tol}_\%` for prices and vol, or
  :math:`k\,|L_A - L_B| > \text{tol}_{pb}` for yields and spreads;
* **1D** gap, in daily standard deviations of :math:`A`:
  :math:`|\Delta_A - \Delta_B| / \hat\sigma_{1J} > z_{\max}`, both changes measured between the
  same two dates.

Proxies are never compared (an ETF is not its index, a future is not spot). A source that
cannot answer for :math:`t_0` leaves the line "indisponible", never "ok".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from market_monitor.analytics.performance import (
    CHECK_GAP,
    CHECK_OK,
    CHECK_UNAVAILABLE,
    change,
    daily_changes,
)
from market_monitor.config import QualitySettings
from market_monitor.referential import ChangeUnit, Instrument
from market_monitor.rolls import roll_days
from market_monitor.text_format import (
    HORIZON_LABELS,
    format_change,
    format_level_with_unit,
    short_date,
)

SIGMA_WINDOW = 252
MIN_SIGMA_OBS = 20


@dataclass(frozen=True)
class CheckResult:
    status: str        # ok | écart | indisponible
    reason: str = ""   # French, for the "to check" list when status is a gap


def cross_check(primary: pd.Series, secondary: pd.Series, inst: Instrument,
                settings: QualitySettings, label: str) -> CheckResult:
    """Compare the last print of ``primary`` with ``secondary`` (see module docstring)."""
    a, b = primary.dropna(), secondary.dropna()
    if a.empty or b.empty or a.index[-1] not in b.index:
        return CheckResult(CHECK_UNAVAILABLE)
    t0 = a.index[-1]
    level_a, level_b = float(a.loc[t0]), float(b.loc[t0])
    if _level_gap(level_a, level_b, inst, settings):
        shown_a = format_level_with_unit(level_a, inst.asset_class.value, inst.quote.value)
        shown_b = format_level_with_unit(level_b, inst.asset_class.value, inst.quote.value)
        return CheckResult(CHECK_GAP, f"niveau {shown_a} contre {shown_b} sur {label}")
    common = a.index.intersection(b.index)
    previous = common[common < t0]
    if previous.empty:
        return CheckResult(CHECK_UNAVAILABLE)
    ref = previous[-1]
    move_a = change(level_a, float(a.loc[ref]), inst.change, inst.bp_factor)
    move_b = change(level_b, float(b.loc[ref]), inst.change, inst.bp_factor)
    sigma = daily_sigma(a, inst)
    if math.isnan(sigma) or math.isnan(move_a) or math.isnan(move_b):
        return CheckResult(CHECK_UNAVAILABLE)
    if abs(move_a - move_b) / sigma > settings.cross_check_max_dev_z:
        unit = inst.change.value
        return CheckResult(CHECK_GAP, (
            f"{HORIZON_LABELS['1d']} {format_change(move_a, unit)} contre "
            f"{format_change(move_b, unit)} sur {label} (depuis le {short_date(ref)})"))
    return CheckResult(CHECK_OK)


def daily_sigma(series: pd.Series, inst: Instrument) -> float:
    """Std of daily changes (last year, roll days excluded); NaN with too few points."""
    s = series.dropna()
    steps = daily_changes(s, inst.change, inst.bp_factor).dropna()
    if inst.roll is not None and not s.empty:
        steps = steps[~steps.index.isin(roll_days(inst.roll, s.index[0], s.index[-1]))]
    steps = steps.tail(SIGMA_WINDOW)
    if len(steps) < MIN_SIGMA_OBS:
        return math.nan
    sigma = float(steps.std(ddof=1))
    return sigma if sigma > 0 else math.nan


def _level_gap(a: float, b: float, inst: Instrument, settings: QualitySettings) -> bool:
    if inst.change is ChangeUnit.BP:
        return abs(a - b) * (inst.bp_factor or 1.0) > settings.cross_check_level_tol_bp
    if b == 0:
        return a != 0
    return abs(a / b - 1.0) * 100.0 > settings.cross_check_level_tol_pct


# ------------------------------------------------------------- "to check" list
def publication_checks(table: pd.DataFrame, notes: dict[str, list[str]] | None = None,
                       errors: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Every point to check before publishing, per instrument id, in plain French.

    Gathers missing data, stale prints, suspect moves (high z-score or second-source gap),
    contract rolls, proxies and the history notes (fallback source, legs out of sync...).
    """
    out: dict[str, list[str]] = {}

    def add(key: str, message: str) -> None:
        bucket = out.setdefault(key, [])
        if message not in bucket:
            bucket.append(message)

    for instrument_id, row in table.iterrows():
        key = str(instrument_id)
        if pd.isna(row["level"]):
            add(key, f"donnée manquante ({(errors or {}).get(key, 'n/d')})")
            continue
        if bool(row["stale"]):
            add(key, f"dernière cotation le {short_date(row['level_date'])}")
        if bool(row.get("suspect", False)):
            add(key, f"mouvement suspect : {row.get('suspect_reason') or 'à vérifier'}")
        if bool(row.get("roll_1d", False)):
            add(key, "1J affecté par un changement de contrat (échéance du front-month)")
        elif bool(row.get("roll_1w", False)):
            add(key, "1S affecté par un changement de contrat (échéance du front-month)")
        if isinstance(row.get("proxy"), str) and row["proxy"]:
            add(key, f"source proxy ({row['proxy']})")
        for note in (notes or {}).get(key, []):
            add(key, note)
    return out


def needs_check(row: pd.Series) -> bool:
    """True when the 1D move of a published line must be checked before use."""
    return bool(row.get("suspect", False)) or bool(row.get("roll_1d", False))
