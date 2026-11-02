# Rates & Bond Analytics

Module « courbes de taux & analytics obligataires » du mini terminal de marché maison, inspiré de **GC** (courbes), **SWPM** (pricer de swaps) et **YAS** (analytics obligataires). Les objets produits ici (conventions, échéanciers, puis courbes) sont réutilisés par le Pricing Lab, le CVA Calculator et Portfolio Intelligence.

> **Statut : version 1.1.0, phases 1 à 10 terminées.** Le module couvre l'architecture, les conventions et la couche de données ; les courbes €STR et EURIBOR 6M en dual curve et les courbes souveraines NSS ; les analytics YAS et les spreads G / I / Z / ASW ; le pricer SWPM avec risque bucketé ; les scénarios de courbe ; le dashboard Streamlit ; une API d'intégration stable. Il compte 344 tests, est propre sous ruff et mypy, avec une CI sous Windows et Linux.

---

## 1. Architecture

```
rates_bond_analytics/
├── config/config.yaml          # provider, cache, mapping id logique -> tickers (aucun secret)
├── .env.example                # FMP_API_KEY, réseau (copier en .env)
├── src/rates_analytics/        # MOTEUR – n'importe jamais Streamlit ni QuantLib
│   ├── conventions/            # day counts, calendriers, BDC, tenors, échéanciers, conventions EUR
│   ├── data/                   # DataProvider abstrait + FMP / ECB / Public / Static, cache parquet
│   ├── curves/                 # YieldCurve, interpolations, bootstrap, €STR, EURIBOR, CurveSet, NSS, courbes proxy
│   ├── bonds/                  # obligation à taux fixe, analytics YAS (durations, KRD), spreads G/I/Z/ASW
│   ├── instruments/            # jambes de swap, OIS, swap vanille dual curve, fixings
│   ├── swaps/                  # pricer SWPM (NPV, couru, par, PV01, DV01) et risque bucketé
│   ├── scenarios/              # chocs de courbe (parallèle, twist, butterfly, custom, IRRBB) et P&L
│   ├── api.py                  # API d'intégration stable pour les autres modules du terminal
│   ├── network.py              # proxy SSL d'entreprise : couche sur terminal_core (jamais verify=False)
│   ├── config.py               # chargement config.yaml + .env
│   └── exceptions.py
├── app/                        # UI Streamlit : connection.py (écran de connexion), services.py, charts.py, theme.py, streamlit_app.py
├── .streamlit/config.toml      # thème sombre « terminal »
├── tests/
│   ├── app/                    # services de l'UI + exécution headless du dashboard
│   ├── unit/                   # tests calculés à la main + garde-fous d'architecture
│   ├── oracle/                 # comparaison aléatoire à QuantLib (oracle de test uniquement)
│   └── test_network.py         # smoke tests live (pytest -m network)
├── data/sample/                # données ILLUSTRATIVES : history.csv (taux), bond_static.csv / bond_prices.csv
├── scripts/                    # générateurs des données d'exemple (obligations, fixings)
├── examples/                   # une démo par phase (phase1_… à phase10_integration.py)
├── .github/workflows/tests.yml # CI : ruff + mypy + pytest (Windows / Linux, 3.11 / 3.12)
└── CHANGELOG.md
```

Principes :

* **Moteur indépendant de l'UI.** Un test (`tests/unit/test_architecture.py`) échoue si un module du moteur importe `streamlit`, `plotly` ou `QuantLib`.
* **Ids logiques.** Le moteur demande `EUR.ESTR.OIS.10Y`, jamais `EESWE10 Curncy`. Le mapping vers chaque source vit dans `config.yaml`.
* **Unités moteur.** Taux en décimal (2,15 % → `0.0215`), prix en % du pair.
* **Dates** : `datetime.date` partout (immuable, sans fuseau).

### Couche de données

```
            ┌──────────────────────── MarketDataService ────────────────────────┐
 moteur ──► │ chaîne de providers ─► cache parquet ─► politique données manquantes│
            └─────┬──────────────┬───────────────┬───────────────┬───────────────┘
           FmpProvider        EcbProvider     PublicProvider          StaticProvider
           (UST)              (€STR, courbe   (CNBC → Bundesbank      (fichiers CSV :
                              AAA BCE)        → Stooq : taux EGB)     swaps, titres)
                 └──── session HTTP unique du terminal (terminal_core) ────┘
```

| Méthode | Retour |
|---|---|
| `history(ids, start, end)` | `DataFrame` (index `date`, une colonne par id) |
| `quotes(ids, as_of)` | `value`, `obs_date` (dernière obs. ≤ `as_of`), `source` |
| `bond_static(isins)` | coupon, fréquence, day count, dates d'émission / 1er coupon / maturité |
| `bond_prices(isins, as_of)` | bid, ask, mid (clean, % pair), `yield_mid` |

