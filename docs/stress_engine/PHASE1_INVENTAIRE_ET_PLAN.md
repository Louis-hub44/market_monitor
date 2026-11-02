# Moteur de stress tests et de scénarios — Phase 1 : inventaire et plan

Base analysée : `portfolio-intelligence_V7.zip`, avec ses 157 tests verts sous Python 3.13.
Ce document précède le code : aucune ligne n'est écrite avant validation.

> **Révision 2 (02/11/2026).** Trois éléments ont été reçus depuis la première version : Rates & Bond Analytics 1.1, Market Monitor 1.6.6 et l'historique des bandeaux. Il n'y a ni portefeuille réel ni série de NAV. Le **§8 remplace** les §2.2, §2.3, §4, la partie « Revalorisation » du §5 et le §7 partout où ils divergent.

---

## 1. Inventaire de l'existant en risque

### 1.1 Ce qui existe et sera réutilisé

| Brique | Fichier | Contenu | Réutilisation |
| --- | --- | --- | --- |
| Noyau obligataire | `services/fixed_income/bond_math.py` | échéancier, prix/YTM (bissection), duration, convexité, DV01, `ZeroCurve` (interpolation linéaire), duration et convexité effectives, KRD par bump triangulaire (Σ KRD = duration effective, testé), spread duration | **Socle de la revalorisation et du mapping des taux.** Repris tel quel et complété (z-spread calibré sur le prix, choc de courbe arbitraire). |
| Analytique obligataire | `services/fixed_income/analytics.py` | `analyse_position` / `analyse_portfolio` : chemin courbe (repricing) ou analytique, couverture, `PositionRisk` | Source des sensibilités du mapping. La branche Bloomberg disparaît. |
| Courbes | `services/fixed_income/curves.py` | `curve_from_tenors`, `build_curves`, `CURVE_SHAPES`, `interpolate_shape` | Formes de courbe des scénarios hypothétiques (après correction, §1.3). |
| Moteur de scénarios | `services/scenarios/engine.py` | `ScenarioEngine` : impact en % de NAV par choc et par position, formule explicite, niveau de confiance | **Conservé.** Il devient un adaptateur : `Shock` est traduit en vecteur de facteurs (phase 4), ce qui évite de maintenir deux moteurs. Ses tests restent verts. |
| Bibliothèque | `services/scenarios/library.py` | taux ±, courbes, actions, FX, crédit, pétrole, 6 macros, `custom_scenario` | Migrée vers la bibliothèque YAML (phase 4). |
| Modèle de résultat | `models/scenario.py` | `Shock`, `Scenario`, `Contribution` (avec formule), `ScenarioResult` (`by_asset_class`, `top_contributors`, `explain`) | **Étendu**, pas remplacé : ajout du facteur, du montant en EUR et de la méthode de revalorisation. |
| Domaine | `models/portfolio.py`, `models/datapoint.py`, `models/enums.py` | `Position` (champs obligataires, sensibilités importées), `Portfolio.coverage()`, `DataPoint` qualifié | Réutilisés. Ajout des champs futures (contrat, nombre de lots, CTD) et fonds (look-through). |
| Métriques | `services/risk/metrics.py` | `to_series`, `returns`, drawdown, VaR 95 % 1 j historique (percentile), corrélations, bêta MCO | Les utilitaires de séries et le bêta sont repris. La VaR actuelle est remplacée (§1.2). |
| Bêtas actions | `services/equity/factors.py` | `estimate_betas` sur un proxy d'indice | Mapping actions → bêta par indice. |
| Import | `services/portfolio/loader.py` | CSV/Excel FR/EN, unités par colonne, détection nominal/valorisation | Étendu aux futures (aujourd'hui exclus, §1.2). |
| Données | `data/market_data.py`, `data/providers/{fmp,yahoo}.py`, `data/cache.py` | chaîne FMP → Yahoo → cache, `DataPoint`, rate limiting, journal | Réutilisés. Ajout des providers ECB, FRED et Stooq ainsi que de l'import de séries CSV/Excel. |
| Réseau | `data/network.py` | CA d'entreprise, diagnostic SSL, session yfinance | Déplacé dans `core/network.py` et durci (§3). |
| UI | `views/dashboard.py`, `views/diversified.py`, `components/charts.py` (rail de risque, impacts classés) | pires scénarios, matrice d'impact, scénario sur mesure | Composants graphiques réutilisés dans la nouvelle page Stress & VaR. |

### 1.2 Ce qui manque ou ne convient pas

| Sujet | Constat | Conséquence |
| --- | --- | --- |
| **VaR** | Seule VaR existante : un percentile 5 % des rendements de la NAV reconstruite sur les **actions, fonds et matières premières uniquement** (`portfolio/analytics.py`). Les obligations n'ont pas de série de prix. | Sur un portefeuille de crédit, la VaR affichée au dashboard est **vide ou fausse**. La VaR doit être construite sur les facteurs, pas sur les prix des lignes. |
| ES, VaR paramétrique et Monte Carlo, GARCH, décomposition, backtesting | Absents | À construire (phases 6 et 7). |
| **Futures** | Le loader les classe en `OTHER` et les **exclut** de la duration et des scénarios (« duration brute, avant couverture »). | Les couvertures EUREX sont ignorées : le risque taux est surestimé. Mapping CTD à construire (phase 2). |
| Fonds | Traités comme des actions (prix et bêta). Rien n'est prévu pour les fonds monétaires ni le look-through. | À construire. |
| **Courbes EUR** | `treasury_curve` ne sert que la courbe US hors Bloomberg. Les obligations EUR retombent sur des **sensibilités analytiques, sans KRD**. Constaté sur l'échantillon : « Pas de courbe disponible ». | Branchement de la courbe AAA de la BCE et des taux souverains par pays (§4). |
| Spread vs prix | En présence d'une courbe, `analyse_position` actualise avec `spread_bp` du fichier. Le commentaire dit « spread calibré pour reproduire le prix », mais aucun calibrage n'est fait. | La revalorisation complète exige un z-spread résolu sur le prix (phase 3). |
| Facteurs de crédit | Un seul choc `CREDIT_SPREAD` pour tout le crédit, sans notation ni secteur | Taxonomie à construire (phase 2). |
| Vol | Aucun facteur | Ajouté (VIX/VSTOXX). Exposition nulle tant qu'il n'y a pas d'option en portefeuille. |
| Limites de risque, exports Excel/PDF | Aucun YAML (PyYAML n'est même pas dans `requirements.txt`). L'Excel sert en lecture seule et il n'y a pas d'export PDF. | À construire (phase 9). |
| Module « Rates & Bond Analytics » (objets `Curve`) | **Absent de la V7.** Seule `ZeroCurve` existe. | Interface `CurveProvider` conçue pour le brancher (§2.3). D'ici là, `ZeroCurve` sert de courbe de référence. |
| Allocation, CVA, backtester | Absents de la V7 | Le moteur expose une API Python stable qu'ils consommeront (§2.1). |

### 1.3 Défauts relevés dans le code existant (vérifiés par exécution)

1. **Choc de taux appliqué sans tenir compte de sa cible.** `ScenarioEngine._impact` ignore `shock.target` pour `RATES_PARALLEL`. Dans le scénario « Choc inflationniste », un Bund reçoit **USD +150 bp et EUR +100 bp, soit +250 bp**. Sur le portefeuille obligataire d'exemple, le scénario ressort à −15,7 % alors que « Taux +100 bp » donne −6,4 %. Même défaut sur les chocs actions ciblés : « Nasdaq −10 % » frappe toutes les actions.
2. **`BULL_FLATTENER` est identique à `BULL_STEEPENER`** dans `CURVE_SHAPES` (−50 / … / −25 dans les deux cas). Un bull flattener fait baisser davantage les taux longs.
3. La spread duration de repli est fixée à `0,95 × duration modifiée` en dur dans `analyse_position`, alors qu'une hypothèse `spread_duration_ratio` existe dans les hypothèses de modèle.
4. Le choix de la courbe pour une position (`_position_curve`) retombe sur **n'importe quelle** courbe disponible. Une obligation EUR peut ainsi être valorisée sur la courbe US, ce qui contredit `build_curves`, qui s'interdit justement de fabriquer une courbe d'une autre devise.

Correctifs proposés dans la phase 4 (points 1 et 2) et dans la phase 3 (points 3 et 4), chacun avec un test de non-régression.

### 1.4 Bloomberg à retirer

77 occurrences dans 10 fichiers : `data/providers/bloomberg.py` (554 lignes), `tests/test_bloomberg.py` (23 tests), `services/portfolio/enrichment.py` (enrichissement YAS), la branche `risk_source == "BLOOMBERG"` de `analytics.py`, `BLOOMBERG_*` dans `constants.py`, `ENABLE_BLOOMBERG*` dans `settings.py` et `.env.example`, la tête de chaîne de `market_data.py`, `radar.py` (calendrier macro), `utils/state.py`, `views/data_quality.py`, ainsi que le README et l'ARCHITECTURE (§4.7).
Retrait prévu en début de phase 2, en un commit isolé. Le nombre de tests passera de 157 à environ 134 avant que les nouveaux tests ne s'ajoutent.

---

## 2. Architecture cible

### 2.1 Principe

Le moteur vit dans `services/stress/`. Il n'importe ni `streamlit` ni `views/`, et **ne fait aucun appel réseau** : il reçoit un `Portfolio`, un `MarketState` (courbes, niveaux) et un `FactorHistory` (DataFrame de mouvements de facteurs). C'est ce qui le rend appelable tel quel par l'allocation, la CVA et le backtester, et testable hors ligne.

```python
engine = StressEngine(portfolio, market_state, history, config=RiskConfig.load())
exposures = engine.map()                        # positions → facteurs
res = engine.run(scenario)                      # P&L par ligne / facteur / classe
var = engine.var(method="mc", level=0.99, horizon=10, garch=True)
bt  = engine.backtest(realised_pnl)             # Kupiec, Christoffersen, feux tricolores
rev = engine.reverse(loss=0.05)                 # scénario le plus plausible pour -5 %
```

### 2.2 Modules

```
core/network.py                 session HTTP unique (proxy, truststore / CA, jamais verify=False)
scripts/check_network.py        diagnostic autonome, sonde chaque source
data/providers/ecb.py           SDMX CSV : courbes YC, €STR, change EXR
data/providers/fred.py          fredgraph.csv (sans clé) ou API avec clé
data/providers/stooq.py         CSV quotidiens (rendements souverains par pays et maturité)
data/providers/file_series.py   import de séries CSV/Excel (spreads, iTraxx, NAV réalisée…)
data/factor_history.py          construit FactorHistory depuis config/risk_factors.yaml
config/risk_factors.yaml        facteur → source(s), ticker, unité, transformation
config/scenarios/*.yaml         bibliothèque extensible (historiques + hypothétiques)
config/risk_limits.yaml         limites et seuils d'alerte
services/stress/
  factors.py                    taxonomie des identifiants de facteurs, FactorSpace
  instruments/                  bond.py · future.py (CTD) · fund.py · mmf.py · equity.py · fx.py
  mapping.py                    Position → [Exposure(facteur, delta, gamma)]
  revaluation.py                FullRevaluer | DeltaGammaRevaluer, rapport d'écart
  scenarios.py                  modèle FactorScenario, hypothétiques, chargeur YAML
  historical.py                 rejeu sur fenêtre, calibration, proxys de facteurs manquants
  var.py                        historique, paramétrique, Monte Carlo (+ GARCH filtré)
  garch.py                      GARCH(1,1) par maximum de vraisemblance (SciPy)
  decomposition.py              VaR/ES marginale et par composante (Euler)
  backtest.py                   Kupiec, Christoffersen, feux tricolores bâlois
  reverse.py                    reverse stress test de Mahalanobis
  limits.py                     contrôle des limites, alertes
  report.py                     StressReport → exports/excel.py, exports/pdf.py
  engine.py                     façade StressEngine
views/stress.py                 page Streamlit (phase 9)
```

### 2.3 Taxonomie des facteurs

| Famille | Identifiant | Unité du choc | Sensibilité |
| --- | --- | --- | --- |
| Taux | `IR.EUR.<pilier>`, avec la courbe AAA de la BCE comme courbe de base | bp absolus | KRD × valeur (DV01 par pilier), convexité |
| Spread souverain | `SOV.<pays>.<pilier>` (IT, FR, ES…, contre la base) | bp | DV01 de la ligne sur ce pays |
| Spread de crédit | `CS.EUR.<IG\|HY>.<notation>.<FIN\|NONFIN>` | bp (absolus par défaut, relatifs en option) | spread duration × valeur |
| Actions | `EQ.<indice>` (SX5E, SPX…) | rendement en % | bêta × valeur |
| Change | `FX.<devise>` contre EUR | % | delta = valeur en devise |
| Vol | `VOL.<indice>` | points de vol | vega (nul sans option) |
| Matières premières | `CMD.<BRENT\|GOLD…>` | % | delta |
| Monétaire | `IR.EUR.ESTR` | bp | duration (WAM) du fonds |

Piliers : 3M, 1Y, 2Y, 5Y, 10Y, 20Y, 30Y (`KEY_RATE_TENORS`), soit les nœuds de la courbe BCE.
`CurveProvider` est un protocole : `ZeroCurve` l'implémente aujourd'hui et les objets `Curve` du module Rates & Bond Analytics s'y brancheront sans toucher au moteur.

---

## 3. Réseau d'entreprise (`core/network.py`)

- **Une seule session** `requests` fabriquée par `get_session()` : tous les providers (ECB, FRED, Stooq, FMP, Yahoo) passent par elle, et aucun `requests.get` ne reste isolé. Un test le contrôle par une recherche dans le code source.
- **Proxy** configurable par `PI_HTTPS_PROXY` / `PI_HTTP_PROXY` / `PI_NO_PROXY`. À défaut, les variables standard `HTTPS_PROXY` / `NO_PROXY` s'appliquent.
- **Vérification TLS**, dans cet ordre :
  1. `truststore` : utilise le magasin de certificats du système d'exploitation, où l'informatique a déjà déployé la racine du proxy ;
  2. sinon le bundle CA de l'entreprise (`PI_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`) ;
  3. sinon `certifi`.
- **`verify=False` disparaît** : `PI_INSECURE_SSL`, la case de l'interface et `silence_insecure_warnings()` sont supprimés. Un test vérifie qu'aucune session ne peut finir avec `verify is False`.
- **yfinance** : `curl_cffi` n'utilise pas le module `ssl` de Python, donc il ignore truststore. On lui impose la session centrale.
- **`scripts/check_network.py`** affiche la configuration effective (proxy, source de confiance) puis sonde ECB, FRED, Stooq, FMP et Yahoo. Pour chaque échec, il qualifie la cause (`ssl`, `proxy`, `dns`, `timeout`, `http`) et donne le correctif. Code retour non nul si une source critique est KO.

Nouvelles dépendances : `scipy` (désormais dans le noyau), `truststore`, `pyyaml`, `matplotlib` pour le PDF (déjà utilisé par Market Monitor). Pas de `arch` : le GARCH(1,1) est écrit sur SciPy (environ 60 lignes, testées contre une série simulée aux paramètres connus).

---

## 4. Sources de données par facteur (sans Bloomberg)

| Facteur | Source primaire | Repli | Historique | Remarques |
| --- | --- | --- | --- | --- |
| Courbe EUR (base) | BCE `YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_<pilier>` (AAA, taux spot) | courbe tous émetteurs (`G_N_C`) | depuis le 6 septembre 2004 | Couvre toutes les fenêtres historiques. |
| €STR | BCE `EST/B.EU000A2X2A25.WT` | EONIA via fichier | depuis octobre 2019 | Avant 2019 : EONIA importé ou repli sur le pilier 3M de la courbe. |
| Rendements souverains DE, FR, IT, ES (2Y, 5Y, 10Y, 30Y) | Stooq (`10dey.b`, `10ity.b`… *à valider en phase 5*) | import CSV | variable selon le ticker | Alimentent `SOV.*`, donc le spread BTP-Bund. |
| Spreads de crédit EUR | **import CSV/Excel** (iTraxx Main et Crossover, indices ICE ou iBoxx exportés de vos outils) | FRED `BAMLHE00EHYIOAS` (HY EUR) ; ETF Yahoo (IEAC, IHYG) en ultime proxy | *voir le risque ci-dessous* | Point dur du projet. |
| Actions | Yahoo (`^STOXX50E`, `^GSPC`…), FMP | Stooq | > 2007 | |
| Change | BCE `EXR/D.<CCY>.EUR.SP00.A` | Yahoo | depuis 1999 | |
| Vol | FRED `VIXCLS` | Yahoo `^V2TX` | VIX depuis 1990 | |
| Matières premières | FRED `DCOILBRENTEU`, Yahoo `BZ=F`, `GC=F` | Stooq | long | |

**Risque principal : l'historique des spreads de crédit.** FRED ne diffuse plus, à ma connaissance, que les trois dernières années des séries ICE BofA, et il n'existe pas de source gratuite quotidienne de spreads EUR par notation qui remonte à 2008. Je le vérifierai en phase 5. Si c'est confirmé, les scénarios Lehman, zone euro et Covid exigeront un **import CSV des spreads**. À défaut, ils seront calibrés par proxy (spreads US par notation × bêta estimé sur la période commune) et marqués **confiance faible** dans le rapport. Aucune série ne sera inventée.

---

## 5. Choix méthodologiques

**Futures EUREX.** Spécifications en YAML : Schatz (1,75–2,25 ans), Bobl (4,5–5,5), Bund (8,5–10,5), Buxl (24–35), OAT long terme (8,5–10,5), coupon notionnel de 6 % et contrat de 100 000 EUR. Pour chaque obligation du panier, le facteur de conversion suit la formule EUREX. Le CTD est retenu dans cet ordre :
1. CTD fourni dans le fichier (ISIN) ;
2. sinon, minimum de la base nette `P − F × CF` si les prix du panier sont disponibles ;
3. sinon, obligation notionnelle à 6 % au milieu de la fenêtre de livraison, **marquée approximation**.

Sensibilité : `DV01_fut = DV01_CTD / CF × lots × 1 000` (taille du contrat / 100), ventilée sur les KRD du CTD. Une valeur de marché nulle n'empêche plus le calcul du risque.

**Fonds.** Si un fichier de composition est fourni, chaque ligne sous-jacente est mappée récursivement au prorata. Sinon, le fonds est représenté par un indice proxy en YAML (bêta, duration et spread duration déclarés), marqué *proxy*. Les fonds monétaires sont mappés sur `IR.EUR.ESTR` avec une duration égale à leur WAM (≤ 60 jours pour un MMF court terme), et le crédit est ignoré par défaut, en option via la WAL.

**Revalorisation.**
- *Complète* : flux re-actualisés sur la courbe choquée, en ajoutant `SOV.*` et `CS.*` au z-spread calibré sur le prix observé.
- *Delta-gamma* : `−Σ KRDₜ·Δyₜ − SD·Δs + ½·C·(Δy+Δs)²` × valeur.

Le rapport affiche l'écart entre les deux par ligne et par scénario. Critère de test : moins de 2 bp de valeur jusqu'à ±100 bp, ce qui est cohérent avec le test existant sur l'approximation d'ordre 2.

**Scénarios historiques.** Mouvement point à point des facteurs sur la fenêtre réelle de choc. Le pire sous-intervalle de N jours est disponible en option. Taux en bp absolus, spreads en absolus par défaut ou en relatif (Δs × s₀/s_fenêtre) pour ne pas sous-estimer un choc de 2008 appliqué aux niveaux actuels. Fenêtres proposées, à valider ensemble :

| Scénario | Fenêtre | Ancrage |
| --- | --- | --- |
| Lehman | 12/09/2008 → 10/10/2008 | faillite le 15/09, pic de panique la semaine du 6/10 |
| Zone euro | 01/07/2011 → 25/11/2011 | pic du spread BTP-Bund en novembre |
| Covid | 21/02/2020 → 23/03/2020 | jusqu'à l'annonce du PEPP et de la Fed |
| Choc de taux 2022 | 03/01/2022 → 21/10/2022 | creux → pic du Bund 10 ans |
| SVB | 08/03/2023 → 20/03/2023 | jusqu'au rachat de Credit Suisse |
| Choc tarifaire | 02/04/2025 → 09/04/2025 | « Liberation Day » → suspension des droits de douane |

Les facteurs absents d'une fenêtre sont reconstruits par régression sur les facteurs présents (projection conditionnelle), et le rapport le signale.

**VaR / ES.**
- *Historique* : portefeuille actuel revalorisé sur les mouvements de facteurs de la période, ce qui la rend valable pour les obligations.
- *Paramétrique* : delta-normal, avec une option delta-gamma Cornish-Fisher.
- *Monte Carlo* : normal ou Student-t. L'option GARCH filtre chaque facteur par un GARCH(1,1), rééchantillonne les résidus standardisés (bootstrap filtré) et les remet à l'échelle de la volatilité conditionnelle du jour.
- *Horizon 10 jours* : rendements 10 j chevauchants pour l'historique, simulation sur 10 jours pour le Monte Carlo, √10 seulement pour le paramétrique, ce que le rapport affiche.
- *Décomposition* : par Euler. Analytique en paramétrique ; en historique et Monte Carlo, par l'espérance conditionnelle des P&L de ligne dans la queue, lissée par noyau pour la VaR. Agrégation par ligne, par classe et par facteur, avec un contrôle que la somme des composantes égale la VaR.

**Backtesting.**
- P&L hypothétique : portefeuille figé, revalorisé sur les mouvements réalisés.
- P&L réalisé : série de NAV importée en CSV.
- Tests : Kupiec (POF), indépendance de Christoffersen et couverture conditionnelle.
- Feux tricolores bâlois sur 250 jours à 99 % : vert de 0 à 4 exceptions, jaune de 5 à 9, rouge à partir de 10.

**Reverse stress test.**
- Linéaire : solution fermée `x* = −L·Σg / (gᵀΣg)`, qui donne le scénario de distance de Mahalanobis minimale tel que le P&L vaut −L.
- Non linéaire : on part de cette solution et on résout par SciPy `minimize` (SLSQP) sous la contrainte d'égalité sur le P&L revalorisé.

Le rapport donne la distance, la probabilité implicite (χ²) et les facteurs dominants.

---

## 6. Phases et critères de sortie

| Phase | Livrable | Critère « propre et testé » |
| --- | --- | --- |
| 2 | Retrait de Bloomberg, `core/network.py` + `check_network.py`, providers ECB/FRED/Stooq/CSV, taxonomie, mapping (obligations, futures via CTD, fonds, MMF, actions, FX) | Σ KRD = duration ; DV01 d'un future = DV01 CTD / CF (cas chiffré à la main) ; look-through = somme des sous-jacents ; aucune session sans vérification ; tests V7 verts |
| 3 | Revalorisation complète et delta-gamma, z-spread, rapport d'écart ; correctifs §1.3 points 3 et 4 | Le z-spread reproduit le prix à 1e-8 près ; écart delta-gamma / complète < 2 bp à ±100 bp ; zéro-coupon exact |
| 4 | Scénarios hypothétiques + YAML ; adaptateur de l'ancien moteur ; correctifs §1.3 points 1 et 2 | Additivité ; ciblage par devise testé ; bull flattener ≠ bull steepener ; anciens tests verts |
| 5 | Scénarios historiques, calibration, proxys de facteurs | Fixtures de séries figées (sans réseau) ; mouvements de fenêtre recalculés à la main sur 2 facteurs |
| 6 | VaR et ES (3 méthodes, GARCH), décomposition | Normal : VaR paramétrique = formule fermée ; convergence MC → paramétrique ; somme des composantes = VaR ; GARCH retrouve des paramètres simulés |
| 7 | Backtesting | Kupiec et Christoffersen confrontés à des valeurs publiées ou à des statistiques recalculées ; bornes des feux tricolores |
| 8 | Reverse stress test | Cas linéaire = solution fermée ; contrainte de perte respectée à 1e-6 près |
| 9 | Page Streamlit, limites YAML, alertes, export Excel/PDF | `AppTest` en mode dégradé ; classeur relu (formules, feuilles) ; PDF généré |
| 10 | Tests de bout en bout sur le portefeuille réel, README/ARCHITECTURE, finitions | Rapport complet sans réseau à partir du cache et des fixtures |

---

## 7. Éléments attendus de l'utilisateur

1. **Le fichier du portefeuille de crédit réel** (CSV/Excel) : ISIN, nominal, prix, coupon, maturité, notation, secteur, pays, et pour les futures le contrat, le nombre de lots et le CTD si connu. Sans lui, les tests « réels » ne peuvent pas avoir lieu, et je n'utiliserai pas les portefeuilles fictifs de `samples/` à sa place.
2. **Le module Rates & Bond Analytics** (objets `Curve`) : il ne figure pas dans la V7. Le moteur s'y branchera via `CurveProvider`.
3. **Les historiques de spreads de crédit** (iTraxx, indices par notation) si vous pouvez les exporter : condition d'un rejeu fiable de 2008, 2011 et 2020.
4. **La série de NAV ou de P&L quotidien réalisé**, pour le backtesting sur P&L réel en plus du P&L hypothétique.
5. **L'emplacement du code** : la V7 n'est pas dans ce dépôt, qui contient Market Monitor.

---

## 8. Révision 2 — intégration de Rates & Bond Analytics et de Market Monitor 1.6.6

### 8.1 Inventaire complémentaire

| Brique | Version, tests | Apport au moteur de stress |
| --- | --- | --- |
| **Rates & Bond Analytics** (`rates_analytics`) | 1.1.0. 348 tests sur 350 passent ; les 2 échecs sont des tests oracle (§8.6). | **Remplace `bond_math.py` pour la revalorisation complète.** Il apporte les conventions de place (TARGET, ACT/ACT ICMA, échéanciers), `FixedRateBond` (couru, prix clean et dirty), `YieldCurve` / `InterpolatedCurve` / `NelsonSiegelSvenssonCurve`, `SpreadedCurve` (choc zéro par morceaux, validé contre QuantLib à 1e-11) et `CurveSet` €STR / EURIBOR 6M. Les courbes souveraines NSS Bund, OAT et BTP sont construites sur les taux génériques quand les données titre par titre manquent. Il fournit aussi `yas` / `curve_risk` (KRD en tente, Σ KRD = duration effective), `z_spread`, `CurveScenario` (parallèle, twist, steepener, flattener, butterfly, custom, IRRBB, additionnables), `scenario_pnl` (Z-spread calibré puis tenu constant) et une API stable (`api.py`). |
| Couche de données RBA | `MarketDataService.history(ids, start, end)` sur des ids logiques, avec chaîne de sources, cache parquet et `DataReport` (manquants, périmés, suspects) | **Devient la couche de données des facteurs.** Ids déjà servis : `EUR.GOVT.{DE,FR,IT,ES}.{2Y,5Y,10Y,30Y}` (CNBC → Bundesbank → Stooq), les zéros Svensson Bundesbank `EUR.BBK.DE.ZERO.*`, les paramètres Svensson AAA de la BCE et l'€STR. |
| Réseau RBA (`network.py`) | Modes `system` (truststore), `ca`, `standard` et `insecure` ; adaptateur HTTPS truststore compatible avec le proxy | **Base de `core/network.py`.** Le mode `insecure` sera supprimé. |
| **Market Monitor 1.6.6** | 480 tests verts | Clients publics éprouvés derrière un proxy : `StooqClient`, `BundesbankClient`, `FredClient` et `FredApiClient` (serveur de secours avec clé), `CnbcClient`, `StoxxClient`, `MsciClient`. Contrôle croisé Stooq / CNBC à 1,5 pb, futures Brent recollés. **`FredClient` et `FredApiClient` sont portés** dans la couche de données RBA. Market Monitor n'est pas importé par le moteur, pour éviter un couplage entre applications. |
| `historique_bandeaux.csv` | 378 lignes du 29/09 au 28/10/2026 ; 21 séances pour la plupart des instruments ; OAT-Bund, HY Europe OAS et HY US OAS depuis le 09/10 (14 obs.) ; Italie 10Y seulement jusqu'au 08/10 | Format **long** (`date, instrument, niveau, variation, source`) pris en charge par l'import `file_series`. Il sert au **contrôle croisé** des niveaux récents, Stooq contre saisie, et documente les sources. **Il ne permet pas de calibrer** les fenêtres historiques : un mois d'historique, et aucune donnée de crédit avant octobre 2026. |

### 8.2 Ce que cela change

**Revalorisation complète** (phase 3).
- **Obligations** : `FixedRateBond` + `SpreadedCurve` de RBA, en remplacement de `bond_math`.
- **Courbe de référence** : la courbe souveraine du pays de la ligne (Bund, OAT, BTP, Bonos). Pour le crédit, la courbe Bund et un Z-spread calibré sur le prix. Ce choix est cohérent avec les OAS ICE, mesurés contre les souverains.
- **Chocs de taux** : `CurveScenario` en mode `zero` sur la courbe NSS, c'est-à-dire la convention RBA des courbes souveraines.
- **Choc de spread** : il s'ajoute au Z-spread.
- **Approximation delta-gamma** : calculée à partir de `curve_risk` (KRD, convexité effective) et de la spread duration. L'écart entre les deux méthodes est publié.

**Facteurs de taux** (remplace le §2.3).
- **Facteurs** : `IR.<pays>.<pilier>` pour les pays DE, FR, IT et ES et les piliers 2Y, 5Y, 10Y et 30Y, plus `IR.EUR.ESTR`. Ce sont exactement les séries dont l'historique quotidien gratuit existe.
- **KRD** : les 10 ténors de RBA (6M … 30Y) sont projetés sur ces piliers par les tentes linéaires de `CurveScenario.custom`. Le choc d'un pilier est donc reproduit à l'identique dans la revalorisation complète.
- **Spread BTP-Bund** : `IR.IT.10Y − IR.DE.10Y`. Un scénario « BTP-Bund +100 pb » choque les piliers IT à piliers DE constants.

**Futures EUREX.** Les paniers livrables sont tirés de l'univers obligataire RBA par pays et fenêtre de maturité. La V7 et RBA fournissent les prix, et le CTD est retenu sur la base nette. Sans prix de marché, faute d'univers réel en données gratuites, le CTD est approché par une obligation notionnelle comme prévu au §5, avec le signalement correspondant.

### 8.3 Historique des spreads de crédit : stratégie révisée

La limite est confirmée par Market Monitor : FRED ne diffuse que **3 ans** des indices ICE BofA, soit novembre 2023 → aujourd'hui. Seul le choc tarifaire de 2025 est couvert directement. Il n'existe pas de série gratuite d'OAS EUR IG.

| Fenêtre | Source du choc de spread | Confiance |
| --- | --- | --- |
| Choc tarifaire 2025 | FRED `BAMLHE00EHYIOAS` (HY EUR) ; IG EUR par ETF (ci-dessous) | élevée (HY), moyenne (IG) |
| SVB 2023, choc de taux 2022, Covid 2020, zone euro 2011 | **Spread implicite d'ETF crédit EUR** (Yahoo : iShares € Corp IG et € HY), soit le rendement de l'ETF moins le rendement du Bund de même duration, rapporté à la spread duration de l'ETF, déclarée en YAML. Contrôle croisé avec `BAA10Y` (FRED, Moody's, hors licence ICE, historique complet). | moyenne |
| Lehman 2008 (avant les ETF EUR) | `BAA10Y` (Baa − Treasury 10 ans, FRED, quotidien depuis 1986) × bêta EUR/US estimé sur la période commune | **faible**, signalée dans le rapport |
| Ventilation par notation et secteur | Ratios de spread duration × niveaux de spread **actuels** par notation, lus dans le portefeuille ou dans un YAML. C'est un choc proportionnel au spread, convention de place. | marquée hypothèse |

La disponibilité réelle de chaque ticker et sa profondeur seront vérifiées en phase 5, avant tout calibrage. Aucune valeur de niveau historique ne sera saisie de mémoire.

### 8.4 Portefeuille de test

Il n'y a pas de portefeuille réel. Je constitue donc un **portefeuille de test crédit EUR**, déclaré fictif dans son nom et ses identifiants, au même titre que les échantillons de RBA (`SMPL-…`). Le fichier `samples/test_credit_eur.csv` est conçu pour reproduire la structure demandée :
- souverains EUR : Bund, OAT, BTP, Bonos, sur 2 à 30 ans, choisis dans l'univers d'exemple RBA (DE 30, FR 30, IT 49 titres) ;
- une vingtaine de corporates EUR : AAA → BB, financières et non financières, 2 à 12 ans, avec Z-spreads plausibles par notation ;
- des futures EUREX en couverture : short Bund et Bobl, Schatz, Buxl, OAT ;
- deux fonds monétaires, l'un avec composition en look-through, l'autre sans, représenté par un proxy.

Il pourra être remplacé par un vrai fichier sans rien changer au moteur. Le gabarit des colonnes sera documenté.

### 8.5 Backtesting sans série de NAV

Le backtest porte sur le **P&L hypothétique** : portefeuille figé, revalorisé chaque jour sur les mouvements de facteurs réalisés. C'est l'un des deux P&L admis par le dispositif bâlois. Il teste la qualité du modèle de risque, mais pas l'effet du trading. L'entrée « P&L réel » reste prévue (CSV) pour le jour où une série de NAV existera.

### 8.6 Points relevés dans les nouveaux modules

1. **RBA, 2 tests oracle en échec avec QuantLib 1.44.** Les tests `test_yield_and_risk_match_quantlib[street-4]` et `[true-0]` portent sur la convexité : 0,096234 contre 0,096113. La documentation RBA dit l'échéancier aligné sur QuantLib 1.43. Les 348 autres tests passent. Correctif proposé : borner `QuantLib<1.44` dans les dépendances de développement, puis analyser l'écart à part. Le moteur de stress n'utilise pas QuantLib.
2. **Les trois projets contiennent un mode `verify=False`** : V7 `PI_INSECURE_SSL`, RBA `mode: insecure`, Market Monitor `insecure_ssl` / `--contourner-ssl`. Les trois ont aussi un provider Bloomberg.
3. **Code réseau en double** : la même logique SSL est écrite trois fois. Le module `core/network.py` unique les remplace dans la V7 et RBA par une fine couche de compatibilité, ce qui garde leurs API et leurs tests.

### 8.7 Architecture révisée (remplace le §2.2)

```
core/                         paquet partagé `terminal_core`
  network.py                  session unique : proxy, truststore → CA d'entreprise → certifi ; jamais verify=False
scripts/check_network.py      sonde ECB, Bundesbank, Stooq, CNBC, FRED, FMP, Yahoo
rates_bond_analytics/         RBA 1.1 (sans Bloomberg, sans mode insecure, + FRED / Yahoo / fichiers)
stress_engine/                nouveau paquet, src layout, dépend de rates_analytics et de terminal_core
  src/stress_engine/          factors · instruments (bond, future, fund, mmf, equity, fx) · mapping ·
                              revaluation · scenarios · historical · var · garch · decomposition ·
                              backtest · reverse · limits · report · api.py (contrat stable)
  config/                     risk_factors.yaml · scenarios/*.yaml · risk_limits.yaml · eurex.yaml
portfolio-intelligence/       V7 (sans Bloomberg) ; nouvelle page Stress & VaR = UI du moteur
```

Le moteur est un **paquet autonome** sur le modèle de RBA, et non un sous-dossier de la V7. L'allocation, la CVA et le backtester l'importent comme ils importent `rates_analytics`. Portfolio Intelligence n'en est qu'un consommateur.

### 8.8 Décisions demandées avant la phase 2

1. **Organisation du dépôt.** Ce dépôt contient Market Monitor **1.0.0** à la racine, alors que la version courante est 1.6.6. Proposition :
   - le dépôt devient le monorepo du terminal (arborescence du §8.7) ;
   - Market Monitor est mis à jour en 1.6.6 dans un sous-dossier `market_monitor/` ;
   - chaque projet garde son `pyproject.toml`, ses tests et sa CI.
2. **Périmètre du retrait de Bloomberg et de `verify=False`** : V7 et RBA d'office. Market Monitor 1.6.6 aussi, ou plus tard dans une tâche distincte ?
3. **Portefeuille de test fictif** (§8.4) : accord sur la composition.
