"""Instrument referential and watchlists (YAML).

The referential is the single place where market conventions live: asset class,
quotation type, unit, change convention (% vs bp), native ticker per provider, unit
scaling, proxies and derived instruments (spreads, slopes). Everything downstream
(performance engine, UI, exports) works on canonical ids only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from market_monitor.config import KNOWN_PROVIDERS
from market_monitor.exceptions import ConfigError
from market_monitor.rolls import RULES as ROLL_RULES

_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class ReferentialError(ConfigError):
    """Invalid instrument referential or watchlist file."""


class AssetClass(StrEnum):
    """Asset classes, in display order."""

    EQUITY = "equity"
    RATES = "rates"
    CREDIT = "credit"
    FX = "fx"
    COMMODITIES = "commodities"
    VOLATILITY = "volatility"
    CRYPTO = "crypto"


class QuoteType(StrEnum):
    """How the level is quoted - drives the default change convention."""

    PRICE = "price"    # level in price units
    YIELD = "yield"    # level in %
    SPREAD = "spread"  # level in bp
    VOL = "vol"        # level in vol points


class ChangeUnit(StrEnum):
    """Convention used to express variations."""

    PCT = "pct"  # relative change, in %
    BP = "bp"    # absolute change, in basis points
    ABS = "abs"  # absolute change, in quote units (vol points...)


DEFAULT_CHANGE = {
    QuoteType.PRICE: ChangeUnit.PCT,
    QuoteType.YIELD: ChangeUnit.BP,
    QuoteType.SPREAD: ChangeUnit.BP,
    QuoteType.VOL: ChangeUnit.ABS,
}
#: multiply a level difference by this factor to get basis points
BP_FACTOR = {QuoteType.YIELD: 100.0, QuoteType.SPREAD: 1.0}
#: plausibility bounds per quote type (catches a wrong ``scale``: ^TNX at 41 instead of 4.1)
SANITY_BOUNDS = {
    QuoteType.PRICE: (0.0, math.inf),
    QuoteType.YIELD: (-5.0, 30.0),
    QuoteType.SPREAD: (-1_000.0, 5_000.0),
    QuoteType.VOL: (0.0, 300.0),
}


@dataclass(frozen=True)
class TickerSpec:
    """Native ticker of one provider, with scaling to the referential unit.

    ``proxy`` describes a source that is not the instrument itself (ETF, future...); for a
    fallback chain ``a|b``, ``alt_proxies`` gives it per alternative, so only the
    alternatives that are proxies are flagged.
    """

    ticker: str
    scale: float = 1.0
    proxy: str | None = None
    alt_proxies: Mapping[str, str] = field(default_factory=dict)

    def proxy_for(self, alternative: str | None) -> str | None:
        """Proxy description of the alternative that served (``None`` if it is the instrument)."""
        if self.proxy:
            return self.proxy
        return self.alt_proxies.get(alternative or "")


@dataclass(frozen=True)
class DerivedSpec:
    r"""Linear combination: :math:`L = m \sum_i w_i L_i` on common dates."""

    legs: tuple[tuple[str, float], ...]
    multiplier: float = 1.0


@dataclass(frozen=True)
class Instrument:
    """One line of the Market Monitor."""

    id: str
    name: str
    asset_class: AssetClass
    group: str
    quote: QuoteType
    unit: str
    change: ChangeUnit
    tickers: Mapping[str, TickerSpec]
    derived: DerivedSpec | None = None
    roll: str | None = None  # contract-roll rule of a generic front-month future

    @property
    def is_derived(self) -> bool:
        return self.derived is not None

    @property
    def bp_factor(self) -> float | None:
        return BP_FACTOR.get(self.quote)

    @property
    def sanity_bounds(self) -> tuple[float, float]:
        return SANITY_BOUNDS[self.quote]


@dataclass(frozen=True)
class Watchlist:
    name: str
    description: str
    instruments: tuple[str, ...]


class Referential:
    """Validated catalogue of instruments and watchlists."""

    def __init__(self, instruments: Iterable[Instrument], watchlists: Iterable[Watchlist] = ()) -> None:
        self._instruments: dict[str, Instrument] = {}
        for inst in instruments:
            if inst.id in self._instruments:
                raise ReferentialError(f"duplicate instrument id {inst.id!r}")
            self._instruments[inst.id] = inst
        self._check_derived()
        self._watchlists = {w.name: w for w in watchlists}
        for wl in self._watchlists.values():
            self.resolve(wl.instruments, context=f"watchlist {wl.name!r}")

    # ------------------------------------------------------------------ access
    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._instruments)

    @property
    def watchlist_names(self) -> tuple[str, ...]:
        return tuple(self._watchlists)

    def __contains__(self, instrument_id: object) -> bool:
        return instrument_id in self._instruments

    def __len__(self) -> int:
        return len(self._instruments)

    def get(self, instrument_id: str) -> Instrument:
        try:
            return self._instruments[instrument_id]
        except KeyError:
            raise ReferentialError(f"unknown instrument {instrument_id!r}") from None

    def watchlist(self, name: str) -> Watchlist:
        try:
            return self._watchlists[name]
        except KeyError:
            raise ReferentialError(
                f"unknown watchlist {name!r} (available: {list(self._watchlists)})"
            ) from None

    # --------------------------------------------------------------- selectors
    def resolve(self, entries: Iterable[str], *, context: str = "selection") -> tuple[str, ...]:
        """Expand ``*`` / ``class:x`` / ``group:x`` / ids into ordered unique ids."""
        out: list[str] = []
        for entry in entries:
            matched = self._expand(entry)
            if not matched:
                raise ReferentialError(f"{context}: {entry!r} matches no instrument")
            out.extend(matched)
        return tuple(dict.fromkeys(out))

    def _expand(self, entry: str) -> list[str]:
        if entry == "*":
            return list(self._instruments)
        kind, sep, value = entry.partition(":")
        if sep and kind == "class":
            return [i for i, inst in self._instruments.items() if inst.asset_class == value]
        if sep and kind == "group":
            return [i for i, inst in self._instruments.items() if inst.group == value]
        return [entry] if entry in self._instruments else []

    def raw_dependencies(self, ids: Iterable[str]) -> tuple[str, ...]:
        """Non-derived ids needed to compute ``ids`` (legs of derived ones included)."""
        out: list[str] = []
        for instrument_id in ids:
            inst = self.get(instrument_id)
            if inst.derived:
                out.extend(leg for leg, _ in inst.derived.legs)
            else:
                out.append(inst.id)
        return tuple(dict.fromkeys(out))

    def symbol_map(self, ids: Iterable[str]) -> dict[str, dict[str, str]]:
        """``{id: {provider: native ticker}}`` for the data service (raw ids only)."""
        return {
            i: {p: spec.ticker for p, spec in self.get(i).tickers.items()}
            for i in self.raw_dependencies(ids)
        }

    # -------------------------------------------------------------- validation
    def _check_derived(self) -> None:
        for inst in self._instruments.values():
            if not inst.derived:
                continue
            quotes = set()
            for leg, _ in inst.derived.legs:
                if leg not in self._instruments:
                    raise ReferentialError(f"{inst.id}: unknown leg {leg!r}")
                if self._instruments[leg].is_derived:
                    raise ReferentialError(f"{inst.id}: leg {leg!r} is itself derived")
                quotes.add(self._instruments[leg].quote)
            if len(quotes) > 1:
                raise ReferentialError(f"{inst.id}: legs mix quote types {sorted(quotes)}")


# ====================================================================== loading
def load_referential(instruments_path: str | Path, watchlists_path: str | Path | None = None) -> Referential:
    """Load and validate the YAML referential (and optional watchlists)."""
    raw_instruments = _read_yaml(Path(instruments_path))
    raw_watchlists = _read_yaml(Path(watchlists_path)) if watchlists_path else {}
    return referential_from_dicts(raw_instruments, raw_watchlists)


def referential_from_dicts(
    raw_instruments: Mapping[str, Any], raw_watchlists: Mapping[str, Any]
) -> Referential:
    """Pure builder; collects every error before raising a single message."""
    defaults = _mapping(raw_instruments.get("defaults"), "defaults")
    entries = _mapping(raw_instruments.get("instruments"), "instruments")
    if not entries:
        raise ReferentialError("no instrument defined")
    instruments: list[Instrument] = []
    errors: list[str] = []
    for instrument_id, raw in entries.items():
        try:
            instruments.append(_parse_instrument(str(instrument_id), raw, defaults))
        except ReferentialError as exc:
            errors.append(str(exc))
    if errors:
        raise ReferentialError("invalid referential:\n  - " + "\n  - ".join(errors))
    watchlists = [
        _parse_watchlist(str(name), raw)
        for name, raw in _mapping(raw_watchlists.get("watchlists"), "watchlists").items()
    ]
    return Referential(instruments, watchlists)


def _read_yaml(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise ReferentialError(f"file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ReferentialError(f"invalid YAML in {path}: {exc}") from exc
    return _mapping(data, str(path))


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReferentialError(f"{where}: expected a mapping")
    return value


def _enum(enum_cls: type[StrEnum], value: Any, where: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        allowed = [e.value for e in enum_cls]
        raise ReferentialError(f"{where}: {value!r} not in {allowed}") from None


def _parse_instrument(instrument_id: str, raw: Any, defaults: Mapping[str, Any]) -> Instrument:
    where = f"instruments.{instrument_id}"
    if not instrument_id or not set(instrument_id) <= _ID_CHARS:
        raise ReferentialError(f"{where}: ids must be UPPER_CASE alphanumerics")
    raw = _mapping(raw, where)
    asset_class = _enum(AssetClass, raw.get("class"), f"{where}.class")
    merged = {**_mapping(defaults.get(asset_class.value), f"defaults.{asset_class}"), **raw}
    quote = _enum(QuoteType, merged.get("quote"), f"{where}.quote")
    change = _enum(ChangeUnit, merged.get("change", DEFAULT_CHANGE[quote]), f"{where}.change")
    if change is ChangeUnit.BP and quote not in BP_FACTOR:
        raise ReferentialError(f"{where}: bp changes require a yield or spread quote")
    name = merged.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ReferentialError(f"{where}.name is required")
    tickers = {p: _parse_ticker(spec, f"{where}.tickers.{p}")
               for p, spec in _mapping(merged.get("tickers"), f"{where}.tickers").items()}
    unknown = set(tickers) - set(KNOWN_PROVIDERS)
    if unknown:
        raise ReferentialError(f"{where}.tickers: unknown providers {sorted(unknown)}")
    derived = _parse_derived(merged.get("derived"), f"{where}.derived")
    if bool(tickers) == bool(derived):
        raise ReferentialError(f"{where}: define either tickers or derived (exactly one)")
    roll = merged.get("roll")
    if roll is not None and roll not in ROLL_RULES:
        raise ReferentialError(f"{where}.roll: {roll!r} not in {sorted(ROLL_RULES)}")
    return Instrument(
        id=instrument_id, name=name.strip(), asset_class=asset_class,
        group=str(merged.get("group") or asset_class.value), quote=quote,
        unit=str(merged.get("unit", "")), change=change, tickers=tickers, derived=derived,
        roll=roll,
    )


def _parse_ticker(spec: Any, where: str) -> TickerSpec:
    if isinstance(spec, str):
        spec = {"ticker": spec}
    spec = _mapping(spec, where)
    ticker = spec.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip() or ticker != ticker.strip():
        raise ReferentialError(f"{where}: invalid ticker {ticker!r}")
    scale = spec.get("scale", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale == 0:
        raise ReferentialError(f"{where}: scale must be a non-zero number")
    proxy = spec.get("proxy")
    if isinstance(proxy, Mapping):
        alternatives = [a.strip() for a in ticker.split("|")]
        unknown = [str(k) for k in proxy if str(k) not in alternatives]
        if unknown:
            raise ReferentialError(f"{where}.proxy: {unknown} are not alternatives of {ticker!r}")
        return TickerSpec(ticker, float(scale), None, {str(k): str(v) for k, v in proxy.items()})
    return TickerSpec(ticker, float(scale), str(proxy) if proxy else None)


def _parse_derived(raw: Any, where: str) -> DerivedSpec | None:
    if raw is None:
        return None
    raw = _mapping(raw, where)
    legs = _mapping(raw.get("legs"), f"{where}.legs")
    if len(legs) < 2:
        raise ReferentialError(f"{where}: at least two legs are required")
    parsed: list[tuple[str, float]] = []
    for leg, weight in legs.items():
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight == 0:
            raise ReferentialError(f"{where}.legs.{leg}: weight must be a non-zero number")
        parsed.append((str(leg), float(weight)))
    multiplier = raw.get("multiplier", 1.0)
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or multiplier == 0:
        raise ReferentialError(f"{where}.multiplier must be a non-zero number")
    return DerivedSpec(tuple(parsed), float(multiplier))


def _parse_watchlist(name: str, raw: Any) -> Watchlist:
    raw = _mapping(raw, f"watchlists.{name}")
    entries = raw.get("instruments")
    if not isinstance(entries, list) or not entries or not all(isinstance(e, str) for e in entries):
        raise ReferentialError(f"watchlists.{name}.instruments must be a non-empty list of strings")
    return Watchlist(name, str(raw.get("description", "")), tuple(entries))
