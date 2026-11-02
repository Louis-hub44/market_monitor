"""Add an ILLUSTRATIVE fixings history (€STR, EURIBOR 6M) to data/sample/history.csv.

Levels follow the ECB deposit-rate path (€STR ~ depo - 9 bp), with an EURIBOR 6M
that anticipates the cuts; small noise. Not real fixings: for seasoned-swap demos
and tests only. Run from the project root:  python scripts/generate_sample_fixings.py
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from rates_analytics.conventions import TARGET

PATH = Path(__file__).resolve().parents[1] / "data" / "sample" / "history.csv"
DEPO = [(date(2023, 9, 20), 0.0400), (date(2024, 6, 12), 0.0375), (date(2024, 9, 18), 0.0350),
        (date(2024, 10, 23), 0.0325), (date(2024, 12, 18), 0.0300), (date(2025, 2, 5), 0.0275),
        (date(2025, 3, 12), 0.0250), (date(2025, 4, 23), 0.0225), (date(2025, 6, 11), 0.0200)]


def depo(d: date) -> float:
    return [r for start, r in DEPO if start <= d][-1]


def main() -> None:
    hist = pd.read_csv(PATH, index_col=0)
    first_existing = pd.Timestamp(hist.index.min()).date()
    days = TARGET.business_days(date(2024, 1, 2), first_existing)[:-1]
    rng = random.Random(7)
    rows = {}
    for d in days:
        estr = depo(d) - 0.0009 + rng.gauss(0, 0.00005)
        ahead = np.mean([depo(min(date(d.year + (d.month + k - 1) // 12, (d.month + k - 1) % 12 + 1, 1),
                                  date(2026, 10, 6))) for k in range(1, 7)])
        rows[d.isoformat()] = {"EUR.ESTR.FIX": round(estr, 6),
                               "EUR.EURIBOR6M.FIX": round(ahead + 0.0012 + rng.gauss(0, 0.0002), 6)}
    old = pd.DataFrame.from_dict(rows, orient="index")
    out = pd.concat([old, hist[~hist.index.isin(old.index)]]).sort_index()
    out.index.name = "date"
    out.to_csv(PATH, float_format="%.6f")
    print(f"{len(old)} fixing days added; history now {len(out)} rows")


if __name__ == "__main__":
    main()
