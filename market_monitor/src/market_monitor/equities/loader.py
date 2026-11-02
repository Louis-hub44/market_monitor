"""Batch loading of the members' daily bars into the :class:`EquityPanel` (Yahoo).

Budget (README § 5.5): ~1 130 symbols with the Stoxx 600, ~700 without. The first load
asks ~13 months per symbol (200-session average, 52-week range, 20-session volume); then
each morning asks the last week only. Symbols go by chunks of ``chunk_size`` in one Yahoo
download, with a pause between chunks; a refused chunk (rate limit, firewall) stops the
run - what is loaded is kept and the next run resumes where it stopped.

Two traps of cached equity prices are handled:

* **restated history**: after a split (or a fix), Yahoo rescales the past closes; the
  overlap between the cache and the new download then disagrees by a constant factor and
  the symbol's whole history is reloaded (otherwise a fake -50 % move would appear);
* **splits** reported by Yahoo are recorded with their date, for the movers' safeguards.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from market_monitor.data.models import BAR_COLUMNS
from market_monitor.data.providers.free import extract_yahoo_field
from market_monitor.data.quality import clean_bars
from market_monitor.equities.panel import EquityPanel
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError
from market_monitor.network import apply_to_yfinance, blocked_reason, captured_logs

logger = logging.getLogger(__name__)

#: symbols -> start, end -> {symbol: bars with an optional ``split`` column}
BarsFetcher = Callable[[list[str], date, date], dict[str, pd.DataFrame]]
RESTATEMENT_TOLERANCE = 0.02     # overlap closes differing by more than 2 % -> history rescaled
EMPTY_CHUNKS_BEFORE_STOP = 2     # consecutive chunks without any data: source blocked


@dataclass
class LoadReport:
    requested: int = 0
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    restated: list[str] = field(default_factory=list)
    splits: dict[str, dict[str, float]] = field(default_factory=dict)
    remaining: list[str] = field(default_factory=list)
    calls: int = 0
    stopped: str | None = None

    def summary(self) -> str:
        parts = [f"{len(self.updated)} titre(s) mis à jour en {self.calls} appel(s)"]
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} déjà à jour")
        if self.restated:
            parts.append(f"historique recalculé (split ?) : {', '.join(self.restated)}")
        if self.failed:
            parts.append(f"{len(self.failed)} sans donnée ({', '.join(sorted(self.failed)[:8])}"
                         + ("…" if len(self.failed) > 8 else "") + ")")
        if self.stopped:
            parts.append(f"arrêt : {self.stopped} ; {len(self.remaining)} titre(s) restant(s), "
                         "relancer plus tard pour reprendre")
        return " ; ".join(parts)


class EquityLoader:
    def __init__(
        self,
        panel: EquityPanel,
        fetch: BarsFetcher,
        *,
        chunk_size: int = 40,
        pause_s: float = 2.0,
        history_days: int = 400,
        lookback_days: int = 7,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if chunk_size < 1 or history_days < 30 or lookback_days < 1 or pause_s < 0:
            raise ValueError("invalid loader settings")
        self._panel, self._fetch = panel, fetch
        self._chunk, self._pause = chunk_size, pause_s
        self._history, self._lookback = timedelta(days=history_days), timedelta(days=lookback_days)
        self._sleep, self._clock = sleep, clock

    def plan(self, tickers: Sequence[str], end: date) -> dict[date, list[str]]:
        """Start date -> symbols: a full history for new symbols, the last week otherwise.

        A symbol downloaded after ``end``'s close is already up to date (skipped).
        """
        plan: dict[date, list[str]] = defaultdict(list)
        for ticker in dict.fromkeys(tickers):
            cov = self._panel.coverage(ticker)
            if cov is None or cov.start > end - self._history + timedelta(days=7):
                plan[end - self._history].append(ticker)
            elif cov.end >= end and cov.fetched_at >= _after_close(end):
                continue
            else:
                plan[max(cov.start, min(cov.end, end) - self._lookback)].append(ticker)
        return dict(plan)

    def refresh(self, tickers: Sequence[str], end: date | None = None,
                progress: Callable[[int, int], None] | None = None) -> LoadReport:
        """Bring ``tickers`` up to ``end`` (default: today), by chunks; resumable."""
        end = end or self._clock().date()
        report = LoadReport(requested=len(dict.fromkeys(tickers)))
        plan = self.plan(tickers, end)
        planned = {t for group in plan.values() for t in group}
        report.unchanged = [t for t in dict.fromkeys(tickers) if t not in planned]
        chunks = [(start, group[k:k + self._chunk]) for start, group in sorted(plan.items())
                  for k in range(0, len(group), self._chunk)]
        empty_streak, done = 0, 0
        for number, (start, chunk) in enumerate(chunks):
            if number:
                self._sleep(self._pause)
            try:
                fetched = self._fetch(list(chunk), start, end)
                report.calls += 1
            except ProviderUnavailableError as exc:
                report.stopped = str(exc)
                report.remaining = [t for _, c in chunks[number:] for t in c]
                break
            except DataProviderError as exc:
                report.calls += 1
                fetched = {}
                logger.warning("equity chunk failed (%s): %s", ", ".join(chunk[:3]), exc)
            raw = {t: b for t, b in fetched.items() if t in chunk}
            got = {t: clean_bars(b) for t, b in raw.items()}
            got = {t: b for t, b in got.items() if not b.empty}
            empty_streak = 0 if got else empty_streak + 1
            self._absorb(got, raw, chunk, start, end, report)
            done += len(chunk)
            if progress is not None:
                progress(done, len(planned))
            if empty_streak >= EMPTY_CHUNKS_BEFORE_STOP:
                report.stopped = "source muette sur deux paquets de suite (limite Yahoo ou pare-feu)"
                report.remaining = [t for _, c in chunks[number + 1:] for t in c]
                break
        return report

    def _absorb(self, got: dict[str, pd.DataFrame], raw: dict[str, pd.DataFrame], chunk: Sequence[str],
                start: date, end: date, report: LoadReport) -> None:
        restated = [t for t, b in got.items() if self._restated(t, b)]
        for ticker in chunk:
            if ticker not in got:
                report.failed[ticker] = "aucune donnée Yahoo (symbole, suspension ou blocage)"
        if restated:  # whole history again, so the past closes share the new scale
            try:
                again = self._fetch(restated, end - self._history, end)
                report.calls += 1
            except DataProviderError:
                again = {}
            for ticker in restated:
                fresh = clean_bars(again.get(ticker))
                if not fresh.empty:
                    got[ticker] = fresh
                    report.restated.append(ticker)
        splits = {t: _splits(raw[t]) for t in got if t in raw}
        splits = {t: s for t, s in splits.items() if s}
        report.splits.update(splits)
        windows = {t: ((end - self._history) if t in report.restated else start, end) for t in got}
        self._panel.update({t: b[list(BAR_COLUMNS)] for t, b in got.items()}, windows,
                           replace=report.restated, splits=splits, now=self._clock())
        report.updated.extend(got)

    def _restated(self, ticker: str, fresh: pd.DataFrame) -> bool:
        """The cached closes and the new ones disagree by a constant factor on their overlap."""
        if self._panel.coverage(ticker) is None:
            return False
        cached = self._panel.bars(ticker)["last"]
        overlap = pd.concat({"old": cached, "new": fresh["last"]}, axis=1, join="inner").dropna()
        overlap = overlap.iloc[:-1]  # the latest close may legitimately be revised
        if overlap.empty:
            return False
        ratio = (overlap["new"] / overlap["old"]).median()
        return bool(abs(ratio - 1.0) > RESTATEMENT_TOLERANCE)


def _after_close(day: date) -> datetime:
    """22:00 UTC on ``day``: every market of the indices (New York included) has closed."""
    return datetime(day.year, day.month, day.day, 22, 0, tzinfo=UTC)


def _splits(bars: pd.DataFrame) -> dict[str, float]:
    if "split" not in bars.columns:
        return {}
    events = bars["split"].dropna()
    events = events[(events > 0) & (events != 1.0)]
    return {f"{d:%Y-%m-%d}": float(v) for d, v in events.items()}


# ------------------------------------------------------------------- Yahoo
def yahoo_equity_fetcher(session: Any = None,
                         download: Callable[..., pd.DataFrame] | None = None) -> BarsFetcher:
    """Yahoo daily bars + splits for a chunk of symbols (one ``yfinance.download``)."""
    def fetch(tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        with captured_logs("yfinance") as messages:  # one console line per symbol otherwise
            try:
                raw = (download or _download)(tickers, start, end, session)
            except ProviderUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - yfinance raises anything
                text = str(exc)
                if "rate" in text.lower() or "429" in text or "too many" in text.lower():
                    raise ProviderUnavailableError(
                        f"Yahoo refuse (limite de requêtes) : {text[:120]}") from exc
                if "certificate" in text.lower() or "curl: (60)" in text:
                    raise ProviderUnavailableError(f"Yahoo refuse : {blocked_reason([text])}") from exc
                raise DataProviderError(f"yfinance download failed: {text[:160]}") from exc
        if (raw is None or raw.empty) and messages:
            reason = blocked_reason(messages)
            if reason.startswith("certificat"):
                raise ProviderUnavailableError(f"Yahoo refuse : {reason}")
        columns = {"open": "Open", "high": "High", "low": "Low", "last": "Close", "volume": "Volume",
                   "split": "Stock Splits"}
        fields = {c: extract_yahoo_field(raw, tickers, yf) for c, yf in columns.items()}
        return {t: pd.DataFrame({c: f[t] for c, f in fields.items() if t in f})
                for t in tickers if t in fields["last"]}
    return fetch


def _download(tickers: list[str], start: date, end: date, session: Any) -> pd.DataFrame:
    import yfinance as yf
    if session is not None:
        apply_to_yfinance(session)
    return yf.download(tickers=tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                       interval="1d", auto_adjust=False, actions=True, progress=False, threads=True,
                       group_by="column", session=session)
