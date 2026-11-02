"""Provider Yahoo Finance — source secondaire **et** filet de secours FMP.

`yfinance` est une dépendance optionnelle : si elle n'est pas installée, le
provider se déclare simplement non configuré et l'orchestrateur passe à la
source suivante.

Ce que Yahoo peut remplacer quand FMP est absent ou hors plan
------------------------------------------------------------
=========================  ==========================================================
Besoin                     Couverture Yahoo
=========================  ==========================================================
Cours et historiques       **Complète** — c'est la source de référence du recoupement.
Publications de résultats  **Bonne** — dates passées et future, EPS estimé et publié,
                           donc surprises calculables. Pas de calendrier global :
                           il faut boucler sur les titres du portefeuille.
Ratios de valorisation     **Partielle** — `Ticker.info` donne les multiples courants
                           (PER, PER estimé, P/B, EV/EBITDA…). L'historique est
                           reconstruit à partir des comptes annuels et des cours,
                           donc marqué ESTIMATED.
Courbe des taux US         **Partielle** — quatre points seulement (^IRX, ^FVX, ^TNX,
                           ^TYX), interpolés. Suffisant pour un ordre de grandeur de
                           duration, pas pour des KRD fines.
Dividendes                 **Complète**.
Screener                   **Partielle** — `yfinance.screen` existe mais l'univers et
                           les champs diffèrent de FMP ; pas de parité de filtres.
Calendrier macro           **Aucune.** Fed, BCE, CPI, NFP ne sont pas exposés. Sans
                           FMP, l'Event Radar se limite aux résultats de sociétés.
=========================  ==========================================================

Toute donnée reconstruite (multiples historiques, courbe interpolée) est
marquée ESTIMATED : elle sert à travailler, pas à publier une VL.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from data.network import apply_to_yfinance, looks_like_ssl_error
from data.providers.base import BaseProvider, ProviderUnavailable

try:
    import yfinance as yf

    _YF_AVAILABLE = True
except Exception:  # pragma: no cover
    yf = None
    _YF_AVAILABLE = False


#: Indices de rendement souverain US publiés par Yahoo (en pourcentage).
#: ^IRX = 13 semaines, ^FVX = 5 ans, ^TNX = 10 ans, ^TYX = 30 ans.
US_YIELD_INDICES: dict[str, str] = {"3M": "^IRX", "5Y": "^FVX", "10Y": "^TNX", "30Y": "^TYX"}

#: Clés `Ticker.info` correspondant aux multiples suivis par la plateforme.
INFO_MULTIPLES: dict[str, str] = {
    "PER": "trailingPE",
    "PER estimé": "forwardPE",
    "P/B": "priceToBook",
    "P/S": "priceToSalesTrailing12Months",
    "EV/EBITDA": "enterpriseToEbitda",
    "EV/Sales": "enterpriseToRevenue",
    "Dividend Yield": "dividendYield",
    "PEG": "trailingPegRatio",
    "ROE": "returnOnEquity",
    "ROA": "returnOnAssets",
    "Marge brute": "grossMargins",
    "Marge EBITDA": "ebitdaMargins",
    "Marge opérationnelle": "operatingMargins",
    "Marge nette": "profitMargins",
    "Croissance CA": "revenueGrowth",
    "Croissance EPS": "earningsGrowth",
}


class YahooProvider(BaseProvider):
    name = "YAHOO"

    def __init__(self) -> None:
        super().__init__(rate_limit_per_min=60)
        # yfinance reçoit la session unique du terminal (proxy, magasin de
        # certificats) au lieu de son client curl_cffi natif : aucune requête
        # ne doit échapper à la politique réseau.
        self.yf_session = self._session
        self.yf_session_applied = apply_to_yfinance(self.yf_session)

    def set_ssl_policy(self, ca_bundle: str | None) -> None:
        """Rebranche yfinance sur la session du nouveau certificat."""
        super().set_ssl_policy(ca_bundle)
        self.yf_session = self._session
        self.yf_session_applied = apply_to_yfinance(self.yf_session)

    @property
    def is_configured(self) -> bool:
        return _YF_AVAILABLE and self.settings.enable_yahoo and not self.settings.offline_mode

    def health(self) -> dict:
        info = super().health()
        info["session"] = ("session du terminal appliquée" if self.yf_session_applied
                           else "session du terminal non appliquée à yfinance")
        return info

    def _guard(self) -> None:
        if not _YF_AVAILABLE:
            raise ProviderUnavailable("Yahoo: paquet yfinance non installé")
        if not self.settings.enable_yahoo:
            raise ProviderUnavailable("Yahoo: désactivé (ENABLE_YAHOO=false)")
        if self.settings.offline_mode:
            raise ProviderUnavailable("Yahoo: mode hors ligne activé")

    def last_price(self, symbol: str) -> tuple[float | None, bool]:
        self._guard()
        key = f"yahoo:last:{symbol}"
        cached = self.cache.get(key, self.settings.cache_ttl_quotes_s)
        if cached is not None:
            return cached.value, False
        try:
            self.limiter.acquire()
            hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
            if hist is None or hist.empty:
                return None, False
            price = float(hist["Close"].dropna().iloc[-1])
            self.cache.set(key, price)
            return price, False
        except Exception as exc:  # yfinance lève des exceptions très variées
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            self.ssl_blocked = looks_like_ssl_error(exc)
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Filet de secours FMP
    # ------------------------------------------------------------------ #
    def treasury_curve(self) -> tuple[dict[str, float] | None, bool]:
        """Courbe US reconstituée depuis les indices de rendement Yahoo.

        Quatre points seulement : c'est une approximation assumée, destinée à
        garder les scénarios de taux opérationnels quand FMP est indisponible.
        """
        self._guard()
        key = "yahoo:curve:us"
        cached = self.cache.get(key, self.settings.cache_ttl_calendar_s)
        if cached is not None:
            return cached.value, False
        curve: dict[str, float] = {}
        try:
            for tenor, ticker in US_YIELD_INDICES.items():
                self.limiter.acquire()
                hist = yf.Ticker(ticker).history(period="5d")
                if hist is None or hist.empty:
                    continue
                curve[tenor] = float(hist["Close"].dropna().iloc[-1]) / 100.0
        except Exception as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            self.ssl_blocked = looks_like_ssl_error(exc)
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc
        if len(curve) < 2:
            return None, False
        self.cache.set(key, curve)
        return curve, False

    def earnings(self, symbol: str, limit: int = 12) -> tuple[list[dict[str, Any]], bool]:
        """Publications au format FMP : date, epsEstimated, eps, revenue…

        `Ticker.earnings_dates` couvre passé et futur, ce qui permet de servir
        à la fois l'historique des surprises et la prochaine date.
        """
        self._guard()
        key = f"yahoo:earnings:{symbol}:{limit}"
        cached = self.cache.get(key, self.settings.cache_ttl_calendar_s)
        if cached is not None:
            return cached.value, False
        try:
            self.limiter.acquire()
            frame = yf.Ticker(symbol).earnings_dates
        except Exception as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            self.ssl_blocked = looks_like_ssl_error(exc)
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc
        if frame is None or getattr(frame, "empty", True):
            return [], False

        def _num(row, *names):
            for name in names:
                if name in row and row[name] == row[name]:   # écarte les NaN
                    return float(row[name])
            return None

        rows: list[dict[str, Any]] = []
        for index, row in frame.head(limit * 2).iterrows():
            rows.append({
                "symbol": symbol,
                "date": index.date().isoformat() if hasattr(index, "date") else str(index)[:10],
                "epsEstimated": _num(row, "EPS Estimate"),
                "eps": _num(row, "Reported EPS"),
                "revenueEstimated": None,
                "revenue": None,
            })
        self.cache.set(key, rows)
        return rows, False

    def earnings_calendar(self, symbols: list[str], start: date,
                          end: date) -> tuple[list[dict[str, Any]], bool]:
        """Calendrier de résultats reconstruit titre par titre.

        Yahoo n'expose pas de calendrier global : on interroge uniquement les
        lignes du portefeuille, ce qui est de toute façon le seul périmètre
        utile ici — et borne le nombre d'appels.
        """
        rows: list[dict[str, Any]] = []
        degraded = False
        for symbol in symbols:
            try:
                records, stale = self.earnings(symbol, limit=4)
                degraded = degraded or stale
            except ProviderUnavailable:
                continue
            for record in records:
                try:
                    when = date.fromisoformat(record["date"])
                except (TypeError, ValueError):
                    continue
                if start <= when <= end:
                    rows.append(record)
        return rows, degraded

    def fundamentals(self, symbol: str) -> tuple[dict[str, Any], bool]:
        """Multiples et marges courants, extraits de `Ticker.info`."""
        self._guard()
        key = f"yahoo:info:{symbol}"
        cached = self.cache.get(key, self.settings.cache_ttl_fundamentals_s)
        if cached is not None:
            return cached.value, False
        try:
            self.limiter.acquire()
            info = yf.Ticker(symbol).info or {}
        except Exception as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            self.ssl_blocked = looks_like_ssl_error(exc)
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc
        payload = {
            label: float(info[field])
            for label, field in INFO_MULTIPLES.items()
            if isinstance(info.get(field), (int, float))
        }
        for extra in ("sector", "industry", "country", "currency", "longName", "marketCap"):
            if info.get(extra) is not None:
                payload[extra] = info[extra]
        self.cache.set(key, payload)
        return payload, False

    def annual_eps_and_revenue(self, symbol: str) -> tuple[list[dict[str, Any]], bool]:
        """EPS dilué et chiffre d'affaires annuels, issus des comptes publiés.

        Sert à reconstruire un historique de PER (cours à la date de clôture
        divisé par l'EPS de l'exercice) quand FMP n'est pas disponible.
        """
        self._guard()
        key = f"yahoo:annual:{symbol}"
        cached = self.cache.get(key, self.settings.cache_ttl_fundamentals_s)
        if cached is not None:
            return cached.value, False
        try:
            self.limiter.acquire()
            stmt = yf.Ticker(symbol).income_stmt
        except Exception as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            self.ssl_blocked = looks_like_ssl_error(exc)
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc
        if stmt is None or getattr(stmt, "empty", True):
            return [], False

        def _row(names: tuple[str, ...], column) -> float | None:
            for name in names:
                if name in stmt.index:
                    value = stmt.loc[name, column]
                    if value == value and value is not None:
                        return float(value)
            return None

        rows = []
        for column in stmt.columns:
            eps = _row(("Diluted EPS", "Basic EPS"), column)
            revenue = _row(("Total Revenue", "Operating Revenue"), column)
            if eps is None and revenue is None:
                continue
            rows.append({
                "date": column.date().isoformat() if hasattr(column, "date") else str(column)[:10],
                "eps": eps,
                "revenue": revenue,
            })
        rows.sort(key=lambda r: r["date"])
        self.cache.set(key, rows)
        return rows, False

    def dividends(self, symbol: str, limit: int = 20) -> tuple[list[dict[str, Any]], bool]:
        self._guard()
        key = f"yahoo:div:{symbol}"
        cached = self.cache.get(key, self.settings.cache_ttl_fundamentals_s)
        if cached is not None:
            return cached.value, False
        try:
            self.limiter.acquire()
            series = yf.Ticker(symbol).dividends
        except Exception as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc
        if series is None or len(series) == 0:
            return [], False
        rows = [{"date": idx.date().isoformat(), "dividend": float(value)}
                for idx, value in series.tail(limit).items()]
        self.cache.set(key, rows)
        return rows, False

    def price_history(self, symbol: str, start: date, end: date | None = None) -> tuple[list[dict[str, Any]], bool]:
        self._guard()
        key = f"yahoo:hist:{symbol}:{start}:{end}"
        cached = self.cache.get(key, self.settings.cache_ttl_quotes_s * 12)
        if cached is not None:
            return cached.value, False
        try:
            self.limiter.acquire()
            hist = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
            if hist is None or hist.empty:
                return [], False
            rows = [
                {"date": idx.date().isoformat(), "close": float(row["Close"])}
                for idx, row in hist.iterrows()
                if row.get("Close") == row.get("Close")  # écarte les NaN
            ]
            self.cache.set(key, rows)
            return rows, False
        except Exception as exc:
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale.value, True
            self.ssl_blocked = looks_like_ssl_error(exc)
            raise ProviderUnavailable(f"Yahoo: {exc}") from exc
