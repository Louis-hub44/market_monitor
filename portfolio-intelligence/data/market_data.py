"""Orchestrateur de données de marché.

Chaîne de résolution (cf. §20) :

    Sources officielles  →  FMP  →  Yahoo  →  Cache

Toute valeur renvoyée est un `DataPoint` : la couche de calcul et l'UI savent
donc en permanence d'où vient le chiffre et quelle confiance lui accorder.
Le service tient également un **journal de collecte** consultable dans la page
Data Quality : quel appel, quelle source, quel niveau, quelle latence.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from config.runtime import clear_local_config, load_local_config, mask_key, save_local_config
from config.settings import get_settings
from data.network import check_connectivity, describe as describe_network
from data.providers.base import ProviderUnavailable
from data.providers.bloomberg import EQUITY_FIELDS, BloombergProvider, to_bloomberg_ticker
from data.providers.fmp import FMPProvider
from data.providers.yahoo import YahooProvider
from models.datapoint import DataPoint, missing, reconcile
from models.enums import DataQuality

log = logging.getLogger(__name__)


@dataclass
class FetchRecord:
    what: str
    source: str
    quality: DataQuality
    latency_ms: float
    note: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MarketDataService:
    def __init__(self) -> None:
        self.settings = get_settings()
        # Ordre de la chaîne : Bloomberg (source de référence du poste) puis
        # FMP puis Yahoo. Bloomberg n'a ni clé ni quota — il a des *droits*,
        # et un champ hors abonnement revient vide, d'où le repli systématique.
        self.bloomberg = BloombergProvider()
        self.fmp = FMPProvider()
        self.yahoo = YahooProvider()
        self.journal: list[FetchRecord] = []

        # État réseau courant : environnement d'abord, puis overrides persistés.
        self.insecure_ssl = self.settings.insecure_ssl
        self.ca_bundle = self.settings.ca_bundle
        self._apply_local_config()

    # ------------------------------------------------------------------ #
    # Configuration modifiable depuis l'interface
    # ------------------------------------------------------------------ #
    def _apply_local_config(self) -> None:
        config = load_local_config()
        if not config:
            return
        if config.get("fmp_api_key"):
            self.fmp.api_key_override = config["fmp_api_key"]
        if "insecure_ssl" in config or "ca_bundle" in config:
            self.set_network(
                bool(config.get("insecure_ssl", self.insecure_ssl)),
                config.get("ca_bundle", self.ca_bundle),
            )

    def set_api_key(self, key: str | None, persist: bool = False) -> None:
        """Applique une clé FMP saisie dans l'interface, sans redémarrage."""
        key = (key or "").strip() or None
        self.fmp.api_key_override = key
        self.fmp.last_error = None
        if persist:
            save_local_config(fmp_api_key=key or "")

    def set_network(self, insecure_ssl: bool, ca_bundle: str | None,
                    persist: bool = False) -> None:
        """Change la politique SSL à chaud sur tous les fournisseurs."""
        self.insecure_ssl = bool(insecure_ssl)
        self.ca_bundle = (ca_bundle or "").strip()
        for provider in (self.bloomberg, self.fmp, self.yahoo):
            provider.set_ssl_policy(self.insecure_ssl, self.ca_bundle or None)
        if persist:
            save_local_config(insecure_ssl=self.insecure_ssl, ca_bundle=self.ca_bundle)

    def forget_local_config(self) -> bool:
        """Efface les réglages persistés (la clé reste active jusqu'au redémarrage)."""
        return clear_local_config()

    def api_key_display(self) -> str:
        return mask_key(self.fmp.api_key)

    def api_key_source(self) -> str:
        if self.fmp.api_key_override:
            return "saisie dans l'interface"
        if self.settings.fmp_api_key:
            return "variable d'environnement (.env)"
        return "aucune"

    # ------------------------------------------------------------------ #
    def _record(self, what: str, source: str, quality: DataQuality, t0: float, note: str = "") -> None:
        self.journal.append(
            FetchRecord(what=what, source=source, quality=quality,
                        latency_ms=(time.perf_counter() - t0) * 1000, note=note)
        )
        if len(self.journal) > 500:
            del self.journal[:-500]

    @staticmethod
    def _quality(stale: bool, base: DataQuality = DataQuality.API_CONFIRMED) -> DataQuality:
        return DataQuality.CACHED if stale else base

    # ------------------------------------------------------------------ #
    # Prix
    # ------------------------------------------------------------------ #
    def price(self, symbol: str, cross_check: bool = False) -> DataPoint[float]:
        """Dernier cours. `cross_check=True` interroge deux sources et croise
        les résultats : deux valeurs concordantes donnent un niveau VERIFIED."""
        t0 = time.perf_counter()
        points: list[DataPoint[float]] = []

        if self.bloomberg.is_configured:
            try:
                raw = self.bloomberg.prices([to_bloomberg_ticker(symbol)])
                for fields in raw.values():
                    if fields.get("PX_LAST") is not None:
                        points.append(DataPoint(float(fields["PX_LAST"]),
                                                DataQuality.API_CONFIRMED, "BLOOMBERG",
                                                as_of=date.today(), note="PX_LAST"))
            except ProviderUnavailable as exc:
                log.info("Bloomberg price %s: %s", symbol, exc)

        try:
            q, stale = self.fmp.quote(symbol)
            if q and q.get("price") is not None:
                points.append(DataPoint(float(q["price"]), self._quality(stale), "FMP",
                                        as_of=date.today(), note="quote FMP"))
        except ProviderUnavailable as exc:
            log.info("FMP price %s: %s", symbol, exc)

        if cross_check or not points:
            try:
                p, stale = self.yahoo.last_price(symbol)
                if p is not None:
                    points.append(DataPoint(p, self._quality(stale), "YAHOO",
                                            as_of=date.today(), note="dernier close Yahoo"))
            except ProviderUnavailable as exc:
                log.info("Yahoo price %s: %s", symbol, exc)

        result = reconcile(points) if points else missing(note=f"Aucun cours disponible pour {symbol}")
        self._record(f"price:{symbol}", result.source, result.quality, t0, result.note)
        return result

    def prices(self, symbols: Iterable[str], cross_check: bool = False) -> dict[str, DataPoint[float]]:
        """Récupération parallèle — le nombre de workers est borné par la config."""
        syms = list(dict.fromkeys(symbols))
        out: dict[str, DataPoint[float]] = {}
        if not syms:
            return out
        with ThreadPoolExecutor(max_workers=min(self.settings.max_workers, len(syms))) as pool:
            futures = {pool.submit(self.price, s, cross_check): s for s in syms}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    out[sym] = fut.result()
                except Exception as exc:  # aucune position ne doit faire tomber la page
                    out[sym] = missing(note=f"Erreur de collecte : {exc}")
        return out

    def price_series(self, symbol: str, lookback_days: int = 750) -> DataPoint[list[dict[str, Any]]]:
        """Historique de clôtures, format [{date, close}] trié chronologiquement."""
        t0 = time.perf_counter()
        start = date.today() - timedelta(days=lookback_days)

        if self.bloomberg.is_configured:
            try:
                rows = self.bloomberg.history(to_bloomberg_ticker(symbol), start)
                if rows:
                    dp = DataPoint(rows, DataQuality.API_CONFIRMED, "BLOOMBERG",
                                   as_of=date.fromisoformat(rows[-1]["date"]),
                                   note=f"{len(rows)} observations (BDH quotidien)")
                    self._record(f"series:{symbol}", "BLOOMBERG", dp.quality, t0, dp.note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("Bloomberg series %s: %s", symbol, exc)

        for fetch, source in ((self.fmp.price_history, "FMP"), (self.yahoo.price_history, "YAHOO")):
            try:
                rows, stale = fetch(symbol, start)
                if rows:
                    norm = sorted(
                        (
                            {"date": str(r.get("date"))[:10],
                             "close": float(r.get("close", r.get("adjClose", 0)) or 0)}
                            for r in rows
                            if (r.get("close") or r.get("adjClose"))
                        ),
                        key=lambda r: r["date"],
                    )
                    dp = DataPoint(norm, self._quality(stale), source,
                                   as_of=date.fromisoformat(norm[-1]["date"]) if norm else None,
                                   note=f"{len(norm)} observations")
                    self._record(f"series:{symbol}", source, dp.quality, t0, dp.note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("%s series %s: %s", source, symbol, exc)

        dp = missing(note=f"Aucun historique disponible pour {symbol}")
        self._record(f"series:{symbol}", "NONE", dp.quality, t0, dp.note)
        return dp

    # ------------------------------------------------------------------ #
    # Courbes de taux
    # ------------------------------------------------------------------ #
    def treasury_curve(self, country: str = "US") -> DataPoint[dict[str, float]]:
        """Courbe souveraine la plus récente, en décimal ({'10Y': 0.0428}).

        Bloomberg couvre toutes les courbes déclarées dans
        `BLOOMBERG_CURVE_TICKERS` (US, Bund, OAT, BTP, Gilt, JGB). Sans
        Bloomberg, seule la courbe US est atteignable — c'était la limite
        signalée depuis le début du projet.

        Source de référence : le Trésor américain publie officiellement cette
        courbe ; FMP la redistribue. Une donnée redistribuée est marquée
        API_CONFIRMED, jamais VERIFIED.
        """
        t0 = time.perf_counter()
        mapping = {
            "month1": "1M", "month2": "2M", "month3": "3M", "month6": "6M",
            "year1": "1Y", "year2": "2Y", "year3": "3Y", "year5": "5Y",
            "year7": "7Y", "year10": "10Y", "year20": "20Y", "year30": "30Y",
        }
        if self.bloomberg.is_configured:
            try:
                curve = self.bloomberg.curve(country)
                if curve:
                    dp = DataPoint(curve, DataQuality.API_CONFIRMED, "BLOOMBERG",
                                   as_of=date.today(),
                                   note=f"Courbe {country} par tickers génériques Bloomberg")
                    self._record(f"curve:{country}", "BLOOMBERG", dp.quality, t0, dp.note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("Bloomberg curve %s: %s", country, exc)

        if country != "US":
            # Ni FMP ni Yahoo n'exposent les courbes non américaines.
            dp = missing(note=(f"Courbe {country} indisponible sans Bloomberg : FMP et Yahoo "
                               f"ne publient que la courbe US."))
            self._record(f"curve:{country}", "NONE", dp.quality, t0, dp.note)
            return dp

        try:
            rows, stale = self.fmp.treasury_rates()
            if rows:
                latest = sorted(rows, key=lambda r: str(r.get("date", "")))[-1]
                curve = {
                    tenor: float(latest[key]) / 100.0
                    for key, tenor in mapping.items()
                    if latest.get(key) not in (None, "")
                }
                if curve:
                    dp = DataPoint(curve, self._quality(stale), "FMP",
                                   as_of=str(latest.get("date"))[:10],
                                   note="Courbe US Treasury redistribuée par FMP")
                    self._record("curve:US", "FMP", dp.quality, t0, dp.note)
                    return dp
        except ProviderUnavailable as exc:
            log.info("FMP treasury: %s", exc)

        # Repli Yahoo : quatre points d'indices de rendement, interpolés.
        try:
            curve, stale = self.yahoo.treasury_curve()
            if curve:
                dp = DataPoint(
                    curve, DataQuality.ESTIMATED, "YAHOO", as_of=date.today(),
                    note=("Courbe reconstituée depuis 4 indices Yahoo (^IRX, ^FVX, ^TNX, ^TYX) "
                          "et interpolée — ordre de grandeur, pas une courbe de valorisation"),
                )
                self._record("curve:US", "YAHOO", dp.quality, t0, dp.note)
                return dp
        except ProviderUnavailable as exc:
            log.info("Yahoo curve: %s", exc)

        dp = missing(note="Courbe US indisponible — les scénarios de courbe seront désactivés")
        self._record("curve:US", "NONE", dp.quality, t0, dp.note)
        return dp

    # ------------------------------------------------------------------ #
    # Calendriers
    # ------------------------------------------------------------------ #
    def economic_calendar(self, days_ahead: int = 14) -> DataPoint[list[dict[str, Any]]]:
        """Calendrier macro : Bloomberg, sinon FMP, sinon rien.

        C'est le seul domaine où Yahoo n'offre aucun repli. Bloomberg le couvre
        entièrement, consensus compris, via les tickers de séries économiques.
        """
        t0 = time.perf_counter()
        today = date.today()

        if self.bloomberg.is_configured:
            try:
                rows = self.bloomberg.economic_calendar(today, today + timedelta(days=days_ahead))
                if rows:
                    note = f"{len(rows)} publications macro (consensus BN_SURVEY_MEDIAN)"
                    dp = DataPoint(rows, DataQuality.API_CONFIRMED, "BLOOMBERG",
                                   as_of=today, note=note)
                    self._record("calendar:macro", "BLOOMBERG", dp.quality, t0, note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("Bloomberg macro calendar: %s", exc)

        try:
            rows, stale = self.fmp.economic_calendar(today, today + timedelta(days=days_ahead))
            dp = DataPoint(rows or [], self._quality(stale), "FMP", as_of=today,
                           note=f"{len(rows or [])} publications macro")
            self._record("calendar:macro", "FMP", dp.quality, t0, dp.note)
            return dp
        except ProviderUnavailable as exc:
            dp = missing(note=(f"Calendrier macro indisponible : {exc}. Aucun repli possible : "
                               "Yahoo n'expose pas les publications de banques centrales "
                               "ni les statistiques macro."))
            self._record("calendar:macro", "NONE", dp.quality, t0, dp.note)
            return dp

    def earnings_calendar(self, days_ahead: int = 30,
                          symbols: list[str] | None = None) -> DataPoint[list[dict[str, Any]]]:
        """Calendrier de résultats.

        FMP fournit un calendrier global ; à défaut (endpoint hors plan, panne),
        Yahoo est interrogé **titre par titre** sur les `symbols` fournis — donc
        uniquement les lignes du portefeuille, ce qui borne le nombre d'appels.
        """
        t0 = time.perf_counter()
        today = date.today()
        end = today + timedelta(days=days_ahead)

        if self.bloomberg.is_configured and symbols:
            try:
                tickers = [to_bloomberg_ticker(s, "EQUITY") for s in symbols]
                rows = [r for r in self.bloomberg.earnings_dates(tickers)
                        if today.isoformat() <= r["date"] <= end.isoformat()]
                if rows:
                    note = f"{len(rows)} publications attendues (EXPECTED_REPORT_DT)"
                    dp = DataPoint(rows, DataQuality.API_CONFIRMED, "BLOOMBERG",
                                   as_of=today, note=note)
                    self._record("calendar:earnings", "BLOOMBERG", dp.quality, t0, note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("Bloomberg earnings calendar: %s", exc)

        try:
            rows, stale = self.fmp.earnings_calendar(today, end)
            if rows:
                dp = DataPoint(rows, self._quality(stale), "FMP", as_of=today,
                               note=f"{len(rows)} publications de résultats")
                self._record("calendar:earnings", "FMP", dp.quality, t0, dp.note)
                return dp
        except ProviderUnavailable as exc:
            log.info("FMP earnings calendar: %s", exc)

        if symbols:
            try:
                rows, stale = self.yahoo.earnings_calendar(symbols, today, end)
                note = (f"{len(rows)} publications reconstituées titre par titre "
                        f"sur {len(symbols)} lignes du portefeuille")
                dp = DataPoint(rows, self._quality(stale), "YAHOO", as_of=today, note=note)
                self._record("calendar:earnings", "YAHOO", dp.quality, t0, note)
                return dp
            except ProviderUnavailable as exc:
                log.info("Yahoo earnings calendar: %s", exc)

        dp = missing(note="Calendrier de résultats indisponible (FMP hors plan et Yahoo muet)")
        self._record("calendar:earnings", "NONE", dp.quality, t0, dp.note)
        return dp

    # ------------------------------------------------------------------ #
    # Données société : FMP puis Yahoo
    # ------------------------------------------------------------------ #
    def earnings(self, symbol: str, limit: int = 12) -> DataPoint[list[dict[str, Any]]]:
        """Publications de résultats, format commun aux deux sources."""
        t0 = time.perf_counter()
        if self.bloomberg.is_configured:
            try:
                rows = self.bloomberg.earnings_dates([to_bloomberg_ticker(symbol, "EQUITY")])
                if rows:
                    dp = DataPoint(rows, DataQuality.API_CONFIRMED, "BLOOMBERG",
                                   note="date attendue et consensus Bloomberg")
                    self._record(f"earnings:{symbol}", "BLOOMBERG", dp.quality, t0, dp.note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("Bloomberg earnings %s: %s", symbol, exc)

        for source, fetch in (("FMP", self.fmp.earnings), ("YAHOO", self.yahoo.earnings)):
            try:
                rows, stale = fetch(symbol, limit=limit)
                if rows:
                    quality = self._quality(stale)
                    note = f"{len(rows)} publications"
                    if source == "YAHOO":
                        note += " — chiffre d'affaires non fourni par Yahoo"
                    dp = DataPoint(rows, quality, source, note=note)
                    self._record(f"earnings:{symbol}", source, quality, t0, note)
                    return dp
            except ProviderUnavailable as exc:
                log.info("%s earnings %s: %s", source, symbol, exc)
        dp = missing(note=f"Aucune publication disponible pour {symbol}")
        self._record(f"earnings:{symbol}", "NONE", dp.quality, t0, dp.note)
        return dp

    def fundamentals(self, symbol: str) -> DataPoint[dict[str, Any]]:
        """Multiples et marges courants (FMP `ratios`+`key-metrics`, sinon Yahoo `info`)."""
        t0 = time.perf_counter()
        if self.bloomberg.is_configured:
            try:
                raw = self.bloomberg.equity_fundamentals([to_bloomberg_ticker(symbol, "EQUITY")])
                for fields in raw.values():
                    payload = {label: fields[code] for label, code in EQUITY_FIELDS.items()
                               if fields.get(code) is not None}
                    if payload:
                        dp = DataPoint(payload, DataQuality.API_CONFIRMED, "BLOOMBERG",
                                       note="ratios Bloomberg (PE_RATIO, BEST_PE_RATIO…)")
                        self._record(f"fundamentals:{symbol}", "BLOOMBERG", dp.quality, t0, dp.note)
                        return dp
            except ProviderUnavailable as exc:
                log.info("Bloomberg fundamentals %s: %s", symbol, exc)

        try:
            ratios, stale_a = self.fmp.ratios(symbol, period="annual", limit=1)
            metrics, stale_b = self.fmp.key_metrics(symbol, period="annual", limit=1)
            payload: dict[str, Any] = {}
            for row in list(ratios or []) + list(metrics or []):
                payload.update(row)
            if payload:
                quality = self._quality(stale_a or stale_b)
                dp = DataPoint(payload, quality, "FMP", note="ratios annuels FMP")
                self._record(f"fundamentals:{symbol}", "FMP", quality, t0, dp.note)
                return dp
        except ProviderUnavailable as exc:
            log.info("FMP fundamentals %s: %s", symbol, exc)

        try:
            payload, stale = self.yahoo.fundamentals(symbol)
            if payload:
                quality = self._quality(stale)
                dp = DataPoint(payload, quality, "YAHOO", note="multiples courants Yahoo (info)")
                self._record(f"fundamentals:{symbol}", "YAHOO", quality, t0, dp.note)
                return dp
        except ProviderUnavailable as exc:
            log.info("Yahoo fundamentals %s: %s", symbol, exc)

        dp = missing(note=f"Aucun ratio disponible pour {symbol}")
        self._record(f"fundamentals:{symbol}", "NONE", dp.quality, t0, dp.note)
        return dp

    def annual_accounts(self, symbol: str) -> DataPoint[list[dict[str, Any]]]:
        """EPS et CA annuels — base de reconstruction d'un historique de multiples."""
        t0 = time.perf_counter()
        try:
            rows, stale = self.yahoo.annual_eps_and_revenue(symbol)
            if rows:
                dp = DataPoint(rows, self._quality(stale), "YAHOO",
                               note=f"{len(rows)} exercices publiés")
                self._record(f"accounts:{symbol}", "YAHOO", dp.quality, t0, dp.note)
                return dp
        except ProviderUnavailable as exc:
            log.info("Yahoo accounts %s: %s", symbol, exc)
        return missing(note=f"Comptes annuels indisponibles pour {symbol}")

    # ------------------------------------------------------------------ #
    def bond_analytics(self, positions) -> dict[str, dict[str, Any]]:
        """Sensibilités obligataires calculées par Bloomberg, par identifiant.

        Renvoie un dictionnaire vide si Bloomberg est absent : l'appelant
        retombe alors sur le calcul interne.
        """
        if not self.bloomberg.is_configured or not positions:
            return {}
        t0 = time.perf_counter()
        mapping = {
            to_bloomberg_ticker(p.identifier, p.asset_class.value): p.identifier
            for p in positions
        }
        try:
            raw = self.bloomberg.bond_analytics(list(mapping))
        except ProviderUnavailable as exc:
            log.info("Bloomberg bond analytics: %s", exc)
            return {}
        out = {mapping[ticker]: fields for ticker, fields in raw.items() if ticker in mapping}
        self._record(f"bond_analytics:{len(out)}", "BLOOMBERG",
                     DataQuality.API_CONFIRMED, t0, f"{len(out)}/{len(mapping)} lignes")
        return out

    def health(self) -> list[dict[str, Any]]:
        return [self.bloomberg.health(), self.fmp.health(), self.yahoo.health()]

    @property
    def ssl_blocked(self) -> bool:
        """Vrai si toutes les sources échouent sur la validation du certificat.

        C'est la signature d'un proxy d'inspection SSL d'entreprise : le
        problème est sur le poste, pas chez les fournisseurs.
        """
        return self.fmp.ssl_blocked and self.yahoo.ssl_blocked

    def network_diagnostic(self) -> dict[str, Any]:
        """Sonde les fournisseurs et qualifie la panne réseau."""
        return check_connectivity(self.fmp._session, timeout=self.settings.http_timeout_s)

    def network_summary(self) -> str:
        return describe_network(self.insecure_ssl, self.ca_bundle)

    def journal_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "horodatage": r.at.strftime("%H:%M:%S"),
                "donnée": r.what,
                "source": r.source,
                "qualité": r.quality.value,
                "latence_ms": round(r.latency_ms, 1),
                "commentaire": r.note,
            }
            for r in reversed(self.journal)
        ]


_service: MarketDataService | None = None


def get_market_data() -> MarketDataService:
    """Singleton applicatif (mis en cache par Streamlit via st.cache_resource)."""
    global _service
    if _service is None:
        _service = MarketDataService()
    return _service
