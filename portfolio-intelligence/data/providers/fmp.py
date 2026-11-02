"""Provider Financial Modeling Prep (API `stable`).

La clé est lue dans `FMP_API_KEY` (voir `config/settings.py`) et n'apparaît
jamais dans le code ni dans les journaux.

Les chemins d'endpoints sont regroupés dans `ENDPOINTS` : si FMP fait évoluer
son API, un seul dictionnaire est à mettre à jour. `self_test()` permet de
vérifier depuis la page Data Quality quels endpoints répondent réellement
avec le plan souscrit (certains sont réservés aux offres payantes).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from config.settings import get_settings
from data.providers.base import BaseProvider, ProviderUnavailable

ENDPOINTS: dict[str, str] = {
    "profile": "profile",
    "quote": "quote",
    "historical_price": "historical-price-eod/full",
    "ratios": "ratios",
    "ratios_ttm": "ratios-ttm",
    "key_metrics": "key-metrics",
    "income_statement": "income-statement",
    "cash_flow": "cash-flow-statement",
    "enterprise_values": "enterprise-values",
    "analyst_estimates": "analyst-estimates",
    "earnings": "earnings",
    "earnings_calendar": "earnings-calendar",
    "dividends": "dividends",
    "screener": "company-screener",
    "treasury_rates": "treasury-rates",
    "economic_calendar": "economics-calendar",
    "economic_indicators": "economics-indicators",
}


class FMPProvider(BaseProvider):
    name = "FMP"

    def __init__(self) -> None:
        settings = get_settings()
        super().__init__(rate_limit_per_min=settings.fmp_rate_limit_per_min)
        self.base_url = settings.fmp_base_url.rstrip("/")
        # Clé saisie dans l'interface : prioritaire sur `.env`, jamais journalisée.
        self.api_key_override: str | None = None

    @property
    def api_key(self) -> str | None:
        return self.api_key_override or self.settings.fmp_api_key

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ #
    def _call(self, endpoint_key: str, params: dict[str, Any] | None = None, ttl_s: int | None = None):
        if not self.is_configured:
            raise ProviderUnavailable(
                "FMP: clé API absente — renseignez-la dans le panneau latéral "
                "ou via FMP_API_KEY dans .env"
            )
        path = ENDPOINTS[endpoint_key]
        params = dict(params or {})
        # La clé n'entre pas dans l'empreinte de cache (elle ne doit pas être
        # écrite sur disque), mais un changement de clé peut changer les droits :
        # `verify_key()` vide le cache en cas de bascule.
        cache_key = f"{path}?{sorted(params.items())}"
        params["apikey"] = self.api_key
        ttl = ttl_s if ttl_s is not None else self.settings.cache_ttl_fundamentals_s
        payload, stale = self.cached_json(cache_key, f"{self.base_url}/{path}", params, ttl)
        if isinstance(payload, dict) and payload.get("Error Message"):
            raise ProviderUnavailable(f"FMP: {payload['Error Message']}")
        return payload, stale

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #
    def profile(self, symbol: str) -> tuple[dict | None, bool]:
        data, stale = self._call("profile", {"symbol": symbol})
        return (data[0] if isinstance(data, list) and data else None), stale

    def quote(self, symbol: str) -> tuple[dict | None, bool]:
        data, stale = self._call("quote", {"symbol": symbol}, ttl_s=self.settings.cache_ttl_quotes_s)
        return (data[0] if isinstance(data, list) and data else None), stale

    def price_history(self, symbol: str, start: date, end: date | None = None) -> tuple[list[dict], bool]:
        end = end or date.today()
        data, stale = self._call(
            "historical_price",
            {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
            ttl_s=self.settings.cache_ttl_quotes_s * 12,
        )
        if isinstance(data, dict):
            data = data.get("historical", [])
        return (data or []), stale

    def ratios(self, symbol: str, period: str = "annual", limit: int = 10) -> tuple[list[dict], bool]:
        data, stale = self._call("ratios", {"symbol": symbol, "period": period, "limit": limit})
        return (data or []), stale

    def key_metrics(self, symbol: str, period: str = "annual", limit: int = 10) -> tuple[list[dict], bool]:
        data, stale = self._call("key_metrics", {"symbol": symbol, "period": period, "limit": limit})
        return (data or []), stale

    def income_statement(self, symbol: str, period: str = "annual", limit: int = 8) -> tuple[list[dict], bool]:
        data, stale = self._call("income_statement", {"symbol": symbol, "period": period, "limit": limit})
        return (data or []), stale

    def analyst_estimates(self, symbol: str, period: str = "annual", limit: int = 8) -> tuple[list[dict], bool]:
        data, stale = self._call("analyst_estimates", {"symbol": symbol, "period": period, "limit": limit})
        return (data or []), stale

    def earnings(self, symbol: str, limit: int = 12) -> tuple[list[dict], bool]:
        data, stale = self._call("earnings", {"symbol": symbol, "limit": limit},
                                 ttl_s=self.settings.cache_ttl_calendar_s)
        return (data or []), stale

    def dividends(self, symbol: str, limit: int = 20) -> tuple[list[dict], bool]:
        data, stale = self._call("dividends", {"symbol": symbol, "limit": limit})
        return (data or []), stale

    def screener(self, **filters: Any) -> tuple[list[dict], bool]:
        params = {k: v for k, v in filters.items() if v not in (None, "", [])}
        params.setdefault("limit", 250)
        data, stale = self._call("screener", params, ttl_s=self.settings.cache_ttl_calendar_s)
        return (data or []), stale

    # ------------------------------------------------------------------ #
    # Calendriers et macro
    # ------------------------------------------------------------------ #
    def earnings_calendar(self, start: date, end: date) -> tuple[list[dict], bool]:
        data, stale = self._call(
            "earnings_calendar",
            {"from": start.isoformat(), "to": end.isoformat()},
            ttl_s=self.settings.cache_ttl_calendar_s,
        )
        return (data or []), stale

    def economic_calendar(self, start: date, end: date) -> tuple[list[dict], bool]:
        data, stale = self._call(
            "economic_calendar",
            {"from": start.isoformat(), "to": end.isoformat()},
            ttl_s=self.settings.cache_ttl_calendar_s,
        )
        return (data or []), stale

    def treasury_rates(self, start: date | None = None, end: date | None = None) -> tuple[list[dict], bool]:
        end = end or date.today()
        start = start or (end - timedelta(days=10))
        data, stale = self._call(
            "treasury_rates",
            {"from": start.isoformat(), "to": end.isoformat()},
            ttl_s=self.settings.cache_ttl_calendar_s,
        )
        return (data or []), stale

    # ------------------------------------------------------------------ #
    def verify_key(self, symbol: str = "AAPL") -> tuple[bool, str]:
        """Teste la clé courante par un appel réel. Renvoie (valide, message)."""
        if not self.is_configured:
            return False, "Aucune clé renseignée."
        try:
            self.cache.clear()          # une nouvelle clé peut ouvrir d'autres droits
            payload, stale = self.profile(symbol)
        except ProviderUnavailable as exc:
            message = str(exc)
            if "authentification refusée" in message or "401" in message or "403" in message:
                return False, "Clé refusée par FMP (401/403). Vérifiez la saisie."
            return False, message
        if not payload:
            return False, ("Réponse vide : la clé est acceptée mais l'endpoint n'est "
                           "pas couvert par votre plan.")
        name = payload.get("companyName") or symbol
        return True, f"Clé valide — {name} récupéré depuis FMP" + (" (cache)" if stale else "")

    def self_test(self, symbol: str = "AAPL") -> dict[str, str]:
        """Teste chaque endpoint et renvoie son statut — utilisé par la page Data Quality."""
        results: dict[str, str] = {}
        probes = {
            "profile": lambda: self.profile(symbol),
            "quote": lambda: self.quote(symbol),
            "ratios": lambda: self.ratios(symbol, limit=1),
            "key_metrics": lambda: self.key_metrics(symbol, limit=1),
            "analyst_estimates": lambda: self.analyst_estimates(symbol, limit=1),
            "earnings": lambda: self.earnings(symbol, limit=1),
            "treasury_rates": lambda: self.treasury_rates(),
            "economic_calendar": lambda: self.economic_calendar(date.today(), date.today() + timedelta(days=7)),
        }
        for key, fn in probes.items():
            try:
                payload, stale = fn()
                if not payload:
                    results[key] = "vide (endpoint indisponible avec ce plan ?)"
                else:
                    results[key] = "cache périmé" if stale else "ok"
            except ProviderUnavailable as exc:
                results[key] = f"indisponible — {exc}"
        return results
