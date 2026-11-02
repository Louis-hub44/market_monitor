# Architecture technique — Portfolio Intelligence & Risk Platform

Document de conception. Il précède le code et fait autorité sur lui : toute
divergence est un bug d'implémentation, pas une évolution tacite.

---

## 1. Revue des briques open source et arbitrages

| Brique | Statut | Décision |
| --- | --- | --- |
| **QuantLib-Python** | BSD modifiée, standard de place | **Dépendance optionnelle**, pour les instruments à option uniquement (callables, FRN, indexés inflation). Non requise au démarrage. |
| **rateslib** | Licence source-available **non commerciale** | **Écartée.** Techniquement excellente, mais inutilisable chez un asset manager sans licence payante. Risque juridique non négociable. |
| **financetoolkit** | MIT, large couverture | **Écartée comme socle.** Mélange collecte de données et calcul, ce qui casse la séparation Data → Logique → UI. Reste une référence croisée utile pour valider des ratios. |
| **empyrical / pyfolio** | Non maintenus depuis plusieurs années | **Écartées.** Les métriques concernées (vol, drawdown, Sharpe, VaR) représentent ~150 lignes qu'il vaut mieux maîtriser et tester soi-même. |
| **OpenBB** | AGPL, plateforme complète | **Écartée comme dépendance** (contamination AGPL sur du code interne). Utile comme source d'inspiration pour les schémas de normalisation multi-providers. |
| **yfinance** | Apache 2.0, API non officielle | **Source secondaire optionnelle.** Sert au recoupement de prix et aux historiques longs. L'application démarre et fonctionne sans. |
| **pandas / NumPy / Plotly / Streamlit** | Socle imposé par le cahier des charges | Retenus. Streamlit ≥ 1.59 pour `st.navigation`. |
| **SciPy / statsmodels** | BSD | Optionnels, réservés aux régressions factorielles avancées. **Exclus du noyau de calcul.** |

### Conséquence structurante

Le **noyau financier est écrit en interne**, sans dépendance externe :
`services/fixed_income/bond_math.py` (~250 lignes) couvre prix, YTM, durations,
convexité, KRD et duration de spread. Motifs :

1. **Auditabilité** — un gérant qui conteste un chiffre doit pouvoir remonter à la
   formule en trois clics, pas dans le code C++ d'une librairie tierce.
2. **Testabilité** — chaque fonction est confrontée à une formule fermée connue
   (voir §6), ce qui donne une preuve de correction plutôt qu'une présomption.
3. **Zéro risque de licence** sur le cœur métier.

---

## 2. Vue d'ensemble

```
                        UI (Streamlit)                    views/, components/
                              │
                    ── ne contient aucun calcul ──
                              │
                    Logique métier & calculs               services/
        ┌─────────────┬───────┴────────┬──────────────┬─────────────┐
     portfolio      equity        fixed_income     scenarios      events
                              │
                    ── ne contient aucun appel API ──
                              │
                      Accès aux données                    data/
              Sources officielles → FMP → Yahoo → Cache
                              │
                     Modèle de domaine                     models/
                Portfolio · Position · DataPoint · Scenario
```

**Règle d'or (§25 du cahier des charges) :** les dépendances ne remontent jamais.
`services/` n'importe pas `views/`. `models/` n'importe rien du projet hors `models/`.
Un import de `streamlit` dans `services/` est un défaut bloquant en revue de code.

Conséquence pratique : tout le moteur est utilisable **hors Streamlit** — en batch
nocturne, dans un notebook, ou derrière une future API FastAPI.

---

## 3. Modèle de domaine

### 3.1 `DataPoint` — la traçabilité comme type

```python
DataPoint(value, quality, source, as_of, fetched_at, note)
```

Aucune valeur venant de l'extérieur ne circule nue. Les cinq niveaux du §19 sont
ordonnés (`DataQuality.rank`), ce qui permet un arbitrage automatique :

- `reconcile()` compare deux sources indépendantes ; concordance à moins de 0,5 %
  ⇒ promotion en **VERIFIED**, sinon la meilleure source est conservée avec une
  note décrivant le désaccord ;
