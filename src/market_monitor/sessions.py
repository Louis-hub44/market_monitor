"""Closing prices only: which of today's prints are still an open session.

A morning review must compare closes. Several free sources (CNBC, Yahoo) publish a
"today" bar as soon as the market opens, so before its close an instrument's print dated
today is a live quote, not a close. :class:`ClosingRule` gives each instrument the Paris
time after which today's session is over (by asset class, display group or instrument,
the most specific wins); a derived line (spread, slope) is open while one of its legs is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any

from market_monitor.referential import Instrument, Referential, ReferentialError

#: Paris time after which the day's session is closed (conservative: settle / close + margin)
DEFAULT_CLASS_CLOSES = {
    "equity": time(17, 45), "rates": time(18, 0), "credit": time(18, 0), "fx": time(23, 5),
    "commodities": time(20, 45), "volatility": time(22, 20), "crypto": time(23, 59),
}
DEFAULT_GROUP_CLOSES = {"États-Unis": time(23, 5), "Japon": time(8, 35)}
DEFAULT_INSTRUMENT_CLOSES = {"NKY": time(8, 35), "CSI300": time(9, 5), "MXEF": time(23, 30)}


@dataclass(frozen=True)
class ClosingRule:
    enabled: bool = True
    default: time = time(17, 45)
    classes: Mapping[str, time] = field(default_factory=lambda: dict(DEFAULT_CLASS_CLOSES))
    groups: Mapping[str, time] = field(default_factory=lambda: dict(DEFAULT_GROUP_CLOSES))
    instruments: Mapping[str, time] = field(default_factory=lambda: dict(DEFAULT_INSTRUMENT_CLOSES))

    def __hash__(self) -> int:  # mappings are not hashable: hash their frozen content
        return hash((self.enabled, self.default, *(tuple(sorted(m.items())) for m in
                                                   (self.classes, self.groups, self.instruments))))

    def close_time(self, inst: Instrument) -> time:
        """Paris time after which today's print of a raw instrument is a close."""
        if inst.id in self.instruments:
            return self.instruments[inst.id]
        if inst.group in self.groups:
            return self.groups[inst.group]
        return self.classes.get(inst.asset_class.value, self.default)

    def open_sessions(self, referential: Referential, ids: Iterable[str], now: datetime) -> set[str]:
        """Among ``ids``, those whose session of ``now``'s day is not closed yet (Paris time)."""
        if not self.enabled:
            return set()
        clock = now.time()

        def is_open(inst: Instrument) -> bool:
            if inst.derived:
                return any(is_open(referential.get(leg)) for leg, _ in inst.derived.legs)
            return clock < self.close_time(inst)

        return {i for i in dict.fromkeys(ids) if i in referential and is_open(referential.get(i))}


def closing_rule_from_dict(raw: Any) -> ClosingRule:
    """``closing_prices: true | false | {enabled, default, classes, groups, instruments}``."""
    if raw is None or raw is True:
        return ClosingRule()
    if raw is False:
        return ClosingRule(enabled=False)
    if not isinstance(raw, Mapping):
        raise ReferentialError("closing_prices must be true, false or a mapping")
    unknown = set(raw) - {"enabled", "default", "classes", "groups", "instruments"}
    if unknown:
        raise ReferentialError(f"closing_prices: unknown keys {sorted(unknown)}")
    base = ClosingRule()

    def times(key: str, defaults: Mapping[str, time]) -> dict[str, time]:
        value = raw.get(key) or {}
        if not isinstance(value, Mapping):
            raise ReferentialError(f"closing_prices.{key} must be a mapping")
        return {**defaults, **{str(k): _time(v, f"closing_prices.{key}.{k}") for k, v in value.items()}}

    return ClosingRule(
        enabled=bool(raw.get("enabled", True)),
        default=_time(raw["default"], "closing_prices.default") if "default" in raw else base.default,
        classes=times("classes", base.classes),
        groups=times("groups", base.groups),
        instruments=times("instruments", base.instruments),
    )


def _time(value: Any, where: str) -> time:
    if isinstance(value, int) and not isinstance(value, bool):  # YAML reads 17:45 as 1065 minutes
        return time(*divmod(value, 60)) if 0 <= value < 24 * 60 else _bad(where, value)
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError:
        return _bad(where, value)


def _bad(where: str, value: Any) -> time:
    raise ReferentialError(f"{where}: expected a time HH:MM, got {value!r}")
