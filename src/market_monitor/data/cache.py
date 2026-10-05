"""Local parquet cache: one file per (provider, field, ticker).

Each file stores the observations plus, in the parquet schema metadata, the
**coverage window** actually requested from the provider and the fetch time. Coverage
!= first/last observation: a window ending on a holiday is still fully covered.
Cache I/O never raises - a broken cache degrades to a cache miss.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from market_monitor.data.models import INDEX_NAME, Field
from market_monitor.data.quality import clean_series

logger = logging.getLogger(__name__)

_META_KEY = b"market_monitor"
_SCHEMA_VERSION = 1
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(ticker: str) -> str:
    """Windows-safe, collision-free file stem for a ticker.

    ``"SX5E Index"`` -> ``"SX5E_Index-1a2b3c4d"``. The hash suffix prevents collisions
    (``"A/B"`` vs ``"A B"``) and reserved names (``CON``, ``NUL``...).
    """
    stem = _UNSAFE_CHARS.sub("_", ticker).strip("_")[:80] or "ticker"
    digest = hashlib.sha1(ticker.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


@dataclass(frozen=True)
class CacheEntry:
    """Cached series with its coverage window ``[covered_start, covered_end]``."""

    series: pd.Series
    covered_start: date
    covered_end: date
    fetched_at: datetime  # tz-aware UTC

    def __post_init__(self) -> None:
        if self.covered_start > self.covered_end:
            raise ValueError("covered_start is after covered_end")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")


class ParquetCache:
    """Parquet store rooted at ``root``."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, provider: str, field: Field, ticker: str) -> Path:
        return self._root / safe_name(provider) / field.value / f"{safe_name(ticker)}.parquet"

    def load(self, provider: str, field: Field, ticker: str) -> CacheEntry | None:
        """Return the cached entry, or ``None`` on miss / corruption / foreign file."""
        path = self.path_for(provider, field, ticker)
        if not path.is_file():
            return None
        try:
            table = pq.read_table(path)
            meta = json.loads((table.schema.metadata or {})[_META_KEY])
            if meta.get("version") != _SCHEMA_VERSION or meta.get("ticker") != ticker:
                logger.warning("Ignoring cache file with unexpected metadata: %s", path)
                return None
            frame = table.to_pandas()
            return CacheEntry(
                series=clean_series(frame["value"]),
                covered_start=date.fromisoformat(meta["covered_start"]),
                covered_end=date.fromisoformat(meta["covered_end"]),
                fetched_at=datetime.fromisoformat(meta["fetched_at"]),
            )
        except (OSError, KeyError, ValueError, TypeError, pa.ArrowException) as exc:
            logger.warning("Unreadable cache file %s (%s) - treated as a miss", path, exc)
            return None

    def save(self, provider: str, field: Field, ticker: str, entry: CacheEntry) -> bool:
        """Atomically write ``entry``; returns ``False`` (and logs) on failure."""
        path = self.path_for(provider, field, ticker)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            frame = pd.DataFrame(
                {"value": entry.series.to_numpy(dtype="float64")},
                index=pd.DatetimeIndex(entry.series.index, name=INDEX_NAME),
            )
            table = pa.Table.from_pandas(frame, preserve_index=True)
            meta = dict(table.schema.metadata or {})
            meta[_META_KEY] = json.dumps(
                {
                    "version": _SCHEMA_VERSION,
                    "provider": provider,
                    "ticker": ticker,
                    "field": field.value,
                    "covered_start": entry.covered_start.isoformat(),
                    "covered_end": entry.covered_end.isoformat(),
                    "fetched_at": entry.fetched_at.isoformat(),
                }
            ).encode("utf-8")
            pq.write_table(table.replace_schema_metadata(meta), tmp)
            os.replace(tmp, path)  # atomic on NTFS as well
            return True
        except (OSError, ValueError, pa.ArrowException) as exc:
            logger.warning("Cache write failed for %s/%s (%s)", provider, ticker, exc)
            tmp.unlink(missing_ok=True)
            return False

    def clear(self, provider: str | None = None) -> None:
        """Delete the whole cache, or a single provider's sub-tree."""
        target = self._root / safe_name(provider) if provider else self._root
        if target.exists():
            shutil.rmtree(target)
