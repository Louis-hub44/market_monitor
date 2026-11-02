# Portfolio Intelligence & Risk Platform

Plateforme interne d'analyse et de gestion des risques pour trois profils de gérants —
**Equity**, **Fixed Income**, **Diversifié** — sur un moteur de données et de
portefeuille commun.

Elle répond à quatre questions :

| Module | Question |
| --- | --- |
| Dashboard | Qu'est-ce qui peut impacter mon portefeuille aujourd'hui, et quels sont mes principaux risques ? |
| Equity | Quelles sociétés sont attractives, et quels sont les risques de ma poche actions ? |
| Fixed Income | Quelle est ma sensibilité à la courbe, et que se passe-t-il si les taux bougent ? |
| Diversifié | Que se passe-t-il si le scénario macro que j'anticipe se réalise ? |

---

## Démarrage

```bash
python -m venv .venv && source .venv/bin/activate     # Python 3.10+
pip install -r requirements.txt

cp .env.example .env        # facultatif : voir ci-dessous
streamlit run app.py
```

**La clé FMP se saisit directement dans l'application** — panneau latéral →
**Connexion** → champ *Clé API Financial Modeling Prep* → `Appliquer`, puis
`Tester` pour valider par un appel réel. Aucun redémarrage. Deux modes :

- **sans mémorisation** (défaut) : la clé ne vit qu'en mémoire, le temps de la session ;
- **avec mémorisation** : écrite dans `.local_config.json` (ignoré par git,
  permissions 0600) — pratique sur un poste personnel, à éviter sur un poste partagé.

L'alternative reste `FMP_API_KEY` dans `.env`. La saisie d'interface est
prioritaire sur l'environnement.

Sans clé API, l'application démarre quand même : les pages fonctionnent en mode
dégradé et indiquent explicitement quelles données manquent. Trois portefeuilles de
démonstration sont fournis dans `samples/` (positions fictives — voir
`samples/README.md`).

```bash
pytest                      # 157 tests
```

---

## Format de fichier portefeuille

CSV ou Excel. Les en-têtes sont reconnus **en français comme en anglais**, dans
n'importe quel ordre, avec virgule ou point décimal.

