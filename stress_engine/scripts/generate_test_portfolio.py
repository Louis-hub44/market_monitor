"""Génère le portefeuille de test crédit EUR (FICTIF) et sa composition en transparence.

    python scripts/generate_test_portfolio.py        # écrit samples/test_credit_eur.csv et samples/test_lookthrough.csv

Tout est **fictif** et déclaré comme tel : identifiants ``TEST-…`` pour les titres inventés,
titres souverains pris dans l'univers d'exemple de Rates & Bond Analytics (``SMPL-…``, lui-même
synthétique). Les prix des titres inventés sont calculés sur la courbe Bund (ou OAT pour le Bonos)
de l'instantané d'exemple du 06/10/2026, plus un Z-spread par notation (table ``Z_SPREAD_BP``) :
ce sont des niveaux plausibles, pas des cotations. Le script est déterministe.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date
from pathlib import Path

from rates_analytics import api
from rates_analytics.bonds import FixedRateBond
from rates_analytics.config import load_settings
from rates_analytics.conventions.market import get_convention
from rates_analytics.curves.shifted import SpreadedCurve

AS_OF = date(2026, 10, 6)
ROOT = Path(__file__).resolve().parents[1]
#: Z-spread de génération contre la courbe Bund, par notation (bp) ; +15 bp pour les financières.
Z_SPREAD_BP = {"AAA": 25, "AA": 55, "A": 85, "BBB": 130, "BB": 280}
FIN_ADD_BP = 15

SOVEREIGNS = [  # (pays, année d'échéance, nominal)
    ("DE", 2029, 15e6), ("DE", 2033, 10e6), ("DE", 2053, 4e6),
    ("FR", 2031, 12e6), ("FR", 2036, 8e6), ("FR", 2048, 3e6),
    ("IT", 2029, 10e6), ("IT", 2034, 8e6), ("IT", 2053, 3e6),
]
# id, nom, notation, secteur, coupon %, échéance, nominal, pays, type d'émetteur
CORPORATES = [
    ("TEST-SSA-2031", "Banque supranationale fictive 2.75% 2031", "AAA", "Supranational", 2.75, "2031-03-15", 5e6, "EU", "SSA"),
    ("TEST-CORP-01", "Utilities Alpha 3.125% 2030", "A-", "Utilities", 3.125, "2030-06-15", 4e6, "FR", "CORP"),
    ("TEST-CORP-02", "Banque Beta senior 3.5% 2029", "A+", "Banks", 3.5, "2029-01-20", 5e6, "FR", "CORP"),
    ("TEST-CORP-03", "Banque Gamma senior 3.875% 2032", "A", "Banks", 3.875, "2032-09-10", 4e6, "DE", "CORP"),
    ("TEST-CORP-04", "Assurance Delta 4.25% 2034", "A-", "Insurance", 4.25, "2034-05-05", 3e6, "IT", "CORP"),
    ("TEST-CORP-05", "Télécom Epsilon 3.0% 2031", "BBB+", "Communication Services", 3.0, "2031-11-25", 4e6, "ES", "CORP"),
    ("TEST-CORP-06", "Automobile Zeta 3.75% 2028", "BBB", "Consumer Cyclical", 3.75, "2028-04-18", 4e6, "DE", "CORP"),
    ("TEST-CORP-07", "Chimie Eta 2.625% 2033", "BBB+", "Basic Materials", 2.625, "2033-02-12", 3e6, "DE", "CORP"),
    ("TEST-CORP-08", "Énergie Theta 4.0% 2035", "BBB", "Energy", 4.0, "2035-10-01", 3e6, "FR", "CORP"),
    ("TEST-CORP-09", "Immobilier Iota 4.5% 2030", "BBB-", "Real Estate", 4.5, "2030-07-07", 2e6, "NL", "CORP"),
    ("TEST-CORP-10", "Banque Kappa Tier 2 5.25% 2033", "BBB-", "Banks", 5.25, "2033-06-30", 2e6, "IT", "CORP"),
    ("TEST-CORP-11", "Santé Lambda 2.875% 2032", "AA-", "Healthcare", 2.875, "2032-03-03", 3e6, "CH", "CORP"),
    ("TEST-CORP-12", "Agroalimentaire Mu 3.25% 2029", "A", "Consumer Defensive", 3.25, "2029-09-15", 3e6, "NL", "CORP"),
    ("TEST-CORP-13", "Industrie Nu 3.625% 2031", "BBB", "Industrials", 3.625, "2031-05-20", 3e6, "FR", "CORP"),
    ("TEST-CORP-14", "Transport Xi 4.375% 2036", "BBB-", "Industrials", 4.375, "2036-01-15", 2e6, "ES", "CORP"),
    ("TEST-CORP-15", "Distribution Omicron 5.5% 2029", "BB+", "Consumer Cyclical", 5.5, "2029-11-30", 2e6, "FR", "CORP"),
    ("TEST-CORP-16", "Câbles Pi 6.0% 2030", "BB", "Communication Services", 6.0, "2030-04-15", 1.5e6, "LU", "CORP"),
    ("TEST-CORP-17", "Loisirs Rho 5.875% 2031", "BB-", "Consumer Cyclical", 5.875, "2031-07-15", 1.5e6, "IT", "CORP"),
    ("TEST-CORP-18", "Banque Sigma covered 2.5% 2030", "AAA", "Banks covered", 2.5, "2030-02-25", 4e6, "DE", "CORP"),
    ("TEST-CORP-19", "Logiciels Tau 3.375% 2034", "A-", "Technology", 3.375, "2034-09-09", 2e6, "DE", "CORP"),
    ("TEST-CORP-20", "Papier commercial Upsilon 0% 2027", "A", "Banks", 0.0, "2027-03-15", 3e6, "FR", "CORP"),
]
FUTURES = [  # id, contrat, lots, avec prix ?
    ("TEST-FUT-SCHATZ", "FGBS DEC26", -60, False), ("TEST-FUT-BOBL", "FGBM DEC26", -40, False),
    ("TEST-FUT-BUND", "FGBL DEC26", -50, True), ("TEST-FUT-BUXL", "FGBX DEC26", -8, False),
    ("TEST-FUT-OAT", "FOAT DEC26", -20, False), ("TEST-FUT-BTP", "FBTP DEC26", -15, False),
]
LOOKTHROUGH = [  # fonds, poids, id, nom, notation, secteur, coupon, échéance, pays, instrument
    ("MMF-B", 0.30, "TEST-CP-B1", "CD Banque Phi 0% 2026-12", "A+", "Banks", 0.0, "2026-12-18", "FR", "BOND"),
    ("MMF-B", 0.25, "TEST-CP-B2", "CP Industrie Chi 0% 2027-01", "A", "Industrials", 0.0, "2027-01-29", "DE", "BOND"),
    ("MMF-B", 0.25, "TEST-CP-B3", "CD Banque Psi 0% 2027-04", "AA-", "Banks", 0.0, "2027-04-16", "NL", "BOND"),
    ("MMF-B", 0.20, "TEST-CASH-B", "Dépôts à vue", "", "", None, "", "", "CASH"),
    ("CREDIT-LT", 0.25, "TEST-LT-1", "Banque Omega senior 3.25% 2030", "A", "Banks", 3.25, "2030-10-10", "FR", "BOND"),
    ("CREDIT-LT", 0.25, "TEST-LT-2", "Industrie Alpha2 3.75% 2032", "BBB", "Industrials", 3.75, "2032-06-01", "DE", "BOND"),
    ("CREDIT-LT", 0.20, "TEST-LT-3", "Utilities Beta2 3.0% 2034", "BBB+", "Utilities", 3.0, "2034-02-20", "ES", "BOND"),
    ("CREDIT-LT", 0.20, "TEST-LT-4", "Télécom Gamma2 5.0% 2029", "BB+", "Communication Services", 5.0, "2029-05-15", "IT", "BOND"),
    ("CREDIT-LT", 0.10, "TEST-LT-5", "Banque Delta2 2.75% 2028", "AA", "Banks", 2.75, "2028-03-01", "DE", "BOND"),
]
HEADER = ["id", "name", "instrument", "issuer_type", "currency", "nominal", "price", "market_value", "coupon",
          "maturity", "country", "rating", "sector", "contract", "lots", "lookthrough", "proxy", "wam_days",
          "wal_days", "fx_rate"]


def rating_z(rating: str, sector: str) -> float:
    base = rating.rstrip("+-")
    bp = Z_SPREAD_BP[base] + (FIN_ADD_BP if any(k in sector.lower() for k in ("bank", "insur", "real")) else 0)
    return bp * 1e-4


def corporate_price(snap, coupon: float, maturity: str, z: float, curve_name: str = "BUND") -> float:
    conv = replace(get_convention("BUND"), name="EUR_CREDIT")
    m = date.fromisoformat(maturity)
    bond = FixedRateBond(isin="X", coupon=coupon / 100, maturity=m, issue_date=m.replace(year=m.year - 12),
                         convention=conv)
    settle = bond.settlement_date(AS_OF)
    dirty = bond.dirty_price_from_curve(SpreadedCurve(snap.gov_curve(curve_name), z), settle)
    return round(dirty - bond.accrued_interest(settle), 3)


def main() -> None:
    snap = api.market_snapshot(AS_OF, settings=load_settings(overrides={"provider": "static", "fallback": []}))
    rows: list[dict] = []
    for country, year, nominal in SOVEREIGNS:
        bond = next(b for b in snap.bonds.values() if b.issuer == country and b.maturity.year == year)
        rows.append({"id": bond.isin, "name": bond.name, "instrument": "BOND", "issuer_type": "GOV",
                     "currency": "EUR", "nominal": int(nominal), "country": country, "rating": "",
                     "price": round(snap.prices[bond.isin], 3)})
    rows.append({"id": "TEST-ES-2033", "name": "Bonos fictif 3.25% 2033", "instrument": "BOND", "issuer_type": "GOV",
                 "currency": "EUR", "nominal": 6_000_000, "coupon": 3.25, "maturity": "2033-04-30", "country": "ES",
                 "price": corporate_price(snap, 3.25, "2033-04-30", 0.0015, "OAT")})
    for cid, name, rating, sector, cpn, mat, nominal, country, issuer in CORPORATES:
        rows.append({"id": cid, "name": name, "instrument": "BOND", "issuer_type": issuer, "currency": "EUR",
                     "nominal": int(nominal), "coupon": cpn, "maturity": mat, "country": country, "rating": rating,
                     "sector": sector, "price": corporate_price(snap, cpn, mat, rating_z(rating, sector))})
    for fid, contract, lots, priced in FUTURES:
        row = {"id": fid, "name": f"{contract} (couverture)", "instrument": "FUTURE", "currency": "EUR",
               "contract": contract, "lots": lots}
        if priced:
            row["price"] = 128.42   # prix de future fictif, sert à tester la règle de la base brute
        rows.append(row)
    rows += [
        {"id": "TEST-MMF-A", "name": "Fonds monétaire A (WAM / WAL déclarées)", "instrument": "MMF",
         "currency": "EUR", "market_value": 20_000_000, "wam_days": 38, "wal_days": 85},
        {"id": "TEST-MMF-B", "name": "Fonds monétaire B (transparence)", "instrument": "MMF", "currency": "EUR",
         "market_value": 15_000_000, "lookthrough": "MMF-B"},
        {"id": "TEST-FUND-IG", "name": "Fonds crédit IG (sans composition)", "instrument": "FUND", "currency": "EUR",
         "market_value": 10_000_000, "proxy": "EUR_IG_CORP"},
        {"id": "TEST-FUND-LT", "name": "Fonds crédit (transparence)", "instrument": "FUND", "currency": "EUR",
         "market_value": 8_000_000, "lookthrough": "CREDIT-LT"},
        {"id": "TEST-CASH-EUR", "name": "Trésorerie EUR", "instrument": "CASH", "currency": "EUR",
         "market_value": 5_000_000},
        {"id": "TEST-CASH-USD", "name": "Trésorerie USD", "instrument": "CASH", "currency": "USD",
         "market_value": 1_000_000, "fx_rate": 0.8915},
    ]
    out = ROOT / "samples" / "test_credit_eur.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, HEADER)
        w.writeheader()
        w.writerows(rows)
    lt_rows = []
    for fund, weight, lid, name, rating, sector, cpn, mat, country, instrument in LOOKTHROUGH:
        row = {"fund": fund, "weight": weight, "id": lid, "name": name, "instrument": instrument, "currency": "EUR",
               "country": country, "rating": rating, "sector": sector}
        if instrument == "BOND":
            row.update(issuer_type="CORP", coupon=cpn, maturity=mat, price=corporate_price(snap, cpn, mat,
                                                                                          rating_z(rating, sector)))
        else:
            row["market_value"] = 100
        lt_rows.append(row)
    out_lt = ROOT / "samples" / "test_lookthrough.csv"
    with out_lt.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, ["fund", "weight", "id", "name", "instrument", "issuer_type", "currency", "coupon",
                                "maturity", "price", "market_value", "country", "rating", "sector"])
        w.writeheader()
        w.writerows(lt_rows)
    print(f"{out} ({len(rows)} lignes), {out_lt} ({len(lt_rows)} lignes)")


if __name__ == "__main__":
    main()
