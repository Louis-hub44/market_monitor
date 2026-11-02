"""Composition referential of the equity indices (``config/constituents/<INDEX>.yaml``).

One file per index::

    index: CAC                 # id of the index in instruments.yaml (its level, for contributions)
    name: CAC 40
    group: core                # core: loaded every morning; extended: on demand (Stoxx 600)
    currency: EUR
    as_of: 2026-10-20          # date of the composition (shown with the movers)
    source: iShares ... / manuel
    weights: holdings          # holdings (ETF file) | marketcap (approx.) | none
    refresh: {kind: ishares, url: "https://..."}   # ishares | fmp | manual
    members:
      - {ticker: MC.PA, name: LVMH, isin: FR0000121014, sector: Consommation discrétionnaire,
         weight: 11.2, mic: XPAR, currency: EUR, fmp: MC.PA}

``ticker`` is the Yahoo symbol (the key of a member); ``weight`` in % of the index (sums to
~100 when known); ``mic`` the ISO market code of the listing (session calendar).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from market_monitor.referential import ReferentialError

GROUPS = ("core", "extended")
WEIGHT_METHODS = ("holdings", "marketcap", "none")
REFRESH_KINDS = ("ishares", "fmp", "wikipedia", "manual")


@dataclass(frozen=True)
class Member:
    ticker: str                 # Yahoo symbol
    name: str
    sector: str = ""
    weight: float | None = None  # % of the index
    isin: str = ""
    mic: str = ""
    currency: str = ""
    fmp: str = ""               # FMP symbol, when it differs from nothing (second source)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ticker": self.ticker, "name": self.name}
        for key in ("isin", "sector", "weight", "mic", "currency", "fmp"):
            value = getattr(self, key)
            if value not in ("", None):
                out[key] = round(value, 4) if key == "weight" else value
        return out


@dataclass(frozen=True)
class Composition:
    index: str
    name: str
    members: tuple[Member, ...]
    group: str = "core"
    currency: str = "EUR"
    as_of: date | None = None
    source: str = ""
    weights: str = "none"
    refresh: Mapping[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(m.ticker for m in self.members)

    @property
    def weighted(self) -> bool:
        return self.weights != "none" and any(m.weight for m in self.members)

    def member(self, ticker: str) -> Member | None:
        return next((m for m in self.members if m.ticker == ticker), None)

    def with_members(self, members: Iterable[Member], *, as_of: date | None, source: str,
                     weights: str) -> Composition:
        return replace(self, members=tuple(members), as_of=as_of, source=source, weights=weights)

    def to_yaml(self) -> str:
        head = {"index": self.index, "name": self.name, "group": self.group, "currency": self.currency,
                "as_of": self.as_of.isoformat() if self.as_of else None, "source": self.source,
                "weights": self.weights, "refresh": dict(self.refresh)}
        text = yaml.safe_dump(head, allow_unicode=True, sort_keys=False, width=110)
        lines = ["members:"] + [f"  - {_flow(m.as_dict())}" for m in self.members]
        return text + "\n".join(lines) + "\n"


def _flow(mapping: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(mapping), allow_unicode=True, default_flow_style=True, sort_keys=False,
                          width=10_000).strip()


# ------------------------------------------------------------------ loading
def composition_from_dict(raw: Mapping[str, Any], where: str = "composition") -> Composition:
    if not isinstance(raw, Mapping):
        raise ReferentialError(f"{where}: expected a mapping")
    index, name = raw.get("index"), raw.get("name")
    if not isinstance(index, str) or not index.strip():
        raise ReferentialError(f"{where}.index is required")
    group = str(raw.get("group", "core"))
    weights = str(raw.get("weights", "none"))
    refresh = raw.get("refresh") or {}
    if group not in GROUPS:
        raise ReferentialError(f"{where}.group must be one of {GROUPS}")
    if weights not in WEIGHT_METHODS:
        raise ReferentialError(f"{where}.weights must be one of {WEIGHT_METHODS}")
    if not isinstance(refresh, Mapping) or refresh.get("kind", "manual") not in REFRESH_KINDS:
        raise ReferentialError(f"{where}.refresh.kind must be one of {REFRESH_KINDS}")
    members, seen = [], set()
    for k, item in enumerate(raw.get("members") or []):
        member = _member(item, f"{where}.members[{k}]")
        if member.ticker in seen:
            raise ReferentialError(f"{where}: duplicate member {member.ticker}")
        seen.add(member.ticker)
        members.append(member)
    as_of = raw.get("as_of")
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    if as_of is not None and not isinstance(as_of, date):
        raise ReferentialError(f"{where}.as_of must be a date (YYYY-MM-DD)")
    return Composition(index=index.strip(), name=str(name or index), members=tuple(members),
                       group=group, currency=str(raw.get("currency", "EUR")), as_of=as_of,
                       source=str(raw.get("source", "")), weights=weights, refresh=dict(refresh))


def _member(raw: Any, where: str) -> Member:
    if not isinstance(raw, Mapping):
        raise ReferentialError(f"{where}: expected a mapping")
    ticker, name = raw.get("ticker"), raw.get("name")
    if not isinstance(ticker, str) or not ticker.strip() or ticker != ticker.strip():
        raise ReferentialError(f"{where}.ticker is required (Yahoo symbol)")
    weight = raw.get("weight")
    if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float))
                               or weight < 0):
        raise ReferentialError(f"{where}.weight must be a non-negative number (% of the index)")
    return Member(ticker=ticker, name=str(name or ticker), sector=str(raw.get("sector") or ""),
                  weight=float(weight) if weight is not None else None, isin=str(raw.get("isin") or ""),
                  mic=str(raw.get("mic") or ""), currency=str(raw.get("currency") or ""),
                  fmp=str(raw.get("fmp") or ""))


def load_composition(path: str | Path) -> Composition:
    file = Path(path)
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ReferentialError(f"unreadable composition {file}: {exc}") from exc
    return replace(composition_from_dict(raw, file.name), path=file)


def save_composition(composition: Composition, path: str | Path | None = None) -> Path:
    file = Path(path or composition.path or f"{composition.index}.yaml")
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(composition.to_yaml(), encoding="utf-8")
    os.replace(tmp, file)
    return file


class Compositions:
    """Every composition file of a directory, by index id."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._items: dict[str, Composition] = {}
        if self._directory.is_dir():
            for file in sorted(self._directory.glob("*.yaml")):
                comp = load_composition(file)
                if comp.index in self._items:
                    raise ReferentialError(f"two composition files for {comp.index}")
                self._items[comp.index] = comp

    @property
    def directory(self) -> Path:
        return self._directory

    def __iter__(self) -> Iterator[Composition]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, index: object) -> bool:
        return index in self._items

    def get(self, index: str) -> Composition:
        if index not in self._items:
            raise ReferentialError(f"no composition for {index!r} (known: {sorted(self._items)})")
        return self._items[index]

    def indices(self, groups: Iterable[str] = GROUPS) -> list[str]:
        wanted = set(groups)
        return [c.index for c in self._items.values() if c.group in wanted]

    def tickers(self, indices: Iterable[str] | None = None) -> list[str]:
        """Unique Yahoo symbols of ``indices`` (default: every core index)."""
        chosen = self.indices(("core",)) if indices is None else list(indices)
        return list(dict.fromkeys(t for i in chosen for t in self.get(i).tickers))

    def member_info(self) -> dict[str, Member]:
        """Ticker -> member (first index wins), for names, sectors and calendars."""
        out: dict[str, Member] = {}
        for comp in self._items.values():
            for member in comp.members:
                out.setdefault(member.ticker, member)
        return out

    def replace(self, composition: Composition) -> None:
        self._items[composition.index] = composition
