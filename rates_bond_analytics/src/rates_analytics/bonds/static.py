"""Build :class:`FixedRateBond` objects from the market-data layer's bond static table."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from rates_analytics.bonds.bond import FixedRateBond
from rates_analytics.conventions.dateutils import to_date
from rates_analytics.conventions.daycount import get_day_counter
from rates_analytics.conventions.market import BondConvention, get_convention
from rates_analytics.conventions.period import Frequency
from rates_analytics.exceptions import ConventionError
from rates_analytics.instruments.common import InstrumentError

logger = logging.getLogger(__name__)

ISSUER_CONVENTIONS = {"DE": "BUND", "FR": "OAT", "IT": "BTP", "ES": "BONO"}


def convention_for_issuer(issuer: str) -> BondConvention:
    """Market convention of a sovereign issuer (ISO country code or convention name)."""
    key = str(issuer).strip().upper()
    return get_convention(ISSUER_CONVENTIONS.get(key, key))


def _opt_date(v) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT or (isinstance(v, str) and not v):
        return None
    return to_date(v)


def bond_from_static(isin: str, row: pd.Series | dict, convention: BondConvention | None = None) -> FixedRateBond:
    """One bond from a static row (columns of ``BOND_STATIC_COLUMNS``).

    ``issue_date`` falls back to ``accrual_start``; the coupon frequency and day
    count of the static data override the convention when present.
    """
    r = dict(row)
    conv = convention or convention_for_issuer(r.get("issuer") or "")
    issue = _opt_date(r.get("accrual_start")) or _opt_date(r.get("issue_date"))
    maturity = _opt_date(r.get("maturity"))
    if issue is None or maturity is None or r.get("coupon") is None or pd.isna(r.get("coupon")):
        raise InstrumentError(f"{isin}: incomplete static data (coupon / issue / maturity)")
    freq = r.get("frequency")
    dc = r.get("day_count")
    return FixedRateBond(
        isin=isin, coupon=float(r["coupon"]), maturity=maturity, issue_date=issue, convention=conv,
        first_coupon_date=_opt_date(r.get("first_coupon_date")),
        day_counter=get_day_counter(dc) if isinstance(dc, str) and dc else None,  # type: ignore[arg-type]
        frequency=Frequency(int(freq)) if freq is not None and not pd.isna(freq) and int(freq) > 0 else None,  # type: ignore[arg-type]
        name=str(r.get("name") or ""), issuer=str(r.get("issuer") or ""),
    )


def bonds_from_static(static: pd.DataFrame, convention: BondConvention | None = None
                      ) -> tuple[list[FixedRateBond], dict[str, str]]:
    """All valid bonds of a static table; returns (bonds, rejected isin -> reason)."""
    bonds, rejected = [], {}
    for isin, row in static.iterrows():
        try:
            bonds.append(bond_from_static(str(isin), row, convention))
        except (InstrumentError, ConventionError, ValueError) as exc:
            rejected[str(isin)] = str(exc)
            logger.warning("Bond %s rejected: %s", isin, exc)
    return bonds, rejected
