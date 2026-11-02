"""Generate the ILLUSTRATIVE sovereign bond universe of data/sample (not real bonds).

Identifiers are synthetic (``SMPL-DE-...``) so they can never be mistaken for real
ISINs. Prices come from a "true" Bund NSS curve plus smooth OAT / BTP spreads,
with 1.5 bp yield noise and one deliberate outlier per issuer.

Run from the project root:  python scripts/generate_sample_bonds.py
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from rates_analytics.bonds import FixedRateBond, convention_for_issuer
from rates_analytics.conventions.dateutils import add_months
from rates_analytics.curves import NSSParams, YieldCurve

OUT = Path(__file__).resolve().parents[1] / "data" / "sample"
AS_OFS = (date(2026, 10, 5), date(2026, 10, 6))
BUND_TRUE = NSSParams(beta0=0.0310, beta1=-0.0115, beta2=-0.0185, tau1=1.8, beta3=0.0060, tau2=10.0)
SPREADS = {"DE": (0.0, 0.0), "FR": (0.0010, 0.0070), "IT": (0.0015, 0.0090)}   # a + b (1 - e^{-t/5})


class TrueCurve(YieldCurve):
    def __init__(self, ref: date, issuer: str, shift: float = 0.0):
        super().__init__(ref, name=f"true-{issuer}")
        self.a, self.b = SPREADS[issuer]
        self.shift = shift

    def _zero(self, t):
        return BUND_TRUE.zero_rate(t) + self.a + self.b * (1 - np.exp(-t / 5)) + self.shift

    def _discount_t(self, t):
        return np.exp(-self._zero(t) * t)

    def _inst_forward_t(self, t):  # not needed for pricing
        return self._zero(t)


def coupon_for(issue: date, rng: random.Random) -> float:
    if issue.year <= 2021:
        return rng.choice([0.0, 0.0, 0.001, 0.0025, 0.005, 0.01])
    return rng.choice([0.017, 0.021, 0.022, 0.023, 0.024, 0.025, 0.026, 0.028, 0.029, 0.031, 0.034])


def universe(issuer: str, rng: random.Random) -> list[dict]:
    """Maturity grid typical of each issuer (Bund 15 Feb/Aug, OAT 25 May/Nov, BTP 1st of month)."""
    if issuer == "DE":
        mats = [date(2026, 12, 11)] + [date(y, m, 15) for y in range(2027, 2037) for m in (2, 8)] + \
               [date(y, 8, 15) for y in (2038, 2040, 2042, 2044, 2046, 2048, 2050, 2053, 2056)]
    elif issuer == "FR":
        mats = [date(2027, 2, 25)] + [date(y, m, 25) for y in range(2027, 2037) for m in (5, 11)] + \
               [date(y, 5, 25) for y in (2038, 2039, 2040, 2042, 2044, 2045, 2048, 2052, 2055)]
    else:
        mats = [date(2026, 12, 1)] + [date(y, m, 1) for y in range(2027, 2037) for m in (2, 4, 8, 10)] + \
               [date(y, 9, 1) for y in (2038, 2040, 2043, 2046, 2049, 2051, 2053, 2056)]
    rows = []
    for _k, mat in enumerate(mats):
        residual = (mat - AS_OFS[-1]).days / 365.25
        tenor = 2 if residual < 2 else 5 if residual < 5 else 10 if residual < 10 else 30
        issue = add_months(mat, -12 * tenor)
        issue = date(issue.year, issue.month, min(issue.day, 20)) if issue.day > 20 else issue
        issue = add_months(issue, -rng.choice([0, 1, 2, 3]))
        conv = convention_for_issuer(issuer)
        months = 12 // int(conv.frequency)
        first = mat
        while add_months(first, -months) > issue:
            first = add_months(first, -months)
        if (first - issue).days < 40 and first != mat:   # very short first period -> long first coupon
            first = add_months(first, months)
        rows.append({"isin": f"SMPL-{issuer}-{mat:%Y%m%d}", "issuer": issuer, "currency": "EUR",
                     "coupon": coupon_for(issue, rng), "frequency": int(conv.frequency), "day_count": "ACT/ACT ICMA",
                     "issue_date": issue, "accrual_start": issue, "first_coupon_date": first, "maturity": mat})
    return rows


def main() -> None:
    rng = random.Random(2026)
    static_rows, price_rows = [], []
    for issuer in ("DE", "FR", "IT"):
        rows = universe(issuer, rng)
        outlier = rows[len(rows) * 2 // 3]["isin"]
        for r in rows:
            conv = convention_for_issuer(issuer)
            r["name"] = f"{conv.name} {r['coupon'] * 100:.3f} {r['maturity']:%d/%m/%Y} (sample)"
            static_rows.append(r)
            bond = FixedRateBond(r["isin"], r["coupon"], r["maturity"], r["issue_date"], conv, r["first_coupon_date"])
            noise = rng.gauss(0, 0.00015) + (0.0020 if r["isin"] == outlier else 0.0)
            for d_i, as_of in enumerate(AS_OFS):
                settle = bond.settlement_date(as_of)
                if settle >= bond.maturity:
                    continue
                curve = TrueCurve(as_of, issuer, shift=0.0004 * (d_i - 1))
                y_fair = bond.yield_from_clean_price(bond.clean_price_from_curve(curve, settle), settle)
                mid = round(bond.clean_price_from_yield(y_fair + noise, settle), 3)
                half = 0.005 + 0.002 * bond.residual_years(as_of)
                price_rows.append({"date": as_of, "isin": r["isin"], "bid": round(mid - half, 3),
                                   "ask": round(mid + half, 3), "mid": mid,
                                   "yield_mid": round(bond.yield_from_clean_price(mid, settle), 7)})
    static = pd.DataFrame(static_rows)[["isin", "name", "issuer", "currency", "coupon", "frequency", "day_count",
                                        "issue_date", "accrual_start", "first_coupon_date", "maturity"]]
    static.to_csv(OUT / "bond_static.csv", index=False)
    pd.DataFrame(price_rows).to_csv(OUT / "bond_prices.csv", index=False)
    print(f"{len(static)} bonds, {len(price_rows)} prices -> {OUT}")


if __name__ == "__main__":
    main()
