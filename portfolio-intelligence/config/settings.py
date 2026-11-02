"""Configuration applicative — lue depuis l'environnement, jamais depuis le code.

Aucune clé API n'apparaît dans le dépôt : `.env` est ignoré par git et
`.env.example` documente les variables attendues.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:  # dépendance optionnelle : l'app démarre sans, avec les variables système
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _network_defaults() -> tuple[bool, str]:
    """Réglages proxy/SSL lus depuis l'environnement (voir `data/network.py`)."""
    from data.network import env_defaults

    return env_defaults()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # --- providers -------------------------------------------------- #
    fmp_api_key: str | None = field(default_factory=lambda: os.getenv("FMP_API_KEY") or None)
    fmp_base_url: str = field(
        default_factory=lambda: os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com/stable")
    )
    fmp_rate_limit_per_min: int = field(default_factory=lambda: _int("FMP_RATE_LIMIT_PER_MIN", 250))
    enable_yahoo: bool = field(default_factory=lambda: _flag("ENABLE_YAHOO", True))

    # --- Bloomberg (Desktop API) ------------------------------------ #
    # Le Terminal doit tourner sur le poste et la session être ouverte : la DAPI
    # n'écoute qu'en local et hérite des droits de l'utilisateur connecté.
    enable_bloomberg: bool = field(default_factory=lambda: _flag("ENABLE_BLOOMBERG", True))
    bloomberg_host: str = field(default_factory=lambda: os.getenv("BLOOMBERG_HOST", "localhost"))
    bloomberg_port: int = field(default_factory=lambda: _int("BLOOMBERG_PORT", 8194))
    bloomberg_timeout_ms: int = field(default_factory=lambda: _int("BLOOMBERG_TIMEOUT_MS", 20_000))
    bloomberg_batch_size: int = field(default_factory=lambda: _int("BLOOMBERG_BATCH_SIZE", 100))
    http_timeout_s: int = field(default_factory=lambda: _int("HTTP_TIMEOUT_S", 15))
    http_retries: int = field(default_factory=lambda: _int("HTTP_RETRIES", 3))
    max_workers: int = field(default_factory=lambda: _int("MAX_WORKERS", 8))

    # --- réseau d'entreprise (proxy d'inspection SSL) --------------- #
    # Priorité au certificat CA : la vérification reste active. Le
    # contournement est un dépannage, pas une configuration cible.
    insecure_ssl: bool = field(default_factory=lambda: _network_defaults()[0])
    ca_bundle: str = field(default_factory=lambda: _network_defaults()[1])

    # --- cache ------------------------------------------------------ #
    cache_dir: Path = field(default_factory=lambda: Path(os.getenv("CACHE_DIR", ROOT / ".cache")))
    cache_ttl_quotes_s: int = field(default_factory=lambda: _int("CACHE_TTL_QUOTES_S", 300))
    cache_ttl_fundamentals_s: int = field(default_factory=lambda: _int("CACHE_TTL_FUNDAMENTALS_S", 86_400))
    cache_ttl_calendar_s: int = field(default_factory=lambda: _int("CACHE_TTL_CALENDAR_S", 3_600))
    offline_mode: bool = field(default_factory=lambda: _flag("OFFLINE_MODE", False))

    # --- application ------------------------------------------------ #
    base_currency: str = field(default_factory=lambda: os.getenv("BASE_CURRENCY", "EUR"))
    risk_free_rate_source: str = field(default_factory=lambda: os.getenv("RISK_FREE_SOURCE", "PROVIDER"))
    portfolios_dir: Path = field(default_factory=lambda: Path(os.getenv("PORTFOLIOS_DIR", ROOT / "samples")))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    @property
    def has_fmp(self) -> bool:
        return bool(self.fmp_api_key)

    @property
    def network_customised(self) -> bool:
        return bool(self.insecure_ssl or self.ca_bundle)

    def masked(self) -> dict[str, str]:
        """Vue de la configuration affichable dans l'UI (clé masquée)."""
        key = self.fmp_api_key
        return {
            "FMP_API_KEY": f"{key[:4]}…{key[-2:]}" if key else "non configurée",
            "FMP_BASE_URL": self.fmp_base_url,
            "ENABLE_YAHOO": str(self.enable_yahoo),
            "BLOOMBERG": (f"{self.bloomberg_host}:{self.bloomberg_port}"
                          if self.enable_bloomberg else "désactivé"),
            "OFFLINE_MODE": str(self.offline_mode),
            "PI_CA_BUNDLE": self.ca_bundle or "aucun",
            "PI_INSECURE_SSL": str(self.insecure_ssl),
            "BASE_CURRENCY": self.base_currency,
            "CACHE_DIR": str(self.cache_dir),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    return s
