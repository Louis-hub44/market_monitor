"""Portefeuille du moteur de stress : lignes (``Holding``) et chargement CSV / Excel.

Le moteur a son propre modèle de ligne, plus riche que celui de Portfolio Intelligence (futures,
fonds en transparence, fonds monétaires) et sans dépendance à une interface. Une ligne décrit
**l'instrument** ; les sensibilités sont calculées par :mod:`stress_engine.mapping`.

Gabarit de fichier (en-têtes français ou anglais, ordre libre, ``,`` ou ``;``) :

=================  =============================================================================
Colonne            Sens
=================  =============================================================================
id                 identifiant (ISIN, code interne) — obligatoire
name               libellé
instrument         BOND | FUTURE | FUND | MMF | EQUITY | CASH | COMMODITY
issuer_type        GOV | CORP | SSA (obligations)
currency           devise de cotation (EUR par défaut)
nominal            nominal (obligations), parts ou titres (fonds, actions)
price              prix clean en % du pair (obligations), cours (futures, actions, fonds)
market_value       valorisation en devise de cotation (fonds, MMF, actions, cash)
coupon, maturity   coupon (``3.25`` ou ``0.0325``), échéance
issue_date         date de jouissance (obligations ; déduite sinon)
frequency          coupons par an (1 par défaut)
country            pays de l'émetteur (courbe des taux)
rating, sector     notation, secteur (facteur crédit)
contract, lots     futures : code EUREX + échéance (``FGBL DEC26``, ``FGBLZ6``), nombre de lots signé
ctd                futures : ISIN du moins cher à livrer, si connu
lookthrough        fonds : identifiant de la composition (fichier de transparence)
proxy              fonds sans composition : profil de ``fund_proxies.yaml`` (``EUR_IG_CORP``…)
wam_days, wal_days fonds monétaires : maturités moyennes pondérées (jours)
index, beta        actions / fonds actions : indice de référence et bêta
fx_rate            devise de base par unité de devise de cotation (sinon taux du marché)
=================  =============================================================================
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd


class InstrumentType(str, Enum):
    BOND = "BOND"
    FUTURE = "FUTURE"
    FUND = "FUND"
    MMF = "MMF"
    EQUITY = "EQUITY"
    CASH = "CASH"
    COMMODITY = "COMMODITY"


class AssetClass(str, Enum):
    """Classe d'actifs des rapports (agrégation des P&L)."""

    GOV_BOND = "GOV_BOND"
    CREDIT = "CREDIT"
    RATES_FUTURE = "RATES_FUTURE"
    FUND = "FUND"
    MONEY_MARKET = "MONEY_MARKET"
    EQUITY = "EQUITY"
    CASH = "CASH"
    COMMODITY = "COMMODITY"


class PortfolioFileError(ValueError):
    """Fichier portefeuille inexploitable."""


@dataclass
class Holding:
    id: str
    instrument: InstrumentType
    name: str = ""
    currency: str = "EUR"
    nominal: float | None = None
    price: float | None = None
    market_value: float | None = None
    issuer_type: str | None = None
    coupon: float | None = None
    maturity: date | None = None
    issue_date: date | None = None
    frequency: int | None = None
    country: str | None = None
    rating: str | None = None
    sector: str | None = None
    contract: str | None = None
    lots: float | None = None
    ctd: str | None = None
    lookthrough: str | None = None
    proxy: str | None = None
    wam_days: float | None = None
    wal_days: float | None = None
    index: str | None = None
    beta: float | None = None
    fx_rate: float | None = None
    region: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def asset_class(self) -> AssetClass:
        if self.instrument is InstrumentType.BOND:
            return AssetClass.CREDIT if (self.issuer_type or "GOV").upper() in ("CORP", "SSA") else AssetClass.GOV_BOND
        return {InstrumentType.FUTURE: AssetClass.RATES_FUTURE, InstrumentType.FUND: AssetClass.FUND,
                InstrumentType.MMF: AssetClass.MONEY_MARKET, InstrumentType.EQUITY: AssetClass.EQUITY,
                InstrumentType.CASH: AssetClass.CASH, InstrumentType.COMMODITY: AssetClass.COMMODITY}[self.instrument]


