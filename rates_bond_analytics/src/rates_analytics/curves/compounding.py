r"""Compounding conventions and rate <-> discount factor conversions.

For an accrual fraction :math:`\tau` and a rate :math:`r`:

=============  ===================================================
SIMPLE         :math:`P = 1 / (1 + r\tau)`
COMPOUNDED     :math:`P = (1 + r/m)^{-m\tau}` (``m`` = frequency)
CONTINUOUS     :math:`P = e^{-r\tau}`
=============  ===================================================
"""

from __future__ import annotations

from enum import Enum

import numpy as np

from rates_analytics.exceptions import ConventionError


class Compounding(str, Enum):
    SIMPLE = "simple"
    COMPOUNDED = "compounded"
    CONTINUOUS = "continuous"

    @classmethod
    def parse(cls, value: str | Compounding) -> Compounding:
        if isinstance(value, Compounding):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ConventionError(f"Unknown compounding {value!r}") from exc


def discount_from_rate(rate, tau, compounding: Compounding | str = Compounding.CONTINUOUS,
                       frequency: int = 1):
    """Discount factor implied by ``rate`` over ``tau`` years (scalar or array)."""
    c = Compounding.parse(compounding)
    rate, tau = np.asarray(rate, dtype=float), np.asarray(tau, dtype=float)
    if c is Compounding.CONTINUOUS:
        out = np.exp(-rate * tau)
    elif c is Compounding.SIMPLE:
        out = 1.0 / (1.0 + rate * tau)
    else:
        if frequency <= 0:
            raise ConventionError("Compounded rates need a positive frequency")
        out = (1.0 + rate / frequency) ** (-frequency * tau)
    return out if out.ndim else float(out)


def rate_from_discount(df, tau, compounding: Compounding | str = Compounding.CONTINUOUS,
                       frequency: int = 1):
    """Rate implied by discount factor ``df`` over ``tau`` years (``tau`` must be > 0)."""
    c = Compounding.parse(compounding)
    df, tau = np.asarray(df, dtype=float), np.asarray(tau, dtype=float)
    if np.any(tau <= 0):
        raise ConventionError("Rate from discount factor needs tau > 0")
    if np.any(df <= 0):
        raise ConventionError("Discount factors must be positive")
    if c is Compounding.CONTINUOUS:
        out = -np.log(df) / tau
    elif c is Compounding.SIMPLE:
        out = (1.0 / df - 1.0) / tau
    else:
        if frequency <= 0:
            raise ConventionError("Compounded rates need a positive frequency")
        out = frequency * (df ** (-1.0 / (frequency * tau)) - 1.0)
    return out if out.ndim else float(out)