| Champ | Alias reconnus | Requis |
| --- | --- | --- |
| Identifiant | ISIN, ticker, symbol, code, valeur | oui |
| Libellé | name, nom, libellé, bond name | recommandé |
| Valorisation | market value, valorisation, montant — **ou** poids — **ou** quantité + prix | oui (l'un des trois) |
| Classe d'actif | asset class, classe d'actif, type | déduite sinon |
| Devise, secteur, pays | currency/devise, sector/secteur, country/pays | recommandés |
| Coupon, maturité | coupon/taux, maturity/échéance | **obligatoires pour la duration** |
| Fréquence, YTM, spread, rating | frequency, yield/rendement, spread, notation | facultatifs |

Conventions gérées automatiquement : coupon saisi en `3.25` ou `0.0325` ; prix
obligataire en pourcentage du nominal ; trésorerie cotée à 100 interprétée au pair ;
dates `JJ/MM/AAAA` ou `AAAA-MM-JJ` ; lignes de cash isolées de la NAV.

**Colonne « nominal » ambiguë.** Beaucoup d'exports nomment *nominal* une
colonne qui contient déjà la **valeur de marché**. La détection s'appuie sur la
rondeur des valeurs — un nominal est rond (multiple de 1 000), une valorisation
traîne des centimes (`900457.05636`) — et le résultat est indiqué dans le
rapport d'import. En cas d'erreur, forcer l'interprétation à l'import
(panneau latéral → *Colonne de quantité*).

**Unités des taux.** L'unité est déterminée par colonne, sur la médiane :
`3.875` et `0.625` dans la même colonne sont lus 3,875 % et 0,625 %. Les spreads
sont normalisés en points de base, qu'ils arrivent en bp, en pourcents ou en
décimal.

**Dérivés.** Futures, forwards, swaps et options sont identifiés et **exclus**
des calculs de duration et de scénarios : leur exposition se mesure en notionnel
de contrat, pas en valeur de marché. La duration affichée est donc *brute, avant
couverture* — le rapport d'import le signale explicitement.

**Fichier fourni en poids uniquement** (sans valorisation) : les poids sont
renormalisés à 100 % et mis à l'échelle sur une NAV notionnelle de 1 000 000 —
sinon la NAV vaudrait 1. Le portefeuille est alors marqué *notionnel* dans
l'interface : les ratios et les impacts en pourcentage restent exacts, seuls les
montants absolus (valorisations, DV01) sont conventionnels. La base est
paramétrable (`notional=`).

Tout ce qui a été **déduit** figure dans le rapport d'import (page Portefeuille →
onglet Rapport d'import) : correspondance des colonnes, classes d'actif inférées,
lignes rejetées et pourquoi.

---

## Structure

```
app.py                    navigation et panneau latéral — aucun calcul
config/                   settings (.env), constantes de marché, hypothèses, design
models/                   Portfolio · Position · DataPoint · Scenario · enums
data/
  providers/              base (cache, rate limit, retries) · fmp · yahoo
  market_data.py          orchestrateur Officiel → FMP → Yahoo → Cache
  cache.py                cache disque à TTL, lecture périmée assumée
services/
  portfolio/              import de fichiers, valorisation, expositions
  equity/                 screener, valorisation historique, résultats, facteurs
  fixed_income/           bond_math (noyau) · analytics · courbes
  scenarios/              moteur commun explicable · bibliothèque de scénarios
  risk/                   volatilité, drawdown, VaR, corrélations, bêtas
  events/                 Event Radar relié au portefeuille
views/                    7 pages Streamlit
components/               composants UI et graphiques Plotly
tests/                    66 tests
samples/                  portefeuilles de démonstration (fictifs)
```

Séparation stricte **Data → Logique → UI** : `services/` n'importe jamais Streamlit,
`models/` n'importe rien du projet. Tout le moteur est donc utilisable en batch, en
notebook ou derrière une future API.

---

## Deux principes qui gouvernent tout le reste

### Aucune donnée synthétique présentée comme réelle

Toute valeur externe circule dans un `DataPoint` portant sa qualité (VERIFIED,
API_CONFIRMED, CACHED, ESTIMATED, UNVERIFIED), sa source et sa date. Deux sources
concordantes ⇒ VERIFIED. Provider indisponible ⇒ repli sur cache **dégradé en
CACHED** avec l'âge affiché. Donnée absente ⇒ « non disponible », jamais un zéro.

Chaque agrégat porte en outre son **taux de couverture** : « duration 5,8 — calculée
sur 72 % de la poche » plutôt qu'un 5,8 trompeur.

### Aucune boîte noire

Chaque impact de scénario est une contribution nommée, avec sa formule :

```
Scénario : Taux +100 bp        Impact portefeuille : -2,74 %

  GOV_BOND        -1,82 %
  CREDIT          -0,61 %
  EQUITY          -0,31 %

OAT 3,00 % 2034 : -D×Δy + ½C×Δy² = -7,42×0,0100 + ½×68,1×0,0100²
                  = -7,08 % × poids 4,30 %
```

Les paramètres qui ne proviennent d'aucune source (sensibilité d'un secteur au
pétrole, par exemple) sont regroupés sous le nom explicite d'**hypothèses de modèle**,
surchargeables par fichier JSON, et tout résultat qui s'en sert est marqué
« confiance faible » avec l'hypothèse affichée.

---

## Thème

Mode **sombre par défaut**, mode clair disponible : sélecteur en haut du panneau
latéral, application immédiate. Le fond sombre n'est pas noir pur mais un
bleu-ardoise profond — le noir absolu écrase les contrastes et fait vibrer le
texte fin.

