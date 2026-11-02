"""Hand-checked unit tests for the conventions layer (no QuantLib needed)."""

from __future__ import annotations

from datetime import date

import pytest

from rates_analytics.conventions import (
    ACT_360,
    ACT_365F,
    ACT_ACT_ICMA,
    ACT_ACT_ISDA,
    TARGET,
    THIRTY_360,
    THIRTY_E_360,
    DateGenerationRule,
    Frequency,
    Period,
    TimeUnit,
    build_accrual_periods,
    generate_schedule,
    get_calendar,
    get_convention,
    get_day_counter,
    maturity_from_tenor,
    money_market_dates,
    spot_date,
    to_date,
)
from rates_analytics.conventions import (
    BusinessDayConvention as BDC,
)
from rates_analytics.conventions.dateutils import add_months, easter_sunday
from rates_analytics.exceptions import ConventionError, ScheduleError


# ------------------------------------------------------------------ periods
@pytest.mark.parametrize("text,n,unit", [
    ("6M", 6, TimeUnit.MONTHS), ("10y", 10, TimeUnit.YEARS), ("1W", 1, TimeUnit.WEEKS),
    ("ON", 1, TimeUnit.DAYS), ("O/N", 1, TimeUnit.DAYS), ("1Y6M", 18, TimeUnit.MONTHS),
    ("-3M", -3, TimeUnit.MONTHS), ("2W3D", 17, TimeUnit.DAYS),
])
def test_period_parse(text, n, unit):
    assert Period.parse(text) == Period(n, unit)


@pytest.mark.parametrize("bad", ["", "M6", "6X", "1Y2W", "abc", "6 M M"])
def test_period_parse_rejects_garbage(bad):
    with pytest.raises(ConventionError):
        Period.parse(bad)


def test_period_helpers():
    assert Period.parse("12M").normalized() == Period(1, TimeUnit.YEARS)
    assert -Period.parse("3M") == Period(-3, TimeUnit.MONTHS)
    assert 2 * Period.parse("6M") == Period(12, TimeUnit.MONTHS)
    assert str(Period.parse("10Y")) == "10Y"
    assert Period.parse("18M").years == pytest.approx(1.5)


def test_frequency_mapping():
    assert Frequency.from_period("6M") is Frequency.SEMIANNUAL
    assert Frequency.from_period("1Y") is Frequency.ANNUAL
    assert Frequency.parse("Q") is Frequency.QUARTERLY
    assert Frequency.SEMIANNUAL.period == Period(6, TimeUnit.MONTHS)
    assert Frequency.ANNUAL.period == Period(1, TimeUnit.YEARS)
    with pytest.raises(ConventionError):
        Frequency.from_period("5M")


# ------------------------------------------------------------- date utils
def test_to_date_and_add_months():
    assert to_date("2026-10-07") == date(2026, 10, 7)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    with pytest.raises(ConventionError):
        to_date("not a date")


@pytest.mark.parametrize("year,expected", [
    (2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)),
    (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28)), (2038, date(2038, 4, 25)),
])
def test_easter(year, expected):
    assert easter_sunday(year) == expected


# ------------------------------------------------------------- day counts
def test_act_conventions():
    d1, d2 = date(2026, 1, 15), date(2026, 7, 15)  # 181 days
    assert ACT_360.year_fraction(d1, d2) == pytest.approx(181 / 360)
    assert ACT_365F.year_fraction(d1, d2) == pytest.approx(181 / 365)


@pytest.mark.parametrize("d1,d2,bond_basis,eurobond", [
    (date(2026, 1, 31), date(2026, 3, 31), 60, 60),
    (date(2026, 1, 30), date(2026, 3, 31), 60, 60),
    (date(2026, 1, 29), date(2026, 3, 31), 62, 61),   # D2=31 kept in bond basis when D1<30
    (date(2026, 2, 28), date(2026, 3, 31), 33, 32),
])
def test_thirty_360_variants(d1, d2, bond_basis, eurobond):
    assert THIRTY_360.day_count(d1, d2) == bond_basis
    assert THIRTY_E_360.day_count(d1, d2) == eurobond


def test_act_act_icma_regular_and_stub():
    # ICMA example: annual coupon 1-Nov-2003 -> 1-Nov-2004 (366 days)
    assert ACT_ACT_ICMA.year_fraction(date(2003, 11, 1), date(2004, 5, 1),
                                      date(2003, 11, 1), date(2004, 11, 1)) == pytest.approx(182 / 366)
    # full regular semi-annual period = 1/F
    assert ACT_ACT_ICMA.year_fraction(date(2026, 3, 1), date(2026, 9, 1),
                                      date(2026, 3, 1), date(2026, 9, 1)) == pytest.approx(0.5)
    # short first coupon: 182 days of a 365-day notional annual period
    assert ACT_ACT_ICMA.year_fraction(date(2026, 8, 15), date(2027, 2, 13),
                                      date(2026, 2, 13), date(2027, 2, 13)) == pytest.approx(182 / 365)


