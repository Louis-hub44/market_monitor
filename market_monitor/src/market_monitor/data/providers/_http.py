"""Small HTTP helper shared by REST providers: retries, back-off, secret redaction."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import requests

from market_monitor.exceptions import ProviderUnavailableError

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Remove secrets (API keys) from a message before it is logged or raised."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: Mapping[str, Any] | None,
    timeout_s: float,
    max_retries: int,
    backoff_s: float,
    source: str,
    secrets: tuple[str, ...] = (),
    sleep: Callable[[float], None] = time.sleep,
    headers: Mapping[str, str] | None = None,
) -> requests.Response:
    """GET with exponential back-off on network errors and retryable statuses.

    The last response is returned as-is (status handling is the caller's job);
    persistent network failures raise :class:`ProviderUnavailableError`.
    """
    for attempt in range(max_retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout_s, headers=headers)
        except requests.RequestException as exc:
            if attempt < max_retries:
                sleep(backoff_s * 2**attempt)
                continue
            detail = redact(f"{type(exc).__name__}: {exc}", secrets)
            raise ProviderUnavailableError(f"{source} unreachable ({detail})") from None
        if response.status_code in RETRY_STATUSES and attempt < max_retries:
            sleep(backoff_s * 2**attempt)
            continue
        return response
    raise AssertionError("unreachable")  # pragma: no cover
