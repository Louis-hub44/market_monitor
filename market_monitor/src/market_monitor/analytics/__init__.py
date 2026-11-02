"""Analytics engine (pure pandas / numpy, independent of the UI)."""

from market_monitor.analytics.comparison import ComparisonResult, compare, period_start
from market_monitor.analytics.correlation import (
    CorrelationResult,
    Frequency,
    MatrixOrder,
    analyse_correlations,
    cluster_order,
    correlation_matrix,
    rolling_correlation,
    to_returns,
)
from market_monitor.analytics.history import UniverseHistory, load_universe_history
from market_monitor.analytics.performance import (
    Horizon,
    PerformanceEngine,
    change,
    daily_changes,
    rank_movers,
    required_start,
    target_date,
    zscore,
)

__all__ = [
    "ComparisonResult", "CorrelationResult", "Frequency", "MatrixOrder", "analyse_correlations",
    "cluster_order", "compare", "correlation_matrix", "period_start", "rolling_correlation",
    "to_returns",
    "Horizon", "PerformanceEngine", "UniverseHistory", "change", "daily_changes", "rank_movers",
    "load_universe_history", "required_start", "target_date", "zscore",
]