def test_act_act_isda_spans_years():
    tau = ACT_ACT_ISDA.year_fraction(date(2023, 11, 1), date(2024, 5, 1))
    assert tau == pytest.approx(61 / 365 + 121 / 366)


def test_day_counter_registry():
    assert get_day_counter("Act/360") is ACT_360
    assert get_day_counter("30/360") is THIRTY_360
    assert get_day_counter("ACT/ACT") is ACT_ACT_ICMA
    assert get_day_counter("30E/360 ISDA", date(2030, 2, 28)).termination_date == date(2030, 2, 28)
    with pytest.raises(ConventionError):
        get_day_counter("ACT/999")


# --------------------------------------------------------------- calendar
def test_target_2026_holidays():
    assert TARGET.holidays(date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 1), date(2026, 12, 25),
    ]  # 26-Dec-2026 is a Saturday
    assert not TARGET.is_business_day(date(2026, 12, 26))
    assert TARGET.is_business_day(date(2026, 10, 7))


def test_business_day_conventions():
    sat = date(2026, 5, 30)  # Saturday, next business day is in June
    assert TARGET.adjust(sat, BDC.FOLLOWING) == date(2026, 6, 1)
    assert TARGET.adjust(sat, BDC.MODIFIED_FOLLOWING) == date(2026, 5, 29)
    assert TARGET.adjust(sat, BDC.PRECEDING) == date(2026, 5, 29)
    assert TARGET.adjust(sat, "U") == sat
    assert TARGET.adjust(date(2026, 8, 1), BDC.MODIFIED_PRECEDING) == date(2026, 8, 3)
    with pytest.raises(ConventionError):
        BDC.parse("sideways")


def test_advance_and_eom():
    assert TARGET.advance(date(2026, 4, 2), 2) == date(2026, 4, 8)          # Easter week-end
    assert TARGET.advance(date(2026, 4, 30), "1M", BDC.MODIFIED_FOLLOWING, True) == date(2026, 5, 29)
    assert TARGET.advance(date(2026, 4, 30), "1M", BDC.MODIFIED_FOLLOWING, False) == date(2026, 5, 29)
    assert TARGET.advance(date(2026, 2, 27), "1M", BDC.MODIFIED_FOLLOWING, True) == date(2026, 3, 31)
    assert TARGET.advance(date(2026, 2, 27), "1M", BDC.MODIFIED_FOLLOWING, False) == date(2026, 3, 27)
    assert TARGET.end_of_month(date(2026, 5, 10)) == date(2026, 5, 29)
    assert TARGET.is_end_of_month(date(2026, 2, 27))


def test_calendar_registry():
    assert get_calendar("target2") is TARGET
    joint = get_calendar("TARGET+WEEKENDS")
    assert joint.is_business_day(date(2026, 10, 7)) and not joint.is_business_day(date(2026, 5, 1))
    with pytest.raises(ConventionError):
        get_calendar("MARS")


# ------------------------------------------------------------- spot / MM
def test_spot_and_money_market_dates():
    trade = date(2026, 4, 2)  # Thursday before Good Friday
    assert spot_date(trade, TARGET) == date(2026, 4, 8)
    assert money_market_dates(trade, "ON", TARGET) == (date(2026, 4, 2), date(2026, 4, 7))
    assert money_market_dates(trade, "TN", TARGET) == (date(2026, 4, 7), date(2026, 4, 8))
    assert money_market_dates(trade, "SN", TARGET) == (date(2026, 4, 8), date(2026, 4, 9))
    assert money_market_dates(trade, "SW", TARGET) == (date(2026, 4, 8), date(2026, 4, 15))
    assert money_market_dates(trade, "3M", TARGET) == (date(2026, 4, 8), date(2026, 7, 8))
    with pytest.raises(ConventionError):
        spot_date(trade, TARGET, -1)


def test_maturity_from_tenor():
    spot = date(2026, 10, 9)
    assert maturity_from_tenor(spot, "10Y", TARGET) == date(2036, 10, 9)
    assert maturity_from_tenor(date(2026, 4, 30), "1Y", TARGET, end_of_month=True) == date(2027, 4, 30)
    assert maturity_from_tenor(spot, "1Y", TARGET, adjust=True) == date(2027, 10, 11)


