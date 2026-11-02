r"""Yield-curve scenarios.

A :class:`CurveScenario` is a shock :math:`\Delta(t)` (decimal) as a function of maturity
:math:`t` in years, piecewise linear between knots and flat outside. It can be applied

* to the **zero curves** (``mode="zero"``): :math:`P'(t) = P(t)\,e^{-\Delta(t)\,t}` on the
  selected curves (all by default, which keeps the €STR / EURIBOR basis unchanged);
* to the **market quotes** (``mode="quotes"``): every instrument quote is moved by
  :math:`\Delta(t_i)` (:math:`t_i` = years to its pillar) and the curve set is
  re-bootstrapped – the way a trader thinks of a par-curve move.

Library (sizes in bp):

=================  ===========================================================
``parallel(s)``    :math:`\Delta(t) = s`
``twist``          linear from ``short_bp`` at ``short_t`` to ``long_bp`` at ``long_t``
``steepener``      long end up relative to the short end (bear: long up, bull: short down)
``flattener``      the opposite
``butterfly``      wings ``wings_bp`` at ``short_t`` / ``long_t``, belly ``belly_bp`` at ``belly_t``
``custom``         any pillars, e.g. ``{"2Y": 10, "10Y": -5}``
``irrbb``          the six BCBS IRRBB (d368) shocks, EUR sizes 200 / 250 / 100 bp
=================  ===========================================================

BCBS shapes (:math:`x = 4` years decay):
:math:`S_{short}(t) = R_{short}e^{-t/x}`, :math:`S_{long}(t) = R_{long}(1 - e^{-t/x})`,
steepener :math:`= -0.65|S_{short}| + 0.9|S_{long}|`, flattener :math:`= 0.8|S_{short}| - 0.6|S_{long}|`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from rates_analytics.conventions.period import Period
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.market_curves import CurveSet
from rates_analytics.curves.shifted import PiecewiseLinearSpread, SpreadedCurve
from rates_analytics.curves.yield_curve import YieldCurve

BP = 1e-4
_DENSE_GRID = tuple(np.round(np.concatenate([np.arange(0, 2, 0.25), np.arange(2, 10, 0.5),
                                             np.arange(10, 61, 1.0)]), 6))


def _years(tenor: str | float | int) -> float:
    if isinstance(tenor, (int, float)):
        return float(tenor)
    p = Period.parse(tenor)
    return p.years


@dataclass(frozen=True)
class CurveScenario:
    """A maturity-dependent shock (decimal), piecewise linear between knots, flat outside."""

    name: str
    shock: PiecewiseLinearSpread
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __call__(self, t) -> np.ndarray:
        return self.shock(t)

    def bp(self, tenors: Sequence[str | float] = ("2Y", "5Y", "10Y", "30Y")) -> dict[str, float]:
        """Shock in bp at a few tenors (for display)."""
        return {str(x): float(self.shock(_years(x))) / BP for x in tenors}

    def __add__(self, other: CurveScenario) -> CurveScenario:
        return CurveScenario(f"{self.name} + {other.name}", self.shock + other.shock)

    def scaled(self, k: float, name: str | None = None) -> CurveScenario:
        return CurveScenario(name or f"{k:g} x {self.name}",
                             PiecewiseLinearSpread(self.shock.times, tuple(k * v for v in self.shock.values)))


# ------------------------------------------------------------------ library
def parallel(bp: float, name: str | None = None) -> CurveScenario:
    return CurveScenario(name or f"Parallel {bp:+g}bp", PiecewiseLinearSpread.constant(bp * BP),
                         f"All maturities {bp:+g}bp", ("parallel",))


def twist(short_bp: float, long_bp: float, short_t: str | float = "2Y", long_t: str | float = "10Y",
          name: str | None = None) -> CurveScenario:
    """Linear between (short_t, short_bp) and (long_t, long_bp), flat outside."""
    a, b = _years(short_t), _years(long_t)
    if not a < b:
        raise CurveError("twist needs short_t < long_t")
    label = name or f"Twist {short_t} {short_bp:+g} / {long_t} {long_bp:+g}"
    return CurveScenario(label, PiecewiseLinearSpread((a, b), (short_bp * BP, long_bp * BP)),
                         f"{short_t}: {short_bp:+g}bp, {long_t}: {long_bp:+g}bp", ("twist",))


def steepener(size_bp: float = 25.0, short_t: str | float = "2Y", long_t: str | float = "10Y",
              kind: str = "bear") -> CurveScenario:
    """``kind='bear'``: long end +size; ``'bull'``: short end -size; ``'pivot'``: -size/2 / +size/2."""
    s = abs(size_bp)
    short_bp, long_bp = {"bear": (0.0, s), "bull": (-s, 0.0), "pivot": (-s / 2, s / 2)}[kind]
    return twist(short_bp, long_bp, short_t, long_t, name=f"{kind.capitalize()} steepener {short_t}/{long_t} {s:g}bp")


def flattener(size_bp: float = 25.0, short_t: str | float = "2Y", long_t: str | float = "10Y",
              kind: str = "bear") -> CurveScenario:
    """``kind='bear'``: short end +size; ``'bull'``: long end -size; ``'pivot'``: +size/2 / -size/2."""
    s = abs(size_bp)
    short_bp, long_bp = {"bear": (s, 0.0), "bull": (0.0, -s), "pivot": (s / 2, -s / 2)}[kind]
    return twist(short_bp, long_bp, short_t, long_t, name=f"{kind.capitalize()} flattener {short_t}/{long_t} {s:g}bp")


def butterfly(belly_bp: float = 15.0, wings_bp: float = 0.0, short_t: str | float = "2Y",
              belly_t: str | float = "5Y", long_t: str | float = "10Y") -> CurveScenario:
    """Belly moves by ``belly_bp`` relative to the wings (positive = belly cheapens)."""
    a, m, b = _years(short_t), _years(belly_t), _years(long_t)
    if not a < m < b:
        raise CurveError("butterfly needs short_t < belly_t < long_t")
    w, c = wings_bp * BP, belly_bp * BP
    return CurveScenario(f"Butterfly {short_t}/{belly_t}/{long_t} belly {belly_bp:+g}bp",
                         PiecewiseLinearSpread((a, m, b), (w, c, w)),
                         f"wings {wings_bp:+g}bp, belly {belly_bp:+g}bp", ("butterfly",))


def custom(pillars: Mapping[str | float, float], name: str = "Custom") -> CurveScenario:
    """Shock per pillar in bp (``{"2Y": 10, "10Y": -5}``), linear in between, flat outside."""
    if not pillars:
        raise CurveError("custom scenario needs at least one pillar")
    pts = sorted((_years(k), float(v) * BP) for k, v in pillars.items())
    times = tuple(t for t, _ in pts)
    if len(set(times)) != len(times):
        raise CurveError("Duplicate pillars in custom scenario")
    return CurveScenario(name, PiecewiseLinearSpread(times, tuple(v for _, v in pts)),
                         ", ".join(f"{k}: {v:+g}bp" for k, v in pillars.items()), ("custom",))


def from_function(name: str, fn: Callable[[np.ndarray], np.ndarray], grid: Iterable[float] = _DENSE_GRID,
                  description: str = "") -> CurveScenario:
    """Sample an analytic shock ``fn(t)`` (decimal) on a dense grid."""
    g = np.asarray(list(grid), float)
    return CurveScenario(name, PiecewiseLinearSpread(tuple(g), tuple(np.asarray(fn(g), float))), description)


IRRBB_SIZES = {"EUR": (200.0, 250.0, 100.0), "USD": (200.0, 300.0, 150.0), "GBP": (250.0, 300.0, 150.0)}


def irrbb_scenarios(currency: str = "EUR", decay: float = 4.0) -> list[CurveScenario]:
    """The six BCBS IRRBB standardised shocks (parallel up/down, steepener, flattener, short up/down)."""
    try:
        r_par, r_short, r_long = (x * BP for x in IRRBB_SIZES[currency.upper()])
    except KeyError as exc:
        raise CurveError(f"No IRRBB shock sizes for {currency}") from exc

    def s_short(t):
        return r_short * np.exp(-t / decay)

    def s_long(t):
        return r_long * (1 - np.exp(-t / decay))

    out = [
        parallel(r_par / BP, "IRRBB parallel up"), parallel(-r_par / BP, "IRRBB parallel down"),
        from_function("IRRBB steepener", lambda t: -0.65 * s_short(t) + 0.9 * s_long(t)),
        from_function("IRRBB flattener", lambda t: 0.8 * s_short(t) - 0.6 * s_long(t)),
        from_function("IRRBB short up", s_short), from_function("IRRBB short down", lambda t: -s_short(t)),
    ]
    return [CurveScenario(s.name, s.shock, s.description or "BCBS d368", ("irrbb",)) for s in out]


def standard_scenarios() -> list[CurveScenario]:
    """A desk-style default set used by the UI."""
    return [parallel(100), parallel(-100), parallel(25), parallel(-25),
            steepener(25, kind="bear"), steepener(25, kind="bull"),
            flattener(25, kind="bear"), flattener(25, kind="bull"),
            butterfly(15), butterfly(-15)]


# ------------------------------------------------------------- application
def shift_curve(curve: YieldCurve, scenario: CurveScenario) -> SpreadedCurve:
    """Zero-rate shock of a single curve (sovereign NSS curves, any YieldCurve)."""
    return SpreadedCurve(curve, scenario.shock, name=f"{curve.name} [{scenario.name}]")


def _pillar_years(as_of: date, pillar: date) -> float:
    return (pillar - as_of).days / 365.0


def shocked_quotes(curves: CurveSet, scenario: CurveScenario, targets: Sequence[str] | None = None
                   ) -> dict[str, float]:
    """Market quotes moved by the scenario shock at each instrument's pillar maturity."""
    blocks = [(curves.overnight_index, curves.discount), *curves.forwards.items()]
    targets = list(targets) if targets is not None else [n for n, _ in blocks]
    quotes = dict(curves.quotes)
    for name, res in blocks:
        if name not in targets:
            continue
        for h in res.helpers:
            if h.market_id in quotes:
                quotes[h.market_id] += float(scenario.shock(_pillar_years(curves.as_of, h.pillar_date)))
    return quotes


