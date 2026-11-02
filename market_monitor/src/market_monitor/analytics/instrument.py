r"""Instrument page: one instrument's real level history and its statistics.

Everything is computed on **closes** in referential units (index points, yield in %,
spread in bp...), under the instrument's change convention:

* changes over 1J / 1S / 1M / 3M / 6M / YTD / 1A / 3A / 5A: last close :math:`L_0` against
  the last close on or before the target date (1J: the previous close; 1M = same day one
  calendar month earlier...); ``NaN`` when that close is more than
  :data:`MAX_REFERENCE_GAP_DAYS` older than the target or the history is too short;
* 52-week high / low of the closes and the distance from them;
* realised volatility over 1M / 3M (21 / 63 sessions), annualised with :math:`\sqrt{252}`:
  in % for prices, in bp for yields and spreads, in points for vol indices;
* worst move over the displayed period - a fall for a price (peak to trough, in %), a
  rise for a yield or a spread (trough to peak, in bp: the bondholder's loss), a fall in
  points for a vol index;
* moving averages over 50 and 200 sessions and the distance from them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from market_monitor.analytics.performance import change, daily_changes
from market_monitor.referential import ChangeUnit, Instrument

#: display periods of the page, shortest first
PROFILE_PERIODS = ("1M", "3M", "6M", "YTD", "1A", "3A", "5A", "Max")
#: change horizons of the statistics
STAT_HORIZONS = ("1J", "1S", "1M", "3M", "6M", "YTD", "1A", "3A", "5A")
_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1A": 12, "3A": 36, "5A": 60}
MAX_START = date(1990, 1, 1)
MAX_REFERENCE_GAP_DAYS = 7
ANNUALISATION = 252
VOL_SESSIONS = {"1M": 21, "3M": 63}
MA_WINDOWS = (50, 200)
#: history loaded before the displayed window, so the 200-session average is defined from its start
MA_WARMUP_DAYS = 300


def period_start(period: str, as_of: date) -> date:
    """First date of a display period ending at ``as_of`` (calendar months, YTD = 31/12)."""
    if period == "Max":
        return MAX_START
    if period == "YTD":
        return date(as_of.year - 1, 12, 31)
    if period == "1S":
        return as_of - timedelta(days=7)
    if period not in _MONTHS:
        raise ValueError(f"unknown period {period!r} (allowed: {PROFILE_PERIODS})")
    return (pd.Timestamp(as_of) - pd.DateOffset(months=_MONTHS[period])).date()


def load_start(period: str, as_of: date) -> date:
    """Start of the history to load: the longest statistic (5A) or the period, plus the
    moving-average warm-up."""
    start = min(period_start(period, as_of), period_start("5A", as_of))
    return max(MAX_START, start - timedelta(days=MA_WARMUP_DAYS)) if period != "Max" else MAX_START


def reference(series: pd.Series, target: date) -> tuple[float, pd.Timestamp | None]:
    """Last close on or before ``target``; ``NaN`` when it is stale or missing."""
    before = series.loc[: pd.Timestamp(target)]
    if before.empty:
        return math.nan, None
    when = before.index[-1]
    if (pd.Timestamp(target) - when).days > MAX_REFERENCE_GAP_DAYS:
        return math.nan, None
    return float(before.iloc[-1]), when


def horizon_changes(closes: pd.Series, inst: Instrument) -> dict[str, float]:
    """Change of the last close over every :data:`STAT_HORIZONS` horizon."""
    if closes.empty:
        return {h: math.nan for h in STAT_HORIZONS}
    last, t0 = float(closes.iloc[-1]), closes.index[-1].date()
    out: dict[str, float] = {}
    for horizon in STAT_HORIZONS:
        if horizon == "1J":
            base = float(closes.iloc[-2]) if len(closes) > 1 else math.nan
        else:
            base, _ = reference(closes, period_start(horizon, t0))
        out[horizon] = math.nan if math.isnan(base) else change(last, base, inst.change, inst.bp_factor)
    return out


def realised_vol(closes: pd.Series, inst: Instrument, sessions: int) -> float:
    """Annualised standard deviation of the last ``sessions`` daily changes."""
    steps = daily_changes(closes, inst.change, inst.bp_factor).dropna().tail(sessions)
    if len(steps) < max(10, sessions // 2):
        return math.nan
    return float(steps.std(ddof=1)) * math.sqrt(ANNUALISATION)


def worst_move(closes: pd.Series, inst: Instrument) -> float:
    """Worst move over ``closes`` for a holder (see module docstring); ``NaN`` if empty."""
    if len(closes) < 2:
        return math.nan
    if inst.change is ChangeUnit.PCT:
        return float(100.0 * (closes / closes.cummax() - 1.0).min())
    if inst.change is ChangeUnit.BP:
        return float((inst.bp_factor or 1.0) * (closes - closes.cummin()).max())
    return float((closes - closes.cummax()).min())


WORST_MOVE_LABELS = {ChangeUnit.PCT: "Perte maximale", ChangeUnit.BP: "Hausse maximale",
                     ChangeUnit.ABS: "Baisse maximale"}


def moving_averages(closes: pd.Series) -> pd.DataFrame:
    """``ma50`` / ``ma200``: averages over that many sessions (NaN until enough history)."""
    return pd.DataFrame({f"ma{n}": closes.rolling(n, min_periods=n).mean() for n in MA_WINDOWS})


@dataclass(frozen=True)
class InstrumentStats:
    level: float
    level_date: pd.Timestamp | None
    changes: dict[str, float]
    high_52w: float
    high_52w_date: pd.Timestamp | None
    low_52w: float
    low_52w_date: pd.Timestamp | None
    from_high_52w: float           # change convention: % / bp / points
    from_low_52w: float
    vol_1m: float
    vol_3m: float
    worst_move: float              # over the displayed period
    worst_move_label: str
    ma50: float
    ma200: float
    to_ma50: float
    to_ma200: float
    sessions: int                  # closes in the displayed period


def instrument_stats(closes: pd.Series, inst: Instrument, window_start: date) -> InstrumentStats:
    """Statistics of ``closes`` (full loaded history); the worst move is over the window."""
    closes = closes.dropna()
    if closes.empty:
        nan = math.nan
        return InstrumentStats(nan, None, horizon_changes(closes, inst), nan, None, nan, None, nan,
                               nan, nan, nan, nan, WORST_MOVE_LABELS[inst.change], nan, nan, nan, nan, 0)
    last, t0 = float(closes.iloc[-1]), closes.index[-1]
    year = closes.loc[t0 - pd.DateOffset(years=1):]
    window = closes.loc[pd.Timestamp(window_start):]
    averages = moving_averages(closes).iloc[-1]

    def dist(level: float) -> float:
        return change(last, level, inst.change, inst.bp_factor) if not math.isnan(level) else math.nan

    return InstrumentStats(
        level=last, level_date=t0, changes=horizon_changes(closes, inst),
        high_52w=float(year.max()), high_52w_date=year[year == year.max()].index[-1],
        low_52w=float(year.min()), low_52w_date=year[year == year.min()].index[-1],
        from_high_52w=dist(float(year.max())), from_low_52w=dist(float(year.min())),
        vol_1m=realised_vol(closes, inst, VOL_SESSIONS["1M"]),
        vol_3m=realised_vol(closes, inst, VOL_SESSIONS["3M"]),
        worst_move=worst_move(window, inst), worst_move_label=WORST_MOVE_LABELS[inst.change],
        ma50=float(averages["ma50"]), ma200=float(averages["ma200"]),
        to_ma50=dist(float(averages["ma50"])), to_ma200=dist(float(averages["ma200"])),
        sessions=len(window),
    )


def spread_vs(yields: pd.Series, benchmark: pd.Series) -> pd.Series:
    """Yield spread in bp on common dates: :math:`100\\,(y - y_{\\text{Bund}})`."""
    both = pd.concat({"y": yields, "b": benchmark}, axis=1, join="inner").dropna()
    return 100.0 * (both["y"] - both["b"])


def pair_spread(levels: pd.Series, other: pd.Series, quote: str, other_quote: str | None) -> pd.Series | None:
    """``levels - other`` in bp on common dates when both are yields (x100) or both spreads."""
    if other_quote != quote or quote not in ("yield", "spread"):
        return None
    both = pd.concat({"a": levels, "b": other}, axis=1, join="inner").dropna()
    return (100.0 if quote == "yield" else 1.0) * (both["a"] - both["b"])


def spread_changes(spread: pd.Series, as_of: date) -> dict[str, float]:
    """Last level and its changes (bp) over 1J / 1S / 1M / YTD / 1A, closes on or before ``as_of``."""
    s = spread.dropna()
    s = s[s.index <= pd.Timestamp(as_of)]
    if s.empty:
        return {}
    last_day, last = s.index[-1], float(s.iloc[-1])
    out = {"level": last}
    targets = {"1J": None, "1S": last_day - pd.Timedelta(days=7), "1M": last_day - pd.DateOffset(months=1),
               "YTD": pd.Timestamp(last_day.year - 1, 12, 31), "1A": last_day - pd.DateOffset(years=1)}
    for label, target in targets.items():
        before = s.iloc[:-1] if target is None else s[s.index <= target]
        out[label] = last - float(before.iloc[-1]) if not before.empty else math.nan
    return out


def resample_bars(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Weekly (``"W"``) or monthly (``"M"``) candles from daily bars, dated by the last session."""
    if bars.empty:
        return bars
    grouper = pd.Grouper(freq="W-FRI" if rule == "W" else "ME")
    groups = bars.groupby(grouper)
    out = pd.DataFrame({
        "open": groups["open"].first(), "high": groups["high"].max(), "low": groups["low"].min(),
        "last": groups["last"].last(), "volume": groups["volume"].sum(min_count=1),
    })
    stamps = bars["last"].groupby(grouper).apply(lambda s: s.index.max())
    out.index = pd.DatetimeIndex(stamps.reindex(out.index).to_numpy(), name="date")
    return out.dropna(subset=["last"])


