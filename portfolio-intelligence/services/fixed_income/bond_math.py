"""Mathématiques obligataires — cœur de calcul, sans dépendance externe.

Toutes les fonctions sont pures et testées contre des formules fermées
connues (obligation zéro-coupon, obligation au pair), cf. `tests/`.

Conventions retenues :
* les taux et coupons sont exprimés en décimal (0.0325 = 3,25 %) ;
* le temps est exprimé en années depuis le règlement, les flux étant placés
  aux dates de coupon en remontant depuis la maturité ;
* l'actualisation utilise la composition à la fréquence du coupon
  (semestrielle par défaut), convention de place pour les obligations
  souveraines et corporate.

Limite assumée : ce module traite les obligations à taux fixe sans option.
Callables, FRN et titres indexés relèvent d'un moteur de pricing dédié
(QuantLib) — voir ARCHITECTURE.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

DAYS_PER_YEAR = 365.25


# --------------------------------------------------------------------- #
# Échéancier
# --------------------------------------------------------------------- #
def years_to_maturity(maturity: date, settlement: date | None = None) -> float:
    settlement = settlement or date.today()
    return max((maturity - settlement).days / DAYS_PER_YEAR, 0.0)


def coupon_times(ttm: float, frequency: int = 2) -> list[float]:
    """Dates de flux (en années) en remontant depuis la maturité."""
    if ttm <= 0:
        return []
    freq = max(1, int(frequency))
    n = max(1, math.ceil(ttm * freq - 1e-9))
    times = [ttm - k / freq for k in range(n)]
    return sorted(t for t in times if t > 1e-9)


def cashflows(face: float, coupon_rate: float, ttm: float, frequency: int = 2) -> list[tuple[float, float]]:
    """Renvoie [(t, flux)] : coupons périodiques + remboursement du nominal."""
    freq = max(1, int(frequency))
    times = coupon_times(ttm, freq)
    if not times:
        return [(0.0, face)]
    cpn = face * coupon_rate / freq
    flows = [(t, cpn) for t in times]
    flows[-1] = (flows[-1][0], flows[-1][1] + face)
    return flows


# --------------------------------------------------------------------- #
# Prix et rendement
# --------------------------------------------------------------------- #
def price_from_yield(face: float, coupon_rate: float, ttm: float, ytm: float, frequency: int = 2) -> float:
    """Prix pied de coupon approché (valeur actuelle des flux restants)."""
    freq = max(1, int(frequency))
    factor = 1.0 + ytm / freq
    if factor <= 0:
        raise ValueError("Rendement incompatible avec la fréquence de coupon")
    return sum(cf / factor ** (freq * t) for t, cf in cashflows(face, coupon_rate, ttm, freq))


def yield_from_price(price: float, face: float, coupon_rate: float, ttm: float,
                     frequency: int = 2, tol: float = 1e-10) -> float:
    """YTM par bissection — robuste, sans dépendance à SciPy.

    Bornes larges (-90 % à +200 %) pour couvrir les cas de taux négatifs
    rencontrés sur les souverains européens.
    """
    if price <= 0:
        raise ValueError("Le prix doit être strictement positif")
    lo, hi = -0.9 * frequency, 2.0
    f = lambda y: price_from_yield(face, coupon_rate, ttm, y, frequency) - price
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        raise ValueError("Aucun rendement dans l'intervalle de recherche")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = f(mid)
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------- #
# Sensibilités analytiques
# --------------------------------------------------------------------- #
@dataclass(frozen=True)
class BondRisk:
    price: float
    macaulay_duration: float
    modified_duration: float
    convexity: float
    dv01: float          # variation de prix pour 1 bp, pour 100 de nominal

    def approx_return(self, dy: float) -> float:
        """Rendement approché : -D·Δy + ½·C·Δy² (développement d'ordre 2)."""
        return -self.modified_duration * dy + 0.5 * self.convexity * dy ** 2


def bond_risk(face: float, coupon_rate: float, ttm: float, ytm: float, frequency: int = 2) -> BondRisk:
    freq = max(1, int(frequency))
    factor = 1.0 + ytm / freq
    flows = cashflows(face, coupon_rate, ttm, freq)

    pv = [cf / factor ** (freq * t) for t, cf in flows]
    price = sum(pv)
    if price <= 0:
        raise ValueError("Prix calculé nul ou négatif")

    macaulay = sum(t * v for (t, _), v in zip(flows, pv)) / price
    modified = macaulay / factor
    convexity = sum(
        t * (t + 1.0 / freq) * cf / factor ** (freq * t + 2) for t, cf in flows
    ) / price
    dv01 = modified * price * 1e-4
    return BondRisk(price=price, macaulay_duration=macaulay, modified_duration=modified,
                    convexity=convexity, dv01=dv01)


# --------------------------------------------------------------------- #
# Courbe zéro-coupon et sensibilités par segment
# --------------------------------------------------------------------- #
@dataclass
class ZeroCurve:
    """Courbe zéro-coupon interpolée linéairement, extrapolation plate."""

    nodes: dict[float, float]      # maturité en années -> taux zéro en décimal
    label: str = "curve"

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValueError("Courbe vide")
        self._t = sorted(self.nodes)
        self._r = [self.nodes[t] for t in self._t]

    def rate(self, t: float) -> float:
        if t <= self._t[0]:
            return self._r[0]
        if t >= self._t[-1]:
            return self._r[-1]
        for i in range(1, len(self._t)):
            if t <= self._t[i]:
                t0, t1 = self._t[i - 1], self._t[i]
                r0, r1 = self._r[i - 1], self._r[i]
                w = (t - t0) / (t1 - t0)
                return r0 + w * (r1 - r0)
        return self._r[-1]

    def discount(self, t: float, spread: float = 0.0) -> float:
        return 1.0 / (1.0 + self.rate(t) + spread) ** t

    def shifted(self, bumps: dict[float, float]) -> "ZeroCurve":
        """Nouvelle courbe décalée ; les bumps sont exprimés en décimal."""
        return ZeroCurve({t: r + bumps.get(t, 0.0) for t, r in self.nodes.items()}, self.label)

    def parallel(self, dy: float) -> "ZeroCurve":
        return ZeroCurve({t: r + dy for t, r in self.nodes.items()}, self.label)

    def key_rate_bumped(self, key_t: float, dy: float) -> "ZeroCurve":
        """Bump triangulaire centré sur `key_t`, nul aux nœuds clés voisins.

        C'est la définition standard des key rate durations (Ho, 1992) :
        la somme des KRD reconstitue la duration effective.
        """
        idx = self._t.index(key_t) if key_t in self._t else None
        if idx is None:
            raise ValueError(f"{key_t} n'est pas un nœud de la courbe")
        prev_t = self._t[idx - 1] if idx > 0 else None
        next_t = self._t[idx + 1] if idx < len(self._t) - 1 else None

        bumps: dict[float, float] = {}
        for t in self._t:
            if t == key_t:
                bumps[t] = dy
            elif prev_t is not None and prev_t <= t < key_t:
                bumps[t] = dy * (t - prev_t) / (key_t - prev_t)
            elif next_t is not None and key_t < t <= next_t:
                bumps[t] = dy * (next_t - t) / (next_t - key_t)
            else:
                bumps[t] = 0.0
        return self.shifted(bumps)


def price_from_curve(flows: list[tuple[float, float]], curve: ZeroCurve, spread: float = 0.0) -> float:
    return sum(cf * curve.discount(t, spread) for t, cf in flows)


def effective_duration(flows: list[tuple[float, float]], curve: ZeroCurve,
                       spread: float = 0.0, bp: float = 1.0) -> float:
    """Duration effective par repricing symétrique (±`bp` points de base)."""
    dy = bp * 1e-4
    p0 = price_from_curve(flows, curve, spread)
    if p0 <= 0:
        return 0.0
    p_up = price_from_curve(flows, curve.parallel(dy), spread)
    p_dn = price_from_curve(flows, curve.parallel(-dy), spread)
    return (p_dn - p_up) / (2 * p0 * dy)


def effective_convexity(flows: list[tuple[float, float]], curve: ZeroCurve,
                        spread: float = 0.0, bp: float = 1.0) -> float:
    dy = bp * 1e-4
    p0 = price_from_curve(flows, curve, spread)
    if p0 <= 0:
        return 0.0
    p_up = price_from_curve(flows, curve.parallel(dy), spread)
    p_dn = price_from_curve(flows, curve.parallel(-dy), spread)
    return (p_up + p_dn - 2 * p0) / (p0 * dy ** 2)


def key_rate_durations(flows: list[tuple[float, float]], curve: ZeroCurve,
                       spread: float = 0.0, bp: float = 1.0) -> dict[float, float]:
    """KRD par nœud de courbe. La somme ≈ duration effective."""
    dy = bp * 1e-4
    p0 = price_from_curve(flows, curve, spread)
    if p0 <= 0:
        return {t: 0.0 for t in curve.nodes}
    out: dict[float, float] = {}
    for key_t in sorted(curve.nodes):
        p_up = price_from_curve(flows, curve.key_rate_bumped(key_t, dy), spread)
        p_dn = price_from_curve(flows, curve.key_rate_bumped(key_t, -dy), spread)
        out[key_t] = (p_dn - p_up) / (2 * p0 * dy)
    return out


def spread_duration(flows: list[tuple[float, float]], curve: ZeroCurve,
                    spread: float = 0.0, bp: float = 1.0) -> float:
    """Sensibilité au spread de crédit, à courbe souveraine inchangée."""
    ds = bp * 1e-4
    p0 = price_from_curve(flows, curve, spread)
    if p0 <= 0:
        return 0.0
    p_up = price_from_curve(flows, curve, spread + ds)
    p_dn = price_from_curve(flows, curve, spread - ds)
    return (p_dn - p_up) / (2 * p0 * ds)