Le thème des composants natifs de Streamlit (grilles de données, menus, champs)
vient de `.streamlit/config.toml`, que Streamlit ne permet pas de changer à
chaud ; il est accordé sur la palette sombre. Le sélecteur pilote tout ce que
l'application dessine elle-même : CSS, graphiques, palettes. Pour travailler en
clair en permanence, reprendre les valeurs de `LIGHT` (`config/theme.py`) dans
`config.toml`.

Les couleurs suivent une règle simple : les **catégories** (secteurs, pays,
devises) reçoivent dix teintes franchement distinctes ; les **intensités**
(key rate durations) un dégradé ; les **impacts signés** une échelle divergente
rouge/bleu centrée sur zéro. Vert et rouge ne servent jamais à coder une
catégorie — dans cette application, le rouge signifie une perte.

---

## Bloomberg (Desktop API)

Quand le Terminal tourne sur le poste, Bloomberg passe **en tête de chaîne**
pour toutes les données. Aucune clé à configurer : la Desktop API écoute sur
`localhost:8194` et hérite des droits de l'utilisateur connecté.

```bash
pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple blpapi
```

Prérequis : Terminal lancé et session ouverte, bibliothèque C++ sur le `PATH`
(`BLPAPI_ROOT` sous Windows). Sans `blpapi`, l'application démarre normalement
et bascule sur FMP puis Yahoo.

Ce que Bloomberg apporte que les autres sources ne peuvent pas :

| Domaine | Apport |
| --- | --- |
| **Calendrier macro** | Fed, BCE, BoE, CPI, NFP, PIB, PMI avec consensus (`BN_SURVEY_MEDIAN`). Aucun équivalent chez Yahoo, et dépendant du plan chez FMP. |
| **Courbes non-USD** | Bund, OAT, BTP, Gilt, JGB par tickers génériques. C'était la limite principale du projet : sans Bloomberg, seule la courbe US est atteignable, et les lignes en euro perdent leurs key rate durations. |
| **Analytique obligataire** | Duration option-adjusted (`YAS_MOD_DUR`), convexité, duration de spread, KRD, Z-spread et OAS — calculées par Bloomberg, donc **cohérentes avec YAS\<GO\>**. Elles remplacent notre calcul interne, qui ne price pas les optionalités (call, FRN, step-up). |
| **Référentiel obligataire** | Coupon, maturité, fréquence, notations, prochaine date de call — de quoi compléter un fichier portefeuille incomplet. |
| **Résultats et consensus** | `EXPECTED_REPORT_DT`, `BEST_EPS`, `BEST_SALES` par titre. |

Vérification des droits : page **Qualité des données → Bloomberg → « Tester
Bloomberg »**. Un champ hors abonnement revient **vide plutôt qu'en erreur**,
c'est le seul moyen fiable de savoir ce que le poste couvre réellement.

Les tickers de courbes et la liste des publications macro suivies sont éditables
dans `config/constants.py` (`BLOOMBERG_CURVE_TICKERS`, `BLOOMBERG_ECO_TICKERS`).

---

## Sources de données : que peut faire Yahoo sans FMP ?

Yahoo Finance (`yfinance`) sert de filet de secours quand la clé FMP manque ou
que l'endpoint n'est pas couvert par le plan souscrit. Le basculement est
automatique et la qualité de la donnée est dégradée en conséquence.

| Besoin | Couverture Yahoo | Qualité |
| --- | --- | --- |
| Cours, historiques, bêtas, volatilités | **complète** | API_CONFIRMED |
| Dates de résultats, EPS estimé et publié, surprises | **bonne** — pas de calendrier global, interrogation titre par titre sur les lignes du portefeuille | API_CONFIRMED |
| Multiples courants (PER, PER estimé, P/B, EV/EBITDA, marges, ROE) | **bonne** — via `Ticker.info` | API_CONFIRMED |
| Historique des multiples | **reconstruite** — cours de clôture d'exercice ÷ EPS dilué publié, sans retraitement | ESTIMATED |
| Courbe des taux US | **approchée** — 4 points seulement (^IRX, ^FVX, ^TNX, ^TYX), interpolés | ESTIMATED |
| Dividendes | **complète** | API_CONFIRMED |
| Screener multicritère | **partielle** — `yfinance.screen` existe mais sans parité de filtres avec FMP ; non branché | — |
| **Calendrier macro** (Fed, BCE, CPI, NFP) | **aucune** — c'est ce que Bloomberg couvre | — |