def apply_scenario(curves: CurveSet, scenario: CurveScenario, mode: str = "quotes",
                   targets: Sequence[str] | None = None) -> CurveSet:
    """Scenario curve set.

    Args:
        mode: ``"quotes"`` (shock market quotes by pillar maturity and re-bootstrap) or
            ``"zero"`` (shift the zero curves directly).
        targets: curve names to shock (``"ESTR"``, ``"EURIBOR6M"``); default all. Shocking
            only ``EURIBOR6M`` is a basis scenario.
    """
    names = [curves.overnight_index, *curves.forwards]
    targets = list(targets) if targets is not None else names
    unknown = set(targets) - set(names)
    if unknown:
        raise CurveError(f"Unknown curves {sorted(unknown)}; available {names}")
    if mode == "zero":
        disc = curves.discount_curve
        new_disc = shift_curve(disc, scenario) if curves.overnight_index in targets else disc
        fwd = {k: (shift_curve(v.curve, scenario) if k in targets else v.curve) for k, v in curves.forwards.items()}
        return curves.with_curves(new_disc, fwd)
    if mode == "quotes":
        return curves.rebuild(shocked_quotes(curves, scenario, targets))
    raise CurveError(f"Unknown scenario mode {mode!r} (use 'quotes' or 'zero')")
