r"""Performance engine: levels, 1D / 1W / MTD / YTD changes and z-scores.

Conventions (see README §9 for the full derivation):

* level :math:`L_0` = last valid print on or before the as-of date :math:`T`, dated :math:`t_0`;
* reference :math:`L_h` = last print on or before the target date of horizon :math:`h`:

  - 1D  : previous print before :math:`t_0` (previous close of *that* market);
  - 1W  : :math:`t_0 - 7` calendar days (same weekday, like-for-like closes);
  - MTD : last calendar day of the month preceding :math:`T`;
  - YTD : 31 December of the year preceding :math:`T`;

* change: ``pct`` :math:`100 (L_0/L_h - 1)`, ``bp`` :math:`k (L_0 - L_h)` with
  :math:`k = 100` for yields in % and :math:`1` for spreads in bp, ``abs`` :math:`L_0 - L_h`;
* z-score over :math:`n` daily steps, with :math:`\mu, \sigma` estimated on the
  ``window`` daily changes **up to the reference date** (out of sample):
  :math:`z = (x - n\mu) / (\sigma \sqrt{n})`;
* contract rolls of generic futures (``roll`` in the referential): changes dated on a roll
  window are excluded from :math:`\mu, \sigma`, and ``roll_1d`` / ``roll_1w`` flag the
  horizons that span one;
* ``suspect``: a 1D move of :math:`|z| \ge` ``suspect_abs_z`` that no roll explains - far
  more often a bad print than a market move; it must be checked before publication.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from market_monitor.config import AnalyticsSettings
from market_monitor.data.quality import empty_series
from market_monitor.referential import ChangeUnit, Instrument
from market_monitor.rolls import roll_days, rolled_between
from market_monitor.text_format import fr_number

BDAYS_PER_WEEK = 5


class Horizon(StrEnum):
    D1 = "1d"
    W1 = "1w"
    MTD = "mtd"
    YTD = "ytd"


#: horizons for which a z-score is reported
ZSCORE_HORIZONS = (Horizon.D1, Horizon.W1)
META_COLUMNS = ["name", "asset_class", "group", "quote", "unit", "change_unit"]
VALUE_COLUMNS = (
    ["level", "level_date"]
    + [f"chg_{h}" for h in Horizon]
    + [f"z_{h}" for h in ZSCORE_HORIZONS]
    + ["stale"]
    + [f"ref_{h}" for h in Horizon]          # reference levels, for audit / Excel formulas
    + [f"ref_date_{h}" for h in Horizon]
    + [f"roll_{h}" for h in ZSCORE_HORIZONS]  # horizon spans a contract roll
    + ["suspect", "suspect_reason", "cross_check"]
)
#: ``cross_check`` values
CHECK_NOT_DONE, CHECK_OK, CHECK_GAP, CHECK_UNAVAILABLE = "", "ok", "écart", "indisponible"
DEFAULT_SUSPECT_ABS_Z = 8.0


# ---------------------------------------------------------------- primitives
def target_date(horizon: Horizon, level_date: date, as_of: date) -> date:
    """Calendar target of a horizon (not used for 1D, which is the previous print)."""
    if horizon is Horizon.W1:
        return level_date - timedelta(days=7)
    if horizon is Horizon.MTD:
        return as_of.replace(day=1) - timedelta(days=1)
    if horizon is Horizon.YTD:
        return date(as_of.year - 1, 12, 31)
    raise ValueError(f"no calendar target for {horizon}")


def change(current: float, reference: float, unit: ChangeUnit, bp_factor: float | None) -> float:
    """Variation between two levels under the instrument's convention."""
    if unit is ChangeUnit.PCT:
        return 100.0 * (current / reference - 1.0) if reference > 0 else math.nan
    if unit is ChangeUnit.BP:
        if bp_factor is None:
            raise ValueError("bp change requires a bp factor")
        return bp_factor * (current - reference)
    return current - reference


def daily_changes(series: pd.Series, unit: ChangeUnit, bp_factor: float | None) -> pd.Series:
    """Print-to-print changes, indexed by the date of the newer print."""
    prev = series.shift(1)
    if unit is ChangeUnit.PCT:
        out = 100.0 * (series / prev.where(prev > 0) - 1.0)
    elif unit is ChangeUnit.BP:
        out = (bp_factor or 1.0) * (series - prev)
    else:
        out = series - prev
    return out.iloc[1:]


def zscore(x: float, n_steps: int, history: pd.Series, settings: AnalyticsSettings) -> float:
    r""":math:`z = (x - n\mu)/(\sigma\sqrt{n})` - NaN when the estimate is not reliable."""
    sample = history.dropna().tail(settings.zscore_window)
    if n_steps < 1 or math.isnan(x) or len(sample) < settings.zscore_min_obs:
        return math.nan
    sigma = float(sample.std(ddof=1))
    if not sigma > 0:
        return math.nan
    mu = float(sample.mean()) if settings.zscore_demean else 0.0
    return (x - n_steps * mu) / (sigma * math.sqrt(n_steps))


def required_start(as_of: date, settings: AnalyticsSettings) -> date:
    """First date to download so that YTD references and z-score windows are complete."""
    ytd = date(as_of.year - 1, 12, 31) - timedelta(days=settings.max_reference_gap_days)
    weeks = math.ceil((settings.zscore_window + BDAYS_PER_WEEK + 10) / BDAYS_PER_WEEK)
    zwin = as_of - timedelta(weeks=weeks + 2)  # +2 weeks of holiday buffer
    return min(ytd, zwin)


