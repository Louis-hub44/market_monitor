r"""Interpolation of a discount curve on :math:`y(t) = -\ln P(0,t)`.

Both schemes interpolate the *integrated* forward :math:`y(t)=\int_0^t f(s)\,ds`
between nodes :math:`(t_i, y_i)` (with :math:`t_0=0, y_0=0`), so node discount
factors are reproduced exactly.

**Log-linear** (linear on :math:`\ln P`): piecewise-constant instantaneous forwards

.. math:: f(t) = f^d_i = \frac{y_i - y_{i-1}}{t_i - t_{i-1}}, \quad t \in (t_{i-1}, t_i].

Robust, local (a quote only moves its own segment) but forwards jump at nodes.

**Monotone convex** (Hagan & West, 2006): continuous instantaneous forwards whose
average over each segment equals the discrete forward :math:`f^d_i`. Node
forwards are weighted averages of adjacent discrete forwards,

.. math::
    f_i = \frac{t_i - t_{i-1}}{t_{i+1} - t_{i-1}} f^d_{i+1}
        + \frac{t_{i+1} - t_i}{t_{i+1} - t_{i-1}} f^d_i,\qquad
    f_0 = f^d_1 - \tfrac12 (f_1 - f^d_1),\quad
    f_n = f^d_n - \tfrac12 (f_{n-1} - f^d_n),

and on each segment :math:`f(t) = f^d_i + g(x)`, :math:`x = (t-t_{i-1})/(t_i-t_{i-1})`,
where :math:`g` is a quadratic (or piecewise quadratic) chosen among four regions
of :math:`(g_0, g_1) = (f_{i-1}-f^d_i,\ f_i-f^d_i)` so that :math:`\int_0^1 g = 0`
and the forward stays monotone between nodes. The optional positivity collar
:math:`0 \le f_i \le 2\min(f^d_i, f^d_{i+1})` keeps forwards non-negative when all
discrete forwards are (off by default: EUR rates have been negative).

Beyond the last node, both schemes extrapolate with a flat instantaneous forward.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

import numpy as np

from rates_analytics.exceptions import RatesAnalyticsError

_EPS = 1e-12


class CurveError(RatesAnalyticsError, ValueError):
    """Invalid curve nodes or evaluation point."""


class InterpolationMethod(str, Enum):
    LOG_LINEAR = "log_linear"
    MONOTONE_CONVEX = "monotone_convex"

    @classmethod
    def parse(cls, value: str | InterpolationMethod) -> InterpolationMethod:
        if isinstance(value, InterpolationMethod):
            return value
        key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {"loglinear": cls.LOG_LINEAR, "log_linear_discount": cls.LOG_LINEAR,
                   "monotoneconvex": cls.MONOTONE_CONVEX, "hagan_west": cls.MONOTONE_CONVEX}
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError as exc:
            raise CurveError(f"Unknown interpolation {value!r}") from exc


class LogDiscountInterpolator(ABC):
    """Interpolator of :math:`y(t)=-\\ln P(t)` on nodes starting at ``t=0``."""

    method: InterpolationMethod

    def __init__(self, times, y) -> None:
        t = np.asarray(times, dtype=float)
        y = np.asarray(y, dtype=float)
        if t.ndim != 1 or t.shape != y.shape or t.size < 2:
            raise CurveError("Need at least two nodes (t=0 and one pillar) of equal length")
        if t[0] != 0.0 or y[0] != 0.0:
            raise CurveError("First node must be (t=0, -lnP=0)")
        if not np.all(np.isfinite(t)) or not np.all(np.isfinite(y)):
            raise CurveError("Non-finite curve nodes")
        h = np.diff(t)
        if np.any(h <= 0):
            raise CurveError("Node times must be strictly increasing")
        self.t, self.y, self.h = t, y, h
        self.fd = np.diff(y) / h  # discrete forwards per segment
        self._build()

    def _build(self) -> None:
        """Hook for subclasses (precomputations)."""

    def _locate(self, t: np.ndarray) -> np.ndarray:
        if np.any(t < 0):
            raise CurveError("Cannot evaluate a curve before its reference date")
        return np.clip(np.searchsorted(self.t, t, side="left") - 1, 0, self.fd.size - 1)

    def value(self, t):
        """:math:`y(t) = -\\ln P(t)` (scalar or array)."""
        ta = np.asarray(t, dtype=float)
        out = self._value(np.atleast_1d(ta))
        return out.reshape(ta.shape) if ta.ndim else float(out[0])

    def forward(self, t):
        """Instantaneous forward :math:`f(t)` (scalar or array)."""
        ta = np.asarray(t, dtype=float)
        out = self._forward(np.atleast_1d(ta))
        return out.reshape(ta.shape) if ta.ndim else float(out[0])

    @abstractmethod
    def _value(self, t: np.ndarray) -> np.ndarray: ...

    @abstractmethod
    def _forward(self, t: np.ndarray) -> np.ndarray: ...


class LogLinearInterpolator(LogDiscountInterpolator):
    """Linear on log discount factors = piecewise flat instantaneous forwards."""

    method = InterpolationMethod.LOG_LINEAR

    def _value(self, t):
        k = self._locate(t)
        return self.y[k] + self.fd[k] * (t - self.t[k])

    def _forward(self, t):
        return self.fd[self._locate(t)]


class MonotoneConvexInterpolator(LogDiscountInterpolator):
    """Hagan-West monotone convex interpolation of instantaneous forwards."""

    method = InterpolationMethod.MONOTONE_CONVEX

    def __init__(self, times, y, positivity: bool = False) -> None:
        self.positivity = positivity
        super().__init__(times, y)

    # ------------------------------------------------------------- set-up
    def _build(self) -> None:
        fd, h = self.fd, self.h
        n = fd.size
        f = np.empty(n + 1)
        if n == 1:
            f[:] = fd[0]
        else:
            w = h[:-1] / (h[:-1] + h[1:])
            f[1:-1] = w * fd[1:] + (1.0 - w) * fd[:-1]
            f[0] = fd[0] - 0.5 * (f[1] - fd[0])
            f[-1] = fd[-1] - 0.5 * (f[-2] - fd[-1])
        if self.positivity:
            if np.any(fd < 0):
                raise CurveError("Positivity collar requires non-negative discrete forwards")
            f[0] = np.clip(f[0], 0.0, 2.0 * fd[0])
            f[-1] = np.clip(f[-1], 0.0, 2.0 * fd[-1])
            if n > 1:
                f[1:-1] = np.clip(f[1:-1], 0.0, 2.0 * np.minimum(fd[:-1], fd[1:]))
        self.f_nodes = f
        g0, g1 = f[:-1] - fd, f[1:] - fd
        region = np.full(n, 4, dtype=int)
        zero = (g0 == 0.0) & (g1 == 0.0)
        r1 = ((g0 < 0) & (-0.5 * g0 <= g1) & (g1 <= -2.0 * g0)) | (
            (g0 > 0) & (-0.5 * g0 >= g1) & (g1 >= -2.0 * g0))
        r2 = ((g0 < 0) & (g1 > -2.0 * g0)) | ((g0 > 0) & (g1 < -2.0 * g0))
        r3 = ((g0 > 0) & (0 > g1) & (g1 > -0.5 * g0)) | ((g0 < 0) & (0 < g1) & (g1 < -0.5 * g0))
        region[r3] = 3
        region[r2] = 2
        region[r1] = 1
        region[zero] = 0
        with np.errstate(divide="ignore", invalid="ignore"):
            eta = np.where(region == 2, (g1 + 2.0 * g0) / (g1 - g0),
                  np.where(region == 3, 3.0 * g1 / (g1 - g0),
                  np.where(region == 4, g1 / (g0 + g1), 0.0)))
            a = np.where(region == 4, -g0 * g1 / (g0 + g1), 0.0)
        eta = np.nan_to_num(eta, nan=0.0)
        a = np.nan_to_num(a, nan=0.0)
        self.g0, self.g1, self.region, self.eta, self.a = g0, g1, region, np.clip(eta, 0.0, 1.0), a

    # --------------------------------------------------------- evaluation
    def _segment(self, t):
        k = self._locate(t)
        x = np.clip((t - self.t[k]) / self.h[k], 0.0, 1.0)
        return k, x

    def _g_and_G(self, k, x, need_g: bool = True, need_G: bool = True):
        """Shape function g(x) and its integral G(x) on the segments ``k`` (region by region)."""
        g = np.zeros_like(x)
        G = np.zeros_like(x)
        reg = self.region[k]
        with np.errstate(all="ignore"):
            for r in (1, 2, 3, 4):
                m = reg == r
                if not m.any():
                    continue
                kk, xx = k[m], x[m]
                g0, g1, eta, a = self.g0[kk], self.g1[kk], self.eta[kk], self.a[kk]
                e, e1 = np.maximum(eta, _EPS), np.maximum(1.0 - eta, _EPS)
                if r == 1:  # single quadratic
                    gv = g0 * (1 - 4 * xx + 3 * xx**2) + g1 * (-2 * xx + 3 * xx**2)
                    Gv = g0 * (xx - 2 * xx**2 + xx**3) + g1 * (xx**3 - xx**2)
                elif r == 2:  # flat at g0 then quadratic to g1
                    left = xx <= eta
                    gv = np.where(left, g0, g0 + (g1 - g0) * ((xx - eta) / e1) ** 2)
                    Gv = np.where(left, g0 * xx, g0 * xx + (g1 - g0) * (xx - eta) ** 3 / (3 * e1**2))
                elif r == 3:  # quadratic from g0 then flat at g1
                    left = xx < eta
                    gv = np.where(left, g1 + (g0 - g1) * ((eta - xx) / e) ** 2, g1)
                    Gv = np.where(left, g1 * xx + (g0 - g1) * (eta**3 - (eta - xx) ** 3) / (3 * e**2),
                                  g1 * xx + (g0 - g1) * eta / 3)
                else:  # two quadratics meeting at the extremum A
                    left = xx <= eta
                    gv = np.where(left, a + (g0 - a) * ((eta - xx) / e) ** 2,
                                  a + (g1 - a) * ((xx - eta) / e1) ** 2)
                    Gv = np.where(left, a * xx + (g0 - a) * (eta**3 - (eta - xx) ** 3) / (3 * e**2),
                                  a * xx + (g0 - a) * eta / 3 + (g1 - a) * (xx - eta) ** 3 / (3 * e1**2))
                if need_g:
                    g[m] = gv
                if need_G:
                    G[m] = Gv
        return (g if need_g else None), (G if need_G else None)

    def _value(self, t):
        k, x = self._segment(t)
        _, G = self._g_and_G(k, x, need_g=False)
        inside = self.y[k] + self.h[k] * (self.fd[k] * x + G)
        beyond = self.y[-1] + self.f_nodes[-1] * (t - self.t[-1])
        return np.where(t > self.t[-1], beyond, inside)

    def _forward(self, t):
        k, x = self._segment(t)
        g, _ = self._g_and_G(k, x, need_G=False)
        return np.where(t > self.t[-1], self.f_nodes[-1], self.fd[k] + g)


def make_interpolator(method: InterpolationMethod | str, times, y, **options) -> LogDiscountInterpolator:
    """Factory: ``method`` in ``{"log_linear", "monotone_convex"}``."""
    m = InterpolationMethod.parse(method)
    if m is InterpolationMethod.LOG_LINEAR:
        return LogLinearInterpolator(times, y)
    return MonotoneConvexInterpolator(times, y, positivity=bool(options.get("positivity", False)))