def candle_frequency(start: date, end: date) -> str:
    """``"D"`` up to 18 months, weekly up to 8 years, monthly beyond (readable candles)."""
    days = (end - start).days
    return "D" if days <= 550 else ("W" if days <= 8 * 365 else "M")


@dataclass
class InstrumentProfile:
    """Everything the instrument page shows, for one instrument and one display period.

    ``bars`` is the whole loaded history (longer than the window: 5-year statistics and the
    200-session average need it); :attr:`window` is what the chart shows.
    """

    instrument: Instrument
    as_of: date
    period: str
    start: date
    bars: pd.DataFrame
    stats: InstrumentStats
    source: str | None = None
    origin: str | None = None
    proxy: str | None = None
    notes: tuple[str, ...] = ()
    error: str | None = None
    warning: str | None = None
    depth: pd.DataFrame | None = None
    benchmark_id: str | None = None
    spread: pd.Series | None = None        # vs the Bund of the same maturity, bp
    overlay_id: str | None = None
    overlay: pd.Series | None = None       # closes of the second instrument
    overlay_name: str | None = None
    overlay_quote: str | None = None       # quote type of the second instrument
    pair_spread: pd.Series | None = None   # instrument minus overlay, bp (two yields or two spreads)
    provisional: bool = False              # today's print dropped: session still open
    requested_start: date | None = None    # period's first date, when the history starts later

    @property
    def window(self) -> pd.DataFrame:
        return self.bars.loc[pd.Timestamp(self.start): pd.Timestamp(self.as_of)]

    @property
    def closes(self) -> pd.Series:
        return self.bars["last"].dropna()

    def averages(self) -> pd.DataFrame:
        """Moving averages computed on the full history, cut to the window."""
        return moving_averages(self.closes).loc[pd.Timestamp(self.start): pd.Timestamp(self.as_of)]

    @property
    def late_start(self) -> bool:
        """The source's history starts after the period's first date (e.g. "Max", 5A): the
        window then starts with the history (``requested_start`` keeps the period's date)."""
        return self.requested_start is not None
