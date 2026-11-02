"""Cache disque à TTL, avec lecture périmée assumée.

Deux modes de lecture :

* `get(key, ttl)` — donnée fraîche, qualité inchangée ;
* `get_stale(key)` — donnée périmée, renvoyée **uniquement** quand tous les
  providers sont indisponibles, et systématiquement dégradée en CACHED avec
  la date de collecte affichée. C'est ce qui permet à l'application de rester
  utilisable pendant une coupure sans jamais mentir sur la fraîcheur.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import get_settings


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    stored_at: float

    @property
    def age_s(self) -> float:
        return time.time() - self.stored_at


class DiskCache:
    def __init__(self, root: Path | None = None, namespace: str = "default") -> None:
        self.root = Path(root or get_settings().cache_dir) / namespace
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.root / f"{digest}.json"

    def _read(self, key: str) -> CacheEntry | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return CacheEntry(value=raw["value"], stored_at=float(raw["stored_at"]))
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return None

    def get(self, key: str, ttl_s: int) -> CacheEntry | None:
        entry = self._read(key)
        if entry is None or entry.age_s > ttl_s:
            return None
        return entry

    def get_stale(self, key: str) -> CacheEntry | None:
        return self._read(key)

    def set(self, key: str, value: Any) -> None:
        try:
            self._path(key).write_text(
                json.dumps({"value": value, "stored_at": time.time()}, default=str),
                encoding="utf-8",
            )
        except (OSError, TypeError):
            pass  # un cache défaillant ne doit jamais interrompre l'application

    def clear(self) -> int:
        n = 0
        for f in self.root.glob("*.json"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        return n

    def stats(self) -> dict[str, Any]:
        files = list(self.root.glob("*.json"))
        return {
            "namespace": self.root.name,
            "entries": len(files),
            "size_kb": round(sum(f.stat().st_size for f in files) / 1024, 1) if files else 0.0,
            "oldest_age_h": round(
                max((time.time() - f.stat().st_mtime for f in files), default=0) / 3600, 1
            ),
        }
