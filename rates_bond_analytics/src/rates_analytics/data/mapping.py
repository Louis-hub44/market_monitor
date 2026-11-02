"""Logical market-data identifiers and their per-provider mapping.

The engine only speaks *logical ids* (``EUR.ESTR.OIS.10Y``, ``EUR.ESTR.FIX``,
``USD.UST.CMT.10Y``...). ``config.yaml`` maps each id to a provider-specific spec::

    market_data:
      EUR.ESTR.OIS.{tenor}:
        tenors: [1W, 1M, 3M, 6M, 1Y, 2Y, 5Y, 10Y]
        ecb: {key: "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_{ecb_tenor}", scale: 0.01}

Template keys containing ``{tenor}`` expand over ``tenors``. Inside a spec the
placeholders ``{tenor}``, ``{ecb_tenor}``, ``{fmp_tenor}``, ``{years}`` (``10``) and
``{bbk_tenor}`` (``R10XX``, Bundesbank) are replaced with the provider's own tenor codes. ``scale`` converts the raw value
to the engine unit (decimal rates: 2.15 % -> 0.0215).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from rates_analytics.conventions.period import Period, TimeUnit
from rates_analytics.exceptions import ConfigurationError

def ecb_tenor_code(p: Period) -> str:
    """ECB yield-curve maturity code: ``3M``, ``1Y``, ``1Y6M``, ``30Y``."""
    months = p.total_months
    if months is None:
        raise ConfigurationError(f"No ECB tenor code for {p}")
    years, rem = divmod(months, 12)
    if years and rem:
        return f"{years}Y{rem}M"
    return f"{years}Y" if years else f"{rem}M"


def fmp_tenor_code(p: Period) -> str:
    """FMP treasury field name: ``month3``, ``year10``."""
    months = p.total_months
    if months is None:
        raise ConfigurationError(f"No FMP tenor code for {p}")
    return f"year{months // 12}" if months % 12 == 0 else f"month{months}"


@dataclass(frozen=True)
class MarketDataEntry:
    """Resolved mapping of one logical id."""

    id: str
    description: str
    specs: Mapping[str, Mapping[str, Any]]  # provider name -> spec

    def spec(self, provider: str) -> Mapping[str, Any] | None:
        return self.specs.get(provider)


class MarketDataMap:
    """Registry ``logical id -> provider specs`` built from ``config.yaml``."""

    _RESERVED = {"tenors", "description"}

    def __init__(self, raw: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._entries: dict[str, MarketDataEntry] = {}
        for key, block in (raw or {}).items():
            self._register(str(key), dict(block or {}))

    def _register(self, key: str, block: dict[str, Any]) -> None:
        description = str(block.get("description", ""))
        provider_blocks = {k: v for k, v in block.items() if k not in self._RESERVED}
        if "{tenor}" not in key:
            self._add(MarketDataEntry(key, description, {p: dict(s) for p, s in provider_blocks.items()}))
            return
        tenors = block.get("tenors")
        if not tenors:
            raise ConfigurationError(f"Template id {key!r} needs a 'tenors' list")
        for t in tenors:
            p = Period.parse(str(t))
            subs = {"tenor": str(t).upper()}
            specs: dict[str, dict[str, Any]] = {}
            for prov, spec in provider_blocks.items():
                specs[prov] = {k: self._format(v, p, subs, prov) for k, v in dict(spec).items()}
            entry_id = key.replace("{tenor}", subs["tenor"])
            self._add(MarketDataEntry(entry_id, description, specs))

    @staticmethod
    def _format(value: Any, p: Period, subs: dict[str, str], provider: str) -> Any:
        if not isinstance(value, str) or "{" not in value:
            return value
        codes = dict(subs)
        if "{ecb_tenor}" in value:
            codes["ecb_tenor"] = ecb_tenor_code(p)
        if "{fmp_tenor}" in value:
            codes["fmp_tenor"] = fmp_tenor_code(p)
        if "{years}" in value or "{bbk_tenor}" in value:
            months = p.total_months
            if months is None or months % 12:
                raise ConfigurationError(f"{{years}} / {{bbk_tenor}} need a whole number of years, got {p}")
            codes["years"] = str(months // 12)
            codes["bbk_tenor"] = f"R{months // 12:02d}XX"
        try:
            return value.format(**codes)
        except KeyError as exc:
            raise ConfigurationError(f"Unknown placeholder {exc} in {provider} spec {value!r}") from exc

    def _add(self, entry: MarketDataEntry) -> None:
        if entry.id in self._entries:
            raise ConfigurationError(f"Duplicate market data id {entry.id!r}")
        self._entries[entry.id] = entry

    def __contains__(self, item: str) -> bool:
        return item in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def ids(self, prefix: str = "") -> list[str]:
        """All ids starting with ``prefix``."""
        return sorted(i for i in self._entries if i.startswith(prefix))

    def entry(self, market_id: str) -> MarketDataEntry | None:
        return self._entries.get(market_id)

    def spec(self, market_id: str, provider: str) -> Mapping[str, Any] | None:
        """Provider spec for an id, or ``None`` if the provider does not carry it."""
        e = self._entries.get(market_id)
        return None if e is None else e.spec(provider)

    def ids_for_provider(self, provider: str) -> list[str]:
        return sorted(i for i, e in self._entries.items() if provider in e.specs)