Le dernier point est la vraie limite : sans FMP, l'Event Radar se réduit aux
résultats de sociétés. Une source officielle (FRED, BCE Data Portal) serait le
bon complément — c'est un provider à écrire, pas un réglage.

---

## Réseau d'entreprise (proxy d'inspection SSL)

Sur un poste équipé de Zscaler, Netskope, Fortinet ou équivalent, le proxy
re-signe le trafic HTTPS avec un certificat interne inconnu de Python. Symptôme
caractéristique : **toutes les sources échouent en même temps**, avec
`certificate verify failed`. L'application ne plante pas — elle bascule sur le
cache et affiche la qualité CACHED — mais plus rien n'est frais.

**Correction depuis l'interface** — panneau latéral → **Connexion** →
*Vérification SSL* :

- **liste déroulante** des magasins de certificats détectés sur le poste
  (Zscaler, Netskope, chemins système), ou saisie d'un chemin manuel ;
- bouton **Diagnostiquer la connexion** qui sonde chaque fournisseur séparément.

Tout s'applique **à chaud**, sur tous les providers, sans redémarrage. Cochez
« Mémoriser ce réglage réseau » pour le conserver d'une session à l'autre.

Diagnostic détaillé : page **Qualité des données → Réseau**, avec la cause
qualifiée par source (`ssl`, `proxy`, `proxy_auth`, `timeout`, `dns`, `blocked`,
`http`). Diagnostic de tout le terminal en ligne de commande : `check-network`.

Équivalents en variables d'environnement, si vous préférez figer le réglage :

```bash
PI_CA_BUNDLE=/chemin/vers/entreprise.pem          # ou TERMINAL_CA_BUNDLE
TERMINAL_HTTPS_PROXY=http://proxy.entreprise.local:8080
```

Toutes les requêtes passent par **la session HTTP unique du terminal**
(`core/terminal_core/network.py`). Par défaut, elle fait confiance au magasin de
certificats du poste (paquet `truststore`), où l'informatique installe la racine
du proxy ; le certificat d'entreprise éventuel y est ajouté. `REQUESTS_CA_BUNDLE`
et `SSL_CERT_FILE` sont aussi reconnues. **La vérification SSL n'est jamais
désactivée** : l'ancien `PI_INSECURE_SSL` est ignoré avec un avertissement.

Le réglage s'applique à **tous** les fournisseurs — le proxy est un problème de
poste, pas de source. `yfinance` reçoit lui aussi la session du terminal (singleton
`YfData`) au lieu de son client `curl_cffi` natif : aucune requête n'échappe à la
politique réseau.

---

## Limites connues

- **Courbes non-USD sans Bloomberg** : FMP et Yahoo ne publient que la courbe US.
  Les lignes en euro retombent alors sur les sensibilités analytiques (pas de KRD).
  Avec Bloomberg, les six courbes sont couvertes.
- **Instruments à option** : callables, FRN et titres indexés inflation ne sont pas
  pricés correctement par le noyau. Activer QuantLib (`requirements.txt`) pour ces cas.
- **Endpoints FMP payants** : calendrier de résultats et historique d'estimations
  dépendent du plan souscrit. La page Qualité des données indique lesquels répondent.
- **Profil factoriel** : alimenté aujourd'hui par le screener enrichi uniquement.
- Les estimations de scénario sont des **ordres de grandeur conditionnels**, jamais
  des prévisions.

Conception détaillée, revue des briques open source et feuille de route :
[`ARCHITECTURE.md`](ARCHITECTURE.md).