- `is_decision_grade` interdit à une donnée UNVERIFIED d'alimenter un calcul ;
- `missing()` matérialise l'absence : **jamais de zéro de repli, jamais de valeur
  inventée.**

### 3.2 `Portfolio` / `Position`

Structure unique pour les trois métiers, avec les champs obligataires à `None` pour
les actions. Méthode clé : **`coverage(attribut)`**, part de la NAV pour laquelle un
attribut est renseigné. C'est ce qui permet d'afficher « duration 5,8 — calculée sur
72 % de la poche » au lieu d'un 5,8 trompeur.

### 3.3 `Scenario` / `Contribution` / `ScenarioResult`

Un résultat de scénario **n'est pas un nombre** : c'est une liste de `Contribution`,
chacune portant la position, le choc, le poids, l'impact, la **formule appliquée**,
les entrées numériques et un niveau de confiance. Le total est une somme vérifiable
(test `test_total_equals_sum_of_contributions`).

---

## 4. Couche données

### 4.1 Chaîne de résolution

```
Sources officielles  →  FMP  →  Yahoo  →  Cache périmé
    VERIFIED          API_CONFIRMED      CACHED
```

Le repli sur cache périmé n'intervient que si **tous** les providers échouent, et
dégrade systématiquement la qualité en `CACHED` avec l'âge de la donnée affiché.
C'est ce qui satisfait l'exigence §27 : l'application ne plante pas quand un
provider tombe, et ne ment pas non plus sur la fraîcheur.

### 4.2 Robustesse

