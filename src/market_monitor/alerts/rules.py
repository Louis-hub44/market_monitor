"""Alert rules (``config/alerts.yaml``), validated against the referential.

Rule types, all evaluated on the performance table (no extra data request):

* ``zscore`` : ``|z|`` of the 1D / 1W change above ``min_abs_z`` (``critical_abs_z``
  escalates the severity), optionally one direction only;
* ``level``  : level above / below a threshold in the referential unit (yield in %,
  spread in bp, price); with ``cross: true`` only when the last session crossed it;
* ``change`` : change over 1D / 1W / MTD / YTD above / below / beyond (``abs_above``) a
  threshold in the instrument's convention (% for prices, bp for rates);
* ``stale``  : last print older than the staleness limit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from market_monitor.analytics.performance import ZSCORE_HORIZONS, Horizon
from market_monitor.referential import Referential, ReferentialError

CONDITIONS = ("above", "below", "abs_above")


class RuleKind(StrEnum):
    ZSCORE = "zscore"
    LEVEL = "level"
    CHANGE = "change"
    STALE = "stale"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class Direction(StrEnum):
    BOTH = "both"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class AlertRule:
    id: str
    name: str
    kind: RuleKind
    instruments: tuple[str, ...]
    severity: Severity = Severity.WARNING
    horizon: Horizon = Horizon.D1
    condition: str | None = None          # above / below / abs_above (level, change)
    threshold: float | None = None
    cross: bool = False                   # level: only when crossed over the last session
    min_abs_z: float | None = None        # zscore
    critical_abs_z: float | None = None   # zscore escalation
    direction: Direction = Direction.BOTH


def load_rules(path: str | Path, referential: Referential) -> tuple[AlertRule, ...]:
    file = Path(path)
    if not file.is_file():
        raise ReferentialError(f"alert rules not found: {file}")
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ReferentialError(f"invalid YAML in {file}: {exc}") from exc
    return rules_from_dict(raw, referential)


def rules_from_dict(raw: Mapping[str, Any], referential: Referential) -> tuple[AlertRule, ...]:
    """Parse every rule, collecting all errors before raising."""
    entries = raw.get("rules") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list):
        raise ReferentialError("alerts: 'rules' must be a list")
    rules: list[AlertRule] = []
    errors: list[str] = []
    for k, entry in enumerate(entries):
        try:
            rules.append(_parse_rule(entry, referential, k))
        except ReferentialError as exc:
            errors.append(str(exc))
    ids = [r.id for r in rules]
    errors += [f"duplicate rule id {i!r}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    if errors:
        raise ReferentialError("invalid alert rules:\n  - " + "\n  - ".join(errors))
    return tuple(rules)


def resolve_selector(entries: list[str], referential: Referential, where: str) -> tuple[str, ...]:
    """Referential selectors plus ``watchlist:<name>``."""
    expanded: list[str] = []
    for entry in entries:
        if entry.startswith("watchlist:"):
            expanded.extend(referential.watchlist(entry.removeprefix("watchlist:")).instruments)
        else:
            expanded.append(entry)
    return referential.resolve(expanded, context=where)


# ----------------------------------------------------------------- parsing
def _parse_rule(raw: Any, referential: Referential, index: int) -> AlertRule:
    if not isinstance(raw, Mapping) or not raw.get("id"):
        raise ReferentialError(f"rules[{index}]: a mapping with an 'id' is required")
    rule_id = str(raw["id"])
    where = f"rule {rule_id!r}"
    kind = _enum(RuleKind, raw.get("type"), f"{where}.type")
    instruments = _instruments(raw, referential, where)
    common = {
        "id": rule_id, "name": str(raw.get("name") or rule_id), "kind": kind,
        "instruments": instruments,
        "severity": _enum(Severity, raw.get("severity", "warning"), f"{where}.severity"),
    }
    if kind is RuleKind.ZSCORE:
        return _zscore_rule(raw, common, where)
    if kind is RuleKind.STALE:
        return AlertRule(**common)
    condition, threshold = _condition(raw, where)
    if kind is RuleKind.LEVEL:
        if condition == "abs_above":
            raise ReferentialError(f"{where}: level rules use 'above' or 'below'")
        _same(referential, instruments, "quote", where)
        return AlertRule(**common, condition=condition, threshold=threshold, cross=bool(raw.get("cross")))
    horizon = _enum(Horizon, raw.get("horizon", "1d"), f"{where}.horizon")
    _same(referential, instruments, "change", where)  # threshold unit must be unambiguous
    return AlertRule(**common, horizon=horizon, condition=condition, threshold=threshold)


def _zscore_rule(raw: Mapping[str, Any], common: dict[str, Any], where: str) -> AlertRule:
    horizon = _enum(Horizon, raw.get("horizon", "1d"), f"{where}.horizon")
    if horizon not in ZSCORE_HORIZONS:
        raise ReferentialError(f"{where}: z-scores exist for 1d and 1w only")
    min_z = _number(raw.get("min_abs_z"), f"{where}.min_abs_z", positive=True)
    critical = raw.get("critical_abs_z")
    critical_z = _number(critical, f"{where}.critical_abs_z", positive=True) if critical is not None else None
    if critical_z is not None and critical_z < min_z:
        raise ReferentialError(f"{where}: critical_abs_z must be >= min_abs_z")
    direction = _enum(Direction, raw.get("direction", "both"), f"{where}.direction")
    return AlertRule(**common, horizon=horizon, min_abs_z=min_z, critical_abs_z=critical_z,
                     direction=direction)


def _instruments(raw: Mapping[str, Any], referential: Referential, where: str) -> tuple[str, ...]:
    if "instrument" in raw:
        entries = [raw["instrument"]]
    else:
        entries = raw.get("instruments") or []
    if not isinstance(entries, list) or not entries or not all(isinstance(e, str) for e in entries):
        raise ReferentialError(f"{where}: 'instrument' or a non-empty 'instruments' list is required")
    return resolve_selector(entries, referential, where)


def _condition(raw: Mapping[str, Any], where: str) -> tuple[str, float]:
    present = [c for c in CONDITIONS if c in raw]
    if len(present) != 1:
        raise ReferentialError(f"{where}: exactly one of {CONDITIONS} is required")
    condition = present[0]
    return condition, _number(raw[condition], f"{where}.{condition}", positive=condition == "abs_above")


def _same(referential: Referential, ids: tuple[str, ...], attribute: str, where: str) -> None:
    values = {getattr(referential.get(i), attribute) for i in ids}
    if len(values) > 1:
        raise ReferentialError(f"{where}: instruments mix {attribute} {sorted(map(str, values))}; "
                               "split the rule so that the threshold has a single unit")


def _number(value: Any, where: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReferentialError(f"{where}: a number is required")
    if positive and value <= 0:
        raise ReferentialError(f"{where}: must be > 0")
    return float(value)


def _enum(enum_cls: type[StrEnum], value: Any, where: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        raise ReferentialError(f"{where}: {value!r} not in {[e.value for e in enum_cls]}") from None
