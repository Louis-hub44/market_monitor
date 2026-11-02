"""Contrat commun aux fournisseurs de données.

Chaque provider :
* expose des méthodes qui renvoient soit des `DataPoint`, soit `None`
  (jamais une valeur inventée) ;
* est isolé : une exception réseau est convertie en `ProviderUnavailable`
  et remonte à l'orchestrateur, qui bascule sur la source suivante.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

import requests

from config.settings import get_settings
from data.cache import DiskCache
from data.network import configure_session, looks_like_ssl_error

log = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    """Le provider n'a pas pu répondre (réseau, quota, authentification)."""


class RateLimiter:
    """Limiteur simple à fenêtre glissante d'une minute, thread-safe."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max(1, max_per_minute)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= self.max_per_minute:
                sleep_for = 60 - (now - self._calls[0]) + 0.05
                log.warning("Rate limit atteint, pause de %.1fs", sleep_for)
                time.sleep(max(0.0, sleep_for))
                self._calls.clear()
            self._calls.append(time.time())


class BaseProvider(ABC):
    name: str = "BASE"

    def __init__(self, rate_limit_per_min: int = 120) -> None:
        self.settings = get_settings()
        self.cache = DiskCache(namespace=self.name.lower())
        self.limiter = RateLimiter(rate_limit_per_min)
        # La session porte la politique SSL du poste : derrière un proxy
        # d'inspection d'entreprise, c'est ici que le certificat interne est
        # reconnu (cf. data/network.py).
        self._session = configure_session(
            requests.Session(),
            insecure_ssl=self.settings.insecure_ssl,
            ca_bundle=self.settings.ca_bundle,
        )
        self._session.headers.update({"User-Agent": "portfolio-intelligence/0.1"})
        self.last_error: str | None = None
        self.ssl_blocked: bool = False

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Le provider dispose-t-il de tout ce qu'il lui faut pour appeler l'API ?"""

    # ------------------------------------------------------------------ #
    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if self.settings.offline_mode:
            raise ProviderUnavailable(f"{self.name}: mode hors ligne activé")

        last_exc: Exception | None = None
        for attempt in range(self.settings.http_retries):
            self.limiter.acquire()
            try:
                r = self._session.get(url, params=params, timeout=self.settings.http_timeout_s)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    last_exc = ProviderUnavailable(f"{self.name}: quota dépassé (429)")
                    continue
                if r.status_code in (401, 403):
                    raise ProviderUnavailable(f"{self.name}: authentification refusée ({r.status_code})")
                r.raise_for_status()
                self.last_error = None
                return r.json()
            except ProviderUnavailable:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                time.sleep(0.4 * (attempt + 1))

        self.last_error = str(last_exc)
        # Un échec de certificat n'est pas une panne du fournisseur : c'est le
        # réseau du poste. On le distingue pour que l'interface propose le
        # correctif au lieu d'accuser la source.
        self.ssl_blocked = looks_like_ssl_error(last_exc)
        if self.ssl_blocked:
            raise ProviderUnavailable(
                f"{self.name}: certificat SSL non validé — proxy d'inspection "
                f"d'entreprise probable (voir la page Qualité des données)"
            )
        raise ProviderUnavailable(f"{self.name}: {last_exc}")

    def cached_json(self, key: str, url: str, params: dict[str, Any] | None, ttl_s: int) -> tuple[Any, bool]:
        """Renvoie `(payload, from_cache_stale)`.

        Ordre : cache frais → appel réseau → cache périmé. Le second membre du
        tuple indique à l'appelant qu'il doit dégrader la qualité en CACHED.
        """
        fresh = self.cache.get(key, ttl_s)
        if fresh is not None:
            return fresh.value, False
        try:
            payload = self._get(url, params)
            self.cache.set(key, payload)
            return payload, False
        except ProviderUnavailable:
            stale = self.cache.get_stale(key)
            if stale is not None:
                log.warning("%s indisponible — repli sur cache périmé (%.0f h)", self.name, stale.age_s / 3600)
                return stale.value, True
            raise

    def set_ssl_policy(self, insecure_ssl: bool, ca_bundle: str | None) -> None:
        """Réapplique la politique SSL sans redémarrer l'application.

        La session est reconfigurée sur place et l'état d'erreur remis à zéro :
        un réglage corrigé doit permettre de retenter immédiatement.
        """
        configure_session(self._session, insecure_ssl=insecure_ssl, ca_bundle=ca_bundle)
        self._session.headers.update({"User-Agent": "portfolio-intelligence/0.1"})
        self.last_error = None
        self.ssl_blocked = False

    def health(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.is_configured,
            "last_error": self.last_error,
            "ssl_blocked": self.ssl_blocked,
            "cache": self.cache.stats(),
        }
