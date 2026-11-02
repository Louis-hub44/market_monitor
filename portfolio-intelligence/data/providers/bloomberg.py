"""Provider Bloomberg (Desktop API).

Prérequis, à vérifier avant de chercher un bug dans ce fichier
-------------------------------------------------------------
1. Le **Terminal doit tourner** sur le poste, session ouverte. La Desktop API
   n'écoute qu'en local (`localhost:8194`) et hérite des droits de
   l'utilisateur connecté : il n'y a ni clé ni jeton à configurer.
2. `blpapi` **ne s'installe pas depuis PyPI**. Il faut l'index Bloomberg :

       pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple blpapi

   La bibliothèque C++ correspondante doit être présente et sur le `PATH`
   (variable `BLPAPI_ROOT` sous Windows).
3. Les droits de données sont ceux de l'abonnement. Un champ auquel le poste
   n'a pas droit revient vide, pas en erreur : `self_test()` sert précisément
   à distinguer les deux.

Conception
----------
Le protocole blpapi est verbeux et impossible à exécuter hors Terminal. Le
module sépare donc strictement :

* les **fonctions pures de transformation** (`parse_reference_rows`,
  `parse_historical_rows`, `to_bloomberg_ticker`…), testables sans Bloomberg et
  couvertes par la suite de tests ;
* la **couche transport** (`_send`), qui ouvre la session, envoie la requête et
  collecte les messages — non testable hors poste équipé, réduite au minimum.

Quand le Terminal est absent, le provider se déclare non configuré et la chaîne
de fournisseurs bascule sur FMP puis Yahoo sans que rien ne casse.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from config.constants import BLOOMBERG_CURVE_TICKERS, BLOOMBERG_ECO_TICKERS, KEY_RATE_TENORS
from config.settings import get_settings
from data.providers.base import BaseProvider, ProviderUnavailable

log = logging.getLogger(__name__)

try:  # pragma: no cover - dépend du poste
    import blpapi

    _BLPAPI_AVAILABLE = True
except Exception:  # noqa: BLE001
    blpapi = None
    _BLPAPI_AVAILABLE = False


# --------------------------------------------------------------------- #
# Champs
# --------------------------------------------------------------------- #
#: Référentiel et cours, communs à toutes les classes d'actif.
CORE_FIELDS: tuple[str, ...] = ("PX_LAST", "CRNCY", "NAME", "SECURITY_TYP", "LAST_UPDATE_DT")

#: Analytique obligataire. Bloomberg calcule ces sensibilités sur sa propre
#: courbe et son modèle d'option : quand elles sont disponibles, elles priment
#: sur notre calcul interne (voir `services/portfolio/enrichment.py`).
BOND_FIELDS: tuple[str, ...] = (
    "PX_DIRTY_MID", "YLD_YTM_MID", "YAS_MOD_DUR", "DUR_ADJ_MID", "CONVEXITY_MID",
    "YAS_RISK", "Z_SPRD_MID", "OAS_SPREAD_MID", "SPREAD_DUR_MID",
    "CPN", "MATURITY", "CPN_FREQ", "RTG_SP", "RTG_MOODY", "COUNTRY_ISO",
    "AMT_OUTSTANDING", "NXT_CALL_DT", "SECURITY_DES",
)

#: Key rate durations exposées par Bloomberg, par tenor de notre grille.
KRD_FIELDS: dict[str, str] = {
    "3M": "KEY_RATE_DUR_3MO", "1Y": "KEY_RATE_DUR_1YR", "2Y": "KEY_RATE_DUR_2YR",
    "5Y": "KEY_RATE_DUR_5YR", "10Y": "KEY_RATE_DUR_10YR", "20Y": "KEY_RATE_DUR_20YR",
    "30Y": "KEY_RATE_DUR_30YR",
}

#: Actions : valorisation, qualité, croissance — l'équivalent des ratios FMP.
EQUITY_FIELDS: dict[str, str] = {
    "PER": "PE_RATIO",
    "PER estimé": "BEST_PE_RATIO",
    "P/B": "PX_TO_BOOK_RATIO",
    "P/S": "PX_TO_SALES_RATIO",
    "EV/EBITDA": "CURRENT_EV_TO_T12M_EBITDA",
    "EV/Sales": "CURRENT_EV_TO_T12M_SALES",
    "FCF Yield": "FREE_CASH_FLOW_YIELD",
    "Dividend Yield": "EQY_DVD_YLD_IND",
    "PEG": "BEST_PEG_RATIO",
    "ROE": "RETURN_COM_EQY",
    "ROIC": "RETURN_ON_INV_CAPITAL",
    "ROA": "RETURN_ON_ASSET",
    "Marge brute": "GROSS_MARGIN",
    "Marge EBITDA": "EBITDA_MARGIN",
    "Marge opérationnelle": "OPER_MARGIN",
    "Marge nette": "PROF_MARGIN",
    "Croissance CA": "SALES_GROWTH",
    "Croissance EPS": "EPS_GROWTH",
    "Beta": "BETA_ADJ_OVERRIDABLE",
    "Capitalisation": "CUR_MKT_CAP",
    "Secteur": "GICS_SECTOR_NAME",
    "Industrie": "GICS_INDUSTRY_NAME",
    "Pays": "COUNTRY_ISO",
}

#: Publications de résultats : date attendue et consensus.
EARNINGS_FIELDS: tuple[str, ...] = (
    "EXPECTED_REPORT_DT", "LATEST_ANNOUNCEMENT_DT", "BEST_EPS", "BEST_SALES",
    "IS_EPS", "SALES_REV_TURN", "ANNOUNCEMENT_DT",
)

#: Calendrier macro : Bloomberg expose la prochaine date de publication et le
#: consensus sur le ticker de la série économique elle-même.
ECO_FIELDS: tuple[str, ...] = (
    "ECO_RELEASE_DT", "ECO_FUTURE_RELEASE_DATE_LIST", "BN_SURVEY_MEDIAN",
    "PX_LAST", "ECO_RELEASE_PERIOD", "SHORT_NAME",
)


# --------------------------------------------------------------------- #
# Fonctions pures — testables sans Terminal
# --------------------------------------------------------------------- #
def to_bloomberg_ticker(identifier: str, asset_class: str | None = None,
                        currency: str | None = None) -> str:
    """Convertit un identifiant de portefeuille en ticker Bloomberg.

    Un ISIN seul est ambigu : Bloomberg exige une « yellow key ». On la déduit
    de la classe d'actif quand elle est connue, et on passe par la syntaxe
    `/isin/` qui laisse Bloomberg résoudre la place de cotation.

    Un identifiant déjà complet (« MSFT US Equity », « GTDEM10Y Govt ») est
    renvoyé tel quel.
    """
    ticker = (identifier or "").strip()
    if not ticker:
        return ""

    known_keys = ("Equity", "Govt", "Corp", "Index", "Curncy", "Comdty", "Mtge", "Pfd")
    if any(ticker.endswith(f" {key}") for key in known_keys):
        return ticker
    if ticker.lower().startswith("/isin/"):
        return ticker

    is_isin = len(ticker) == 12 and ticker[:2].isalpha() and ticker[2:].isalnum()
    yellow_key = {
        "GOV_BOND": "Govt", "CREDIT": "Corp", "EQUITY": "Equity",
        "FUND": "Equity", "COMMODITY": "Comdty", "FX": "Curncy",
    }.get((asset_class or "").upper())

    if is_isin:
        # `/isin/` accepte un suffixe de yellow key ; sans elle Bloomberg choisit
        # le type le plus probable, ce qui suffit pour les obligations.
        return f"/isin/{ticker}" + (f" {yellow_key}" if yellow_key in ("Govt", "Corp") else "")
    if yellow_key:
        return f"{ticker} {yellow_key}"
    return f"{ticker} Equity"


def parse_reference_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalise une réponse ReferenceData en {ticker: {champ: valeur}}.

    Les champs vides et les erreurs de champ sont écartés silencieusement :
    dans une requête de 25 champs, un droit manquant sur l'un d'eux ne doit pas
    invalider les 24 autres.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker = row.get("security")
        if not ticker:
            continue
        fields = {
            key: value for key, value in (row.get("fields") or {}).items()
            if value is not None and value != ""
        }
        if fields:
            out[ticker] = fields
    return out


def parse_historical_rows(rows: list[dict[str, Any]], field: str = "PX_LAST") -> list[dict[str, Any]]:
    """Normalise une réponse HistoricalData au format commun {date, close}."""
    series: list[dict[str, Any]] = []
    for row in rows:
        raw_date = row.get("date")
        value = row.get(field, row.get("value"))
        if raw_date is None or value is None:
            continue
        if isinstance(raw_date, (date, datetime)):
            raw_date = raw_date.strftime("%Y-%m-%d")
        try:
            series.append({"date": str(raw_date)[:10], "close": float(value)})
        except (TypeError, ValueError):
            continue
    return sorted(series, key=lambda item: item["date"])


def extract_krd(fields: dict[str, Any]) -> dict[str, float]:
    """Key rate durations Bloomberg, remises sur la grille de tenors interne."""
    krd: dict[str, float] = {}
    for tenor in KEY_RATE_TENORS:
        raw = fields.get(KRD_FIELDS.get(tenor, ""))
        if raw is None:
            continue
        try:
            krd[tenor] = float(raw)
        except (TypeError, ValueError):
            continue
    return krd


def parse_eco_releases(ticker: str, fields: dict[str, Any], start: date,
                       end: date) -> list[dict[str, Any]]:
    """Transforme les champs d'une série économique en événements datés.

    `ECO_FUTURE_RELEASE_DATE_LIST` est un champ « bulk » : Bloomberg renvoie
    plusieurs lignes, une par publication à venir. On retient celles qui tombent
    dans la fenêtre demandée, et on retombe sur `ECO_RELEASE_DT` (prochaine
    date, champ scalaire) si la liste n'est pas accessible avec l'abonnement.
    """
    meta = BLOOMBERG_ECO_TICKERS.get(ticker, {})
    label = meta.get("label") or fields.get("SHORT_NAME") or ticker
    events: list[dict[str, Any]] = []

    def _as_date(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None

    raw_list = fields.get("ECO_FUTURE_RELEASE_DATE_LIST")
    candidates: list[Any] = []
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                candidates.extend(item.values())
            else:
                candidates.append(item)
    if not candidates and fields.get("ECO_RELEASE_DT") is not None:
        candidates = [fields["ECO_RELEASE_DT"]]

    for candidate in candidates:
        when = _as_date(candidate)
        if when is None or not (start <= when <= end):
            continue
        events.append({
            "date": when.isoformat(),
            "event": label,
            "country": meta.get("country"),
            "factor": meta.get("factor"),
            "estimate": fields.get("BN_SURVEY_MEDIAN"),
            "previous": fields.get("PX_LAST"),
            "period": fields.get("ECO_RELEASE_PERIOD"),
            "ticker": ticker,
        })
    return events


def parse_earnings_dates(ticker: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Prochaine publication de résultats, au format commun aux providers."""

    def _as_iso(value: Any) -> str | None:
        if isinstance(value, (date, datetime)):
            return value.strftime("%Y-%m-%d")
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except (TypeError, ValueError):
            return None

    when = _as_iso(fields.get("EXPECTED_REPORT_DT")) or _as_iso(fields.get("ANNOUNCEMENT_DT"))
    if not when:
        return None
    return {
        "symbol": ticker,
        "date": when,
        "epsEstimated": fields.get("BEST_EPS"),
        "eps": fields.get("IS_EPS"),
        "revenueEstimated": fields.get("BEST_SALES"),
        "revenue": fields.get("SALES_REV_TURN"),
    }


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    """Découpe une liste de titres en requêtes de taille raisonnable."""
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start:start + size]


