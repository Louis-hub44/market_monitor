"""Local parquet cache.

Layout (one folder per provider, readable with any parquet tool)::

    <cache_dir>/<provider>/history/<id>.parquet          one series per logical id
    <cache_dir>/<provider>/<dataset>/<partition>.parquet  tables (quotes per as-of, bond static...)

Each file has a ``.meta.json`` sidecar holding the fetch timestamp and, for
series, the covered date window. Freshness rules:

* a history window ending before today is immutable -> never expires;
* a window that includes today expires after ``ttl_hours_live``;
* tables have an explicit TTL passed by the caller (``None`` = never expires).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(key: str) -> str:
    """Filesystem-safe (Windows included) and collision-free file stem."""
    stem = _SAFE.sub("_", key).strip("._")[:80] or "key"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SeriesMeta:
    start: date
    end: date
    fetched_at: datetime


class ParquetCache:
    """Parquet-backed cache for series and small tables."""

    def __init__(self, directory: Path | str, ttl_hours_live: float = 4.0) -> None:
        self.directory = Path(directory)
        self.ttl_live = timedelta(hours=ttl_hours_live)

    # ---------------------------------------------------------------- paths
    def _path(self, provider: str, dataset: str, key: str) -> Path:
        return self.directory / _SAFE.sub("_", provider) / _SAFE.sub("_", dataset) / f"{_safe_name(key)}.parquet"

    @staticmethod
    def _meta_path(path: Path) -> Path:
        return path.with_suffix(".meta.json")

    def _read_meta(self, path: Path) -> dict | None:
        mp = self._meta_path(path)
        if not (path.is_file() and mp.is_file()):
            return None
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Corrupted cache metadata %s, ignoring", mp)
            return None

    def _write(self, path: Path, df: pd.DataFrame, meta: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        df.to_parquet(tmp)
        tmp.replace(path)
        self._meta_path(path).write_text(json.dumps(meta, default=str), encoding="utf-8")

    # --------------------------------------------------------------- series
    def series_meta(self, provider: str, market_id: str) -> SeriesMeta | None:
        meta = self._read_meta(self._path(provider, "history", market_id))
        if meta is None:
            return None
        return SeriesMeta(date.fromisoformat(meta["start"]), date.fromisoformat(meta["end"]),
                          datetime.fromisoformat(meta["fetched_at"]))

    def get_series(self, provider: str, market_id: str, start: date, end: date,
                   today: date | None = None) -> pd.Series | None:
        """Cached series restricted to ``[start, end]`` if the cache covers and is fresh."""
        meta = self.series_meta(provider, market_id)
        if meta is None or meta.start > start or meta.end < end:
            return None
        today = today or date.today()
        if meta.end >= today and _utcnow() - meta.fetched_at > self.ttl_live:
            return None
        path = self._path(provider, "history", market_id)
        try:
            s = pd.read_parquet(path)["value"]
        except (OSError, KeyError, ValueError) as exc:
            logger.warning("Unreadable cache file %s (%s), ignoring", path, exc)
            return None
        mask = (s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))
        return s.loc[mask].rename(market_id)

    def put_series(self, provider: str, market_id: str, series: pd.Series, start: date, end: date) -> None:
        """Store a series fetched for the window ``[start, end]`` (merged with existing data)."""
        path = self._path(provider, "history", market_id)
        new = series.dropna().astype(float).rename("value")
        new.index = pd.DatetimeIndex(new.index, name="date")
        old_meta = self.series_meta(provider, market_id)
        if old_meta is not None and old_meta.start <= end and old_meta.end >= start:
            try:
                old = pd.read_parquet(path)["value"]
                new = new.combine_first(old).sort_index()
                start, end = min(start, old_meta.start), max(end, old_meta.end)
            except (OSError, KeyError, ValueError):
                pass
        self._write(path, new.to_frame(), {"id": market_id, "start": start.isoformat(),
                                           "end": end.isoformat(), "fetched_at": _utcnow().isoformat()})

    # ---------------------------------------------------------------- tables
    def get_table(self, provider: str, dataset: str, partition: str,
                  ttl: timedelta | None = None) -> pd.DataFrame | None:
        """Cached table, or ``None`` if absent / older than ``ttl``."""
        path = self._path(provider, dataset, partition)
        meta = self._read_meta(path)
        if meta is None:
            return None
        if ttl is not None and _utcnow() - datetime.fromisoformat(meta["fetched_at"]) > ttl:
            return None
        try:
            return pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("Unreadable cache file %s (%s), ignoring", path, exc)
            return None

    def upsert_table(self, provider: str, dataset: str, partition: str, df: pd.DataFrame) -> None:
        """Insert/replace rows (by index) of a cached table."""
        path = self._path(provider, dataset, partition)
        if path.is_file():
            try:
                old = pd.read_parquet(path)
                df = pd.concat([old.loc[~old.index.isin(df.index)], df])
            except (OSError, ValueError):
                pass
        self._write(path, df, {"dataset": dataset, "partition": partition,
                               "fetched_at": _utcnow().isoformat()})

    def clear(self, provider: str | None = None) -> int:
        """Delete cached files (all or one provider). Returns the number of files removed."""
        root = self.directory / provider if provider else self.directory
        if not root.exists():
            return 0
        n = 0
        for f in sorted(root.rglob("*"), reverse=True):
            if f.is_file():
                f.unlink()
                n += 1
        return n
