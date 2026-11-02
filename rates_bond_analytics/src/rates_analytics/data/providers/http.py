"""Small HTTP helper shared by the REST providers (retries, timeouts, errors)."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from rates_analytics.exceptions import DataProviderError

logger = logging.getLogger(__name__)


class HttpClient:
    """``requests`` wrapper with retry on transient failures (5xx, 429, timeouts)."""

    def __init__(self, timeout_s: float = 30.0, retries: int = 2, backoff_s: float = 1.0,
                 session: Any | None = None, secrets: tuple[str, ...] = ()) -> None:
        self.timeout = timeout_s
        self._secrets = tuple(x for x in secrets if x)
        self.retries = retries
        self.backoff = backoff_s
        self.session = session or requests.Session()

    def _mask(self, text: str) -> str:
        """Remove secrets (API keys) from messages before they reach logs or the UI."""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def get(self, url: str, params: dict[str, Any] | None = None, allow_404: bool = False,
            headers: dict[str, str] | None = None):
        """GET returning the response; ``None`` on 404 when ``allow_404``.

        Raises:
            DataProviderError: after exhausting retries or on 4xx errors.
        """
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                extra = {"headers": headers} if headers else {}
                resp = self.session.get(url, params=params, timeout=self.timeout, **extra)
            except requests.RequestException as exc:
                last_exc = exc
            else:
                if resp.status_code == 404 and allow_404:
                    return None
                if resp.status_code in (429,) or resp.status_code >= 500:
                    last_exc = DataProviderError(f"HTTP {resp.status_code} on {url}")
                elif resp.status_code >= 400:
                    raise DataProviderError(self._mask(f"HTTP {resp.status_code} on {url}: {resp.text[:200]}"))
                else:
                    return resp
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        raise DataProviderError(self._mask(f"GET {url} failed after {self.retries + 1} attempts: {last_exc}"))
