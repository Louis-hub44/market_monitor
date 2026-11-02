r"""Nelson-Siegel (NS) and Nelson-Siegel-Svensson (NSS) parametric curves.

Continuously compounded zero rate (Svensson, 1994; ECB / Bundesbank method):

.. math::
    z(t) = \beta_0
         + \beta_1 \frac{1 - e^{-t/\tau_1}}{t/\tau_1}
         + \beta_2 \left(\frac{1 - e^{-t/\tau_1}}{t/\tau_1} - e^{-t/\tau_1}\right)
         + \beta_3 \left(\frac{1 - e^{-t/\tau_2}}{t/\tau_2} - e^{-t/\tau_2}\right)

Instantaneous forward:

.. math::
    f(t) = \beta_0 + \beta_1 e^{-t/\tau_1} + \beta_2 \frac{t}{\tau_1} e^{-t/\tau_1}
         + \beta_3 \frac{t}{\tau_2} e^{-t/\tau_2}

Interpretation: :math:`\beta_0` long-term level, :math:`\beta_0+\beta_1` short
rate, :math:`\beta_2`, :math:`\beta_3` humps located around :math:`\tau_1`, :math:`\tau_2`.
NS is the special case :math:`\beta_3 = 0`. Time is ACT/365F from the reference date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

import numpy as np

from rates_analytics.conventions.calendar import TARGET, Calendar
from rates_analytics.curves.interpolation import CurveError
from rates_analytics.curves.yield_curve import YieldCurve


class ParametricModel(str, Enum):
    NS = "ns"
    NSS = "nss"

    @classmethod
    def parse(cls, value: str | ParametricModel) -> ParametricModel:
        if isinstance(value, ParametricModel):
            return value
        key = str(value).strip().lower().replace("-", "").replace("_", "")
        aliases = {"ns": cls.NS, "nelsonsiegel": cls.NS, "nss": cls.NSS, "svensson": cls.NSS,
                   "nelsonsiegelsvensson": cls.NSS}
        try:
            return aliases[key]
        except KeyError as exc:
            raise CurveError(f"Unknown parametric model {value!r}") from exc


def _loading_slope(x: np.ndarray) -> np.ndarray:
    """(1 - e^{-x}) / x with its limit 1 at x = 0."""
    small = np.abs(x) < 1e-6
    xs = np.where(small, 1.0, x)
    return np.where(small, 1.0 - x / 2.0 + x * x / 6.0, -np.expm1(-xs) / xs)


def _loading_slope_derivative(x: np.ndarray) -> np.ndarray:
    """d/dx [(1 - e^{-x}) / x] = (x e^{-x} - (1 - e^{-x})) / x^2, limit -1/2 at 0."""
    small = np.abs(x) < 1e-4
    xs = np.where(small, 1.0, x)
    exact = (xs * np.exp(-xs) + np.expm1(-xs)) / (xs * xs)
    return np.where(small, -0.5 + x / 3.0 - x * x / 8.0, exact)


@dataclass(frozen=True)
class NSSParams:
    """NS / NSS parameters (decimals for betas, years for taus)."""

    beta0: float
    beta1: float
    beta2: float
    tau1: float
    beta3: float = 0.0
    tau2: float | None = None

    def __post_init__(self) -> None:
        vals = [self.beta0, self.beta1, self.beta2, self.beta3, self.tau1]
        if self.tau2 is not None:
            vals.append(self.tau2)
        if not np.all(np.isfinite(vals)):
            raise CurveError("NSS parameters must be finite")
        if self.tau1 <= 0 or (self.tau2 is not None and self.tau2 <= 0):
            raise CurveError("NSS decay parameters tau must be positive")
        if self.tau2 is None and self.beta3 != 0.0:
            raise CurveError("beta3 needs tau2 (Svensson term)")

    @property
    def model(self) -> ParametricModel:
        return ParametricModel.NS if self.tau2 is None else ParametricModel.NSS

    def to_array(self) -> np.ndarray:
        if self.model is ParametricModel.NS:
            return np.array([self.beta0, self.beta1, self.beta2, self.tau1])
        return np.array([self.beta0, self.beta1, self.beta2, self.beta3, self.tau1, self.tau2])

    @classmethod
    def from_array(cls, model: ParametricModel | str, x) -> NSSParams:
        m = ParametricModel.parse(model)
        x = [float(v) for v in x]
        if m is ParametricModel.NS:
            return cls(x[0], x[1], x[2], x[3])
        return cls(x[0], x[1], x[2], x[4], beta3=x[3], tau2=x[5])

    def as_dict(self) -> dict[str, float | None]:
        return {"beta0": self.beta0, "beta1": self.beta1, "beta2": self.beta2, "beta3": self.beta3,
                "tau1": self.tau1, "tau2": self.tau2}

    # ---------------------------------------------------------- formulas
    def zero_rate(self, t) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        x1 = t / self.tau1
        l1 = _loading_slope(x1)
        z = self.beta0 + self.beta1 * l1 + self.beta2 * (l1 - np.exp(-x1))
        if self.tau2 is not None:
            x2 = t / self.tau2
            z = z + self.beta3 * (_loading_slope(x2) - np.exp(-x2))
        return z

    def zero_rate_gradient(self, t) -> np.ndarray:
        """d z(t) / d params, columns in :meth:`to_array` order (analytic, for the fit Jacobian)."""
        t = np.asarray(t, dtype=float)
        x1 = t / self.tau1
        l1, e1, d1 = _loading_slope(x1), np.exp(-x1), _loading_slope_derivative(x1)
        dx1 = -x1 / self.tau1                       # dx1 / dtau1
        dz_dtau1 = self.beta1 * d1 * dx1 + self.beta2 * (d1 * dx1 + e1 * dx1)
        cols = [np.ones_like(t), l1, l1 - e1]
        if self.tau2 is None:
            return np.column_stack(cols + [dz_dtau1])
        x2 = t / self.tau2
        l2, e2, d2 = _loading_slope(x2), np.exp(-x2), _loading_slope_derivative(x2)
        dx2 = -x2 / self.tau2
        dz_dtau2 = self.beta3 * (d2 * dx2 + e2 * dx2)
        return np.column_stack(cols + [l2 - e2, dz_dtau1, dz_dtau2])

    def forward_rate(self, t) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        x1 = t / self.tau1
        f = self.beta0 + self.beta1 * np.exp(-x1) + self.beta2 * x1 * np.exp(-x1)
        if self.tau2 is not None:
            x2 = t / self.tau2
            f = f + self.beta3 * x2 * np.exp(-x2)
        return f


class NelsonSiegelSvenssonCurve(YieldCurve):
    """Discount curve :math:`P(t) = e^{-z(t)\\,t}` with NS / NSS zero rates."""

    def __init__(self, reference_date: date, params: NSSParams, calendar: Calendar = TARGET,
                 name: str = "") -> None:
        super().__init__(reference_date, calendar, name)
        self.params = params

    def _discount_t(self, t):
        return np.exp(-self.params.zero_rate(t) * t)

    def _inst_forward_t(self, t):
        return self.params.forward_rate(t)

    def zero_rate_cc(self, t):
        """Continuously compounded NSS zero rate at time(s) ``t`` (exact, also at t = 0)."""
        return self.params.zero_rate(t)
