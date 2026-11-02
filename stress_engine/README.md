# Moteur de stress tests et de scénarios

Moteur de risque du terminal : facteurs de risque, revalorisation, scénarios hypothétiques et
historiques, VaR / ES, backtesting et reverse stress test. Il est **indépendant de toute interface** :
pas de Streamlit, aucun appel réseau. Il reçoit un portefeuille et un état de marché construit par
Rates & Bond Analytics, ce qui permet à l'allocation, à la CVA et au backtester de l'appeler comme
une bibliothèque. La conception complète se trouve dans `../docs/stress_engine/PHASE1_INVENTAIRE_ET_PLAN.md`.

**État : phase 2 terminée** (projection des positions sur les facteurs). Phases suivantes :
revalorisation complète / delta-gamma, scénarios, VaR, backtesting, reverse stress test, UI.

```bash
pip install -e ../core -e ../rates_bond_analytics -e ".[dev]"
pytest                                   # 60 tests, hors ligne
python scripts/generate_test_portfolio.py   # régénère le portefeuille de test fictif
```

```python
from datetime import date
from rates_analytics import api
from stress_engine import EngineConfig, MarketState, load_portfolio, load_lookthrough, map_portfolio

cfg = EngineConfig.load()
snap = api.market_snapshot(date(2026, 10, 6))                       # courbes, univers obligataire, prix
rates = cfg.factors["rates"]
market = MarketState.from_snapshot(snap, rates["curve_names"], rates["missing_curve_fallback"])
result = map_portfolio(load_portfolio("samples/test_credit_eur.csv"), market, cfg,
                       load_lookthrough("samples/test_lookthrough.csv"))
result.by_factor()      # deltas agrégés (P&L en EUR pour +1 bp / +100 %)
result.summary()        # méthode, qualité, valeur, DV01, CS01, notes par ligne
result.matrix()         # positions × facteurs
```

## Facteurs (`config/risk_factors.yaml`)

| Famille | Identifiant | Choc | Remarque |
| --- | --- | --- | --- |
| Taux | `IR.<pays>.<pilier>` : DE, FR, IT, ES × 2, 5, 10, 30 ans | bp | Piliers choisis là où l'historique quotidien gratuit existe. Spread BTP-Bund = `IR.IT.10Y − IR.DE.10Y`. |
| Monétaire | `IR.EUR.ESTR` | bp | Fonds monétaires et titres à moins d'un an. |
| Crédit | `CS.EUR.<notation>.<FIN\|NONFIN>` | bp | AAA → CCC. Notations S&P / Fitch / Moody's reconnues, secteur classé par mots-clés. |
| Actions | `EQ.<indice>` | rendement | Bêta × indice. |
| Change | `FX.<devise>` | rendement | Devise contre EUR. |
| Volatilité | `VOL.<indice>` | points | Exposition nulle tant qu'il n'y a pas d'option. |
| Matières premières | `CMD.<nom>` | rendement | |

Convention : `delta` est le P&L en EUR pour **+1 unité** de facteur. Une obligation détenue a donc un
delta de taux négatif (−DV01). Au premier ordre, le P&L d'un choc vaut `Σ delta · choc`.

## Projection des positions (`stress_engine.mapping`)

| Instrument | Méthode | Qualité |
| --- | --- | --- |
| Obligation (État, entreprise, supranational) | Objet `FixedRateBond` de Rates & Bond Analytics, valorisé sur la courbe souveraine du pays (Bund pour le crédit). Le **Z-spread est calibré sur le prix observé**. Les DV01 par pilier sont calculés par bumps en tente (leur somme égale la duration effective), le CS01 par le Z-spread. La convexité est conservée. | élevée (moyenne sans prix : prix théorique) |
| Future EUREX (Schatz, Bobl, Bund, Buxl, OAT, BTP) | Panier livrable de l'univers ; facteur de conversion = prix clean à 6 % le jour de livraison ; CTD = celui fourni, sinon base brute minimale, sinon prix converti minimal. `DV01 = DV01_CTD / CF × lots`. Valeur de marché nulle. | élevée (faible si obligation notionnelle) |
| Fonds avec composition | Transparence : chaque ligne est projetée puis pondérée (poids renormalisés si besoin). | élevée |
| Fonds sans composition | Profil de `config/fund_proxies.yaml` : duration sur les piliers encadrants, spread duration, bêtas. | faible (hypothèse) |
| Fonds monétaire | `DV01 = VM × WAM/365` sur l'€STR, `CS01 = VM × WAL/365` sur le crédit bancaire. Valeurs par défaut signalées. | moyenne |
| Action, matière première, cash | Direct (bêta, 1:1), plus le change hors EUR. | élevée |

Une ligne non projetable est marquée `EXCLUDED` avec sa raison, et la couverture du calcul
(`result.coverage()`) en tient compte. Toute approximation figure dans les notes de la ligne :
courbe de substitution, date de jouissance déduite, bêta ou WAM par défaut, prix théorique…

## Portefeuille de test (fictif)

`samples/test_credit_eur.csv` (43 lignes) et `samples/test_lookthrough.csv` (2 compositions) sont
**fictifs**, comme leurs identifiants `TEST-…`. Les souverains `SMPL-…` viennent de l'univers d'exemple,
lui-même synthétique, de Rates & Bond Analytics. Le portefeuille contient :
- 10 souverains EUR (Bund, OAT, BTP et un Bonos sans courbe propre) ;
- 20 corporates et 1 supranational, de AAA à BB-, financières et non financières, dont un papier commercial ;
- 6 futures EUREX vendeurs ;
- 2 fonds monétaires, l'un avec WAM/WAL, l'autre en transparence ;
- 2 fonds crédit, l'un en transparence, l'autre en proxy ;
- de la trésorerie en EUR et en USD.

Le script `scripts/generate_test_portfolio.py` le régénère à l'identique. Un vrai fichier suit le même
gabarit, décrit dans `stress_engine/portfolio.py` : en-têtes français ou anglais, `,` ou `;`.

## Limites connues (phase 2)

- Facteurs de conversion BTP : ils sont recalculés au rendement semestriel de 6 %, ce qui reste à
  confronter au facteur publié par EUREX. Le portage et l'option de livraison sont ignorés.
- Les obligations hors EUR utilisent une courbe de substitution, signalée. Les instruments à option
  (callables, FRN) sont traités comme des obligations à taux fixe.
- Les bêtas actions et les historiques de facteurs arrivent en phase 5. Les bêtas par défaut sont signalés.
