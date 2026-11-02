r"""Sovereign yield curves: today against J-1, S-1 and M-1, slopes and spreads to the Bund.

Built on the performance engine (same closes, same references, same closing-price rule):
for every curve point (``curve: {country, tenor}`` in the referential) the last close and
the closes of the previous session (J-1), one week (S-1) and one calendar month (M-1)
before it. In bp:

* change of a point: :math:`100\,(y_0 - y_h)`;
* slope :math:`a\text{s}b\text{s}`: :math:`100\,(y_b - y_a)` (e.g. 2s10s), and its change;
* spread to the Bund of the same maturity: :math:`100\,(y - y_{\text{DE}})`, and its change.

A curve is only as good as its points: points of different dates or from several sources
are reported in ``notes`` (a slope mixing a Friday close and a Monday close is a fake move).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from market_monitor.analytics.history import source_label
from market_monitor.referential import BENCHMARK_COUNTRY, Referential

#: comparisons shown against today's curve: label -> performance horizon
COMPARISONS = {"J-1": "1d", "S-1": "1w", "M-1": "1m"}
#: default countries, display order
DEFAULT_COUNTRIES = ("DE", "FR", "IT", "ES", "US")
COUNTRY_LABELS = {"DE": "Allemagne (Bund)", "FR": "France (OAT)", "IT": "Italie (BTP)",
                  "ES": "Espagne (Bonos)", "US": "États-Unis (UST)", "GB": "Royaume-Uni (Gilt)",
                  "JP": "Japon (JGB)"}
SHORT_LABELS = {"DE": "Bund", "FR": "OAT", "IT": "BTP", "ES": "Bonos", "US": "UST", "GB": "Gilt",
                "JP": "JGB"}
SLOPES = ((2.0, 10.0), (5.0, 30.0), (2.0, 30.0))


def tenor_label(tenor: float) -> str:
    return f"{tenor:g} ans"


def slope_label(short: float, long: float) -> str:
    return f"{short:g}s{long:g}s"


def curve_ids(referential: Referential, countries: Iterable[str] | None = None) -> list[str]:
    """Curve points of ``countries`` (default: every country), by country then tenor."""
    wanted = list(countries) if countries is not None else None
    points = [(i, referential.get(i).curve) for i in referential.ids if referential.get(i).curve]
    order = {c: k for k, c in enumerate(wanted or DEFAULT_COUNTRIES)}
    chosen = [(i, c) for i, c in points if c is not None and (wanted is None or c.country in wanted)]
    chosen.sort(key=lambda p: (order.get(p[1].country, len(order)), p[1].country, p[1].tenor))
    return [i for i, _ in chosen]


@dataclass
class CurveSet:
    """Curve points, slopes and spreads to the Bund for one as-of date.

    ``points``: one row per curve point - ``country``, ``tenor``, ``name``, ``level`` (%),
    ``level_date``, ``source``, and per comparison ``h`` in :data:`COMPARISONS`: ``ref_<h>``
    (yield, %), ``ref_date_<h>`` and ``chg_<h>`` (bp).
    ``slopes``: ``country``, ``slope``, ``level`` (bp), ``chg_<h>`` (bp).
    ``spreads``: ``country``, ``tenor``, ``level`` (bp vs the Bund), ``chg_<h>`` (bp).
    """

    as_of: date
    points: pd.DataFrame
    slopes: pd.DataFrame
    spreads: pd.DataFrame
    notes: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    provisional: frozenset[str] = frozenset()

    @property
    def countries(self) -> list[str]:
        return list(dict.fromkeys(self.points["country"])) if not self.points.empty else []

    def curve(self, country: str, comparison: str | None = None) -> pd.Series:
        """Yields of ``country`` by tenor: today, or at ``comparison`` (``"J-1"``...)."""
        rows = self.points[self.points["country"] == country].sort_values("tenor")
        column = "level" if comparison is None else f"ref_{comparison}"
        return pd.Series(rows[column].to_numpy(dtype="float64"), index=rows["tenor"].to_numpy())


def build_curves(table: pd.DataFrame, referential: Referential, as_of: date,
                 provisional: frozenset[str] = frozenset(),
                 errors: dict[str, str] | None = None) -> CurveSet:
    """Curve set from a performance table restricted to curve points."""
    rows = []
    for instrument_id, r in table.iterrows():
        curve = referential.get(str(instrument_id)).curve
        if curve is None or pd.isna(r["level"]):
            continue
        row = {"id": instrument_id, "country": curve.country, "tenor": curve.tenor, "name": r["name"],
               "level": r["level"], "level_date": r["level_date"], "source": r.get("origin")}
        for label, horizon in COMPARISONS.items():
            row[f"ref_{label}"] = r[f"ref_{horizon}"]
            row[f"ref_date_{label}"] = r[f"ref_date_{horizon}"]
            row[f"chg_{label}"] = r[f"chg_{horizon}"]
        rows.append(row)
    columns = ["id", "country", "tenor", "name", "level", "level_date", "source"] + [
        f"{p}_{label}" for label in COMPARISONS for p in ("ref", "ref_date", "chg")]
    points = pd.DataFrame(rows, columns=columns).set_index("id")
    return CurveSet(as_of, points, _slopes(points), _spreads(points), _notes(points),
                    dict(errors or {}), provisional)


def _at(points: pd.DataFrame, country: str, tenor: float) -> pd.Series | None:
    match = points[(points["country"] == country) & (points["tenor"] == tenor)]
    return match.iloc[0] if not match.empty else None


def _difference(a: pd.Series, b: pd.Series) -> dict[str, float]:
    """``100 (a - b)`` today and its change against every comparison, bp."""
    out = {"level": 100.0 * (a["level"] - b["level"])}
    for label in COMPARISONS:
        before = 100.0 * (a[f"ref_{label}"] - b[f"ref_{label}"])
        out[f"chg_{label}"] = out["level"] - before
    return out


def _slopes(points: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for country in dict.fromkeys(points["country"]):
        for short, long in SLOPES:
            a, b = _at(points, country, long), _at(points, country, short)
            if a is not None and b is not None:
                rows.append({"country": country, "slope": slope_label(short, long),
                             **_difference(a, b)})
    return pd.DataFrame(rows, columns=["country", "slope", "level", *[f"chg_{c}" for c in COMPARISONS]])


def _spreads(points: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, point in points[points["country"] != BENCHMARK_COUNTRY].iterrows():
        bund = _at(points, BENCHMARK_COUNTRY, point["tenor"])
        if bund is not None:
            rows.append({"country": point["country"], "tenor": point["tenor"],
                         **_difference(point, bund)})
    return pd.DataFrame(rows, columns=["country", "tenor", "level", *[f"chg_{c}" for c in COMPARISONS]])


def _notes(points: pd.DataFrame) -> list[str]:
    """Points to check: a curve made of several dates or of several sources."""
    notes = []
    for country in dict.fromkeys(points["country"]):
        rows = points[points["country"] == country]
        name = SHORT_LABELS.get(country, country)
        dates = sorted({d for d in rows["level_date"] if pd.notna(d)})
        if len(dates) > 1:
            pairs = zip(rows["tenor"], rows["level_date"], strict=True)
            late = ", ".join(f"{tenor_label(t)} au {d:%d/%m}" for t, d in pairs if d != dates[-1])
            notes.append(f"{name} : points de dates différentes ({late}, autres au {dates[-1]:%d/%m})")
        sources = sorted({source_label(s).split(" ")[0] for s in rows["source"] if isinstance(s, str)})
        if len(sources) > 1:
            notes.append(f"{name} : sources mêlées ({', '.join(sources)}), pentes à lire avec prudence")
    return notes