* **Choix du provider** : `data.provider` (`fmp` | `free` | `public` | `static`) ; `data.fallback` liste les sources tentées pour les ids manquants.
* **Données manquantes** : `missing_policy` = `raise` (`MissingDataError`), `warn` (NaN + log) ou `drop`. Le `DataReport` du dernier appel (`svc.last_report`) indique quelle source a servi chaque id, les ids manquants, **périmés** (> 5 jours) ou **suspects** (hors `plausible_rate_range`).
* **Cache parquet** (`data/cache/<provider>/...`) : une fenêtre historique close est immuable ; une fenêtre incluant aujourd'hui expire après `ttl_hours_live` ; les données statiques obligataires après `ttl_days_static`.
* **Secrets** : `FMP_API_KEY` uniquement dans `.env` ; la clé est masquée dans tous les messages d'erreur.
* **Sans Bloomberg** : le provider Bloomberg a été retiré (plus d'accès au Terminal). Les cotations sans source gratuite (OIS €STR, IRS et FRA EURIBOR) viennent d'un fichier (provider `static`, par exemple un export du desk) ou sont reconstruites en proxy (§ 11.3).

---

## 2. Installation (Windows / PyCharm)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ../core               # session réseau partagée du terminal (monorepo)
pip install -e .[dev]                 # moteur + pytest, QuantLib < 1.44 (oracle de test), ruff, mypy
copy .env.example .env                # puis renseigner FMP_API_KEY
```

Dans PyCharm : marquer `src/` comme *Sources Root*, interpréteur = `.venv`, configuration pytest sur `tests/`.

```bash
pytest                    # 344 tests (unitaires + oracle QuantLib + UI), sans réseau
pytest --cov              # + couverture (93 %)
ruff check . && mypy      # lint + typage
pytest -m network         # appels réels (diagnostic réseau, ECB, CNBC/Bundesbank, FMP, instantané gratuit) depuis votre poste
python examples/phase1_conventions_demo.py
python examples/phase2_estr_curve.py   # écrit examples/output/estr_curve.html
python examples/phase3_dual_curve.py
python examples/phase4_sovereign_curves.py
python examples/phase5_yas.py
python examples/phase6_spreads.py
python examples/phase7_swpm.py
python examples/phase8_scenarios.py
python examples/phase10_integration.py  # API d'intégration, écrit curve_grid.csv et curves.json
streamlit run app/streamlit_app.py      # dashboard (données d'exemple par défaut)
```

---

## 3. Conventions de marché

### 3.1 Day counts

Fraction d'année $\tau(d_1, d_2)$ entre deux dates :

| Convention | Formule | Usage |
|---|---|---|
| ACT/360 | $\tau = \dfrac{d_2 - d_1}{360}$ | €STR, EURIBOR, jambe flottante, dépôts |
| ACT/365F | $\tau = \dfrac{d_2 - d_1}{365}$ | GBP, temps de modèle |
| 30/360 (Bond Basis, ISDA 4.16(f)) | $\tau = \dfrac{360(Y_2-Y_1) + 30(M_2-M_1) + (D_2-D_1)}{360}$ avec $D_1 = 31 \to 30$ ; $D_2 = 31 \to 30$ si $D_1 \ge 30$ | jambe fixe des swaps EURIBOR |
| 30E/360 (Eurobond) | même formule, $D_1, D_2 = \min(D, 30)$ | eurobonds |
| 30E/360 ISDA | fins de mois → 30, sauf fin février à la date de terminaison | |
| 30/360 US | règles de fin février (SIA) | |
| ACT/ACT ICMA | $\tau = \dfrac{1}{F}\,\dfrac{d_2 - d_1}{E - S}$ sur la période de référence $[S, E]$ | Bund, OAT, BTP |
| ACT/ACT ISDA | $\tau = \displaystyle\sum_{y} \frac{\text{jours dans } y}{365 \text{ ou } 366}$ | |

Pour l'**ACT/ACT ICMA**, $F$ est le nombre de coupons par an. Les coupons irréguliers (stub court ou long) sont décomposés sur des **périodes notionnelles régulières** obtenues en décalant la période de référence d'un tenor (règle ICMA 251). Pour un premier coupon long, $d_1 < S$ :

$$\tau = \tau(d_1, S \mid [S - T, S]) + \tau(S, d_2 \mid [S, E])$$

### 3.2 Calendrier TARGET2 et business day conventions

Jours de fermeture TARGET2 (depuis 2000) : week-ends, 1ᵉʳ janvier, Vendredi saint, lundi de Pâques, 1ᵉʳ mai, 25 et 26 décembre (Pâques par l'algorithme grégorien de Meeus). Conventions : *Following*, **Modified Following** (on avance sauf si on change de mois, auquel cas on recule), *Preceding*, *Modified Preceding*, *Half-Month MF*, *Nearest*, *Unadjusted*.

Exemple : samedi 30/05/2026 → Following = lundi 01/06, **MF = vendredi 29/05**.

### 3.3 Spot lag et dates monétaires

$$\text{spot} = \text{trade} + 2 \text{ jours ouvrés TARGET}$$

Exemple : trade jeudi 02/04/2026 → vendredi saint et lundi de Pâques fermés → **spot = 08/04/2026**. ON = trade → +1 j.o., TN = +1 → +2, SN = spot → spot + 1 j.o., tenors ≥ 1W = spot → spot + tenor (MF, règle de fin de mois).

### 3.4 Échéanciers

`generate_schedule` déroule les dates **à rebours depuis la maturité** (`BACKWARD`, stub en tête, standard swaps et obligations) ou **en avant** (`FORWARD`, stub en queue) ; stub court par défaut, `long_stub=True` pour le fusionner ; `first_date` / `next_to_last_date` pour les premiers/derniers coupons irréguliers explicites ; règle **EOM** pour les échéances de fin de mois. L'objet `Schedule` garde à la fois les dates **non ajustées** (accrual des obligations, périodes de référence ICMA) et **ajustées** (accrual des swaps).

`build_accrual_periods` produit pour chaque période : dates d'accrual, date de paiement (avec *payment lag*, 1 j.o. pour les OIS €STR), date de fixing (EURIBOR : 2 j.o. TARGET avant le début), période de référence ICMA et $\tau$.

### 3.5 Conventions EUR enregistrées

| Nom | Spot | Jambe fixe | Jambe flottante | Autres |
|---|---|---|---|---|
| `EUR_ESTR_OIS` | T+2 | annuel ACT/360 MF | annuel ACT/360 composé | payment lag 1 j., coupon unique < 1Y, EOM |
| `EUR_EURIBOR_6M_SWAP` | T+2 | annuel 30/360 MF | semestriel ACT/360, fixing T−2 | EOM |
| `EUR_EURIBOR_3M_SWAP` | T+2 | annuel 30/360 MF | trimestriel ACT/360 | EOM |
| `BUND`, `OAT`, `BONO` | T+2 | coupon annuel ACT/ACT ICMA | — | accrual non ajusté, paiement Following |
| `BTP` | T+2 | coupon semestriel ACT/ACT ICMA | — | rendement composé semestriellement |

---

### 3.6 Exemple chiffré

```text
Trade date 2026-10-07, EUR spot (T+2 TARGET) = 2026-10-09

€STR OIS 3Y – fixed leg (annual ACT/360, MF, payment lag 1):
     start        end payment_date  year_fraction
2026-10-09 2027-10-11   2027-10-12       1.019444     <- 09/10/2027 est un samedi : MF -> lundi 11
2027-10-11 2028-10-09   2028-10-10       1.011111
2028-10-09 2029-10-09   2029-10-10       1.013889
```

Première période : $\tau = 367/360 = 1{,}019444$.

**Coupon couru d'un Bund 2,50 % 15/02/2035**, trade 07/10/2026, règlement 09/10/2026 : dernier coupon 15/02/2026, prochain 15/02/2027 (365 jours), 236 jours courus :

$$AI = 100 \times c \times \frac{1}{F}\frac{d_{settle} - S}{E - S} = 100 \times 2{,}5\% \times \frac{236}{365} = 1{,}616438$$

QuantLib (`FixedRateBond.accruedAmount`) donne `1.6164383561643847` : identique.

---

## 4. Courbe €STR OIS (phase 2)

### 4.1 L'objet courbe

`YieldCurve` est l'interface commune à tout le terminal (Pricing Lab, CVA, Portfolio Intelligence) :

```python
curve.discount(d)                        # P(0,t) – date, temps ou tableau numpy
curve.zero_rate(d, "continuous")         # ou "compounded", frequency=1 / "simple"
curve.forward_rate(d1, d2, ACT_360)      # forward simple
curve.instantaneous_forward(t)
curve.to_frame()                         # piliers : date, t, DF, zéro, forward instantané
```

Le temps est mesuré en ACT/365F depuis la date de référence : $t = (d - d_0)/365$. `InterpolatedCurve` est définie par ses DF aux piliers et une interpolation sur $y(t) = -\ln P(0,t) = \int_0^t f(s)\,ds$.

### 4.2 Interpolations

**Log-linéaire** (linéaire sur $\ln P$) : forwards instantanés constants par segment,

$$f(t) = f^d_i = \frac{y_i - y_{i-1}}{t_i - t_{i-1}}, \qquad t \in\, ]t_{i-1}, t_i].$$

Méthode **locale** : une cotation ne déplace que son segment, ce qui donne un risque bucketé propre. En contrepartie, les forwards sautent aux piliers.

**Monotone convexe** (Hagan & West, 2006) : forwards instantanés **continus**, dont la moyenne sur chaque segment vaut $f^d_i$. Les forwards aux nœuds sont des moyennes pondérées :

$$f_i = \frac{t_i - t_{i-1}}{t_{i+1} - t_{i-1}}\, f^d_{i+1} + \frac{t_{i+1} - t_i}{t_{i+1} - t_{i-1}}\, f^d_i, \qquad f_0 = f^d_1 - \tfrac12(f_1 - f^d_1), \qquad f_n = f^d_n - \tfrac12(f_{n-1} - f^d_n)$$

Sur chaque segment, $f(t) = f^d_i + g(x)$ avec $x = \frac{t - t_{i-1}}{t_i - t_{i-1}}$, $g(0) = g_0 = f_{i-1} - f^d_i$ et $g(1) = g_1 = f_i - f^d_i$. Selon la position de $(g_0, g_1)$, $g$ est une parabole ou deux morceaux de parabole (4 régions), choisis pour que $\int_0^1 g = 0$ et qu'aucun extremum parasite n'apparaisse. Par exemple, en région (i) :

$$g(x) = g_0\,(1 - 4x + 3x^2) + g_1\,(3x^2 - 2x)$$

Une option `positivity=True` borne les forwards : $0 \le f_i \le 2\min(f^d_i, f^d_{i+1})$. Elle est désactivée par défaut, car l'€STR a été négatif. Au-delà du dernier pilier, on extrapole à forward instantané constant.

### 4.3 Instruments et bootstrap

* **Dépôt ON** (€STR) : $r = \frac{1}{\tau}\left(\frac{P(s)}{P(e)} - 1\right)$, $\tau$ ACT/360, de $T$ à $T+1$ jour ouvré. Le matin de $T$, seul le fixing de $T-1$ est publié (08h00 CET) : c'est lui qui sert de cotation ON.
* **OIS €STR** spot (T+2), jambes annuelles ACT/360, coupon unique sous 1 an, *payment lag* 1 jour. Sur une courbe déterministe, la capitalisation quotidienne se télescope exactement :

$$\prod_k (1 + r_k\delta_k) = \frac{P(s)}{P(e)} \quad\Rightarrow\quad K^{par} = \frac{\sum_j \left(\frac{P(s_j)}{P(e_j)} - 1\right) P(p_j)}{\sum_i \tau_i\, P(p_i)}$$

**Algorithme** (`curves/bootstrap.py`) :
1. Les instruments sont triés par **pilier**, c'est-à-dire leur dernière date pertinente (pour un OIS, le dernier paiement, soit maturité + 1 jour). Deux instruments sur le même pilier sont rejetés.
2. **Passe 1, séquentielle** : pour chaque pilier $i$, on résout $y_i = -\ln P_i$ par Brent pour que l'instrument $i$ se reprice exactement.
3. **Passes globales (monotone convexe uniquement)** : la méthode n'est pas locale, car le nœud $i+1$ modifie la forme du segment $i$. On re-résout donc chaque nœud sur la courbe complète jusqu'à ce que $\max_i|\Delta y_i| < 10^{-13}$. Il faut en général environ 9 passes, soit moins d'une seconde pour 28 instruments.

Le gabarit `ESTR_CURVE_INSTRUMENTS` (ON + OIS 1W → 50Y) est exprimé en ids logiques. `estr_curve_from_market(service, as_of)` construit la courbe depuis n'importe quel provider. Une cotation manquante est ignorée et listée dans `result.skipped`.

### 4.4 Exemple chiffré : les deux premiers piliers à la main

Trade date 07/10/2026 (données d'exemple) : €STR = 1,926 %, OIS 1W = 1,927 %.

**Pilier 1 (ON, 08/10/2026)** : $P = \dfrac{1}{1 + 1{,}926\% \times 1/360} = 0{,}99994650$.

**Pilier 2 (OIS 1W)** : départ spot 09/10, fin 16/10, paiement 19/10 (pilier). Avec un coupon unique,

$$K\,\tau\,P(p) = \left(\frac{P(s)}{P(e)} - 1\right) P(p) \;\Rightarrow\; \frac{P(09/10)}{P(16/10)} = 1 + 1{,}927\% \times \frac{7}{360}.$$

En log-linéaire, le forward est constant sur $[08/10, 19/10]$, donc $\frac{P(s)}{P(e)} = e^{f \cdot 7/365}$, ce qui donne $f = \frac{365}{7}\ln\left(1 + 0{,}01927 \cdot \frac{7}{360}\right) = 1{,}95340\%$. On en déduit

$$P(19/10) = 0{,}99994650 \times e^{-0{,}0195340 \times 11/365} = 0{,}99935801,$$

exactement la valeur du moteur (et de QuantLib).

Sur la courbe complète :

| Pilier | Cotation | DF | Zéro (cont., ACT/365F) |
|---|---|---|---|
| OIS 1Y (12/10/2027) | 1,9750 % | 0,980104 | 1,9825 % |
| OIS 10Y (10/10/2036) | 2,5310 % | 0,774108 | 2,5562 % |
| OIS 30Y (10/10/2056) | 2,6550 % | 0,450235 | 2,6573 % |

Un OIS payeur 7Y à 2,40 % sur 10 M€ a un taux par de 2,39200 %, une NPV de −5 194 € et une DV01 d'environ 6 492 €/bp. Les deux interpolations donnent le même résultat à 0,01 € près, puisque les deux courbes repricent exactement les mêmes instruments.

> Les cotations de `data/sample/history.csv` sont **illustratives** (niveaux plausibles, pas des données de marché). Les cotations EURIBOR sont générées à partir de la courbe €STR et d'un spread de basis lisse, pour rester cohérentes entre elles. En production, fournis de vraies cotations de swap par fichier (provider `static`), sinon les courbes proxy (§ 11.3) prennent le relais.

## 5. Courbe EURIBOR 6M en dual curve (phase 3)

### 5.1 Pourquoi deux courbes

Depuis 2008, l'écart EURIBOR – taux sans risque (le *basis*) n'est plus négligeable. On sépare donc :

* la courbe d'**actualisation** $P_d$ : €STR, le taux de rémunération du collatéral sous CSA ;
* une courbe de **projection** par ténor, ici $P_{6M}$, qui sert uniquement à calculer les forwards EURIBOR 6M :

$$F_j = \frac{1}{\tau_j}\left(\frac{P_{6M}(s_j)}{P_{6M}(e_j)} - 1\right)$$

### 5.2 Instruments et pricing

| Instrument | Ids logiques | Taux implicite |
|---|---|---|
| Fixing EURIBOR 6M (dépôt spot → +6M) | `EUR.EURIBOR6M.FIX` | $\frac{1}{\tau}\left(\frac{P_{6M}(s)}{P_{6M}(e)}-1\right)$ |
| FRA 1x7 … 12x18 | `EUR.EURIBOR6M.FRA.6X12` | idem, sur $[\text{spot}+m\text{M},\ +6\text{M}]$ |
| IRS 2Y … 50Y, fixe annuel 30/360 contre 6M ACT/360 | `EUR.EURIBOR6M.SWAP.10Y` | $K = \dfrac{\sum_j F_j \tau_j P_d(p_j)}{\sum_i \tau_i^{30/360} P_d(p_i)}$ |

Les FRA s'arrêtent à 12x18 : le FRA 18x24 tomberait sur le même pilier que le swap 2Y.

Points de méthode :
- **Bootstrap :** le moteur résout les nœuds de $P_{6M}$ avec $P_d$ figée (`SwapHelper(..., discount_curve=estr)`).
- **Calcul des forwards :** ils sont projetés sur la période d'accrual (« par coupons », le défaut QuantLib). L'option `indexed_coupons=True` les projette plutôt sur la période propre de l'index ; l'écart est inférieur à 0,05 bp sur un 10Y.
- **Mode single curve :** avec `dual_curve=False`, on retrouve l'ancienne méthode mono-courbe, gardée pour comparaison.

`VanillaSwap` (`instruments/irs.py`) donne la NPV, le taux par, le spread par, l'annuité et les forwards projetés. Il servira de base au pricer SWPM de la phase 7.

### 5.3 CurveSet : l'objet consommé par les autres modules

```python
curves = build_eur_curves(as_of, quotes)              # ou eur_curves_from_market(service, as_of)
curves.discount_curve                                 # €STR
curves.forward_curve("EURIBOR6M")                     # projection 6M ("ESTR" -> courbe d'actualisation)
curves.basis_spreads()                                # basis €STR / 6M par ténor
curves.forward_comparison()                           # forwards 6M EURIBOR vs €STR composé
```

Le **basis** est le spread $b$ à ajouter à la jambe €STR composée pour égaler la jambe EURIBOR 6M :

$$b(T) = \frac{\text{PV}_{6M}(T) - \text{PV}_{€STR}(T)}{\sum_j \tau_j P_d(p_j)}$$

### 5.4 Exemple chiffré (données d'exemple du 06/10/2026)

**Premier pilier, à la main.** Fixing 6M = 2,1373 %, du 08/10/2026 au 08/04/2027 (182 jours) :

$$\frac{P_{6M}(08/10)}{P_{6M}(08/04)} = 1 + 2{,}1373\% \times \frac{182}{360} = 1{,}010805$$

En log-linéaire, le forward est constant de $t_0$ jusqu'au pilier. On a donc $f = \frac{\ln 1{,}010805}{182/365} = 2{,}15536\%$ et

$$P_{6M}(08/04/2027) = e^{-f \times 184/365} = 0{,}989193$$

C'est exactement le DF du moteur. Le forward 6M spot recalculé sur la courbe redonne bien 2,1373 %.

**Basis €STR / 6M** (bp) :

| 2Y | 5Y | 10Y | 20Y | 30Y | 50Y |
|---|---|---|---|---|---|
| 18,2 | 16,1 | 13,8 | 11,3 | 10,1 | 9,0 |

**Dual vs single curve.** Les deux courbes repricent les mêmes cotations. Les forwards 6M lointains s'écartent pourtant : −0,19 bp en 2036, +0,86 bp en 2056.

Un IRS payeur 10Y à 2,50 % sur 10 M€ vaut 182 460 € en dual curve contre 181 012 € en single curve, soit 1 448 € d'écart. C'est l'effet d'actualisation sur un swap hors marché.

## 6. Courbes souveraines Nelson-Siegel-Svensson (phase 4)

### 6.1 L'obligation (`bonds/`)

`FixedRateBond` couvre les obligations d'État de la zone euro :
- coupons courus sur dates **non ajustées** en ACT/ACT ICMA, avec premiers coupons courts ou longs ;
- paiement le jour ouvré TARGET suivant ;
- règlement T+2 ;
- le coupon revient à l'acheteur si le règlement tombe strictement avant la date de coupon.

Le rendement suit la **convention ICMA (street)**, composé à la fréquence $F_y$ (annuelle pour Bund et OAT, semestrielle pour BTP) :

$$P_{dirty} = \sum_k \frac{CF_k}{(1 + y/F_y)^{w_k}}, \qquad w_k = F_y \sum_{j\le k}\tau_j^{ICMA}$$

Ici $\tau_1$ court du règlement au prochain coupon. Les durations, la convexité, la DV01 et les spreads arrivent en phases 5 et 6.

### 6.2 Le modèle

Taux zéro continu (Svensson 1994, méthode BCE et Bundesbank) :

$$z(t) = \beta_0 + \beta_1 \frac{1-e^{-t/\tau_1}}{t/\tau_1} + \beta_2\left(\frac{1-e^{-t/\tau_1}}{t/\tau_1} - e^{-t/\tau_1}\right) + \beta_3\left(\frac{1-e^{-t/\tau_2}}{t/\tau_2} - e^{-t/\tau_2}\right)$$

$$f(t) = \beta_0 + \beta_1 e^{-t/\tau_1} + \beta_2 \tfrac{t}{\tau_1} e^{-t/\tau_1} + \beta_3 \tfrac{t}{\tau_2} e^{-t/\tau_2}$$

Lecture des paramètres :
- $\beta_0$ : niveau long, c'est-à-dire l'asymptote, au-delà des données ;
- $\beta_0 + \beta_1$ : taux court ;
- $\beta_2$ et $\beta_3$ : bosses autour de $\tau_1$ et $\tau_2$.

Nelson-Siegel correspond au cas $\beta_3 = 0$. `NelsonSiegelSvenssonCurve` est une `YieldCurve` : elle s'utilise comme les courbes bootstrappées.

### 6.3 Le fit

On minimise les écarts de prix *clean* pondérés par l'inverse de la duration modifiée, ce qui revient à peu près à minimiser les écarts de rendement. C'est l'objectif de la Bundesbank et de `FittedBondDiscountCurve` dans QuantLib :

$$\min_\theta \sum_i \left(\frac{\hat P_i(\theta) - P_i}{D_i^{mod}}\right)^2, \qquad \hat P_i = \frac{\sum_k CF_{ik}\, e^{-z(t_{ik}) t_{ik}}}{e^{-z(t_s) t_s}} - AI_i$$

Mise en œuvre :
- **Optimisation multi-start :** Levenberg-Marquardt borné (`least_squares`, TRF) avec **Jacobien analytique**, lancé depuis 15 couples de départ $(\tau_1, \tau_2)$. On garde la meilleure solution. Un fit prend moins d'une seconde.
- **Paramétrisation :** $\tau_2 = \tau_1 + \delta$ avec $\delta \ge 1$ an. Les deux termes de bosse ont la même forme, donc imposer $\tau_1 < \tau_2$ ne perd rien. L'écart minimal évite la dégénérescence classique ($\tau_1 \approx \tau_2$ avec des $\beta_2$ et $\beta_3$ énormes qui se compensent).
- **Filtre de maturité :** les obligations à moins de 3 mois de l'échéance sont exclues, comme à la BCE.
- **Outliers :** on retire une obligation à la fois tant que la plus grosse erreur de rendement dépasse $\max(5\text{ bp},\ 3 \times \text{RMSE})$, au plus 3 retraits.
- **Choix du modèle :** `model="auto"` prend NSS à partir de 12 obligations utilisables, NS en dessous.

Le résultat (`SovereignCurveFit`) contient :
- la courbe ;
- une table par obligation : prix et rendements de marché et du modèle, **rich/cheap** en bp (positif = obligation cheap face à la courbe), raison d'exclusion ;
- la RMSE ;
- les rendements par (`par_yields()`) ;
- les obligations classées de la plus cheap à la plus rich (`rich_cheap()`).

`zero_spreads(fits, "BUND")` calcule les spreads OAT-Bund et BTP-Bund.

**Univers obligataire :** il se définit dans `config.yaml` (`sovereign_curves`) par une liste d'ISIN, un fichier CSV, ou, par défaut, tous les titres de l'émetteur connus du provider statique.

### 6.4 Exemple chiffré (données d'exemple du 06/10/2026)

| Courbe | Obligations utilisées | RMSE | Par 2Y | Par 10Y | Par 30Y | Exclues |
|---|---|---|---|---|---|---|
| Bund | 28 / 30 | 1,42 bp | 1,964 % | 2,718 % | 3,042 % | 1 titre < 3 mois, 1 outlier (+16 bp) |
| OAT | 29 / 30 | 1,22 bp | 2,308 % | 3,426 % | 3,868 % | 1 outlier (+19 bp) |
| BTP | 47 / 49 | 1,50 bp | 2,403 % | 3,614 % | 4,032 % | 1 titre < 3 mois, 1 outlier (+19 bp) |

Le spread zéro face au Bund à 10 ans vaut **+71,6 bp pour l'OAT** et **+94,0 bp pour le BTP**. L'univers d'exemple a été généré avec 1,5 bp de bruit et un outlier planté par émetteur. Le fit retrouve les trois outliers et une RMSE égale au bruit.

**Une obligation à la main.** Prenons un Bund fictif 2,60 % 15/02/2036, émis le 15/01/2026, donc avec un premier coupon court d'un mois, $\tau = 31/365$. On le traite le 06/10/2026, règlement le 08/10/2026 :
- coupon couru : $AI = 2{,}6 \times \frac{235}{365} = 1{,}673973$ ;
- sur la courbe Bund fittée : prix *dirty* $= \sum_k CF_k P(t_k)/P(t_s) = 100{,}9499$, prix *clean* $= 99{,}2759$, rendement ICMA $= 2{,}6876\,\%$.

> L'univers `SMPL-…` est **synthétique**, généré par `scripts/generate_sample_bonds.py`. Ce ne sont pas de vrais ISIN, et les prix sont produits par une vraie courbe NSS, des spreads lisses et du bruit. En production, renseigne les ISIN de chaque émetteur et fournis leurs données (référentiel, prix) par fichier, ou laisse les courbes s'ajuster sur les taux génériques (§ 11.3).

## 7. Analytics obligataires type YAS (phase 5)

### 7.1 Calculatrice prix ↔ rendement

`yield_analytics(bond, settlement, clean=...)` ou `(..., ytm=...)` : on donne exactement l'un des deux. Le résultat contient :
- dernier et prochain coupon, jours courus, coupon couru, prix clean et dirty ;
- rendement **street** (ICMA, actualisation sur les dates de coupon théoriques) ;
- **true yield** (actualisation sur les dates de paiement réelles, convention QuantLib) ;
- rendement équivalent annuel, $y_{1} = (1 + y_F/F)^F - 1$, utile pour comparer BTP et Bund ;
- current yield.

### 7.2 Sensibilités au rendement

Avec $t_k = w_k/F$, $v = 1 + y/F$ et $PV_k = CF_k\,v^{-w_k}$ :

$$D^{mac} = \frac{\sum_k t_k PV_k}{P}, \qquad D^{mod} = \frac{D^{mac}}{1+y/F}, \qquad C = \frac{\sum_k t_k\,(t_k + 1/F)\,PV_k}{P\,(1+y/F)^2}$$

$$DV01 = D^{mod}\,P_{dirty}\times 10^{-4} \;(\text{pour } 100), \qquad \text{Risk (Bloomberg)} = D^{mod}\,P_{dirty}/100$$

La DV01 est l'approximation au premier ordre. QuantLib ajoute un terme de convexité dans `basisPointValue`. Les tests retirent ce terme avant de comparer.

### 7.3 Risque courbe : duration effective et key rate durations

`curves/shifted.py` : $P'(t) = P(t)\,e^{-s(t)\,t}$, où $s(t)$ est un spread zéro linéaire par morceaux. On l'utilise pour :
- les chocs parallèles ;
- les chocs en **tente** sur chaque ténor clé $T_k$ (1 en $T_k$, 0 aux ténors voisins, plat aux extrémités). Les tentes somment à 1 partout.

Sur la courbe, éventuellement décalée du Z-spread de l'obligation (phase 6) :

$$D^{eff} = \frac{P_- - P_+}{2P_0\Delta}, \qquad C^{eff} = \frac{P_- + P_+ - 2P_0}{P_0\Delta^2}, \qquad KRD_k = \frac{P^{(k)}_- - P^{(k)}_+}{2P_0\Delta}, \qquad \sum_k KRD_k = D^{eff}$$

Les ténors clés par défaut sont 6M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y, 15Y, 20Y et 30Y, avec $\Delta$ = 1 bp. `krd_dv01(nominal)` convertit en € par bp.

`SpreadedCurve` resservira pour le Z-spread (phase 6) et les scénarios (phase 8).

### 7.4 Écran YAS

`yas(bond, as_of, clean=..., curve=..., nominal=...)` regroupe :
- le bloc rendement ;
- les montants de règlement (principal, couru, total, DV01 en €) ;
- le tableau des flux (montant, temps, PV au rendement, DF et PV sur la courbe) ;
- le risque courbe et les KRD.

`analytics_table(bonds, prix, as_of)` donne la vue univers, une ligne par titre.

### 7.5 Exemple chiffré

**BTP 3,40 % 01/04/2035** (titre d'exemple), traité le 06/10/2026, règlement le 08/10/2026, prix 98,977, nominal 10 M€.

| Rubrique | Valeurs |
|---|---|
| Coupon couru | 7 jours, $3{,}4/2 \times 7/182 = 0{,}065385$ |
| Prix dirty | 99,042385 |
| Rendement street (semestriel) | 3,54062 % |
| True yield | 3,53943 % (certains coupons, dont celui du 01/04/2028, tombent un week-end) |
| Équivalent annuel | 3,57196 % |
| Macaulay / modifiée | 7,4271 / 7,2979 |
| Convexité | 61,50 |
| Risk | 7,2280 |
| Montant total | 9 904 238,46 € |
| DV01 | 7 228 € par bp |

**KRD sur la courbe BTP fittée** (€ par bp, 10 M€) :

| 2Y | 3Y | 5Y | 7Y | 10Y | Somme |
|---|---|---|---|---|---|
| 65 | 155 | 290 | 3 590 | 3 238 | 7 369 |

La somme est égale à la DV01 parallèle de la courbe (7 369 €).

**Pourquoi deux DV01 différentes.** Les mesures n'ont pas la même définition : 7 228 € pour la DV01 au rendement (choc du rendement semestriel) contre 7 369 € pour la DV01 courbe (choc des taux zéro continus, proche d'une duration de Macaulay). Elles ne coïncident pas, et c'est normal.

**Prix modèle vs marché.** Sur la courbe, le prix dirty modèle vaut 99,366 contre 99,042 au marché, car le titre est cheap d'environ 4,5 bp. Avec `yas(..., spread="z")` (phase 6), la courbe est d'abord décalée du Z-spread pour recaler le modèle sur le prix de marché avant de calculer les KRD.

## 8. Spreads G, I, Z et ASW (phase 6)

Module `bonds/spreads.py`. Tous les spreads sont en décimal (× 1e4 pour des bp), les prix pour 100.

| Spread | Définition | Référence |
|---|---|---|
| **G-spread** | $G = y_{bond} - y^{par}_{gov}(T)$ | rendement **par** sur la courbe souveraine, calculé avec l'échéancier même de l'obligation, ce qui neutralise l'effet coupon ; ou interpolation linéaire de rendements benchmark (style Bloomberg) |
| **I-spread** | $I = y^{1Y}_{bond} - S(T)$ | taux par d'un swap spot → **maturité exacte** de l'obligation (fixe annuel 30/360 contre 6M, actualisation €STR), soit un swap à date cassée et non une interpolation de piliers |
| **Z-spread** | $P_{dirty} = \sum_k CF_k \dfrac{P(t_k)e^{-z t_k}}{P(t_s)e^{-z t_s}}$ | décalage constant de la courbe zéro (continu, ACT/365F) ; on peut le calculer contre la courbe 6M, €STR ou souveraine |
| **ASW par-par** | voir ci-dessous | EURIBOR 6M (dual curve) ou €STR (OIS-ASW) |

Pour l'I-spread, le rendement de l'obligation est converti à la composition de la jambe fixe du swap (annuelle). Un BTP semestriel à 3,5406 % devient ainsi 3,5720 % avant de soustraire le taux swap.

**Asset swap par-par.** L'investisseur paie 100 pour l'obligation et un swap. Dans ce swap, il verse les coupons de l'obligation et reçoit l'index + $A$ sur 100. L'écart $100 - P_{dirty}$ est réglé à la date de règlement. Tout est actualisé sur €STR :

$$A = \frac{(100 - P_{dirty})\,P_d(t_s) + \sum_k c_k\,P_d(t_k) - \text{PV}_{float}}{100\sum_j \tau_j\,P_d(p_j)}, \qquad \text{PV}_{float} = 100\sum_j F_j\,\tau_j\,P_d(p_j)$$

En single curve, la jambe variable se télescope et on retrouve la formule classique $A = P_d(t_s)\,(P^{courbe}_{dirty} - P_{dirty}) / \text{BPV}$. Un test le vérifie.

**KRD calées sur le marché.** `yas(..., curve=..., spread="z")` décale d'abord la courbe du Z-spread de l'obligation, de sorte que le prix modèle égale le prix de marché. Les durations effectives et les KRD sont ensuite calculées autour du prix réellement payé.

### Exemple chiffré

**BTP 3,40 % 01/04/2035** (titre d'exemple), prix 98,977, données d'exemple du 06/10/2026.

| Spread | Valeur | Référence |
|---|---|---|
| G | 92,4 bp | rendement par Bund 2,616 % |
| I | 92,5 bp | swap 6M à maturité 2,647 % |
| Z vs courbe 6M | 90,0 bp | |
| Z vs €STR | 104,3 bp | |
| Z vs Bund | 91,8 bp | |
| ASW par-par 6M | 88,9 bp | voir décomposition ci-dessous |
| ASW €STR | 103,8 bp | |

Décomposition de l'ASW 6M, pour 100 de nominal :

$$\frac{0{,}9576 + 26{,}0776 - 20{,}1362}{7{,}7636} = 88{,}9 \text{ bp}$$

**Lecture des écarts :**
- **€STR contre 6M :** un spread contre €STR dépasse le spread contre EURIBOR d'environ le basis €STR/6M (≈ 15 bp à 8 ans).
- **Obligations sous le pair :** l'ASW par-par est inférieur au Z-spread. L'investisseur paie 100 pour un titre qui en vaut moins, et le spread s'applique à tout le nominal. Exemple extrême : BTP 0 % 2051 (titre d'exemple), coté autour de 36, rendement 4,13 %, ASW 6M de 68,9 bp contre un Z de 126,2 bp.
- **G-spread des BTP contre le Bund :** de 20 bp à 2 ans à 112 bp à 25 ans. On retrouve la structure par terme du spread BTP-Bund de l'univers d'exemple.

## 9. Pricer de swaps type SWPM et risque bucketé (phase 7)

### 9.1 Swaps en vie et fixings

`Fixings` (`instruments/fixings.py`) est une série `date → taux` indexée par **date de taux**. On la charge avec `fixings_from_market(service, "EUR.ESTR.FIX", début, fin)`.

**IRS EURIBOR.**
- Une période dont la date de fixing est passée utilise le fixing publié. S'il manque, le moteur lève `MissingFixingError`.
- Un fixing daté du jour de la courbe est utilisé s'il est publié, sinon il est projeté.
- Les périodes déjà payées (paiement ≤ date de courbe) sont exclues.

**OIS €STR.** Les jours passés sont capitalisés un à un, puis la suite est télescopée sur la courbe :

$$1 + g = \prod_{d < t_0} \left(1 + r_d\,\delta_d\right) \times \frac{P(d^*)}{P(e)}$$

Ici $\delta_d$ est la fraction ACT/360 entre deux jours ouvrés TARGET, et $d^*$ le premier jour sans fixing connu.

### 9.2 Écran SWPM

`swpm(swap, curves, fixings)` fonctionne pour un `VanillaSwap` comme pour un `OvernightIndexedSwap`, qu'il soit spot, forward ou en vie. Tous les montants sont vus du **détenteur** :

| Rubrique | Définition |
|---|---|
| NPV (dirty) | $\text{PV}_{float} - \text{PV}_{fixed}$ pour un payeur |
| Couru | intérêts courus des périodes en cours jusqu'à la date de courbe |
| NPV clean | NPV − couru |
| Taux par | $K^* = \text{PV}_{float}/A$ |
| Spread par | $s^* = s - \text{NPV}_{payeur}/\text{BPV}_{float}$ |
| PV01 | $A \times 1\text{ bp}$ |
| DV01 | variation de valeur pour +1 bp sur **toutes les cotations de marché** |
| Tableaux de flux | par jambe, avec date de fixing, taux, statut (*fixed / partially fixed / projected*), montant, DF, PV |

### 9.3 Risque bucketé

`bucketed_risk(curves, pricer)` fonctionne pour n'importe quel instrument pricé sur un `CurveSet` : swap, OIS, obligation. Il donne la DV01 **par cotation de marché**, c'est-à-dire par dépôt, FRA, OIS et IRS.

**Méthode du Jacobien** (par défaut, moins d'une seconde). Notons $y$ les nœuds des deux courbes ($-\ln P$ aux piliers €STR et EURIBOR) et $q(y)$ les cotations qu'elles impliquent :

$$\frac{\partial V}{\partial q} = J^{-\top}\,\frac{\partial V}{\partial y}, \qquad J = \frac{\partial q}{\partial y}$$

- Les deux dérivées s'obtiennent par différences centrées sur les nœuds, **sans re-bootstrapper**.
- $J$ est carrée, car le bootstrap reprice chaque instrument exactement.
- $J$ est triangulaire par blocs : les cotations EURIBOR dépendent des nœuds €STR via l'actualisation, l'inverse est faux.

**Méthode par re-bootstrap** (`method="rebuild"`) : chaque cotation est bumpée de ±1 bp, puis on refait tout le bootstrap. Elle est plus lente et sert à valider le Jacobien. `parallel_dv01` donne la DV01 SWPM classique : toutes les cotations à +1 bp, re-bootstrap complet.

> **Monotone convexe.** L'interpolation n'est que *par morceaux* régulière, car la forme Hagan-West change de région avec les données. Un re-bootstrap à 1 bp peut donc s'écarter du Jacobien de quelques % sur certains buckets. Avec un petit bump (`quote_bump=1e-6`), on retrouve le Jacobien. En log-linéaire, les deux méthodes coïncident.

### 9.4 Exemple chiffré : swap receveur en vie

**Le swap :** receveur 2,45 % contre EURIBOR 6M, du 12/03/2025 au 12/03/2032, 25 M€. Valorisé le 06/10/2026, avec données et fixings d'exemple.

**Couru, à la main.**
- Jambe fixe : du 12/03 au 06/10/2026, soit 204 jours en 30/360, donc $25\text{M} \times 2{,}45\% \times 204/360 = 347\,083$.
- Jambe variable : fixing 2,1112 % (fixé le 10/09/2026), du 14/09 au 06/10, soit 22 jours, donc $25\text{M} \times 2{,}1112\% \times 22/360 = 32\,254$.
- Couru net pour le receveur : **314 829 €**.

**Résultat SWPM :**

| Rubrique | Valeur |
|---|---|
| NPV | 253 101 € |
| NPV clean | −61 727 € |
| Taux par | 2,27009 % |
| Spread par | +19,37 bp |
| PV01 | 14 068 € |
| DV01 (somme des buckets) | −11 561 € |
| DV01 (re-bootstrap complet) | −11 557 € |

**Buckets principaux.**
- IRS 5Y : −6 675 € ; IRS 6Y : −5 966 €. La maturité mars 2032 tombe entre les piliers 5Y (oct. 2031) et 6Y (oct. 2032), et le risque se répartit entre les deux.
- DEP 6M : +1 071 €. Le prochain coupon variable est projeté sur le début de la courbe.
- Les buckets €STR restent sous 100 € : ils ne jouent que via l'actualisation.

**OIS payeur 1,95 % en vie** (05/11/2025 → 05/11/2027, 50 M€) : NPV 8 525 €, couru −7 740 €. La première période est *partially fixed* : les fixings passés sont capitalisés, puis le reste est projeté. Le risque se concentre sur les OIS 1Y (4 419 €) et 18M (1 033 €).

> Les fixings de `data/sample/history.csv` sont **illustratifs**. Ils suivent la trajectoire du taux de dépôt BCE (€STR ≈ dépôt − 9 bp), générés par `scripts/generate_sample_fixings.py`.

## 10. Scénarios de courbe (phase 8)

### 10.1 Bibliothèque

Un `CurveScenario` est un choc $\Delta(t)$ fonction de la maturité. Il est linéaire entre ses nœuds et plat au-delà. Les scénarios s'additionnent (`+`) et se multiplient par un scalaire (`scaled`).

| Constructeur | Forme |
|---|---|
| `parallel(25)` | $\Delta(t) = 25$ bp |
| `twist(s, l, "2Y", "10Y")` | linéaire de $s$ à 2 ans à $l$ à 10 ans |
| `steepener(25, kind=…)` | *bear* : 10Y +25 bp ; *bull* : 2Y −25 bp ; *pivot* : ±12,5 bp |
| `flattener(25, kind=…)` | l'inverse du steepener |
| `butterfly(15, wings_bp=0, "2Y", "5Y", "10Y")` | le ventre bouge de +15 bp par rapport aux ailes |
| `custom({"2Y": -10, "30Y": 20})` | choc libre par pilier |
| `irrbb_scenarios("EUR")` | les 6 chocs standard du Comité de Bâle (BCBS d368), tailles EUR : parallèle 200 bp, court 250 bp, long 100 bp |

Pour les chocs IRRBB, avec une décroissance $x = 4$ ans :

$$S_{court}(t) = R_{court}\,e^{-t/x}, \qquad S_{long}(t) = R_{long}\left(1 - e^{-t/x}\right)$$

$$\text{steepener} = -0{,}65\,|S_{court}| + 0{,}9\,|S_{long}|, \qquad \text{flattener} = 0{,}8\,|S_{court}| - 0{,}6\,|S_{long}|$$

Les planchers post-choc de l'EBA ne sont pas appliqués.

### 10.2 Deux façons d'appliquer un choc

| Mode | Mécanique | Usage |
|---|---|---|
| `"quotes"` | chaque cotation bouge de $\Delta(t_i)$, où $t_i$ est la maturité de son pilier, puis on **re-bootstrappe** | vision trader : « la courbe des taux par monte de 25 bp à 10 ans » |
| `"zero"` | $P'(t) = P(t)\,e^{-\Delta(t)\,t}$ sur les courbes zéro | chocs réglementaires (IRRBB), courbes NSS souveraines |

`targets=["EURIBOR6M"]` ne choque que la courbe de projection : c'est un **scénario de basis**. Les courbes souveraines NSS ne sont pas bootstrappées depuis des cotations, donc elles reçoivent toujours la version « zéro » du choc.

Les deux modes ne racontent pas la même histoire. Prenons le butterfly 2Y/5Y/10Y avec les ailes immobiles :
- en mode `quotes`, le payeur 10Y ne bouge pas (−43 €), puisque son taux par à 10 ans est inchangé ;
- en mode `zero`, il gagne 15 142 €, car les taux zéro intermédiaires ont monté.

### 10.3 P&L de portefeuille

On décrit le portefeuille avec des `Position(nom, instrument, nominal, clean_price, curve, fixings)` :
- **Swaps et OIS** : revalorisés en entier sur le `CurveSet` choqué. Les fixings passés restent inchangés.
- **Obligations** : pricées sur une courbe de référence (`"ESTR"`, `"EURIBOR6M"` ou une courbe souveraine). Le Z-spread est calibré une fois sur le prix de marché, puis **gardé constant** : c'est un choc de taux pur, sans mouvement de spread.

$$\text{P\&L}_{bond} = \frac{N}{100}\left[P^{scén}_{dirty}(z_0) - P^{base}_{dirty}(z_0)\right]$$

`scenario_pnl(book, base, scénarios, mode)` renvoie la matrice positions × scénarios, avec une ligne TOTAL. `par_rate_table` donne les taux swap par avant le choc et leur variation par scénario.

### 10.4 Exemple chiffré (données d'exemple du 06/10/2026)

**Le book :**
- receveur IRS 2,45 % 2032 en vie, 25 M€ ;
- payeur IRS 10Y 2,70 %, 20 M€ ;
- receveur OIS 2Y, 50 M€ ;
- long BTP 3,40 % 2035, 10 M€ ;
- short Bund 2,40 % 2034, 10 M€.

**P&L par position, mode `quotes`, en € :**

| Position | +25 bp | Bear steepener 2Y/10Y | Butterfly 2Y/5Y/10Y +15 bp |
|---|---|---|---|
| Rec IRS 2032 | −286 598 | −137 316 | −170 649 |
| Pay IRS 10Y | +433 544 | +435 679 | −43 |
| Rec OIS 2Y | −245 490 | −422 | −675 |
| Long BTP 2035 | −181 742 | −138 573 | −37 222 |
| Short Bund 2034 | +176 827 | +124 634 | +47 365 |
| **Total** | **−103 459** | **+284 002** | **−161 223** |

**Lecture.**
- Le payeur 10Y profite du steepener, alors que l'OIS 2Y n'y réagit presque pas : le choc est nul à 2 ans.
- Le butterfly pénalise le receveur 2032, dont la maturité tombe dans le ventre de la courbe.
- Le couple long BTP / short Bund est quasi neutre en parallèle (−4 915 €) : c'est essentiellement une position de spread.

**IRRBB, mode `zero`.** Le pire choc pour ce book est *short up* : −1,63 M€.

## 11. Réseau d'entreprise et données gratuites

### 11.1 Proxy d'inspection SSL

Au bureau, un proxy (Zscaler, Netskope…) réécrit le trafic HTTPS avec un certificat interne que Python ne connaît pas. Toutes les sources échouent alors en même temps (`certificate verify failed`). Toutes les requêtes passent par **la session HTTP unique du terminal** (`core/terminal_core/network.py`, partagée avec Portfolio Intelligence et le moteur de stress). `rates_analytics/network.py` n'en est qu'une couche de compatibilité.

| Mode | Confiance | Usage |
|---|---|---|
| `auto` (par défaut) | magasin de certificats du poste (`truststore`), complété du certificat d'entreprise s'il est fourni ; sinon le certificat d'entreprise ; sinon `certifi` | bureau comme maison, sans configuration |
| `system` | magasin de certificats du poste (+ certificat d'entreprise éventuel) | forcer le magasin Windows |
| `ca` | certificat racine fourni (`.pem` / `.crt`), fusionné avec `certifi` | si le certificat n'est pas dans le magasin Windows |
| `standard` | `certifi` seul | hors proxy |

**La vérification des certificats n'est jamais désactivée.** L'ancien mode `insecure` est refusé dans le code. S'il figure encore dans un `config.yaml` ou une variable d'environnement (`RATES_NETWORK_MODE=insecure`, `MARKET_MONITOR_INSECURE_SSL`), il est ignoré avec un avertissement. Un test vérifie qu'aucun `verify=False` ne subsiste dans les sources.

**Écran de connexion.** Le dashboard s'ouvre sur une page qui demande la source et le mode réseau **avant** tout appel de données. Le bouton *Tester les sources* interroge ECB, FMP, CNBC, Bundesbank et Stooq en parallèle. Il qualifie chaque échec (certificat, proxy, authentification proxy, filtrage, délai, DNS, source en erreur) et désigne la cause dominante. Si le chargement échoue ensuite sur des certificats, un bandeau propose le magasin Windows ou le certificat d'entreprise.

**Scripts et API.** Le mode et le proxy se règlent dans le bloc `network:` de `config.yaml` (`mode`, `ca_bundle`, `proxy`, `no_proxy`). Ils sont surchargés par `.env` (`TERMINAL_TRUST`, `TERMINAL_CA_BUNDLE`, `TERMINAL_HTTPS_PROXY`, `TERMINAL_NO_PROXY` ; les anciennes variables `RATES_*` et `MARKET_MONITOR_CA_BUNDLE` restent lues). `api.market_snapshot(..., network=NetworkSettings(mode="system"))` force un mode. Diagnostic complet en ligne de commande : `check-network`.

Un pare-feu qui **bloque** un site (page de blocage, tunnel refusé) ne se règle pas côté poste. Le diagnostic le signale, et la demande d'ouverture va à l'informatique.

### 11.2 Sources gratuites ajoutées

| Id logique | Source (première disponible) | Usage |
|---|---|---|
| `EUR.GOVT.{DE,FR,IT,ES}.{2Y,5Y,10Y,30Y}` | CNBC → Bundesbank (Bund) → Stooq | taux génériques souverains |
| `EUR.BBK.DE.ZERO.{1Y…30Y}` | Bundesbank, courbe zéro-coupon Svensson officielle des titres fédéraux | piliers supplémentaires du Bund |
| `EUR.ESTR.FIX`, `EUR.ECB.AAA.NSS.*` | ECB Data Portal | €STR, paramètres Svensson AAA de la BCE |

Les sources sont chaînées (`cnbc:…|bbk:…|stooq:…`). Une source entièrement indisponible (page anti-robot, hôte filtré) est ignorée pour le reste de la requête, au lieu d'épuiser un délai par id.

### 11.3 Courbes proxy quand il n'y a pas de cotations de swap

Aucune source gratuite ne publie de cotations OIS €STR ou de swaps EURIBOR. Sans fichier de cotations, les courbes de swap ne peuvent donc pas être bootstrappées sur le marché. Au lieu d'échouer, `curves/proxy.py` **reconstruit les cotations manquantes**, puis le bootstrap normal tourne dessus. Tous les écrans (SWPM, spreads, scénarios) fonctionnent alors sans changement :

$$z_{OIS}(t) = z_{AAA}(t) + s_\infty(t) + \big(z_{€STR} - z_{AAA}(0) - s_\infty(0)\big)\,e^{-t/h}, \qquad z_{6M}(t) = z_{OIS}(t) + b(t)$$

- $z_{AAA}$ est la courbe Svensson AAA publiée par la BCE ;
- $s_\infty$ est le spread OIS − AAA et $b$ la basis 6M, tous deux tirés des tables `proxy_curves` de `config.yaml` ;
- le terme exponentiel cale le court terme sur le fixing €STR.

Les spreads par défaut sont des **hypothèses** : des ordres de grandeur de 2025-26, à ajuster aux niveaux vus sur le desk.

Les courbes proxy sont signalées partout : pastille « Courbes swap · PROXY » dans la barre du haut, encadré explicatif, onglet *Données* avec les entrées utilisées, `snap.proxy` / `snap.sources` dans l'API. Un test vérifie que la basis reconstruite par le bootstrap retrouve celle de la configuration (17 bp à 2 ans, 10 bp à 10 ans).

**Souverains sans données titre par titre.** Si l'univers obligataire est indisponible (free / FMP), chaque courbe est ajustée en Nelson-Siegel sur les taux génériques, traités comme des obligations au pair cotées 100. Pour le Bund, les zéros Bundesbank s'y ajoutent. La pastille indique alors « taux génériques ». Le rich/cheap n'y a pas de sens.

## 12. Dashboard Streamlit (phase 9)

```bash
streamlit run app/streamlit_app.py
```

Le dashboard s'ouvre sur l'**écran de connexion** (§ 11.1) : source de données, puis mode réseau, test des sources, puis lancement. Le thème « terminal » (graphite bleuté, accent ambre, chiffres en police mono) est commun avec Market Monitor (`app/theme.py`). La barre du haut affiche des pastilles de provenance : source, date, courbes de marché ou proxy, souverains sur obligations ou sur taux génériques.

L'interface ne fait **aucun calcul**. Elle est construite en trois couches :
- `app/services.py` (Python pur, testé) appelle le moteur et produit des tables ;
- `app/charts.py` transforme ces tables en figures Plotly ;
- `app/streamlit_app.py` ne fait que de l'affichage.

L'état de marché (courbes, fits souverains, univers obligataire, fixings) est construit une fois par combinaison date / provider / interpolation, puis mis en cache (`st.cache_resource`).

**Barre latérale :** date de valorisation, interpolation (log-linéaire ou monotone convexe), rechargement, état de la connexion avec le bouton *Changer la source / connexion*.

| Onglet | Équivalent Bloomberg | Contenu |
|---|---|---|
| **Curves** | GC | taux swap 2/5/10/30Y ; courbes zéro et forwards 6M €STR / EURIBOR ; taux par, basis €STR/6M ; rapports de bootstrap (erreur de repricing par instrument) |
| **Sovereigns** | GC / RV | courbes NSS Bund / OAT / BTP avec les rendements de marché (× = titre exclu) ; rendements par et RMSE ; spreads vs Bund ; rich/cheap par titre ; paramètres NSS |
| **Bond (YAS)** | YAS | calculatrice prix → rendement (street et true) ; durations, convexité, DV01, couru ; G / I / Z / ASW ; KRD sur une courbe au choix recalée du Z-spread ; flux ; spreads de tout l'univers d'un émetteur |
| **Swap (SWPM)** | SWPM | IRS 6M ou OIS €STR, spot / forward / en vie, taux fixe prérempli au taux par ; NPV, NPV clean, couru, taux et spread par, PV01, DV01 ; risque bucketé par cotation ; flux des deux jambes avec statut des fixings |
| **Scenarios** | — | bibliothèque (parallèles, steepeners, flatteners, butterfly, IRRBB) + choc sur mesure éditable ; mode cotations ou zéro ; scénario de basis ; P&L du book par scénario ; taux par avant/après ; vue de courbe choquée |
| **Data** | — | chaîne de providers, cache, cotations utilisées, rapport du dernier appel de données (manquants, périmés, suspects) |

Le book des scénarios est construit à partir de l'univers (`default_book`) : payeur 10Y, receveur OIS 2Y, long BTP et short Bund de maturité supérieure à 7 ans. Pour brancher ton propre portefeuille, il suffit de remplacer cette fonction ou de lui passer une liste de `Position`.

Les tests de l'interface (`tests/app/`) couvrent les services et l'exécution complète du dashboard en mode headless (`streamlit.testing.AppTest`) : 6 onglets rendus, aucune exception.

## 13. Intégration avec les autres modules (phase 10)

`rates_analytics.api` est le **contrat** avec le reste du terminal. Il ne renvoie que des objets Python ou pandas, et ses signatures ne changent qu'avec une version majeure (`api.API_VERSION`). Les autres projets n'ont donc rien à réécrire : ils importent ce module.

```python
from rates_analytics import api

snap = api.market_snapshot(as_of)                  # provider de config.yaml / .env
api.discount_factors(snap.curves, [1.0, 5.0, 10.0])  # Series de DF €STR (dates ou années)
api.curve_export(snap.curves)                      # grille mensuelle t, df_*, zero_*, fwd_* (€STR, EURIBOR 6M)
payload = api.curves_to_dict(snap.curves)          # nœuds JSON …
curves = api.curves_from_dict(payload)             # … reconstruits à l'identique (bit à bit)
api.bond_table(snap, snap.bonds)                   # YAS + G / I / Z / ASW, une ligne par ISIN
api.swap_risk(snap, "IRS", start, end, 0.025, 10e6, pay_fixed=True)  # NPV, par, DV01, buckets
```

| Module consommateur | Fonction | Usage |
|---|---|---|
| **CVA Calculator** | `curve_export`, `curves_to_dict` | DF d'actualisation et forwards 6M pour l'exposition Monte Carlo des IRS |
| **Pricing Lab** | `discount_factors`, `curves_from_dict`, `swap_risk` | actualisation des produits structurés, taux sans risque |
| **Portfolio Intelligence** | `bond_table`, `scenarios.scenario_pnl` | sensibilités, spreads, P&L de scénarios des lignes taux |
| **PTF Event Radar** | `bond_table` (`maturity`, `coupon`) | échéances et coupons à venir des lignes obligataires |
| **Valuation Screener** | `discount_factors` | taux sans risque €STR pour le WACC et l'actualisation |

Le transport par JSON permet aussi de calculer les courbes une seule fois (par exemple dans une tâche du matin), puis de les partager entre tous les modules sans nouvel appel aux données.

## 14. Qualité

| Outil | Commande | État |
|---|---|---|
| Tests | `pytest` | 344 tests, sans réseau, environ 1 min 45 |
| Couverture | `pytest --cov` | 93 % (moteur + services UI) |
| Lint / imports | `ruff check .` | 0 alerte (règles E, F, W, I, UP, B, RUF, C4) |
| Typage | `mypy` | 0 erreur sur `src/rates_analytics` |
| CI | `.github/workflows/tests.yml` | Windows + Linux, Python 3.11 et 3.12 |

La phase 10 ajoute aussi des **invariants transverses** (`tests/unit/test_api_and_invariants.py`) :
- un swap au taux par a une NPV nulle ;
- payeur et receveur sont symétriques ;
- un choc zéro de +1 bp déplace toutes les courbes de 1 bp, et la NPV d'environ le DV01 ;
- prix, rendement et Z-spread sont cohérents entre eux, et le spread monte quand le prix baisse.

Ces invariants ont révélé un bug, corrigé depuis. Pour un IRS dont le premier fixing tombe le jour de la courbe, `npv` projetait ce taux alors que les tables SWPM utilisaient le fixing publié. Les deux chemins utilisent désormais la même règle, et un test de non-régression le vérifie.

## 15. Validation

| Test | Contenu |
|---|---|
| `test_target_business_days_1999_2060` | les 22 646 jours de 1999 à 2060 vs `ql.TARGET()` |
| `test_adjust_matches_quantlib` | 7 conventions BDC × chaque jour 2018-2032 |
| `test_advance_matches_quantlib` | 4 000 décalages aléatoires (D/W/M/Y, EOM, BDC) |
| `test_day_counts_match_quantlib` | 5 000 paires aléatoires par convention |
| `test_act_act_icma_with_reference_periods` | 3 000 cas réguliers / premier long / dernier long |
| `test_schedule_matches_quantlib` | 3 000 échéanciers aléatoires (rule, BDC, EOM, stubs) |
| `test_bond_coupon_accruals_match_quantlib_fixed_rate_bond` | 600 obligations : dates de paiement, périodes de référence, $\tau$ |
| `test_curve_matches_quantlib` (12 cas) | courbe d'exemple + 5 courbes aléatoires (dont taux négatifs), 2 interpolations : DF sur une grille de 5 jours jusqu'à 50 ans vs `PiecewiseLogLinearDiscount` / `PiecewiseConvexMonotoneForward` |
| `test_ois_par_rates_match_quantlib` | 80 OIS (spot et forward-start) vs `MakeOIS(...).fairRate()` |
| `test_euribor_curve_matches_quantlib` (8 cas) | courbe 6M dual curve (exemple + 3 scénarios de niveau/pente/basis), 2 interpolations, vs `DepositRateHelper` + `FraRateHelper` + `SwapRateHelper(discountingCurve=€STR)` |
| `test_single_curve_mode_matches_quantlib` | mode single curve vs `SwapRateHelper` sans courbe d'actualisation |
| `test_swap_par_rates_match_quantlib` | 60 IRS (spot et forward-start) : taux par et forwards projetés vs `MakeVanillaSwap` |
| `tests/unit/test_dual_curve.py` | reconstruction exacte de courbes plates, dual ≠ single, basis nul si projection = actualisation, gestion des cotations manquantes |
| `test_accrued_and_yield_match_quantlib` | 300 obligations aléatoires (Bund, OAT, BTP ; premiers coupons courts et longs ; rendements de −0,6 % à 8 %) : couru, prix ↔ rendement vs `BondFunctions` |
| `test_dirty_price_from_nss_curve_matches_quantlib` | fonction d'actualisation NSS vs `FittedBondDiscountCurve(SvenssonFitting, paramètres)` ; prix *dirty* vs `DiscountingBondEngine` |
| `test_nss_fit_is_optimal_for_quantlib` (3 émetteurs) | le pricer QuantLib reproduit notre fonction objectif (1e-8) ; l'optimiseur QuantLib lancé depuis notre solution ne trouve pas mieux ; écart de courbe < 0,5 bp |
| `tests/unit/test_bonds_and_nss.py` | reconstruction exacte d'une courbe NSS, détection d'un outlier planté, filtres, NS automatique, rendements par sur courbe plate, univers configurés |
| `test_yield_and_risk_match_quantlib` (2 × 250 obligations) | street ↔ QuantLib paiements non ajustés, true yield ↔ QuantLib *Following* : rendement 1e-12, Macaulay et modifiée 1e-11, convexité 1e-10, DV01 1e-9 |
| `tests/unit/test_yas.py` | durations et convexité vs différences finies, zéro-coupon, Σ KRD = duration effective, duration effective sur courbe plate, rapport YAS |
| `test_z_spread_matches_quantlib` | 80 obligations : Z-spread vs `BondFunctions.zSpread` (continu, ACT/365F) sur la courbe €STR, 1e-10 |
| `test_asw_par_par_matches_quantlib` | 60 obligations : ASW par-par vs `AssetSwap(parAssetSwap=True).fairSpread()`, projection EURIBOR 6M, actualisation €STR, 1e-10 |
| `test_swap_rate_to_bond_maturity_matches_quantlib` | 40 swaps à date cassée (référence de l'I-spread) vs `MakeVanillaSwap`, 1e-11 |
| `tests/unit/test_spreads.py` | définition du Z-spread, titre au pair sur la courbe, identité ASW en single curve, ASW €STR − ASW 6M ≈ basis, KRD calées sur le Z |
| `test_seasoned_irs_matches_quantlib` (6 swaps) | IRS en vie (départ jusqu'à 4 ans dans le passé, payeur ou receveur, avec ou sans spread) vs `MakeVanillaSwap` + `Euribor6M.addFixing` : NPV 1e-6 € sur 10 M€, taux et spread par 1e-12 |
| `test_seasoned_ois_matches_quantlib` (6 OIS) | OIS €STR en vie, capitalisation des fixings quotidiens vs `MakeOIS` + `Estr.addFixings` : NPV 1e-6 €, taux par 1e-12 |
| `tests/unit/test_swap_pricer.py` | capitalisation €STR à la main, fixing du jour, couru et NPV clean, buckets concentrés sur le bon pilier, **Jacobien = re-bootstrap**, Σ buckets = DV01 parallèle (1e-4) |
| `test_zero_shift_matches_piecewise_zero_spreaded` | choc zéro par morceaux vs `PiecewiseZeroSpreadedTermStructure` (linéaire, continu) : DF à 1e-11 sur 45 ans |
| `test_quote_scenario_matches_quantlib_rebootstrap` (3 scénarios) | cotations choquées puis re-bootstrap €STR + EURIBOR vs QuantLib (DF 1e-11) ; NPV de swaps après choc vs `MakeVanillaSwap` |
| `tests/unit/test_scenarios.py` | formes des chocs et formules IRRBB, décalage exact des taux zéro, taux par décalés du choc, P&L +1 bp = DV01 SWPM, P&L obligation ≈ −duration effective, convexité |
| `tests/unit/test_api_and_invariants.py` | API d'intégration (export, aller-retour JSON bit à bit, table obligataire, risque swap), invariants transverses, non-régression fixing du jour |
| `tests/unit/test_network_ssl.py` | modes SSL (session par mode, magasin Windows via `truststore`, priorité des variables d'environnement), classement des erreurs (certificat, page de blocage du proxy, délai), cause dominante |
| `tests/unit/test_public_sources_and_proxy.py` | parseurs CNBC / SDMX / Stooq, chaîne de sources et coupe-circuit, courbes proxy (court terme sur l'€STR, basis retrouvée au bootstrap), instantané « mode gratuit » complet sans cotation de swap |
| `tests/unit/test_curves.py` | repricing < $10^{-8}$ bp, reconstruction exacte d'une courbe plate, localité du log-linéaire, continuité et intégrale du monotone convexe (4 régions), positivité |

Tolérances : fractions d'année $10^{-13}$ ; DF de courbe $10^{-11}$ ; forwards 1 jour $10^{-5}$ bp ; taux par OIS et IRS $10^{-11}$ ; forwards EURIBOR projetés $10^{-12}$ ; couru et prix $10^{-9}$ ; rendements $10^{-11}$.

**Day count ICMA, cas limite.** Un premier coupon long payé après sa fin théorique (jour férié) donne $d_1 < S < E < d_2$, que l'implémentation ISMA de QuantLib rejette. On le traite en coupant en $E$ (partie longue, puis période notionnelle suivante). Les tests ignorent ces cas pour QuantLib, soit environ 6 % des tirages aléatoires.

**Rendement : convention ICMA et non QuantLib.** `BondFunctions.bondYield` actualise sur les dates de **paiement** ajustées, alors que la convention ICMA (Bloomberg YAS) actualise sur les dates de coupon **théoriques**. Avec des paiements non ajustés, les deux coïncident à $10^{-11}$. Avec des paiements *Following*, QuantLib s'écarte de quelques centièmes de bp quand un coupon tombe un week-end. La logique d'échéancier est alignée sur QuantLib 1.43. Pour le monotone convexe, l'oracle est `ConvexMonotone(quadraticity=0, monotonicity=1, forcePositive=False)` : c'est la méthode Hagan-West de base, sans le lissage par défaut propre à QuantLib. Sur la courbe d'exemple, l'écart mesuré est d'environ 1e-8 bp sur toute la courbe. L'extrapolation au-delà de 50 ans est une convention propre à chaque librairie et n'est pas comparée.

---

## 16. Feuille de route

| Phase | Contenu | Statut |
|---|---|---|
| 1 | Architecture, day counts, calendriers, échéanciers, couche de données | ✅ |
| 2 | Bootstrap €STR OIS (dépôts + OIS, interpolation log-DF linéaire puis monotone convexe) | ✅ |
| 3 | Courbe EURIBOR 6M en dual curve | ✅ |
| 4 | Courbes souveraines NSS (Bund, OAT, BTP) | ✅ |
| 5 | Analytics obligataires YAS | ✅ |
| 6 | Spreads G, I, Z, ASW | ✅ |
| 7 | Pricer IRS et risque bucketé | ✅ |
| 8 | Scénarios de courbe | ✅ |
| 9 | Dashboard Streamlit | ✅ |
| 10 | Tests, API d'intégration, qualité (ruff, mypy, CI), documentation | ✅ |