- `RateLimiter` à fenêtre glissante, thread-safe, calibré sur le quota FMP ;
- retries avec back-off exponentiel ; 429 retenté, 401/403 remonté immédiatement
  (inutile d'insister sur une clé invalide) ;
- collecte parallèle bornée (`ThreadPoolExecutor`, `MAX_WORKERS`) — une exception
  sur une ligne ne fait pas tomber la page, elle produit un `missing()` ;
- **journal de collecte** en session : quel appel, quelle source, quelle qualité,
  quelle latence. Consultable dans la page Qualité des données.

### 4.3 FMP

API `stable` (`https://financialmodelingprep.com/stable/...`). Tous les chemins sont
regroupés dans le dictionnaire `ENDPOINTS` de `data/providers/fmp.py` : une évolution
de l'API se corrige à un seul endroit. `self_test()` sonde chaque endpoint et affiche
son statut réel — plusieurs (calendrier de résultats notamment) sont réservés aux
plans payants, et il vaut mieux le savoir explicitement que par une page vide.

Clé lue dans `FMP_API_KEY` via `.env`, jamais dans le code, jamais journalisée,
masquée dans l'affichage de configuration.

### 4.4 Réseau d'entreprise (`data/network.py`)

Un proxy d'inspection SSL est la panne la plus fréquente en installation
d'entreprise, et la plus trompeuse : elle ressemble à une panne de fournisseur
alors qu'elle vient du poste. Le module la traite comme un cas de première
classe.

- **Session unique du terminal** : tous les providers (et yfinance) utilisent la
  session de `terminal_core.network` : proxy configurable, magasin de certificats
  du poste (`truststore`) complété du certificat d'entreprise, sinon ce certificat,
  sinon certifi. **`verify=False` est impossible** (la session le refuse). Un
  chemin de certificat invalide est ignoré sans dégrader la vérification.
- **Qualification de la panne** : `BaseProvider` distingue un échec de
  certificat d'une indisponibilité normale et positionne `ssl_blocked`. Quand
  toutes les sources sont bloquées pour ce motif, l'interface accuse le proxy,
  pas les fournisseurs, et affiche directement le correctif.
- **Diagnostic** : `check_connectivity()` sonde chaque source et classe la cause
  (`ok`, `ssl`, `proxy`, `timeout`, `dns`, `blocked`). Une source qui répond et
  une autre qui échoue ⇒ problème de fournisseur ; les deux en échec SSL ⇒
  problème de poste.
- **Cas yfinance** : la session du terminal lui est imposée via le singleton
  `YfData` (`session=` sur `yf.Ticker` ne couvre pas tous les appels), à la place
  de son client `curl_cffi` natif.

L'ancien contournement `PI_INSECURE_SSL` est **ignoré** (avertissement journalisé).

### 4.5 Repli Yahoo sur les fonctions FMP

FMP segmente ses endpoints par plan : un utilisateur peut avoir une clé valide
et recevoir des réponses vides. La chaîne bascule donc **par fonction**, pas
globalement.

| Fonction | Chemin | Qualité du repli |
| --- | --- | --- |
| `treasury_curve` | FMP `treasury-rates` → 4 indices Yahoo interpolés | ESTIMATED |
| `earnings` | FMP `earnings` → `Ticker.earnings_dates` | API_CONFIRMED (CA absent) |
| `earnings_calendar` | FMP calendrier global → boucle Yahoo sur les symboles du portefeuille | API_CONFIRMED |
| `fundamentals` | FMP `ratios`+`key-metrics` → `Ticker.info` | API_CONFIRMED |
| `valuation_history` | FMP ratios annuels → reconstruction cours ÷ EPS publié | ESTIMATED |
| `economic_calendar` | FMP seul — **aucun repli possible** | — |

Deux principes : le repli **dégrade explicitement la qualité** (une courbe à
quatre points interpolés n'est pas une courbe de valorisation), et l'absence de
repli est **dite** plutôt que masquée par un tableau vide.

### 4.6 Configuration à chaud (`config/runtime.py`)

Trois couches, par priorité décroissante : **interface > environnement (.env) >
défaut**. `Settings` est un dataclass figé lu au démarrage ; les réglages que
l'utilisateur doit pouvoir changer sans redémarrer (clé FMP, politique SSL)
passent par `MarketDataService.set_api_key()` / `set_network()`, qui
reconfigurent les sessions des providers sur place.

Deux points de conception :

- **Le changement de certificat est réversible.** `set_network(ca)` rebranche
  tous les providers sur la session partagée correspondante ; `set_network(None)`
  revient au magasin du poste. Couvert par
  `test_certificate_change_is_applied_to_all_providers_and_reversible`.
- **La clé n'est jamais écrite dans le cache.** Elle est ajoutée aux paramètres
  de requête *après* le calcul de l'empreinte de cache. `verify_key()` purge le
  cache lors d'une bascule, car deux clés peuvent ouvrir des droits différents.
  L'affichage passe par `mask_key()` : jamais la clé entière, y compris pour les
  clés courtes.

Persistance optionnelle dans `.local_config.json` (gitignoré, 0600, liste
blanche de clés autorisées à la lecture pour qu'un fichier trafiqué ne puisse
pas injecter de paramètres arbitraires). La clé y est **en clair**, comme dans
`.env` : acceptable sur un poste dédié, pas sur un partage réseau — d'où le
choix de ne rien persister par défaut.

### 4.7 Voie d'extension (nouvelle source)

Un nouveau provider hérite de `BaseProvider` (qui apporte cache, rate limiting,
retries) et s'insère en tête de chaîne dans `MarketDataService`. Aucune modification
des couches calcul et UI.

Bloomberg a été retiré (plus d'accès au Terminal) : la chaîne est FMP → Yahoo →
cache, et les courbes EUR viennent du module Rates & Bond Analytics.

---

## 5. Moteurs de calcul

### 5.1 Obligataire

Deux chemins, choisis automatiquement selon les données disponibles :

| Données disponibles | Méthode | Qualité |
| --- | --- | --- |
| Courbe zéro-coupon de marché | Repricing symétrique ±1 bp ⇒ duration/convexité **effectives**, KRD par bump triangulaire (Ho, 1992) | API_CONFIRMED |
| Prix seul | YTM par bissection, puis formules analytiques | ESTIMATED |
| Ni prix ni YTM, mais une courbe | Valorisation théorique sur courbe + spread fourni | ESTIMATED |
| Ni maturité ni coupon | **Position exclue**, signalée dans les avertissements | UNVERIFIED |

Propriété vérifiée par test : **Σ KRD = duration effective**. C'est le contrôle qui
prouve que la décomposition par maturité est cohérente avec la sensibilité globale.

### 5.2 Moteur de scénarios

Pipeline : `Portefeuille → expositions → choc → impact actif → impact position → total`.

| Classe d'actif | Choc | Formule |
| --- | --- | --- |
| Actions | Indice | `β × Δindice` (β par régression, sinon hypothèse) |
| Actions | Taux, pétrole, crédit | sensibilité sectorielle × choc — **hypothèse de modèle, confiance faible** |
| Obligations | Taux parallèle | `−D×Δy + ½C×Δy²` |
| Obligations | Courbe | `−Σ KRDₜ × Δyₜ` |
| Crédit | Spread | `−SD×Δs` |
| Toutes | Change | exposition directe ; appréciation de la devise de base ⇒ `1/(1+x)−1` sur les actifs en devises |

Hiérarchie des sensibilités : (1) calculée sur données réelles → confiance élevée ;
(2) analytique → moyenne ; (3) hypothèse de modèle documentée → **faible**, avec
l'hypothèse affichée à l'écran.

### 5.3 Hypothèses de modèle — le point à surveiller

Chiffrer « pétrole +20 % » sur une action exige un paramètre de sensibilité qui
n'existe dans aucune base. Ces paramètres sont isolés dans
`config/constants.py::DEFAULT_MODEL_ASSUMPTIONS`, surchargeables sans toucher au code
via `MODEL_ASSUMPTIONS_FILE`, et **tout résultat qui s'en sert est marqué confiance
faible**. Ils ne sont utilisés qu'en dernier recours : les bêtas actions sont estimés
par régression MCO sur historiques réels dès que les données existent.

C'est le seul endroit de la plateforme où des nombres non issus d'une source
existent, et ils sont nommés « hypothèses », pas « données ».

---

## 6. Stratégie de test

157 tests, dont le principe est de **ne jamais tester un calcul contre lui-même** :

| Test | Référence externe |
| --- | --- |
| Zéro-coupon | `P = 100/(1+y)^T`, `D = T`, `C = T(T+1)/(1+y)²` |
| Obligation au pair | formule d'annuité `D = ((1+y)/y)(1−(1+y)^−T)` = 2,8334 (6 %, 3 ans) |
| Duration modifiée | dérivée numérique du prix |
| Convexité | dérivée seconde numérique |
| KRD | somme = duration effective |
| Approximation d'ordre 2 | écart au repricing exact < 2 bp jusqu'à ±100 bp |
| Bêta | réplique à levier 2 ⇒ β = 2,00 ; R² > 0,99 |
| Scénarios | additivité, monotonie, effet de convexité (gain de baisse > perte de hausse) |
| Pages | `AppTest` sur les 7 vues **en mode dégradé** (sans réseau ni clé) |
| Event Radar | construction sur fournisseur factice, avec et sans calendrier macro (régression `UnboundLocalError`) |
| Import de fichiers réels | nominal vs valorisation, unités de taux par colonne, spreads normalisés, dérivés isolés, coupons < 1 % préservés |
| Réseau et configuration | détection SSL, session unique du terminal, `verify=False` impossible (et absent des sources), certificat invalide non dégradant, réversibilité du changement de certificat, masquage de clé |
| Thème | palettes complètes, couleurs de catégories distinctes, signe jamais réutilisé, 12 graphiques construits dans les deux modes |
| Replis Yahoo | bascule courbe / résultats / calendrier, qualité dégradée en ESTIMATED, absence de repli macro explicite |
| Point d'entrée | `AppTest` sur `app.py` + validation des icônes Streamlit (attrape les erreurs d'API que `HTTP 200` ne révèle pas) |

Le dernier point mérite d'être souligné : la panne de provider est le scénario de
production le plus fréquent, et il est testé comme un cas nominal.

---

## 7. Interface

Parti pris : **un instrument de mesure, pas un terminal noir**. Fond clair haute
lisibilité (usage prolongé), encre ardoise, un seul accent bleu de signalisation,
rouge et vert réservés au signe des impacts. Tous les chiffres en IBM Plex Mono à
chiffres tabulaires — les colonnes s'alignent parfaitement, ce qui est la seule chose
qu'un gérant regarde vraiment. Signature visuelle : le **rail de risque**, barres
horizontales proportionnelles à l'impact, utilisé partout où des scénarios sont classés.

Vocabulaire graphique du dashboard, du général au particulier :

| Élément | Rôle | Pourquoi ce choix |
| --- | --- | --- |
| **Bande d'identité** | portefeuille, NAV, arrêté, concentration | Répond en un coup d'œil à « quel fonds je regarde » — la question posée avant toute lecture de chiffre |
| **Titres de bloc à filet** | rythme la page | Hiérarchise sans consommer les niveaux Markdown ni alourdir |
| **NAV + drawdown superposés** | trajectoire | Le niveau seul cache l'amplitude des replis ; les deux axes partagent le temps |
| **Barre d'allocation empilée** | composition | Plus lisible qu'un camembert pour comparer des poids, et bien plus compacte |
| **Classement d'impacts étiqueté** | risques dominants | L'ordre et la valeur sont lus directement, sans survol |
| **Rail de risque** | scores d'événements | Même grammaire visuelle que les impacts, donc lecture immédiate |

Les anneaux sont réservés aux répartitions de faible cardinalité (≤ 8 postes) ;
au-delà, une barre ou une légende chiffrée reste plus lisible.

Aucune imitation d'interface propriétaire.

Note d'implémentation : les pages sont dans `views/` et non `pages/`, pour éviter la
découverte automatique multipage de Streamlit qui entrerait en conflit avec
`st.navigation`. La hiérarchie de navigation demandée au §24 est inchangée.

---

## 8. Performance

Cache disque à TTL différencié (cours 5 min, fondamentaux 24 h, calendriers 1 h) ;
`st.cache_resource` pour le service de données ; mémoïsation en session des calculs
lourds, invalidée au changement de portefeuille ; collecte parallèle bornée ;
**chargement progressif** — historiques, calendriers et corrélations sont derrière un
bouton explicite, car ils coûtent un appel par ligne.

---

## 9. État d'avancement et suite

| Phase | Périmètre | État |
| --- | --- | --- |
| 1 | Architecture, Portfolio Engine, Data Engine | **Terminée, testée** |
| 2 | Equity — screener, valorisation, résultats, facteurs | Écrite, **à valider contre l'API réelle** |
| 3 | Fixed Income — duration, KRD, courbes, scénarios | **Terminée, testée** |
| 4 | Diversifié — allocation, macro, matrice d'impact | **Terminée** |
| 5 | Event Radar | Écrit, dépend d'endpoints FMP payants |
| 6 | Moteur de scénarios commun | **Terminé, testé** |
| 7 | Qualité des données, validation, tests | **Terminée** |
| 8 | Finition UI/UX | En cours |

### Priorités immédiates

1. **Valider les endpoints FMP avec ta clé** (page Qualité des données →
   « Tester les endpoints ») et ajuster `ENDPOINTS` selon ton plan.
2. **Courbes non-USD** : seule la courbe US Treasury est exposée par FMP. Bund, OAT
   et Gilt nécessitent une source complémentaire (BCE Data Portal, Banque de France) —
   sans elles, les KRD des lignes en euro retombent sur l'approche analytique.
3. **Calibrer les hypothèses sectorielles** sur ton historique plutôt que sur les
   valeurs de départ, en remplaçant progressivement les paramètres par des
   régressions — chaque paramètre remplacé fait passer un résultat de « confiance
   faible » à « confiance élevée ».
4. **Profil factoriel** : brancher un cache de ratios fondamentaux par ligne du
   portefeuille (aujourd'hui alimenté uniquement par le screener enrichi).