@dataclass
class StressPortfolio:
    name: str
    holdings: list[Holding]
    base_currency: str = "EUR"
    source: str | None = None
    notes: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.holdings)

    def __len__(self) -> int:
        return len(self.holdings)

    def get(self, holding_id: str) -> Holding:
        for h in self.holdings:
            if h.id == holding_id:
                return h
        raise KeyError(holding_id)


# ------------------------------------------------------------------- lecture
ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "isin", "identifiant", "identifier", "code", "ticker", "symbol"),
    "name": ("name", "nom", "libelle", "libellé", "description"),
    "instrument": ("instrument", "type", "instrument_type", "type d'instrument"),
    "issuer_type": ("issuer_type", "type emetteur", "type émetteur", "emetteur", "émetteur"),
    "currency": ("currency", "devise", "ccy"),
    "nominal": ("nominal", "quantity", "quantite", "quantité", "face", "notional", "parts", "shares"),
    "price": ("price", "prix", "cours", "clean price", "prix clean"),
    "market_value": ("market_value", "market value", "valorisation", "mv", "montant"),
    "coupon": ("coupon", "taux nominal"),
    "maturity": ("maturity", "maturite", "maturité", "echeance", "échéance"),
    "issue_date": ("issue_date", "issue date", "date d'emission", "date d'émission", "jouissance"),
    "frequency": ("frequency", "frequence", "fréquence"),
    "country": ("country", "pays"),
    "rating": ("rating", "notation"),
    "sector": ("sector", "secteur"),
    "contract": ("contract", "contrat", "future"),
    "lots": ("lots", "contracts", "contrats", "nb lots"),
    "ctd": ("ctd", "cheapest", "moins cher a livrer"),
    "lookthrough": ("lookthrough", "look-through", "transparence", "composition"),
    "proxy": ("proxy", "profil proxy"),
    "wam_days": ("wam_days", "wam", "wam (jours)"),
    "wal_days": ("wal_days", "wal", "wal (jours)"),
    "index": ("index", "indice", "benchmark"),
    "beta": ("beta", "bêta"),
    "fx_rate": ("fx_rate", "fx", "taux de change"),
    "region": ("region", "région", "zone"),
}
_NUMERIC = ("nominal", "price", "market_value", "coupon", "frequency", "lots", "wam_days", "wal_days", "beta",
            "fx_rate")
_DATES = ("maturity", "issue_date")
_INSTRUMENT_WORDS = {
    InstrumentType.BOND: ("bond", "obligation", "govt", "gov", "corp", "credit", "sovereign"),
    InstrumentType.FUTURE: ("future", "futures", "fut"),
    InstrumentType.MMF: ("mmf", "monetaire", "monétaire", "money market"),
    InstrumentType.FUND: ("fund", "fonds", "etf", "opcvm", "sicav"),
    InstrumentType.EQUITY: ("equity", "action", "stock", "share"),
    InstrumentType.CASH: ("cash", "liquidite", "liquidité", "depot", "dépôt"),
    InstrumentType.COMMODITY: ("commodity", "matiere", "matière"),
}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def map_columns(columns: Iterable[str]) -> dict[str, str]:
    """Colonne du fichier -> champ de :class:`Holding`."""
    lookup = {alias: key for key, aliases in ALIASES.items() for alias in aliases}
    return {c: lookup[_norm(c)] for c in columns if _norm(c) in lookup}