# -------------------------------------------------------------- schedules
def test_eur_swap_fixed_leg_schedule():
    s = generate_schedule(date(2026, 10, 9), date(2031, 10, 9), "1Y", TARGET, BDC.MODIFIED_FOLLOWING)
    assert s.adjusted == (date(2026, 10, 9), date(2027, 10, 11), date(2028, 10, 9),
                          date(2029, 10, 9), date(2030, 10, 9), date(2031, 10, 9))
    assert s.unadjusted[1] == date(2027, 10, 9)
    assert all(s.is_regular) and s.n_periods == 5


def test_short_and_long_front_stub():
    args = (date(2026, 10, 9), date(2029, 3, 15), "1Y", TARGET, BDC.MODIFIED_FOLLOWING)
    short = generate_schedule(*args)
    assert short.adjusted == (date(2026, 10, 9), date(2027, 3, 15), date(2028, 3, 15), date(2029, 3, 15))
    assert short.is_regular == (False, True, True)
    long_ = generate_schedule(*args, long_stub=True)
    assert long_.adjusted == (date(2026, 10, 9), date(2028, 3, 15), date(2029, 3, 15))
    assert long_.is_regular == (False, True)


def test_back_stub_forward_rule():
    s = generate_schedule(date(2026, 10, 9), date(2029, 3, 15), "1Y", TARGET,
                          rule=DateGenerationRule.FORWARD)
    assert s.adjusted[-2:] == (date(2028, 10, 9), date(2029, 3, 15))
    assert s.is_regular[-1] is False


def test_eom_rule_schedule():
    s = generate_schedule(date(2026, 2, 27), date(2028, 2, 29), "6M", TARGET,
                          BDC.MODIFIED_FOLLOWING, end_of_month=True)
    assert s.adjusted == (date(2026, 2, 27), date(2026, 8, 31), date(2027, 2, 26),
                          date(2027, 8, 31), date(2028, 2, 29))


def test_schedule_validation():
    with pytest.raises(ScheduleError):
        generate_schedule(date(2030, 1, 1), date(2026, 1, 1), "1Y", TARGET)
    with pytest.raises(ScheduleError):
        generate_schedule(date(2026, 1, 1), date(2030, 1, 1), "1Y", TARGET, first_date=date(2031, 1, 1))


def test_ois_accrual_periods_payment_lag():
    conv = get_convention("EUR_ESTR_OIS")
    s = generate_schedule(date(2026, 10, 9), date(2028, 10, 9), conv.fixed_leg.tenor, conv.calendar,
                          conv.fixed_leg.bdc, end_of_month=conv.end_of_month)
    periods = build_accrual_periods(s, conv.fixed_leg.day_counter, payment_lag=conv.fixed_leg.payment_lag,
                                    payment_bdc=conv.fixed_leg.bdc)
    assert [p.payment_date for p in periods] == [date(2027, 10, 12), date(2028, 10, 10)]
    assert periods[0].year_fraction == pytest.approx(367 / 360)


def test_euribor_float_leg_fixings():
    conv = get_convention("EUR_EURIBOR_6M_SWAP")
    s = generate_schedule(date(2026, 10, 9), date(2027, 10, 9), conv.float_leg.tenor, conv.calendar,
                          conv.float_leg.bdc, end_of_month=conv.end_of_month)
    periods = build_accrual_periods(s, conv.float_leg.day_counter, fixing_lag=conv.index.fixing_lag)
    assert [p.fixing_date for p in periods] == [date(2026, 10, 7), date(2027, 4, 7)]
    assert periods[0].adj_end == date(2027, 4, 9)


def test_bond_accrues_on_unadjusted_dates():
    bund = get_convention("BUND")
    # coupon date 15-Aug-2027 is a Sunday: accrual stays on the 15th, payment on Monday 16th
    s = generate_schedule(date(2026, 8, 15), date(2028, 8, 15), bund.frequency.period, bund.calendar,
                          bund.accrual_bdc, bund.accrual_bdc)
    periods = build_accrual_periods(s, bund.day_counter, accrue_on_adjusted=False,
                                    payment_bdc=bund.payment_bdc)
    assert periods[0].end == date(2027, 8, 15)
    assert periods[0].payment_date == date(2027, 8, 16)
    assert periods[0].year_fraction == pytest.approx(1.0)


def test_convention_registry():
    assert get_convention("btp").frequency is Frequency.SEMIANNUAL
    assert get_convention("EUR_EURIBOR_6M_SWAP").fixed_leg.day_counter is THIRTY_360
    with pytest.raises(ConventionError):
        get_convention("JGB")
