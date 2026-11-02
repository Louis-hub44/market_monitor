r"""Scenario P&L of a book of swaps and bonds.

* swaps / OIS: full revaluation on the scenario :class:`CurveSet` (past fixings kept);
* bonds: priced on a reference curve (``"ESTR"``, ``"EURIBOR6M"`` or a government curve
  name) shifted by the bond's Z-spread, calibrated once on the base market price and
  **held constant** in the scenarios (pure rates move, no spread move):

.. math::
    \text{P\&L}_{bond} = \frac{N}{100}\left[P^{scen}_{dirty}(z_0) - P^{base}_{dirty}(z_0)\right]

Government curves (NSS fits) are not bootstrapped from quotes: they always receive
the zero-rate version of the shock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

from rates_analytics.bonds.bond import FixedRateBond
from rates_analytics.bonds.spreads import z_spread
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.market_curves import CurveSet
from rates_analytics.curves.shifted import SpreadedCurve
from rates_analytics.curves.yield_curve import YieldCurve
from rates_analytics.instruments.fixings import Fixings
from rates_analytics.instruments.irs import VanillaSwap
from rates_analytics.instruments.ois import OvernightIndexedSwap
from rates_analytics.scenarios.curve_scenarios import CurveScenario, apply_scenario, shift_curve
from rates_analytics.swaps.pricer import swap_npv

Instrument = VanillaSwap | OvernightIndexedSwap | FixedRateBond


@dataclass
class Position:
    """A book line.

    Attributes:
        name: label.
        instrument: swap, OIS or bond.
        nominal: bond face amount (swaps carry their own notional; ignored for them).
        clean_price: bond market price used to calibrate the Z-spread (None: z = 0).
        curve: bond reference curve: ``"ESTR"``, ``"EURIBOR6M"`` or a government curve name.
        fixings: past fixings for seasoned swaps.
    """

    name: str
    instrument: Instrument
    nominal: float = 1_000_000.0
    clean_price: float | None = None
    curve: str = "ESTR"
    fixings: Fixings | Mapping | None = None
    _z: float | None = field(default=None, init=False, repr=False)


@dataclass
class CurveEnvironment:
    """Swap curve set + government curves (one scenario state)."""

    curves: CurveSet
    gov_curves: dict[str, YieldCurve] = field(default_factory=dict)

    def curve(self, name: str) -> YieldCurve:
        if name in self.gov_curves:
            return self.gov_curves[name]
        if name == self.curves.overnight_index:
            return self.curves.discount_curve
        return self.curves.forward_curve(name)


def _bond_value(pos: Position, env: CurveEnvironment) -> float:
    b = pos.instrument
    assert isinstance(b, FixedRateBond)
    settle = b.settlement_date(env.curves.as_of)
    ref = env.curve(pos.curve)
    if pos._z is None:
        raise CurveError("Bond Z-spread not calibrated (value the base environment first)")
    priced_on = SpreadedCurve(ref, pos._z) if pos._z else ref
    return b.dirty_price_from_curve(priced_on, settle) * pos.nominal / 100.0


def position_value(pos: Position, env: CurveEnvironment) -> float:
    """Present value of a position in an environment (bond: dirty value of the nominal)."""
    if isinstance(pos.instrument, FixedRateBond):
        return _bond_value(pos, env)
    return swap_npv(pos.instrument, env.curves, pos.fixings)


def calibrate(positions: Sequence[Position], base: CurveEnvironment) -> None:
    """Calibrate bond Z-spreads on the base environment (in place)."""
    for p in positions:
        if isinstance(p.instrument, FixedRateBond):
            if p.clean_price is None:
                p._z = 0.0
            else:
                b = p.instrument
                p._z = z_spread(b, p.clean_price, b.settlement_date(base.curves.as_of), base.curve(p.curve))


def scenario_environment(base: CurveEnvironment, scenario: CurveScenario, mode: str = "quotes",
                         targets: Sequence[str] | None = None, shock_gov: bool = True) -> CurveEnvironment:
    """Apply a scenario to the swap curves (``mode``) and, in zero terms, to the government curves."""
    curves = apply_scenario(base.curves, scenario, mode, targets)
    gov = {k: (shift_curve(v, scenario) if shock_gov else v) for k, v in base.gov_curves.items()}
    return CurveEnvironment(curves, gov)


def scenario_pnl(positions: Sequence[Position], base: CurveEnvironment, scenarios: Sequence[CurveScenario],
                 mode: str = "quotes", targets: Sequence[str] | None = None) -> pd.DataFrame:
    """P&L matrix: one row per position (+ TOTAL), one column per scenario (currency units)."""
    if not positions:
        raise CurveError("No position")
    calibrate(positions, base)
    base_values = {p.name: position_value(p, base) for p in positions}
    cols = {}
    for sc in scenarios:
        env = scenario_environment(base, sc, mode, targets)
        cols[sc.name] = {p.name: position_value(p, env) - base_values[p.name] for p in positions}
    out = pd.DataFrame(cols)
    out.insert(0, "base_value", pd.Series(base_values))
    out.loc["TOTAL"] = out.sum()
    return out


def par_rate_table(base: CurveSet, scenario_curves: Mapping[str, CurveSet],
                   tenors: Sequence[str] = ("2Y", "5Y", "10Y", "30Y"), index: str = "EURIBOR6M") -> pd.DataFrame:
    """Par swap rates (EURIBOR 6M) per tenor: base level and change (bp) in each scenario."""
    def par(cs: CurveSet, t: str) -> float:
        s = VanillaSwap.from_tenor(cs.as_of, t)
        return s.fair_rate(cs.discount_curve, cs.forward_curve(index))

    rows = {}
    for t in tenors:
        b = par(base, t)
        row = {"base": b}
        row.update({name: (par(cs, t) - b) * 1e4 for name, cs in scenario_curves.items()})
        rows[t] = row
    return pd.DataFrame(rows).T

