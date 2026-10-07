"""Daily-macro layout (``config/daily_macro.yaml``): bands, columns, movers rule."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from market_monitor.export.display import (
    CHANGE_UNITS,
    DEFAULT_LINE,
    DEFAULT_STYLE,
    LineFormat,
    NumberStyle,
)
from market_monitor.referential import Referential, ReferentialError

EXPORTABLE_COLUMNS = ("chg_1d", "chg_1w", "chg_mtd", "chg_ytd")


@dataclass(frozen=True)
class Section:
    title: str
    instruments: tuple[str, ...]
    lines: tuple[LineFormat, ...] = ()  # parallel to ``instruments`` (empty = defaults)

    def line(self, instrument_id: str) -> LineFormat:
        if instrument_id in self.instruments and self.lines:
            return self.lines[self.instruments.index(instrument_id)]
        return DEFAULT_LINE


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
    style: NumberStyle = DEFAULT_STYLE

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
        style=_style(raw.get("format") or {}),
    )


LINE_KEYS = {"id", "label", "decimals", "suffix", "change", "change_decimals"}
SECTION_DEFAULT_KEYS = ("decimals", "suffix", "change", "change_decimals")


def _section(raw: Any, referential: Referential, index: int) -> Section:
    """Entries are ids / selectors, or ``{id: X, label:, decimals:, suffix:, change:, ...}``."""
    if not isinstance(raw, Mapping) or not raw.get("title") or not raw.get("instruments"):
        raise ReferentialError(f"sections[{index}] needs a title and instruments")
    where = f"daily-macro section {raw['title']!r}"
    entries = raw["instruments"] if isinstance(raw["instruments"], list) else [raw["instruments"]]
    defaults = {k: raw[k] for k in SECTION_DEFAULT_KEYS if k in raw}
    ids: list[str] = []
    lines: list[LineFormat] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            unknown = set(entry) - LINE_KEYS
            if "id" not in entry or unknown:
                raise ReferentialError(f"{where}: line {dict(entry)} needs 'id' (allowed keys: "
                                       f"{sorted(LINE_KEYS)})")
            resolved = referential.resolve([str(entry["id"])], context=where)
            if len(resolved) != 1:
                raise ReferentialError(f"{where}: {entry['id']!r} must name one instrument")
            options = {**defaults, **{k: v for k, v in entry.items() if k != "id"}}
        else:
            resolved = referential.resolve([entry], context=where)
            options = dict(defaults)
        for instrument_id in resolved:
            if instrument_id not in ids:
                ids.append(instrument_id)
                lines.append(_line_format(options, where))
    return Section(str(raw["title"]), tuple(ids), tuple(lines))


def _line_format(options: Mapping[str, Any], where: str) -> LineFormat:
    for key in ("decimals", "change_decimals"):
        value = options.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or not 0 <= value <= 6):
            raise ReferentialError(f"{where}: {key} must be an integer between 0 and 6")
    change = options.get("change")
    if change is not None and change not in CHANGE_UNITS:
        raise ReferentialError(f"{where}: change must be one of {CHANGE_UNITS}")
    label, suffix = options.get("label"), options.get("suffix")
    return LineFormat(
        label=str(label) if label else None,
        decimals=options.get("decimals"),
        suffix=str(suffix) if suffix is not None else None,
        change_unit=change,
        change_decimals=options.get("change_decimals"),
    )


def _style(raw: Any) -> NumberStyle:
    if not isinstance(raw, Mapping):
        raise ReferentialError("daily-macro format must be a mapping")
    unknown = set(raw) - {"thousands_separator", "compact_units", "bp_unit"}
    if unknown:
        raise ReferentialError(f"unknown format options {sorted(unknown)}")
    bp_unit = str(raw.get("bp_unit", DEFAULT_STYLE.bp_unit)).strip()
    if not bp_unit:
        raise ReferentialError("format.bp_unit cannot be empty")
    return NumberStyle(
        thousands_separator=bool(raw.get("thousands_separator", DEFAULT_STYLE.thousands_separator)),
        compact_units=bool(raw.get("compact_units", DEFAULT_STYLE.compact_units)),
        bp_unit=bp_unit,
    )


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
