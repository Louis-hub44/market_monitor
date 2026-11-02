"""Moteur d'import de portefeuilles (§21).

Objectifs :
* accepter les fichiers tels que les gérants les produisent (CSV, XLSX,
  colonnes en français ou en anglais, séparateurs et décimales variables) ;
* ne jamais deviner en silence : tout ce qui a été déduit est consigné dans
  un rapport d'import affiché à l'écran ;
* refuser proprement plutôt que produire un portefeuille faux.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, IO

import pandas as pd

from models.enums import AssetClass, DataQuality, PortfolioType
from models.portfolio import Portfolio, Position

# --------------------------------------------------------------------- #
# Dictionnaire de correspondance des colonnes
# --------------------------------------------------------------------- #
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "identifier": ("isin", "identifier", "id", "ticker", "symbol", "code", "sedol", "cusip", "valeur"),
    "name": ("name", "nom", "libelle", "libellé", "security", "description", "bond name", "designation"),
    "asset_class": ("asset class", "assetclass", "classe d'actif", "classe actif", "type", "categorie", "catégorie"),
    "quantity": ("quantity", "quantite", "quantité", "nominal", "shares", "titres", "face", "notional"),
    "price": ("price", "prix", "cours", "clean price", "market price"),
    "market_value": ("market value", "marketvalue", "mv", "valorisation", "valeur de marche",
                     "valeur de marché", "montant", "exposure"),
    "weight": ("weight", "poids", "pct", "%", "allocation"),
    "currency": ("currency", "devise", "ccy", "cur"),
    "sector": ("sector", "secteur", "gics sector", "industry group"),
    "industry": ("industry", "industrie", "sous-secteur"),
    "country": ("country", "pays", "domicile", "issuer country"),
    "region": ("region", "région", "zone"),
    "coupon": ("coupon", "coupon rate", "taux", "taux nominal"),
    "maturity": ("maturity", "maturite", "maturité", "echeance", "échéance", "maturity date", "redemption"),
    "frequency": ("frequency", "frequence", "fréquence", "coupon frequency", "periodicite", "périodicité"),
    "ytm": ("ytm", "yield", "rendement", "yield to maturity", "taux actuariel"),
    "spread_bp": ("spread", "spread bp", "z-spread", "oas", "asw"),
    "rating": ("rating", "notation", "note", "issuer rating"),
    "beta": ("beta", "bêta"),
    "modified_duration": ("modified duration", "duration modifiee", "duration modifiée", "mod duration", "duration"),
    "fx_rate": ("fx", "fx rate", "taux de change", "fx to base", "change"),
}

ASSET_CLASS_KEYWORDS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.EQUITY: ("equity", "action", "stock", "share", "equities"),
    AssetClass.GOV_BOND: ("gov", "govt", "government", "souverain", "etat", "état", "treasury",
                          "oat", "bund", "gilt", "btp", "sovereign"),
    AssetClass.CREDIT: ("credit", "corporate", "corp", "ig", "high yield", "hy", "obligation d'entreprise"),
    AssetClass.CASH: ("cash", "liquidite", "liquidité", "monetaire", "monétaire", "mmf", "depot", "dépôt"),
    AssetClass.FX: ("fx", "change", "forward", "devise"),
    AssetClass.COMMODITY: ("commodity", "matiere", "matière", "gold", "or", "oil", "petrole", "pétrole"),
    AssetClass.FUND: ("fund", "fonds", "etf", "opcvm", "sicav", "ucits", "mutual"),
    # Les dérivés sont identifiés pour être **exclus** des calculs de duration :
    # un future n'a pas de valorisation significative (marge quotidienne) et son
    # exposition se mesure en notionnel de contrat, pas en valeur de marché.
    AssetClass.OTHER: ("future", "futures", "forward", "swap", "option", "cds",
                       "derivative", "derive", "dérivé", "irs"),
}


@dataclass
class ImportReport:
    """Trace complète de l'import — affichée dans l'interface."""

    file_name: str
    rows_read: int = 0
    rows_kept: int = 0
    detected_type: PortfolioType | None = None
    column_mapping: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rows_kept > 0


# --------------------------------------------------------------------- #
def _norm(col: str) -> str:
    return re.sub(r"\s+", " ", str(col).strip().lower()).replace("_", " ")


def map_columns(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """Associe les colonnes du fichier aux champs du modèle."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    normalized = {col: _norm(col) for col in df.columns}

    for field_name, aliases in COLUMN_ALIASES.items():
        for col, norm in normalized.items():
            if col in used:
                continue
            if norm in aliases:
                mapping[field_name] = col
                used.add(col)
                break
        if field_name in mapping:
            continue
        for col, norm in normalized.items():   # second passage : correspondance partielle
            if col in used:
                continue
            if any(alias in norm for alias in aliases):
                mapping[field_name] = col
                used.add(col)
                break

    unmapped = [c for c in df.columns if c not in used]
    return mapping, unmapped


