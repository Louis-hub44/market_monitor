r"""Cross-asset correlations on convention-consistent changes.

Returns use each instrument's change convention: simple % returns for prices, bp
changes for yields and spreads, point changes for vol indices (correlation is scale
invariant, the sign is what matters: +0.5 between SX5E and BUND_10Y means yields rise
when equities rise).

Calendars: levels are put on a Monday-Friday grid with a bounded forward-fill (a
holiday gives a zero change for the closed market). Asynchronous closes (Tokyo vs New
York) bias daily correlations towards zero: the weekly frequency (Friday to Friday) is
the robust alternative.

Matrix ordering can follow the referential or a hierarchical clustering on the
correlation distance :math:`d_{ij} = \sqrt{(1 - \rho_{ij}) / 2}` (average linkage,
optimal leaf ordering).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform

from market_monitor.analytics.performance import daily_changes
from market_monitor.data.quality import align_calendars
from market_monitor.referential import Instrument

MIN_FRACTION = 0.8  # share of the window that must be observed for a pair


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class MatrixOrder(StrEnum):
    REFERENTIAL = "referential"
    CLUSTER = "cluster"


DAYS_PER_PERIOD = {Frequency.DAILY: 7 / 5, Frequency.WEEKLY: 7.0}


def to_returns(levels: pd.DataFrame, instruments: Sequence[Instrument], frequency: Frequency,
               max_fill_days: int = 3) -> pd.DataFrame:
    """Convention-consistent changes on a business-day or Friday-weekly grid."""
    ids = [i.id for i in instruments if i.id in levels.columns]
    frame = levels[ids].dropna(how="all")
    if frame.empty:
        return pd.DataFrame(columns=ids, dtype="float64")
    grid = pd.bdate_range(frame.index.min(), frame.index.max(), name="date")
    frame = align_calendars(frame.reindex(grid), max_fill_days)
    if frequency is Frequency.WEEKLY:
        frame = frame.resample("W-FRI").last()
    by_id = {i.id: i for i in instruments}
    return pd.DataFrame(
        {c: daily_changes(frame[c], by_id[c].change, by_id[c].bp_factor) for c in ids}
    )


def correlation_matrix(returns: pd.DataFrame, window: int, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Pearson correlations over the last ``window`` periods up to ``end`` (pairwise)."""
    if window < 3:
        raise ValueError("window must be >= 3")
    sample = (returns.loc[:end] if end is not None else returns).tail(window)
    return sample.corr(min_periods=math.ceil(MIN_FRACTION * window))


def rolling_correlation(returns: pd.DataFrame, a: str, b: str, window: int) -> pd.Series:
    """Rolling Pearson correlation of two return columns."""
    return returns[a].rolling(window, min_periods=math.ceil(MIN_FRACTION * window)).corr(returns[b])


def cluster_order(corr: pd.DataFrame) -> list[str]:
    """Leaf order of an average-linkage clustering on :math:`\\sqrt{(1-\\rho)/2}`."""
    labels = list(corr.index)
    if len(labels) < 3:
        return labels
    rho = corr.to_numpy(dtype="float64", copy=True)
    rho[np.isnan(rho)] = 0.0  # unknown pairs treated as uncorrelated
    np.fill_diagonal(rho, 1.0)
    dist = np.sqrt(np.clip(0.5 * (1.0 - rho), 0.0, None))
    np.fill_diagonal(dist, 0.0)
    tree = linkage(squareform(dist, checks=False), method="average", optimal_ordering=True)
    return [labels[i] for i in leaves_list(tree)]


def required_start(as_of: date, window: int, lag: int, frequency: Frequency, history: int = 0) -> date:
    """First date to download for ``window + lag`` (+ ``history``) return periods."""
    periods = window + lag + history + 5
    return as_of - timedelta(days=math.ceil(periods * DAYS_PER_PERIOD[frequency]) + 14)


@dataclass
class CorrelationResult:
    """Current matrix, matrix ``lag`` periods earlier, and the underlying returns."""

    matrix: pd.DataFrame
    previous: pd.DataFrame | None
    returns: pd.DataFrame
    window: int
    frequency: Frequency
    end: pd.Timestamp | None
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)

    @property
    def change(self) -> pd.DataFrame | None:
        """Change in correlation over the lag (``None`` without a previous matrix)."""
        return None if self.previous is None else self.matrix - self.previous


def analyse_correlations(
    levels: pd.DataFrame,
    instruments: Sequence[Instrument],
    as_of: date,
    *,
    window: int,
    frequency: Frequency = Frequency.DAILY,
    lag: int = 0,
    order: MatrixOrder = MatrixOrder.REFERENTIAL,
) -> CorrelationResult:
    """Correlation matrix at ``as_of`` (and ``lag`` periods before), ordered."""
    returns = to_returns(levels.loc[: pd.Timestamp(as_of)], instruments, frequency)
    returns = returns.dropna(how="all")
    errors = {i.id: "not enough data for this window" for i in instruments
              if i.id not in returns or returns[i.id].tail(window).count() < MIN_FRACTION * window}
    usable = [i.id for i in instruments if i.id not in errors]
    if len(usable) < 2 or returns.empty:
        empty = pd.DataFrame(index=usable, columns=usable, dtype="float64")
        return CorrelationResult(empty, None, returns, window, frequency, None, errors)
    returns = returns[usable]
    end = returns.index[-1]
    matrix = correlation_matrix(returns, window, end)
    previous = None
    if lag > 0 and len(returns) > window + lag:
        previous = correlation_matrix(returns, window, returns.index[-1 - lag])
    labels = cluster_order(matrix) if order is MatrixOrder.CLUSTER else usable
    matrix = matrix.loc[labels, labels]
    previous = previous.loc[labels, labels] if previous is not None else None
    return CorrelationResult(matrix, previous, returns, window, frequency, end, errors)
