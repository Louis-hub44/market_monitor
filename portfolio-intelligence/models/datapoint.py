"""Enveloppe de traçabilité des données.

Règle structurante de la plateforme : **aucune valeur numérique ne circule
sans son niveau de qualité et sa provenance**. Les couches de calcul
travaillent sur des `float`, mais tout ce qui entre depuis un provider et
tout ce qui est affiché passe par un `DataPoint`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Generic, Iterable, TypeVar

from models.enums import DataQuality

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DataPoint(Generic[T]):
    value: T | None
    quality: DataQuality
    source: str                       # "FMP", "YAHOO", "CACHE:FMP", "USER_FILE", "MODEL"
    as_of: date | datetime | None = None
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    note: str = ""                    # explication lisible : pourquoi ce niveau de qualité

    @property
    def is_usable(self) -> bool:
        return self.value is not None and self.quality.is_decision_grade

    def or_none(self) -> T | None:
        """Valeur si elle est exploitable, sinon None (jamais de valeur inventée)."""
        return self.value if self.is_usable else None

    def degraded(self, quality: DataQuality, note: str = "") -> "DataPoint[T]":
        return DataPoint(
            value=self.value,
            quality=quality,
            source=self.source,
            as_of=self.as_of,
            fetched_at=self.fetched_at,
            note=note or self.note,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "quality": self.quality.value,
            "source": self.source,
            "as_of": str(self.as_of) if self.as_of else None,
            "fetched_at": self.fetched_at.isoformat(),
            "note": self.note,
        }


def missing(source: str = "NONE", note: str = "Donnée indisponible") -> DataPoint[Any]:
    """Absence explicite de donnée — préférée à un zéro ou une valeur de repli."""
    return DataPoint(value=None, quality=DataQuality.UNVERIFIED, source=source, note=note)


def user_input(value: T, note: str = "Valeur fournie par le fichier portefeuille") -> DataPoint[T]:
    return DataPoint(value=value, quality=DataQuality.API_CONFIRMED, source="USER_FILE", note=note)


def reconcile(points: Iterable[DataPoint[float]], tolerance: float = 0.005) -> DataPoint[float]:
    """Arbitre plusieurs sources pour une même grandeur numérique.

    - deux sources indépendantes concordantes (écart relatif < `tolerance`)
      => VERIFIED,
    - sinon la meilleure source disponible est conservée, avec une note
      décrivant le désaccord.
    """
    usable = [p for p in points if p.value is not None]
    if not usable:
        return missing(note="Aucune source n'a renvoyé de valeur")

    usable.sort(key=lambda p: p.quality.rank)
    best = usable[0]
    if len(usable) == 1:
        return best

    others = [p for p in usable[1:] if p.source.split(":")[0] != best.source.split(":")[0]]
    for other in others:
        ref = abs(best.value) if best.value else 1.0
        if abs(best.value - other.value) / max(ref, 1e-12) <= tolerance:
            return DataPoint(
                value=best.value,
                quality=DataQuality.VERIFIED,
                source=f"{best.source}+{other.source}",
                as_of=best.as_of,
                note=f"Concordance {best.source}/{other.source} (écart < {tolerance:.2%})",
            )

    if others:
        spread = max(abs(best.value - o.value) for o in others)
        return DataPoint(
            value=best.value,
            quality=best.quality,
            source=best.source,
            as_of=best.as_of,
            note=f"Désaccord entre sources (écart max {spread:.4g}) — valeur retenue : {best.source}",
        )
    return best