def _to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("%", "").replace("\u202f", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value).strip()[:10], fmt).date()
        except ValueError:
            continue
    try:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
        return None if pd.isna(parsed) else parsed.date()
    except Exception:
        return None


def infer_asset_class(row: dict[str, Any], report: ImportReport) -> AssetClass:
    raw = str(row.get("asset_class") or "").lower()
    if raw:
        # Les dérivés sont testés d'abord : un « EURO-BUND FUTURE » contient les
        # mots-clés d'un souverain, et serait sinon valorisé comme une obligation.
        if any(k in raw for k in ASSET_CLASS_KEYWORDS[AssetClass.OTHER]):
            return AssetClass.OTHER
        for ac, keywords in ASSET_CLASS_KEYWORDS.items():
            if any(k in raw for k in keywords):
                return ac
    if row.get("maturity") is not None and row.get("coupon") is not None:
        rating = str(row.get("rating") or "").upper()
        name = str(row.get("name") or "").lower()
        is_govt = any(k in name for k in ASSET_CLASS_KEYWORDS[AssetClass.GOV_BOND]) or rating.startswith("AAA")
        inferred = AssetClass.GOV_BOND if is_govt else AssetClass.CREDIT
        report.inferences.append(
            f"{row.get('identifier')}: classe d'actif déduite ({inferred.value}) à partir du couple coupon/maturité"
        )
        return inferred
    if "cash" in str(row.get("name") or "").lower():
        return AssetClass.CASH
    report.inferences.append(f"{row.get('identifier')}: classe d'actif supposée EQUITY (aucune information de type)")
    return AssetClass.EQUITY


def detect_rate_unit(series: pd.Series) -> float:
    """Facteur de conversion d'une colonne de taux vers le décimal.

    L'unité est une propriété de la **colonne**, pas de la ligne : décider au cas
    par cas avec une règle du type « si la valeur dépasse 1, diviser par 100 »
    détruit les coupons inférieurs à 1 % — un 0,625 % serait lu comme 62,5 %.

    On tranche donc sur la médiane : au-dessus de 1, la colonne est en pourcents ;
    en dessous, elle est déjà en décimal. Renvoie 100.0 ou 1.0.
    """
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values[(values != 0) & (values.abs() < 1000)]
    if values.empty:
        return 1.0
    return 100.0 if float(values.median()) > 1.0 else 1.0


def detect_spread_unit(series: pd.Series) -> float:
    """Facteur de conversion d'une colonne de spread vers les points de base."""
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values[values != 0]
    if values.empty:
        return 1.0
    median = float(values.abs().median())
    if median < 0.5:        # 0.0085 -> 85 bp
        return 10_000.0
    if median < 20:         # 0.85 % -> 85 bp
        return 100.0
    return 1.0              # déjà en bp