# ------------------------------------------------------------------- engine
class PerformanceEngine:
    """Pure computation on harmonised levels (no I/O, no UI)."""

    def __init__(self, settings: AnalyticsSettings | None = None,
                 suspect_abs_z: float = DEFAULT_SUSPECT_ABS_Z) -> None:
        if suspect_abs_z <= 0:
            raise ValueError("suspect_abs_z must be > 0")
        self._settings = settings or AnalyticsSettings()
        self._suspect_abs_z = suspect_abs_z

    @property
    def settings(self) -> AnalyticsSettings:
        return self._settings

    def compute(self, levels: pd.DataFrame, instruments: Sequence[Instrument], as_of: date) -> pd.DataFrame:
        """One row per instrument (input order), columns META + VALUE."""
        rows = {}
        for inst in instruments:
            series = levels[inst.id] if inst.id in levels.columns else empty_series()
            rows[inst.id] = {**_meta(inst), **self._values(inst, series, as_of)}
        table = pd.DataFrame.from_dict(rows, orient="index", columns=META_COLUMNS + VALUE_COLUMNS)
        table.index.name = "id"
        return _typed(table)

    def _values(self, inst: Instrument, series: pd.Series, as_of: date) -> dict[str, Any]:
        s = series.dropna()
        s = s[s.index <= pd.Timestamp(as_of)]
        values: dict[str, Any] = {c: math.nan for c in VALUE_COLUMNS}
        values.update(level_date=pd.NaT, stale=True, **{f"ref_date_{h}": pd.NaT for h in Horizon},
                      **{f"roll_{h}": False for h in ZSCORE_HORIZONS},
                      suspect=False, suspect_reason="", cross_check=CHECK_NOT_DONE)
        if s.empty:
            return values
        t0, level = s.index[-1], float(s.iloc[-1])
        daily = daily_changes(s, inst.change, inst.bp_factor)
        if inst.roll is not None:  # a roll jump is a calendar spread, not volatility
            daily = daily[~daily.index.isin(roll_days(inst.roll, s.index[0], t0))]
        values.update(level=level, level_date=t0, stale=self._is_stale(t0.date(), as_of))
        for horizon in Horizon:
            ref = self._reference(horizon, s, as_of)
            if ref is None:
                continue
            ref_date, ref_level = ref
            x = change(level, ref_level, inst.change, inst.bp_factor)
            values.update({f"chg_{horizon}": x, f"ref_{horizon}": ref_level,
                           f"ref_date_{horizon}": ref_date})
            if horizon in ZSCORE_HORIZONS:
                n_steps = int((s.index > ref_date).sum())
                values[f"z_{horizon}"] = zscore(x, n_steps, daily[daily.index <= ref_date], self._settings)
                values[f"roll_{horizon}"] = rolled_between(inst.roll, ref_date, t0)
        z_1d = values["z_1d"]
        if not values["roll_1d"] and not math.isnan(z_1d) and abs(z_1d) >= self._suspect_abs_z:
            values.update(suspect=True,
                          suspect_reason=f"variation de séance de {fr_number(abs(z_1d), 1)} écarts-types")
        return values

    def _reference(self, horizon: Horizon, s: pd.Series, as_of: date) -> tuple[pd.Timestamp, float] | None:
        """Reference print of a horizon, or ``None`` if missing / too far from its target."""
        t0 = s.index[-1]
        if horizon is Horizon.D1:
            if len(s) < 2:
                return None
            ref_date, target = s.index[-2], t0.date()
        else:
            target = target_date(horizon, t0.date(), as_of)
            before = s[s.index <= pd.Timestamp(target)]
            if before.empty:
                return None
            ref_date = before.index[-1]
        if (target - ref_date.date()).days > self._settings.max_reference_gap_days:
            return None
        return ref_date, float(s.loc[ref_date])

    def _is_stale(self, level_date: date, as_of: date) -> bool:
        return int(np.busday_count(level_date, as_of)) > self._settings.stale_bdays


def _meta(inst: Instrument) -> dict[str, Any]:
    return {
        "name": inst.name, "asset_class": inst.asset_class.value, "group": inst.group,
        "quote": inst.quote.value, "unit": inst.unit, "change_unit": inst.change.value,
    }


def _typed(table: pd.DataFrame) -> pd.DataFrame:
    numeric = (["level"] + [c for c in VALUE_COLUMNS if c.startswith(("chg_", "z_"))]
               + [f"ref_{h}" for h in Horizon])
    table[numeric] = table[numeric].astype("float64")
    for column in ["level_date"] + [c for c in VALUE_COLUMNS if c.startswith("ref_date_")]:
        table[column] = pd.to_datetime(table[column])
    for column in ["stale", "suspect"] + [f"roll_{h}" for h in ZSCORE_HORIZONS]:
        table[column] = table[column].astype(bool)
    for column in ("suspect_reason", "cross_check"):
        table[column] = table[column].astype("object")
    return table


def rank_movers(table: pd.DataFrame, horizon: Horizon = Horizon.D1, n: int = 6) -> pd.DataFrame:
    """Largest moves by absolute z-score.

    Excluded: rows without a z-score, stale rows, suspect rows and rows whose horizon
    spans a contract roll.
    """
    if horizon not in ZSCORE_HORIZONS:
        raise ValueError(f"no z-score for horizon {horizon}")
    column = f"z_{horizon}"
    valid = table[table[column].notna() & ~table["stale"]]
    # a bad print or a contract roll is not a market move
    if "suspect" in valid.columns:
        valid = valid[~valid["suspect"].astype(bool)]
    if f"roll_{horizon}" in valid.columns:
        valid = valid[~valid[f"roll_{horizon}"].astype(bool)]
    order = valid[column].abs().sort_values(ascending=False).index
    return valid.loc[order[:n]]
