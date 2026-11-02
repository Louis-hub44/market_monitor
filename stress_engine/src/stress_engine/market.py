"""État de marché consommé par le mapping et la revalorisation.

Il est construit à partir d'un instantané de Rates & Bond Analytics (courbes souveraines NSS,
univers obligataire, prix, courbes €STR) : le moteur ne fait lui-même aucun appel réseau.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rates_analytics.bonds import FixedRateBond
from rates_analytics.curves import YieldCurve

from stress_engine.config import StressConfigError


@dataclass
class MarketState:
    """Courbes par pays (``DE``, ``FR``…), univers obligataire, prix clean, change.

    Attributes:
        curves: courbe de valorisation par pays.
        bonds / prices: univers des titres connus (paniers livrables des futures, transparence)
            et leurs prix clean en % du pair.
        fx: devise de base par unité de devise (``{"USD": 0.89}``).
        short_rate_curve: courbe monétaire (€STR) pour les fonds monétaires.
        sources: provenance de chaque bloc (pour le rapport).
    """

    as_of: date
    curves: dict[str, YieldCurve]
    bonds: dict[str, FixedRateBond] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    fx: dict[str, float] = field(default_factory=dict)
    short_rate_curve: YieldCurve | None = None
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing_curve_fallback: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot: Any, curve_names: Mapping[str, str],
                      missing_curve_fallback: Mapping[str, str] | None = None,
                      fx: Mapping[str, float] | None = None) -> MarketState:
        """Depuis ``rates_analytics.api.market_snapshot`` (``curve_names`` : pays -> courbe RBA)."""
        curves = {country: snapshot.sovereigns[name].curve for country, name in curve_names.items()
                  if name in snapshot.sovereigns}
        missing = [f"{c} ({n})" for c, n in curve_names.items() if n not in snapshot.sovereigns]
        warnings = list(getattr(snapshot, "warnings", []))
        if missing:
            warnings.append("courbes souveraines absentes de l'instantané : " + ", ".join(missing))
        short = None
        try:
            short = snapshot.curves.discount_curve
        except AttributeError:
            pass
        return cls(as_of=snapshot.as_of, curves=curves, bonds=dict(snapshot.bonds), prices=dict(snapshot.prices),
                   fx=dict(fx or {}), short_rate_curve=short, sources=dict(getattr(snapshot, "sources", {})),
                   warnings=warnings, missing_curve_fallback={k.upper(): v.upper()
                                                              for k, v in (missing_curve_fallback or {}).items()})

    def curve_for(self, country: str) -> tuple[YieldCurve, str]:
        """(courbe, pays de la courbe effectivement utilisée) — suit la chaîne de repli configurée."""
        seen: list[str] = []
        c = country.upper()
        while c not in self.curves:
            seen.append(c)
            nxt = self.missing_curve_fallback.get(c)
            if nxt is None or nxt in seen:
                raise StressConfigError(f"aucune courbe pour {country} (chaîne essayée : {' -> '.join(seen)})")
            c = nxt
        return self.curves[c], c

    def fx_to_base(self, currency: str, base: str = "EUR") -> float | None:
        if currency.upper() == base.upper():
            return 1.0
        return self.fx.get(currency.upper())