def looks_like_market_value(series: pd.Series) -> bool:
    """La colonne de « quantité » contient-elle en réalité des valorisations ?

    Un nominal obligataire ou un nombre de titres est **rond** : multiple de la
    dénomination (1 000, 100 000) ou au moins entier. Une valorisation, elle,
    traîne des centimes : `900457.05636`.

    C'est le test décisif, car les exports de gérants nomment souvent
    « nominal » une colonne qui contient déjà la valeur de marché. Multiplier
    une valorisation par le prix donne alors une NAV fausse — de 2 % sur une
    obligation cotée près du pair, d'un facteur 1 000 sur une part de fonds
    cotée 1 168 €.
    """
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values[values != 0]
    if len(values) < 3:
        return False
    integer_share = float((values % 1 == 0).mean())
    return integer_share < 0.5


def detect_portfolio_type(positions: list[Position]) -> PortfolioType:
    total = sum(p.market_value_base for p in positions) or 1.0
    bond_w = sum(p.market_value_base for p in positions if p.is_bond) / total
    eq_w = sum(p.market_value_base for p in positions if p.asset_class is AssetClass.EQUITY) / total
    if bond_w >= 0.85:
        return PortfolioType.FIXED_INCOME
    if eq_w >= 0.85:
        return PortfolioType.EQUITY
    return PortfolioType.MULTI_ASSET


# --------------------------------------------------------------------- #
def read_table(source: str | Path | IO[bytes] | bytes, file_name: str = "portefeuille") -> pd.DataFrame:
    """Lit un CSV ou un XLSX en devinant le séparateur."""
    name = file_name.lower()
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(source)
    try:
        return pd.read_csv(source, sep=None, engine="python")
    except Exception:
        if hasattr(source, "seek"):
            source.seek(0)
        return pd.read_csv(source, sep=";")


DEFAULT_NOTIONAL = 1_000_000.0


