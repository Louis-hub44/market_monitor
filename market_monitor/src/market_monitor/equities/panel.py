"""Local store of the members' daily bars: one wide parquet per field, plus coverage.

``<directory>/last.parquet`` (date x Yahoo symbol), ``open`` / ``high`` / ``low`` / ``volume``
likewise, and ``coverage.json``: per symbol the window already loaded, the time of the last
download, the source and the splits seen. ~1 100 symbols x 400 sessions read in well under a
second, so the views never wait on the network.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from market_monitor.data.models import BAR_COLUMNS, INDEX_NAME

logger = logging.getLogger(__name__)
COVERAGE_FILE = "coverage.json"


@dataclass
class Coverage:
    start: date
    end: date
    fetched_at: datetime
    source: str = "yahoo"
    splits: dict[str, float] = field(default_factory=dict)   # ISO date -> ratio

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "fetched_at": self.fetched_at.isoformat(), "source": self.source, "splits": self.splits}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Coverage:
        return cls(date.fromisoformat(raw["start"]), date.fromisoformat(raw["end"]),
                   datetime.fromisoformat(raw["fetched_at"]), str(raw.get("source", "yahoo")),
                   {str(k): float(v) for k, v in (raw.get("splits") or {}).items()})


class EquityPanel:
    """Wide per-field store; every write is atomic, a broken file reads as empty."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._frames: dict[str, pd.DataFrame] | None = None
        self._coverage: dict[str, Coverage] | None = None
        self._stamp = ""

    @property
    def directory(self) -> Path:
        return self._dir

    # ----------------------------------------------------------------- read
    def field(self, name: str) -> pd.DataFrame:
        """``last`` / ``open`` / ``high`` / ``low`` / ``volume``: date x symbol."""
        return self._load()[name]

    def bars(self, ticker: str) -> pd.DataFrame:
        frames = self._load()
        columns = {c: frames[c][ticker] for c in BAR_COLUMNS if ticker in frames[c].columns}
        if "last" not in columns:
            return pd.DataFrame(columns=list(BAR_COLUMNS), index=pd.DatetimeIndex([], name=INDEX_NAME))
        return pd.DataFrame(columns).reindex(columns=list(BAR_COLUMNS)).dropna(subset=["last"])

    def coverage(self, ticker: str | None = None) -> Any:
        cov = self._load_coverage()
        return dict(cov) if ticker is None else cov.get(ticker)

    def stamp(self) -> str:
        """Modification stamp of the stored panel; reloads it when another process wrote it."""
        path = self._dir / COVERAGE_FILE
        current = str(path.stat().st_mtime_ns) if path.is_file() else ""
        if current != self._stamp:
            self._frames, self._coverage, self._stamp = None, None, current
        return current

    @property
    def tickers(self) -> list[str]:
        return list(self._load_coverage())

    def _load(self) -> dict[str, pd.DataFrame]:
        if self._frames is None:
            self._frames = {c: self._read(c) for c in BAR_COLUMNS}
        return self._frames

    def _read(self, name: str) -> pd.DataFrame:
        path = self._dir / f"{name}.parquet"
        if not path.is_file():
            return pd.DataFrame(index=pd.DatetimeIndex([], name=INDEX_NAME), dtype="float64")
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - a broken file must not break the views
            logger.warning("unreadable equity panel %s (%s): ignored", path, exc)
            return pd.DataFrame(index=pd.DatetimeIndex([], name=INDEX_NAME), dtype="float64")
        frame.index = pd.DatetimeIndex(frame.index, name=INDEX_NAME)
        return frame.astype("float64")

    def _load_coverage(self) -> dict[str, Coverage]:
        if self._coverage is None:
            path = self._dir / COVERAGE_FILE
            try:
                raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                self._coverage = {t: Coverage.from_dict(v) for t, v in raw.items()}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("unreadable equity coverage %s (%s): reloading everything", path, exc)
                self._coverage = {}
        return self._coverage

    # ---------------------------------------------------------------- write
    def update(self, bars: Mapping[str, pd.DataFrame], windows: Mapping[str, tuple[date, date]],
               *, replace: Iterable[str] = (), source: str = "yahoo",
               splits: Mapping[str, Mapping[str, float]] | None = None,
               now: datetime | None = None) -> None:
        """Merge downloaded bars (``replace``: the symbol's whole history is substituted)."""
        if not bars:
            return
        now = now or datetime.now(UTC)
        frames, coverage = self._load(), self._load_coverage()
        replaced = set(replace)
        for column in BAR_COLUMNS:
            current = frames[column]
            drop = [t for t in bars if t in replaced and t in current.columns]
            current = current.drop(columns=drop)
            new = pd.DataFrame({t: b[column] for t, b in bars.items() if column in b.columns})
            if new.empty:
                frames[column] = current
                continue
            new.index = pd.DatetimeIndex(new.index, name=INDEX_NAME)
            merged = new.combine_first(current) if not current.empty else new
            frames[column] = merged.sort_index().astype("float64")
        for ticker, (start, end) in windows.items():
            if ticker not in bars:
                continue
            old = coverage.get(ticker)
            keep = old is not None and ticker not in replaced
            seen = dict(old.splits) if keep and old is not None else {}
            seen.update((splits or {}).get(ticker, {}))
            coverage[ticker] = Coverage(min(start, old.start) if keep and old else start,
                                        max(end, old.end) if keep and old else end, now, source, seen)
        self._save()

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        for column, frame in (self._frames or {}).items():
            path = self._dir / f"{column}.parquet"
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            frame.to_parquet(tmp)
            os.replace(tmp, path)
        path = self._dir / COVERAGE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({t: c.as_dict() for t, c in (self._coverage or {}).items()}, indent=0),
                       encoding="utf-8")
        os.replace(tmp, path)
        self._stamp = str(path.stat().st_mtime_ns)
