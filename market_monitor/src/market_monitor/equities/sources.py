"""Where compositions come from: iShares holdings files, FMP lists, market-cap weights.

* **iShares** publishes the daily holdings of its ETFs as CSV (English or German site): the
  physically replicating ETFs of the Euro Stoxx 50, Stoxx 600, DAX, FTSE MIB, S&P 500 and
  Nasdaq 100 give members, sectors (GICS) and weights close to the index's;
* **FMP** lists the S&P 500 and Nasdaq 100 members (no weights; not on the free plan);
* **Wikipedia** lists the S&P 500 and Nasdaq 100 members with their GICS sector (no weights):
  the free fallback when the iShares file or FMP is out of reach;
* the CAC 40 and the IBEX 35 have no free holdings file: their YAML is kept by hand and the
  weights can be estimated from the total market capitalisations (``weights: marketcap``:
  an approximation - the index weights by free float).
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

from market_monitor.equities.constituents import Composition, Member
from market_monitor.exceptions import DataProviderError, ProviderUnavailableError

#: (keywords in the exchange name, Yahoo suffix, ISO MIC), first match wins - Paris before "nyse"
EXCHANGES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("paris",), ".PA", "XPAR"),
    (("amsterdam",), ".AS", "XAMS"),
    (("brussels", "bruxelles"), ".BR", "XBRU"),
    (("lisbon",), ".LS", "XLIS"),
    (("dublin", "irish"), ".IR", "XDUB"),
    (("xetra", "frankfurt", "deutsche b"), ".DE", "XETR"),
    (("italiana", "milan"), ".MI", "XMIL"),
    (("madrid", "bme", "spanish"), ".MC", "XMAD"),
    (("london",), ".L", "XLON"),
    (("six swiss", "swiss", "zurich"), ".SW", "XSWX"),
    (("helsinki",), ".HE", "XHEL"),
    (("copenhagen",), ".CO", "XCSE"),
    (("stockholm", "omx nordic", "nasdaq omx nordic"), ".ST", "XSTO"),
    (("oslo",), ".OL", "XOSL"),
    (("wiener", "vienna"), ".VI", "XWBO"),
    (("warsaw",), ".WA", "XWAR"),
    (("nasdaq",), "", "XNAS"),
    (("new york", "nyse", "cboe", "bats"), "", "XNYS"),
)
#: tickers whose Yahoo symbol is not the iShares ticker with the exchange suffix
YAHOO_EXCEPTIONS = {"BRKB": "BRK-B", "BFB": "BF-B", "BRK.B": "BRK-B", "BF.B": "BF-B"}
SECTORS_FR = {
    "information technology": "Technologie", "it": "Technologie", "informationstechnologie": "Technologie",
    "financials": "Finance", "finanzwesen": "Finance", "finanzen": "Finance",
    "health care": "Santé", "healthcare": "Santé", "gesundheitsversorgung": "Santé",
    "consumer discretionary": "Consommation discrétionnaire",
    "zyklische konsumgüter": "Consommation discrétionnaire",
    "consumer staples": "Consommation de base", "nichtzyklische konsumgüter": "Consommation de base",
    "industrials": "Industrie", "industrie": "Industrie",
    "energy": "Énergie", "energie": "Énergie",
    "materials": "Matériaux", "materialien": "Matériaux", "basic materials": "Matériaux",
    "communication": "Communication", "communication services": "Communication",
    "kommunikation": "Communication", "telekommunikation": "Communication",
    "utilities": "Services aux collectivités", "versorger": "Services aux collectivités",
    "versorgungsbetriebe": "Services aux collectivités",
    "real estate": "Immobilier", "immobilien": "Immobilier",
    "telecommunications": "Communication",
    "basic resources": "Matériaux", "chemicals": "Matériaux",
    "consumer cyclical": "Consommation discrétionnaire", "consumer defensive": "Consommation de base",
    "technology": "Technologie", "financial services": "Finance",
}
_HEADER_ALIASES = {
    "ticker": ("ticker", "emittententicker", "issuer ticker"),
    "name": ("name",),
    "sector": ("sector", "sektor"),
    "asset_class": ("asset class", "anlageklasse"),
    "weight": ("weight (%)", "gewichtung (%)", "weight"),
    "exchange": ("exchange", "börse", "boerse"),
    "currency": ("market currency", "marktwährung", "currency", "währung"),
    "isin": ("isin",),
    "location": ("location", "standort"),
}
EQUITY_CLASSES = ("equity", "aktien")
_DATE_LINE = re.compile(r"(holdings as of|positionen per|fondspositionen am|stand)", re.IGNORECASE)


def sector_fr(sector: str) -> str:
    return SECTORS_FR.get(sector.strip().lower(), sector.strip())


def exchange_codes(exchange: str) -> tuple[str, str] | None:
    """``"Euronext Paris"`` -> ``(".PA", "XPAR")``; ``None`` when unknown."""
    text = exchange.strip().lower()
    for keywords, suffix, mic in EXCHANGES:
        if any(k in text for k in keywords):
            return suffix, mic
    return None


def yahoo_symbol(ticker: str, suffix: str) -> str:
    """iShares ticker + exchange suffix -> Yahoo symbol (``NOVO B`` -> ``NOVO-B.CO``)."""
    raw = ticker.strip().upper()
    if raw in YAHOO_EXCEPTIONS:
        return YAHOO_EXCEPTIONS[raw]
    core = raw.rstrip(".").replace(" ", "-").replace("/", "-")
    if suffix:
        core = core.replace(".", "-")
    elif "." in core:  # US share classes: BRK.B -> BRK-B
        core = core.replace(".", "-")
    return f"{core}{suffix}"


def _number(text: str) -> float:
    cleaned = text.strip().replace(" ", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "") if cleaned.rfind(".") > cleaned.rfind(",") else \
            cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return math.nan


def _parse_date(text: str) -> date | None:
    value = text.strip().strip('"').strip()
    for fmt in ("%b %d, %Y", "%d/%b/%Y", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Holdings:
    as_of: date | None
    members: tuple[Member, ...]
    skipped: tuple[str, ...]       # lines without a known exchange (to map by hand)


def parse_ishares_csv(text: str, overrides: Mapping[str, str] | None = None) -> Holdings:
    """Equity lines of an iShares holdings CSV (UK, US or German site).

    ``overrides``: iShares ticker or ISIN -> Yahoo symbol, for listings the mapping misses.
    """
    text = text.lstrip("﻿")
    lines = text.splitlines()
    as_of = None
    header_at = None
    for k, line in enumerate(lines):
        cells = next(csv.reader([line]), [])
        if cells and _DATE_LINE.search(cells[0]) and len(cells) > 1:
            as_of = _parse_date(cells[1])
        first = cells[0].strip().lower() if cells else ""
        if first in _HEADER_ALIASES["ticker"] and any(c.strip().lower() in _HEADER_ALIASES["weight"]
                                                      for c in cells):
            header_at = k
            break
    if header_at is None:
        raise DataProviderError("not an iShares holdings file (no Ticker / Weight header)")
    reader = csv.reader(lines[header_at:])
    header = [h.strip().lower() for h in next(reader)]
    column = {key: next((header.index(a) for a in aliases if a in header), None)
              for key, aliases in _HEADER_ALIASES.items()}
    if column["ticker"] is None or column["weight"] is None:
        raise DataProviderError("iShares file without ticker or weight column")
    overrides = dict(overrides or {})
    members: list[Member] = []
    skipped: list[str] = []
    for cells in reader:
        if len(cells) < len(header) // 2:
            continue

        def cell(key: str, row: list[str] = cells) -> str:
            index = column[key]
            return row[index].strip() if index is not None and index < len(row) else ""

        asset = cell("asset_class").lower()
        if column["asset_class"] is not None and not any(a in asset for a in EQUITY_CLASSES):
            continue
        ticker, isin = cell("ticker"), cell("isin")
        weight = _number(cell("weight"))
        if not ticker or ticker == "-" or math.isnan(weight):
            continue
        codes = exchange_codes(cell("exchange"))
        symbol = overrides.get(isin) or overrides.get(ticker)
        if symbol is None and codes is None:
            skipped.append(f"{ticker} ({cell('exchange') or 'place inconnue'})")
            continue
        suffix, mic = codes if codes is not None else ("", "")
        members.append(Member(
            ticker=symbol or yahoo_symbol(ticker, suffix), name=_title(cell("name")),
            sector=sector_fr(cell("sector")), weight=weight, isin=isin, mic=mic,
            currency=cell("currency"), fmp=symbol or yahoo_symbol(ticker, suffix),
        ))
    merged: dict[str, Member] = {}
    for member in members:  # two lines of one listing (e.g. share classes): weights add up
        if member.ticker in merged:
            previous = merged[member.ticker]
            merged[member.ticker] = Member(**{**previous.__dict__,
                                              "weight": (previous.weight or 0) + (member.weight or 0)})
        else:
            merged[member.ticker] = member
    return Holdings(as_of, tuple(merged.values()), tuple(skipped))


def _title(name: str) -> str:
    """``"ASML HOLDING NV"`` -> ``"Asml Holding NV"`` (legal suffixes kept upper case)."""
    keep = {"NV", "SA", "SE", "AG", "PLC", "SPA", "ASA", "AB", "OYJ", "INC", "CORP", "LTD", "CO", "II",
            "III", "A", "B", "C", "ADR", "REIT", "NA", "N.V.", "S.A.", "S.P.A."}
    return " ".join(w if w.upper() in keep else w.capitalize() for w in name.split()) if name.isupper() \
        else name


# ------------------------------------------------------------------ weights
MarketCaps = Callable[[list[str]], dict[str, float]]


def marketcap_weights(members: Iterable[Member], caps: Mapping[str, float]) -> tuple[Member, ...]:
    """Weights in % proportional to the market capitalisations (members without one: none)."""
    members = tuple(members)
    total = sum(v for t, v in caps.items() if v and v > 0 and any(m.ticker == t for m in members))
    if not total:
        return members
    return tuple(Member(**{**m.__dict__, "weight": 100.0 * caps[m.ticker] / total})
                 if caps.get(m.ticker, 0) > 0 else Member(**{**m.__dict__, "weight": None})
                 for m in members)


#: share of the members with a market cap below which no weight is published
MIN_CAP_COVERAGE = 0.8
#: consecutive symbols without a market cap, from the start, after which Yahoo is deemed blocked
CAP_PROBE = 5


def yahoo_market_caps(session: Any = None) -> MarketCaps:
    """Market capitalisations from Yahoo (one quote request per symbol, weekly use).

    yfinance's own per-symbol error logs are kept out of the console; when the first
    :data:`CAP_PROBE` symbols all fail (SSL proxy, firewall, rate limit), the run stops with
    the reason instead of trying every symbol.
    """
    def fetch(tickers: list[str]) -> dict[str, float]:
        import yfinance as yf

        from market_monitor.network import apply_to_yfinance, blocked_reason, captured_logs
        if session is not None:
            apply_to_yfinance(session)
        out: dict[str, float] = {}
        with captured_logs("yfinance") as messages:
            for k, ticker in enumerate(tickers):
                try:
                    value = yf.Ticker(ticker, session=session).fast_info["marketCap"]
                except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the refresh
                    messages.append(str(exc))
                    value = None
                if value and value == value:
                    out[ticker] = float(value)
                if k + 1 == CAP_PROBE and not out:
                    raise ProviderUnavailableError(f"Yahoo ne répond pas ({blocked_reason(messages)})")
        return out
    return fetch


def weigh_by_market_cap(members: Iterable[Member], market_caps: MarketCaps | None,
                        name: str) -> tuple[tuple[Member, ...], str, str | None]:
    """``(members, weights method, note)``: market-cap weights when Yahoo covers enough members.

    Below :data:`MIN_CAP_COVERAGE` the members are kept without weights (``"none"``) and the
    note says why - never a weight computed on a handful of names.
    """
    members = tuple(members)
    if market_caps is None:
        return tuple(Member(**{**m.__dict__, "weight": None}) for m in members), "none", None
    caps = market_caps([m.ticker for m in members])
    found = sum(1 for m in members if caps.get(m.ticker, 0) > 0)
    if found < MIN_CAP_COVERAGE * len(members):
        note = (f"{name} : capitalisations Yahoo pour {found}/{len(members)} titres seulement, "
                "poids non calculés")
        return tuple(Member(**{**m.__dict__, "weight": None}) for m in members), "none", note
    return marketcap_weights(members, caps), "marketcap", None


# ------------------------------------------------------------------- wikipedia
class _Tables(HTMLParser):
    """Rows of the ``wikitable`` tables of a page (text of each cell)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1 and "wikitable" in (dict(attrs).get("class") or ""):
                self.tables.append([])
        elif self._depth == 1 and self.tables and tag == "tr":
            self._row = []
        elif self._row is not None and tag in ("td", "th"):
            self._cell = []
        elif self._cell is not None and tag in ("sup", "style"):
            self._cell.append("\x00")  # footnote markers are dropped below

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth -= 1
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = "".join(self._cell).split("\x00")[0]
            self._row.append(" ".join(text.split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


SYMBOL_HEADERS = ("symbol", "ticker")
NAME_HEADERS = ("security", "company", "name")
SECTOR_HEADERS = ("gics sector", "icb industry[13]", "icb industry", "sector")


def parse_wikipedia_members(html: str, mic: str = "XNYS") -> tuple[Member, ...]:
    """US index members from a Wikipedia constituents table (symbol, company, GICS sector)."""
    parser = _Tables()
    parser.feed(html)
    for table in parser.tables:
        if not table:
            continue
        header = [h.strip().lower() for h in table[0]]
        symbol = next((header.index(h) for h in SYMBOL_HEADERS if h in header), None)
        name = next((header.index(h) for h in NAME_HEADERS if h in header), None)
        if symbol is None or name is None:
            continue
        sector = next((k for k, h in enumerate(header) if any(h.startswith(x) for x in SECTOR_HEADERS)),
                      None)
        members, seen = [], set()
        for row in table[1:]:
            if len(row) <= max(symbol, name):
                continue
            raw = row[symbol].strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            members.append(Member(ticker=yahoo_symbol(raw, ""), name=row[name].strip(),
                                  sector=sector_fr(row[sector]) if sector is not None and sector < len(row)
                                  else "", mic=mic, currency="USD", fmp=raw.replace(".", "-")))
        if len(members) >= 50:
            return tuple(members)
    raise DataProviderError("no constituents table (Symbol / Security columns) on the Wikipedia page")


# ------------------------------------------------------------------ refresh
@dataclass(frozen=True)
class RefreshResult:
    composition: Composition
    added: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def summary(self) -> str:
        comp = self.composition
        parts = [f"{comp.name} : {len(comp.members)} titres au "
                 f"{comp.as_of:%d/%m/%Y}" if comp.as_of else f"{comp.name} : {len(comp.members)} titres"]
        if self.added:
            parts.append(f"entrées {', '.join(self.added)}")
        if self.removed:
            parts.append(f"sorties {', '.join(self.removed)}")
        if self.skipped:
            parts.append(f"{len(self.skipped)} ligne(s) sans place connue : {', '.join(self.skipped[:5])}")
        parts += list(self.notes)
        return " ; ".join(parts)


def _diff(old: Composition, new: Composition) -> tuple[tuple[str, ...], tuple[str, ...]]:
    before, after = set(old.tickers), set(new.tickers)
    return tuple(sorted(after - before)), tuple(sorted(before - after))


def from_holdings(composition: Composition, holdings: Holdings, origin: str) -> RefreshResult:
    if not holdings.members:
        raise DataProviderError(f"{composition.name}: no equity line in the holdings file")
    updated = composition.with_members(
        holdings.members, as_of=holdings.as_of or date.today(), weights="holdings",
        source=f"{origin}, positions du {(holdings.as_of or date.today()):%d/%m/%Y}")
    return RefreshResult(updated, *_diff(composition, updated), holdings.skipped)


def refresh_composition(
    composition: Composition,
    *,
    fetch_text: Callable[[str], str] | None = None,
    fmp_list: Callable[[str], list[dict[str, Any]]] | None = None,
    market_caps: MarketCaps | None = None,
    today: date | None = None,
) -> RefreshResult:
    """New composition according to ``composition.refresh``.

    ``kind``: ``ishares`` (holdings file, real weights), ``fmp`` / ``wikipedia`` (member
    lists, weights from the market caps), ``manual`` (members kept, market-cap weights
    re-estimated). ``fallback: wikipedia`` (+ ``wikipedia_url``) is tried when the main source
    fails. Nothing is returned for saving when the weights could not be refreshed.
    """
    kind = composition.refresh.get("kind", "manual")
    today = today or date.today()
    if kind == "manual" and composition.refresh.get("ishares_url"):
        kind = "ishares"  # a holdings file was found for a hand-kept index
    fallback = composition.refresh.get("fallback")
    try:
        return _refresh_from(kind, composition, fetch_text, fmp_list, market_caps, today)
    except DataProviderError as exc:
        if fallback != "wikipedia" or kind == "wikipedia":
            raise
        result = _refresh_from("wikipedia", composition, fetch_text, fmp_list, market_caps, today)
        return replace(result, notes=(f"source principale en échec ({exc}) : liste Wikipedia",
                                      *result.notes))


def _refresh_from(kind: str, composition: Composition, fetch_text: Callable[[str], str] | None,
                  fmp_list: Callable[[str], list[dict[str, Any]]] | None,
                  market_caps: MarketCaps | None, today: date) -> RefreshResult:
    refresh = composition.refresh
    if kind == "ishares":
        url = str(refresh.get("url") or refresh.get("ishares_url") or "")
        if not url or fetch_text is None:
            raise DataProviderError(f"{composition.name}: no iShares URL configured "
                                    "(refresh.url) - or import a downloaded file")
        holdings = parse_ishares_csv(fetch_text(url), refresh.get("overrides"))
        return from_holdings(composition, holdings, "iShares")
    if kind in ("fmp", "wikipedia"):
        if kind == "fmp":
            if fmp_list is None:
                raise DataProviderError(f"{composition.name}: FMP unavailable (FMP_API_KEY)")
            rows = fmp_list(str(refresh.get("list", "sp500-constituent")))
            members: tuple[Member, ...] = tuple(
                Member(ticker=yahoo_symbol(str(r["symbol"]), ""), name=str(r.get("name") or r["symbol"]),
                       sector=sector_fr(str(r.get("sector") or "")), mic="XNYS", currency="USD",
                       fmp=str(r["symbol"])) for r in rows if r.get("symbol"))
            origin = f"FMP ({refresh.get('list')})"
        else:
            url = str(refresh.get("wikipedia_url") or refresh.get("url") or "")
            if not url or fetch_text is None:
                raise DataProviderError(f"{composition.name}: no Wikipedia page configured (wikipedia_url)")
            members = parse_wikipedia_members(fetch_text(url), str(refresh.get("mic", "XNYS")))
            origin = "Wikipedia"
        if not members:
            raise DataProviderError(f"{composition.name}: {origin} returned no member")
        weighted, method, note = weigh_by_market_cap(members, market_caps, composition.name)
        source = origin + (", poids estimés par la capitalisation (Yahoo)" if method == "marketcap" else "")
        updated = composition.with_members(weighted, as_of=today, weights=method, source=source)
        return RefreshResult(updated, *_diff(composition, updated), notes=(note,) if note else ())
    # manual: members kept; weights re-estimated from market caps when asked for
    if composition.weights == "marketcap" and market_caps is not None:
        weighted, method, note = weigh_by_market_cap(composition.members, market_caps, composition.name)
        if method != "marketcap":
            raise DataProviderError(note or f"{composition.name}: no market cap")
        updated = composition.with_members(
            weighted, as_of=today, weights="marketcap",
            source="liste manuelle, poids estimés par la capitalisation (Yahoo)")
        return RefreshResult(updated, (), ())
    return RefreshResult(composition, (), ())