def load_portfolio(source: str | Path | IO[bytes] | bytes, file_name: str | None = None,
                   name: str | None = None, base_currency: str = "EUR",
                   portfolio_type: PortfolioType | None = None,
                   fx_rates: dict[str, float] | None = None,
                   notional: float = DEFAULT_NOTIONAL,
                   quantity_means_value: bool | None = None) -> tuple[Portfolio, ImportReport]:
    """Point d'entrée unique de l'import.

    `fx_rates` : {devise: taux vers la devise de base}. Les positions dont la
    devise n'a pas de taux fourni sont conservées à 1.0 et signalées — la
    conversion est ensuite recalculée par le service de données de marché.

    `quantity_means_value` : force l'interprétation de la colonne de quantité —
    `False` = nominal ou nombre de titres (à multiplier par le prix), `True` =
    valorisation déjà calculée. `None` (défaut) déclenche la détection
    automatique décrite dans `looks_like_market_value`.

    `notional` : quand le fichier ne contient que des **poids** (pas de
    valorisation), les montants sont mis à l'échelle sur cette base afin que la
    NAV soit lisible. Le portefeuille est alors marqué `is_notional` : tous les
    ratios et impacts restent exacts (ils ne dépendent que des poids), mais les
    montants absolus — DV01, valorisations — sont conventionnels.
    """
    file_name = file_name or (Path(str(source)).name if isinstance(source, (str, Path)) else "portefeuille")
    report = ImportReport(file_name=file_name)

    try:
        df = read_table(source, file_name)
    except Exception as exc:
        report.errors.append(f"Lecture impossible : {exc}")
        return Portfolio(name=name or file_name, portfolio_type=PortfolioType.MULTI_ASSET), report

    df = df.dropna(how="all")
    report.rows_read = len(df)
    mapping, unmapped = map_columns(df)
    report.column_mapping = mapping
    report.unmapped_columns = unmapped

    if "identifier" not in mapping and "name" not in mapping:
        report.errors.append("Aucune colonne d'identification (ISIN, ticker ou nom) n'a été trouvée.")
        return Portfolio(name=name or file_name, portfolio_type=PortfolioType.MULTI_ASSET), report
    if not any(k in mapping for k in ("market_value", "weight", "quantity")):
        report.errors.append("Aucune colonne de valorisation (market value, poids ou quantité) n'a été trouvée.")
        return Portfolio(name=name or file_name, portfolio_type=PortfolioType.MULTI_ASSET), report

    # La colonne « quantité » contient-elle déjà des valorisations ?
    quantity_is_value = quantity_means_value
    if quantity_is_value is None and "market_value" not in mapping and "quantity" in mapping:
        quantity_is_value = looks_like_market_value(df[mapping["quantity"]])
        if quantity_is_value:
            report.inferences.append(
                f"Colonne « {mapping['quantity']} » interprétée comme une **valorisation** "
                f"(valeurs non entières) et non comme un nominal : elle n'est donc pas "
                f"multipliée par le prix. Forcez l'interprétation à l'import si c'est faux."
            )
    quantity_is_value = bool(quantity_is_value)

    # Unités des colonnes de taux, déterminées une fois pour toute la colonne.
    coupon_divisor = detect_rate_unit(df[mapping["coupon"]]) if "coupon" in mapping else 1.0
    ytm_divisor = detect_rate_unit(df[mapping["ytm"]]) if "ytm" in mapping else 1.0
    spread_factor = detect_spread_unit(df[mapping["spread_bp"]]) if "spread_bp" in mapping else 1.0
    if coupon_divisor == 100.0:
        report.inferences.append("Colonne coupon lue en pourcents (3.875 = 3,875 %).")
    if spread_factor != 1.0:
        report.inferences.append(
            f"Colonne spread convertie en points de base (facteur ×{spread_factor:g})."
        )

    fx_rates = fx_rates or {}
    positions: list[Position] = []
    cash_total = 0.0
    weight_derived = 0        # lignes dont la valorisation vient d'un poids

    for _, raw in df.iterrows():
        row = {field: raw.get(col) for field, col in mapping.items()}
        row["coupon"] = _to_float(row.get("coupon"))
        row["maturity"] = _to_date(row.get("maturity"))

        identifier = str(row.get("identifier") or row.get("name") or "").strip()
        if not identifier or identifier.lower() == "nan":
            continue

        asset_class = infer_asset_class(row, report)
        currency = str(row.get("currency") or base_currency).strip().upper()[:3] or base_currency

        quantity = _to_float(row.get("quantity"))
        price = _to_float(row.get("price"))
        market_value = _to_float(row.get("market_value"))

        if market_value is None and quantity is not None and quantity_is_value:
            market_value = quantity          # la colonne porte déjà la valorisation
        elif market_value is None and quantity is not None and price is not None:
            # Pour une obligation, le prix est en pourcentage du nominal.
            market_value = quantity * price / 100.0 if row.get("maturity") else quantity * price
            report.inferences.append(f"{identifier}: valorisation calculée (quantité × prix)")
        if market_value is None:
            weight = _to_float(row.get("weight"))
            if weight is None:
                report.errors.append(f"{identifier}: ligne ignorée (aucune valorisation exploitable)")
                continue
            market_value = weight        # mis à l'échelle après la boucle
            weight_derived += 1

        coupon = row.get("coupon")
        if coupon is not None:
            coupon = coupon / coupon_divisor
        if coupon is not None and not (-0.01 <= coupon <= 0.5):
            # Un « coupon » de 100 000 est une taille de contrat de future, pas un
            # taux. Mieux vaut perdre la ligne pour la duration que de calculer une
            # sensibilité absurde sur un chiffre mal interprété.
            report.inferences.append(
                f"{identifier}: coupon {row.get('coupon')} hors plage plausible "
                f"(0–50 %) — ignoré, la ligne sera exclue des calculs de duration"
            )
            coupon = None
        ytm = _to_float(row.get("ytm"))
        if ytm is not None:
            ytm = ytm / ytm_divisor

        fx = _to_float(row.get("fx_rate")) or fx_rates.get(currency, 1.0)
        if currency != base_currency and currency not in fx_rates and not _to_float(row.get("fx_rate")):
            report.inferences.append(f"{identifier}: taux de change {currency}/{base_currency} inconnu, pris à 1,0")

        if asset_class is AssetClass.CASH:
            # Une ligne de trésorerie cotée à 100 suit la convention « pourcentage
            # du nominal » des obligations : 1 500 000 à 100 = 1 500 000, pas 150 M.
            if price is not None and abs(price - 100.0) < 1e-9 and quantity is not None:
                market_value = quantity
                report.inferences.append(
                    f"{identifier}: trésorerie cotée à 100 interprétée au pair "
                    f"(nominal = valorisation)"
                )
            cash_total += market_value * fx
            continue

        positions.append(
            Position(
                identifier=identifier,
                name=str(row.get("name") or identifier),
                asset_class=asset_class,
                quantity=quantity,
                price=price,
                market_value=market_value,
                currency=currency,
                fx_rate_to_base=fx,
                sector=(str(row["sector"]).strip() if row.get("sector") else None),
                country=(str(row["country"]).strip() if row.get("country") else None),
                region=(str(row["region"]).strip() if row.get("region") else None),
                coupon=coupon,
                maturity=row.get("maturity"),
                frequency=int(_to_float(row.get("frequency")) or 2),
                ytm=ytm,
                spread_bp=(_to_float(row.get("spread_bp")) * spread_factor
                           if _to_float(row.get("spread_bp")) is not None else None),
                issuer_rating=(str(row["rating"]).strip() if row.get("rating") else None),
                beta=_to_float(row.get("beta")),
                modified_duration=_to_float(row.get("modified_duration")),
                quality=DataQuality.API_CONFIRMED,
                metadata={"industry": row.get("industry")},
            )
        )

    # ------------------------------------------------------------------ #
    # Mise à l'échelle des portefeuilles fournis en poids
    # ------------------------------------------------------------------ #
    is_notional = False
    if weight_derived and positions:
        total_weight = sum(p.market_value for p in positions) + cash_total
        if total_weight <= 0:
            report.errors.append("Somme des poids nulle : portefeuille inexploitable.")
        else:
            if abs(total_weight - 1.0) > 0.02:
                report.inferences.append(
                    f"Somme des poids du fichier = {total_weight:.2%} — les poids ont été "
                    f"renormalisés à 100 %."
                )
            scale = notional / total_weight
            for position in positions:
                position.market_value *= scale
            cash_total *= scale
            is_notional = True
            report.inferences.append(
                f"Fichier fourni en poids : NAV notionnelle de {notional:,.0f} {base_currency} "
                f"— les ratios sont exacts, les montants absolus sont conventionnels."
                .replace(",", " ")
            )

    derivatives = [p for p in positions if p.asset_class is AssetClass.OTHER]
    if derivatives:
        report.inferences.append(
            f"{len(derivatives)} ligne(s) de dérivés détectée(s) "
            f"({', '.join(p.identifier for p in derivatives[:5])}"
            f"{'…' if len(derivatives) > 5 else ''}) : exclues des calculs de duration et "
            f"de scénarios. Leur exposition se mesure en notionnel de contrat, pas en "
            f"valeur de marché — la duration affichée est donc **brute, avant couverture**."
        )

    report.rows_kept = len(positions)
    detected = portfolio_type or detect_portfolio_type(positions)
    report.detected_type = detected

    portfolio = Portfolio(
        name=name or Path(file_name).stem,
        portfolio_type=detected,
        positions=positions,
        base_currency=base_currency,
        as_of=date.today(),
        cash=cash_total,
        source_file=file_name,
        is_notional=is_notional,
        notional_base=notional if is_notional else None,
        warnings=report.errors + report.inferences[:10],
    )
    return portfolio, report