def to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(" ", "").replace("%", "")
    if not text or text.lower() in ("nan", "none", "-", "n/a"):
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def to_date(value: Any) -> date | None:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    fmt = "%Y-%m-%d" if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None
    ts = pd.to_datetime(text[:10] if fmt else text, format=fmt, dayfirst=fmt is None, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def parse_instrument(value: Any, row: Mapping[str, Any]) -> InstrumentType:
    text = _norm(value or "")
    for itype in InstrumentType:
        if text == itype.value.lower():
            return itype
    for itype, words in _INSTRUMENT_WORDS.items():
        if text and any(w in text for w in words):
            return itype
    if row.get("contract"):
        return InstrumentType.FUTURE
    if row.get("coupon") is not None or row.get("maturity"):
        return InstrumentType.BOND
    raise PortfolioFileError(f"{row.get('id')}: type d'instrument introuvable ({value!r})")


def holding_from_row(row: Mapping[str, Any]) -> Holding:
    data = {k: v for k, v in row.items() if k in ALIASES}
    if not str(data.get("id") or "").strip():
        raise PortfolioFileError("ligne sans identifiant")
    for key in _NUMERIC:
        data[key] = to_float(data.get(key))
    for key in _DATES:
        data[key] = to_date(data.get(key))
    for key, value in list(data.items()):
        if isinstance(value, str):
            data[key] = value.strip() or None
        elif value is not None and isinstance(value, float) and pd.isna(value):
            data[key] = None
    coupon = data.get("coupon")
    if coupon is not None and coupon > 0.25:        # 3.25 -> 0.0325
        data["coupon"] = coupon / 100.0
    if data.get("frequency") is not None:
        data["frequency"] = int(data["frequency"])
    instrument = parse_instrument(data.pop("instrument", None), data)
    data["currency"] = (data.get("currency") or "EUR").upper()
    for key in ("country", "issuer_type", "contract", "proxy", "lookthrough", "index", "region"):
        if data.get(key):
            data[key] = str(data[key]).strip().upper() if key != "lookthrough" else str(data[key]).strip()
    return Holding(id=str(data.pop("id")).strip(), instrument=instrument, **data)


def read_frame(path: str | Path, sheet: str | int | None = None) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise PortfolioFileError(f"fichier introuvable : {p}")
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(p, sheet_name=sheet if sheet is not None else 0, dtype=str)
    with p.open(encoding="utf-8-sig") as fh:
        header = fh.readline()
    sep = ";" if header.count(";") > header.count(",") else ","
    return pd.read_csv(p, sep=sep, dtype=str, encoding="utf-8-sig", keep_default_na=False)


def holdings_from_frame(frame: pd.DataFrame) -> tuple[list[Holding], list[str]]:
    """Lignes valides et messages de rejet."""
    columns = map_columns(frame.columns)
    if "id" not in columns.values():
        raise PortfolioFileError("colonne d'identifiant absente (id / isin / identifiant)")
    holdings, notes = [], []
    for i, raw in enumerate(frame.to_dict(orient="records"), start=2):
        row = {columns[c]: v for c, v in raw.items() if c in columns}
        try:
            holdings.append(holding_from_row(row))
        except PortfolioFileError as exc:
            notes.append(f"ligne {i} ignorée : {exc}")
    return holdings, notes


def load_portfolio(path: str | Path, name: str | None = None, base_currency: str = "EUR",
                   sheet: str | int | None = None) -> StressPortfolio:
    holdings, notes = holdings_from_frame(read_frame(path, sheet))
    return StressPortfolio(name or Path(path).stem, holdings, base_currency, str(path), notes)


def load_lookthrough(path: str | Path, sheet: str | int | None = None) -> dict[str, list[tuple[Holding, float]]]:
    """Fichier de transparence : colonne ``fund`` (identifiant de composition), ``weight`` + gabarit de ligne.

    Renvoie ``{fonds: [(ligne, poids)]}`` ; les poids sont pris tels quels (en décimal ou en %).
    """
    frame = read_frame(path, sheet)
    fund_col = next((c for c in frame.columns if _norm(c) in ("fund", "fonds", "lookthrough", "portefeuille")), None)
    weight_col = next((c for c in frame.columns if _norm(c) in ("weight", "poids", "weight %", "poids %")), None)
    if fund_col is None or weight_col is None:
        raise PortfolioFileError("transparence : colonnes 'fund' et 'weight' requises")
    out: dict[str, list[tuple[Holding, float]]] = {}
    holdings, notes = holdings_from_frame(frame.drop(columns=[fund_col, weight_col]))
    if notes:
        raise PortfolioFileError("transparence : " + "; ".join(notes[:3]))
    weights = [to_float(w) for w in frame[weight_col]]
    if any(w is None for w in weights):
        raise PortfolioFileError("transparence : poids manquant ou illisible")
    for fund, holding, weight in zip(frame[fund_col], holdings, weights):
        out.setdefault(str(fund).strip(), []).append((holding, float(weight or 0.0)))
    for fund, rows in out.items():            # poids en % (somme ~100) ou en décimal (somme ~1), par fonds
        if sum(w for _, w in rows) > 1.5:
            out[fund] = [(h, w / 100.0) for h, w in rows]
    return out