# --------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------- #
class BloombergProvider(BaseProvider):
    """Accès Desktop API. Session ouverte paresseusement, réutilisée ensuite."""

    name = "BLOOMBERG"

    def __init__(self) -> None:
        super().__init__(rate_limit_per_min=600)
        settings = get_settings()
        self.host = settings.bloomberg_host
        self.port = settings.bloomberg_port
        self.timeout_ms = settings.bloomberg_timeout_ms
        self.batch_size = settings.bloomberg_batch_size
        # Attention au nommage : `BaseProvider._session` est la session HTTP
        # (requests). La session blpapi est un objet totalement différent, sur
        # une socket locale — lui donner le même nom écrasait la première et
        # cassait l'application de la politique SSL.
        self._bbg_session = None
        self._bbg_service = None

    # -- disponibilité ------------------------------------------------ #
    @property
    def library_available(self) -> bool:
        return _BLPAPI_AVAILABLE

    @property
    def is_configured(self) -> bool:
        return (_BLPAPI_AVAILABLE and self.settings.enable_bloomberg
                and not self.settings.offline_mode)

    def _guard(self) -> None:
        if not _BLPAPI_AVAILABLE:
            raise ProviderUnavailable(
                "Bloomberg : paquet blpapi absent. Installation depuis l'index Bloomberg "
                "(voir l'en-tête de data/providers/bloomberg.py)"
            )
        if not self.settings.enable_bloomberg:
            raise ProviderUnavailable("Bloomberg : désactivé (ENABLE_BLOOMBERG=false)")
        if self.settings.offline_mode:
            raise ProviderUnavailable("Bloomberg : mode hors ligne activé")

    # -- transport ---------------------------------------------------- #
    def _connect(self):  # pragma: no cover - nécessite un Terminal
        self._guard()
        if self._bbg_session is not None:
            return self._bbg_session
        options = blpapi.SessionOptions()
        options.setServerHost(self.host)
        options.setServerPort(self.port)
        session = blpapi.Session(options)
        if not session.start():
            raise ProviderUnavailable(
                f"Bloomberg : connexion refusée sur {self.host}:{self.port}. "
                "Le Terminal est-il lancé et la session ouverte ?"
            )
        if not session.openService("//blp/refdata"):
            session.stop()
            raise ProviderUnavailable("Bloomberg : service //blp/refdata indisponible")
        self._bbg_session = session
        self._bbg_service = session.getService("//blp/refdata")
        return session

    def _send(self, request) -> list[Any]:  # pragma: no cover - nécessite un Terminal
        """Envoie une requête et collecte les messages jusqu'à la réponse finale."""
        session = self._connect()
        session.sendRequest(request)
        messages: list[Any] = []
        while True:
            event = session.nextEvent(self.timeout_ms)
            for message in event:
                messages.append(message)
            if event.eventType() == blpapi.Event.RESPONSE:
                break
            if event.eventType() == blpapi.Event.TIMEOUT:
                raise ProviderUnavailable(
                    f"Bloomberg : délai dépassé ({self.timeout_ms} ms)"
                )
        return messages

    def reference(self, tickers: list[str], fields: list[str],
                  overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:  # pragma: no cover
        """ReferenceDataRequest (équivalent BDP), par lots."""
        self._guard()
        result: dict[str, dict[str, Any]] = {}
        for batch in chunked(list(dict.fromkeys(tickers)), self.batch_size):
            self.limiter.acquire()
            request = self._bbg_service.createRequest("ReferenceDataRequest")
            for ticker in batch:
                request.append("securities", ticker)
            for field in fields:
                request.append("fields", field)
            for key, value in (overrides or {}).items():
                override = request.getElement("overrides").appendElement()
                override.setElement("fieldId", key)
                override.setElement("value", value)

            rows: list[dict[str, Any]] = []
            for message in self._send(request):
                if not message.hasElement("securityData"):
                    continue
                data = message.getElement("securityData")
                for i in range(data.numValues()):
                    entry = data.getValueAsElement(i)
                    security = entry.getElementAsString("security")
                    if entry.hasElement("securityError"):
                        log.info("Bloomberg : %s inconnu ou sans droit", security)
                        continue
                    field_data = entry.getElement("fieldData")
                    fields_out: dict[str, Any] = {}
                    for j in range(field_data.numElements()):
                        element = field_data.getElement(j)
                        name = str(element.name())
                        try:
                            if element.isArray():
                                fields_out[name] = [
                                    {str(sub.getElement(k).name()): sub.getElement(k).getValue()
                                     for k in range(sub.numElements())}
                                    for sub in (element.getValueAsElement(v)
                                                for v in range(element.numValues()))
                                ]
                            else:
                                fields_out[name] = element.getValue()
                        except Exception:  # noqa: BLE001 - champ exotique
                            continue
                    rows.append({"security": security, "fields": fields_out})
            result.update(parse_reference_rows(rows))
        return result

    def history(self, ticker: str, start: date, end: date | None = None,
                field: str = "PX_LAST") -> list[dict[str, Any]]:  # pragma: no cover
        """HistoricalDataRequest (équivalent BDH), quotidien."""
        self._guard()
        end = end or date.today()
        self.limiter.acquire()
        request = self._bbg_service.createRequest("HistoricalDataRequest")
        request.append("securities", ticker)
        request.append("fields", field)
        request.set("startDate", start.strftime("%Y%m%d"))
        request.set("endDate", end.strftime("%Y%m%d"))
        request.set("periodicitySelection", "DAILY")
        request.set("nonTradingDayFillOption", "ACTIVE_DAYS_ONLY")

        rows: list[dict[str, Any]] = []
        for message in self._send(request):
            if not message.hasElement("securityData"):
                continue
            data = message.getElement("securityData")
            if data.hasElement("securityError"):
                return []
            series = data.getElement("fieldData")
            for i in range(series.numValues()):
                point = series.getValueAsElement(i)
                if not point.hasElement(field):
                    continue
                rows.append({"date": point.getElementAsDatetime("date").date(),
                             field: point.getElementAsFloat(field)})
        return parse_historical_rows(rows, field)

    # -- surface fonctionnelle --------------------------------------- #
    def prices(self, tickers: list[str]) -> dict[str, dict[str, Any]]:  # pragma: no cover
        return self.reference(tickers, list(CORE_FIELDS))

    def bond_analytics(self, tickers: list[str]) -> dict[str, dict[str, Any]]:  # pragma: no cover
        """Sensibilités calculées par Bloomberg : duration, convexité, KRD, spread."""
        return self.reference(tickers, list(BOND_FIELDS) + list(KRD_FIELDS.values()))

    def equity_fundamentals(self, tickers: list[str]) -> dict[str, dict[str, Any]]:  # pragma: no cover
        return self.reference(tickers, list(EQUITY_FIELDS.values()))

    def earnings_dates(self, tickers: list[str]) -> list[dict[str, Any]]:  # pragma: no cover
        raw = self.reference(tickers, list(EARNINGS_FIELDS))
        records = [parse_earnings_dates(ticker, fields) for ticker, fields in raw.items()]
        return [record for record in records if record]

    def economic_calendar(self, start: date, end: date,
                          tickers: list[str] | None = None) -> list[dict[str, Any]]:  # pragma: no cover
        """Calendrier macro, reconstruit depuis les séries économiques suivies.

        C'est la capacité qui n'a **aucun équivalent** chez FMP gratuit ni chez
        Yahoo : décisions de banques centrales, CPI, NFP, PIB, PMI, avec le
        consensus Bloomberg.
        """
        watchlist = tickers or list(BLOOMBERG_ECO_TICKERS)
        raw = self.reference(watchlist, list(ECO_FIELDS))
        events: list[dict[str, Any]] = []
        for ticker, fields in raw.items():
            events.extend(parse_eco_releases(ticker, fields, start, end))
        return sorted(events, key=lambda event: event["date"])

    def curve(self, country: str = "US") -> dict[str, float]:  # pragma: no cover
        """Courbe souveraine, en décimal, depuis les tickers génériques."""
        mapping = BLOOMBERG_CURVE_TICKERS.get(country.upper())
        if not mapping:
            return {}
        raw = self.reference(list(mapping.values()), ["PX_LAST"])
        curve: dict[str, float] = {}
        for tenor, ticker in mapping.items():
            value = (raw.get(ticker) or {}).get("PX_LAST")
            if value is None:
                continue
            try:
                curve[tenor] = float(value) / 100.0
            except (TypeError, ValueError):
                continue
        return curve

    # -- diagnostic ---------------------------------------------------- #
    def self_test(self, symbol: str = "IBM US Equity") -> dict[str, str]:  # pragma: no cover
        """Vérifie connexion et droits, capacité par capacité.

        Un champ sans droit revient vide plutôt qu'en erreur : ce test est le
        seul moyen fiable de savoir ce que l'abonnement du poste couvre.
        """
        results: dict[str, str] = {}
        if not _BLPAPI_AVAILABLE:
            return {"blpapi": "paquet absent — installer depuis l'index Bloomberg"}
        try:
            self._connect()
            results["connexion"] = f"ok ({self.host}:{self.port})"
        except ProviderUnavailable as exc:
            return {"connexion": str(exc)}

        probes = {
            "cours (PX_LAST)": lambda: self.reference([symbol], ["PX_LAST"]),
            "fondamentaux actions": lambda: self.equity_fundamentals([symbol]),
            "résultats attendus": lambda: self.earnings_dates([symbol]),
            "courbe US": lambda: self.curve("US"),
            "courbe Bund": lambda: self.curve("DE"),
            "calendrier macro": lambda: self.economic_calendar(
                date.today(), date.today() + timedelta(days=30)),
        }
        for label, probe in probes.items():
            try:
                payload = probe()
                results[label] = "ok" if payload else "vide (droits insuffisants ?)"
            except ProviderUnavailable as exc:
                results[label] = f"indisponible — {exc}"
            except Exception as exc:  # noqa: BLE001
                results[label] = f"erreur — {exc}"
        return results

    def set_ssl_policy(self, insecure_ssl: bool, ca_bundle: str | None) -> None:
        """Sans objet ici : la Desktop API est une socket locale, pas du HTTPS.

        Aucun proxy d'inspection d'entreprise ne s'interpose sur localhost:8194 —
        c'est d'ailleurs l'un des avantages de cette source sur un réseau filtré.
        """
        return None

    def health(self) -> dict[str, Any]:
        info = super().health()
        info["library"] = "blpapi présent" if _BLPAPI_AVAILABLE else "blpapi absent"
        info["endpoint"] = f"{self.host}:{self.port}"
        return info

    def close(self) -> None:  # pragma: no cover
        if self._bbg_session is not None:
            try:
                self._bbg_session.stop()
            finally:
                self._bbg_session = None
                self._bbg_service = None
