"""Daily-macro layout (``config/daily_macro.yaml``): bands, columns, movers rule."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from market_monitor.referential import Referential, ReferentialError

EXPORTABLE_COLUMNS = ("chg_1d", "chg_1w", "chg_mtd", "chg_ytd")


@dataclass(frozen=True)
class Section:
    title: str
    instruments: tuple[str, ...]


@dataclass(frozen=True)
class MoversRule:
    universe: tuple[str, ...]  # resolved instrument ids
    count: int = 8
    min_abs_z: float = 1.5
    strong_z: float = 2.5
    show_zscore: bool = True


@dataclass(frozen=True)
class DailyMacroLayout:
    title: str
    sections: tuple[Section, ...]
    columns: tuple[str, ...]
    movers: MoversRule
    file_prefix: str = "daily_macro"

    @property
    def section_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(i for s in self.sections for i in s.instruments))


def load_layout(path: str | Path, referential: Referential) -> DailyMacroLayout:
    """Read and validate the layout against the referential."""
    file = Path(path)
    if not file.is_file():
        raise ReferentialError(f"daily-macro layout not found: {file}")
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ReferentialError(f"invalid YAML in {file}: {exc}") from exc
    return layout_from_dict(raw, referential)


def layout_from_dict(raw: Mapping[str, Any], referential: Referential) -> DailyMacroLayout:
    if not isinstance(raw, Mapping):
        raise ReferentialError("daily-macro layout must be a mapping")
    sections = tuple(_section(s, referential, k) for k, s in enumerate(raw.get("sections") or []))
    if not sections:
        raise ReferentialError("daily-macro layout needs at least one section")
    columns = tuple(raw.get("columns") or ("chg_1d", "chg_ytd"))
    unknown = [c for c in columns if c not in EXPORTABLE_COLUMNS]
    if unknown:
        raise ReferentialError(f"unknown export columns {unknown} (allowed: {EXPORTABLE_COLUMNS})")
    prefix = str(raw.get("file_prefix", "daily_macro"))
    if not prefix or any(ch in prefix for ch in '<>:"/\\|?* '):
        raise ReferentialError(f"invalid file_prefix {prefix!r}")
    return DailyMacroLayout(
        title=str(raw.get("title", "Revue de marché")),
        sections=sections,
        columns=columns,
        movers=_movers(raw.get("movers") or {}, referential),
        file_prefix=prefix,
    )


def _section(raw: Any, referential: Referential, index: int) -> Section:
    if not isinstance(raw, Mapping) or not raw.get("title") or not raw.get("instruments"):
        raise ReferentialError(f"sections[{index}] needs a title and instruments")
    ids = referential.resolve(raw["instruments"], context=f"daily-macro section {raw['title']!r}")
    return Section(str(raw["title"]), ids)


def _movers(raw: Mapping[str, Any], referential: Referential) -> MoversRule:
    universe = raw.get("universe", "home")
    if isinstance(universe, str) and universe in referential.watchlist_names:
        entries: list[str] = list(referential.watchlist(universe).instruments)
    else:
        entries = [universe] if isinstance(universe, str) else list(universe)
    count = raw.get("count", 8)
    min_abs_z, strong_z = raw.get("min_abs_z", 1.5), raw.get("strong_z", 2.5)
    if not isinstance(count, int) or count < 0:
        raise ReferentialError("movers.count must be a non-negative integer")
    if not all(isinstance(v, (int, float)) and v >= 0 for v in (min_abs_z, strong_z)):
        raise ReferentialError("movers.min_abs_z and movers.strong_z must be >= 0")
    return MoversRule(
        universe=referential.resolve(entries, context="daily-macro movers"),
        count=count, min_abs_z=float(min_abs_z), strong_z=float(strong_z),
        show_zscore=bool(raw.get("show_zscore", True)),
    )
