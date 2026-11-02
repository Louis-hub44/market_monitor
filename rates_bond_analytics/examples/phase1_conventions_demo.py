"""Phase 1 demo: spot date, swap schedules and bond accrued interest.

Run from the project root:  python examples/phase1_conventions_demo.py
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from rates_analytics.conventions import (
    ACT_ACT_ICMA,
    TARGET,
    build_accrual_periods,
    generate_schedule,
    get_convention,
    maturity_from_tenor,
    periods_to_frame,
    settlement_date,
    spot_date,
)


def ois_fixed_leg(trade_date: date, tenor: str) -> pd.DataFrame:
    """Fixed-leg accrual periods of a spot-starting €STR OIS."""
    conv = get_convention("EUR_ESTR_OIS")
    start = spot_date(trade_date, conv.calendar, conv.spot_lag)
    end = maturity_from_tenor(start, tenor, conv.calendar, end_of_month=conv.end_of_month)
    leg = conv.fixed_leg
    sched = generate_schedule(start, end, leg.tenor, conv.calendar, leg.bdc, end_of_month=conv.end_of_month)
    periods = build_accrual_periods(sched, leg.day_counter, payment_bdc=leg.bdc, payment_lag=leg.payment_lag)
    return periods_to_frame(periods)[["start", "end", "payment_date", "year_fraction", "is_regular"]]


def bund_accrued(coupon: float, maturity: date, trade_date: date) -> tuple[date, float]:
    """Accrued interest (per 100 nominal) of an annual Bund settling T+2."""
    bund = get_convention("BUND")
    settle = settlement_date(trade_date, bund.calendar, bund.settlement_lag)
    sched = generate_schedule(date(maturity.year - 30, maturity.month, maturity.day), maturity,
                              bund.frequency.period, bund.calendar, bund.accrual_bdc, bund.accrual_bdc)
    for p in build_accrual_periods(sched, bund.day_counter, accrue_on_adjusted=False):
        if p.start <= settle < p.end:
            return settle, 100 * coupon * ACT_ACT_ICMA.year_fraction(p.start, settle, p.ref_start, p.ref_end)
    raise ValueError("settlement outside the bond life")


if __name__ == "__main__":
    trade = date(2026, 10, 7)
    print(f"Trade date {trade}, EUR spot (T+2 TARGET) = {spot_date(trade, TARGET)}\n")
    print("€STR OIS 3Y – fixed leg (annual ACT/360, MF, payment lag 1):")
    print(ois_fixed_leg(trade, "3Y").to_string(index=False), "\n")
    settle, ai = bund_accrued(0.025, date(2035, 2, 15), trade)
    print(f"Bund 2.50% 15-Feb-2035, settlement {settle}: accrued = {ai:.6f} per 100")
