# Historique des versions

## 1.6.6
- **Fiche instrument : écart entre deux obligations.** Superposer un taux à un autre (OAT 10 ans
  contre BTP 10 ans, OAT 2 ans contre OAT 10 ans, Bonos 5 ans contre BTP 10 ans…) affiche le
  graphique de l'écart en pb (instrument − instrument superposé), avec son niveau et ses
  variations 1J / 1S / 1M / YTD / 1A. Même chose entre deux spreads. Le spread contre le Bund
  reste affiché dessous (une seule fois quand le Bund est l'instrument superposé).

## 1.6.5
- **Brent sur le contrat le plus traité** (comme Investing et la presse) : le contrat suivant est
  coté dès 5 jours ouvrés avant l'échéance du premier (`roll_ahead: 5`, nouvelle source
  `fut:` qui recolle le générique Yahoo `BZ=F` et les contrats `BZ<mois><aa>.NYM`). Le 29/10/2026,
  la veille de l'échéance de décembre (115,32 $), le bandeau et le graphique passent sur janvier
  (~111,6 $). FMP (`BZUSD`, générique) n'est plus utilisé pour le Brent ; `BZ=F` reste en secours.
- Le jour du changement de contrat est signalé comme un roll (variation 1J et z-score non
  interprétés, pas d'alerte), comme le jour d'échéance.

## 1.6.4
- **Taux souverains européens (Bund, OAT, BTP, Bonos, Gilt, JGB) : Stooq en première source**,
  CNBC en secours. Depuis le 25/10/2026, les barres quotidiennes CNBC ne donnaient plus la
  clôture (27/10 : Bund 3,340 % au lieu de 3,355 %, OAT 4,908 % au lieu de 4,924 %).
- Contrôle croisé Stooq / CNBC à 1,5 pb près (`quality.cross_check_quote_tol_bp`) : un écart
  passe la ligne (et les spreads qui en dépendent) en « À vérifier avant diffusion ».
- Premier lancement : l'historique de ces taux est rechargé une fois depuis Stooq.

## 1.6.3
- `market-monitor diagnostic-taux` compare le Bund et l'OAT 10 ans sur les dernières séances :
  CNBC (horodatage brut de chaque barre et jour retenu), Stooq et Bundesbank côte à côte, avec un
  verdict « concorde au jour près » / « en retard d'une séance ».

## 1.6.2
- **Rechargement manuel** (`ui.refresh: manual`, par défaut) : changer de vue ne réinterroge
  plus les sources (FMP compris). Les données sont chargées à l'ouverture et au clic sur
  « Actualiser les données » ; seules les données jamais chargées sont téléchargées en naviguant.
  Heure du dernier chargement sous le bouton. Règle des clôtures appliquée à l'heure du
  chargement. `ui.refresh: auto` garde l'ancien comportement.

## 1.6.1
- **Fiche instrument** : le second instrument superposé (ex. DAX sur le CAC 40) restait hors du
  graphique, son axe recevant l'échelle de l'instrument principal. Chaque axe a désormais sa
  propre échelle.
- **Compositions derrière un proxy SSL** :
  - poids par capitalisation publiés seulement si Yahoo couvre au moins 80 % des titres ; sinon
    composition inchangée (liste tenue à la main) ou enregistrée sans poids, avec le motif (la 1.6.0
    réécrivait la date de composition sans poids quand Yahoo ne répondait pas) ;
  - arrêt dès les 5 premiers titres quand Yahoo est bloqué, motif en une ligne (certificat SSL
    refusé…) au lieu des erreurs yfinance titre par titre ; Yahoo n'est pas redemandé pour les
    indices suivants ;
  - option `--contourner-ssl` sur `constituents refresh | check` et `equities refresh` ;
  - Nasdaq 100 par la liste Wikipedia (le plan FMP gratuit ne la sert pas), S&P 500 : Wikipedia
    en secours du fichier iShares ;
  - fichier iShares : page d'entrée du site sautée (`siteEntryPassthrough`), page reçue à la place
    du CSV enregistrée dans `.cache/equities/debug/` avec son titre.
- Cours des titres : un téléchargement Yahoo refusé pour certificat SSL arrête le chargement avec
  le motif (reprise au lancement suivant).

## 1.6.0 — écran de screening de la séance (27/10/2026)
Market Monitor devient un écran de screening de la séance précédente, au niveau de Koyfin :
vue d'ouverture « Séance d'hier », vue d'ensemble façon Koyfin, fiche instrument, courbes de
taux, top movers par indice (contributions, largeur de marché), valeurs marquantes et PNG des
courbes dans la daily macro (options). Moteur toujours indépendant de Streamlit, sans
dépendance à Bloomberg, clôtures uniquement, contrôle croisé avant diffusion.
Nouvelle dépendance : `exchange_calendars`. Configuration : sections `equities` et
`ui.default_view` de `config.yaml`, `config/constituents/`, options `stocks` et
`curves_chart` de `daily_macro.yaml`.

### Phase 2 : historiques longs
- **Barres OHLC** (`get_bars`) : ouverture, plus haut, plus bas, clôture, volume en un appel
  pour Yahoo, FMP, Stooq et CNBC ; clôture seule pour les autres sources. Cache par champ
  (format inchangé, caches existants valides).
- **Chargement par tranches** de 5 ans (`cache.tranche_days`), de la plus récente à la plus
  ancienne, arrêté où l'historique de la source s'arrête : la période « Max » ne coûte qu'une
  fois.
- **Profondeur par source** : `MarketMonitor.depth`, commande `market-monitor depth`.

### Phase 3 : fiche instrument
- Nouvelle vue **Fiche instrument** : niveau réel d'un instrument sur 1M → Max, chandeliers
  quand l'OHLC est disponible, moyennes mobiles 50 / 200, plus haut et plus bas, second
  instrument superposé ; ouverture par lien `?fiche=ID`.
- Taux souverains : spread contre le Bund de même maturité (nouveau champ `curve` du
  référentiel sur les 22 taux souverains).
- Statistiques : variations 1J → 5A, plus haut / bas 52 semaines, volatilité réalisée 1M / 3M,
  pire mouvement de la période, écart aux moyennes mobiles.
- Tableau des clôtures quotidiennes exportable en CSV et Excel.
- Période longue : la source la plus profonde prend le relais (FMP gratuit limité à 5 ans).

### Phase 4 : vue d'ensemble façon Koyfin
- Tableaux pleine largeur par classe d'actifs : dernier cours, 1J / 1S / 1M / 3M / YTD / 1A
  (nouveaux horizons du moteur), sparkline 1 mois, barre de position dans le range
  52 semaines, écart au plus haut, z-score 1J.
- Sélection d'une ligne : ouverture de la fiche instrument.
- Tableaux avant la heatmap. L'export daily macro garde ses horizons (1J, 1S, MTD, YTD).

### Phase 5 : courbes de taux
- Nouvelle vue **Courbes de taux** (Bund, OAT, BTP, Bonos, UST ; Gilt, JGB) : courbes du jour,
  aujourd'hui contre J-1 / S-1 / M-1, histogramme des variations par maturité, pentes 2s10s /
  5s30s / 2s30s, spreads contre le Bund par maturité, niveaux.
- Points de dates différentes ou de sources mêlées signalés.
- PNG imprimable des courbes (`export/curves_chart.py`), téléchargeable depuis la vue.

### Phase 6 : composition des indices et chargement par lots
- Référentiel de composition `config/constituents/*.yaml` (8 indices) : symbole Yahoo, ISIN,
  nom, secteur, poids, place, devise, date de composition.
- Sources : fichiers des positions iShares (lecture des versions anglaise et allemande),
  liste FMP, listes tenues à la main avec poids estimés par la capitalisation.
- Commandes `constituents list | refresh | import | check` et `equities refresh | status`.
- Stockage des cours des titres (`.cache/equities`), chargement Yahoo par paquets avec pause,
  arrêt sur refus et reprise, détection des historiques réajustés après split, splits notés.
- Calendriers de séances par place (`exchange_calendars`, nouvelle dépendance).
- *Tester la connexion* sonde aussi ishares.com.

### Phase 7 : top movers par indice
- Nouvelle vue **Top movers** : par indice, sur la dernière séance close de sa place, 10
  hausses et 10 baisses (variation, cours, volume / moyenne 20 séances, secteur, contribution
  en points), graphique des contributions, performance moyenne par secteur, largeur de marché
  (hausses / baisses, % au-dessus des MM 50 / 200, plus hauts / bas 52 semaines).
- Contributions : poids de la composition dérivés des cours jusqu'à la veille, comparées à la
  variation réelle de l'indice (écart signalé). Conversion en euros des titres en autre devise.
- Garde-fous : seuil de variation configurable, split signalé ou typique, volume nul → « À
  vérifier » ; titre non coté sur la séance de sa place signalé et non compté ; jour férié
  local ignoré ; contrôle FMP à la demande des titres affichés.
- Stoxx 600 chargé seulement sur bouton. Clic sur un titre : fiche instrument du titre
  (`EQ:<symbole>`, aussi par `?fiche=`), sans toucher aux 84 lignes de la vue d'ensemble.

### Phase 8 : « La séance d'hier »
- Nouvelle vue d'ouverture (`ui.default_view`, `seance` dans la configuration livrée) : bandeau
  des indices, meilleurs et moins bons secteurs Stoxx 600, taux 10 ans / 2s10s / spreads contre
  le Bund, plus forts z-scores, trois hausses et trois baisses par indice, change / matières
  premières / volatilité, alertes.
- Moteur sans Streamlit (`analytics/session_review.py`) ; chaque bloc se dégrade seul (sans
  cours des titres, sans courbes, watchlist réduite).
- Libellés de vues raccourcis (« Fiche », « Courbes ») pour tenir sur une ligne.

### Phase 9 : daily macro
- Option `stocks` de `daily_macro.yaml` : « Valeurs marquantes », trois hausses et trois
  baisses par indice en une phrase (CAC 40, Euro Stoxx 50, S&P 500), feuille Excel dédiée,
  contrôle FMP des titres cités (écart → titre écarté et signalé ; non contrôlé → « À vérifier
  avant diffusion »).
- Option `curves_chart` : PNG imprimable des courbes de taux dans l'export
  (`_courbes.png`), téléchargeable depuis la vue Daily macro.
- Les deux options sont désactivées par défaut : export identique à la 1.5.

### Phase 10 : finitions
- Temps de chargement mesurés (README § 14), test du bouton de rechargement des titres.
- README : présentation, démarrage rapide (compositions, cours des titres), routine du matin,
  limites connues de la v1.6.

## 1.5.11
- Bund bloqué sur la Bundesbank (3,53 au lieu de 3,486) : quand CNBC refusait le chargement de
  ~15 mois (« 503 Backend fetch failed »), le Bund passait sur la source de secours et le cache
  l'y gardait. Désormais :
  - une longue fenêtre refusée par CNBC est redemandée par tranches de 120 jours ;
  - une série servie par une source de secours est redemandée en entier à la chaîne à chaque
    rafraîchissement : la source préférée (CNBC) la reprend dès qu'elle répond.

## 1.5.10
- `diagnostic-taux` reproduit aussi le chargement de la revue pour le Bund et l'OAT : chaîne
  configurée, fenêtre complète (~15 mois, cache compris), source qui a réellement servi,
  avertissements de secours et erreurs ; et une requête CNBC brute sur la même fenêtre.

## 1.5.9 — dates CNBC corrigées, Bund aligné sur Investing
- **Correctif** : CNBC date la séance du lundi au samedi précédent (barres « 03/10 » et
  « 10/10 » = lundis 5 et 12/10/2026). Le cours en séance du lundi passait donc pour la
  dernière clôture, malgré le filtre « clôtures uniquement » (OAT 4,846 %, Bund en séance).
  Une barre datée du week-end est désormais rattachée au lundi suivant ; les séries CNBC déjà
  en cache sont rechargées une fois.
- Bund : CNBC de nouveau en tête (clôture identique à Investing : 3,4857 % le 09/10/2026),
  Bundesbank en secours (maturité constante 10 ans, ~4 pb au-dessus, 2 décimales).

## 1.5.8
- `diagnostic-taux --contourner-ssl` : même test derrière le proxy d'inspection SSL (la
  commande ne passe pas par l'écran de connexion du dashboard).
- Recherche BCE corrigée (les clés du jeu FM ont 7 dimensions : HTTP 400 auparavant),
  fréquences quotidienne (D) et jours ouvrés (B).

## 1.5.7 — diagnostic des sources de taux
- `market-monitor diagnostic-taux` : teste depuis le poste la Bundesbank (Bund 10 ans), CNBC
  (Bund et OAT 10 ans) et la BCE (rendements quotidiens par pays), avec la réponse brute de
  chaque serveur (code HTTP, début du contenu) et ce qui en est lu.
- Messages d'erreur plus précis : statut et message renvoyés par CNBC, première ligne reçue
  de la Bundesbank.
- Bundesbank : une réponse SDMX-ML (XML) est lue aussi, si le serveur ignore la demande de CSV.

## 1.5.6 — Bund sur la courbe officielle de la Bundesbank
- Bund 2, 5, 10 et 30 ans : courbe officielle de la Bundesbank en premier (publiée en fin de
  journée ; quelques pb d'écart possibles avec le générique d'Investing), puis CNBC et Stooq.
  Les spreads OAT-Bund et BTP-Bund mêlent donc deux sources (CNBC pour l'OAT et le BTP).
- Une ligne publiée restée une séance en retard sur la clôture attendue (source qui publie
  tard, jour férié local) est listée dans « À vérifier avant diffusion ».

## 1.5.5 — Euro Stoxx 50 officiel, CNBC plus robuste
- **Euro Stoxx 50 : clôture officielle.** FMP n'est plus utilisé pour cet indice : STOXX le
  calcule jusqu'à 22 h et FMP retenait la valeur de fin de soirée (6 197,10 au lieu de
  6 173,37 le 09/10/2026). Yahoo sert la clôture officielle.
- **CNBC** : appels espacés et nouvel essai après une pause quand CNBC renvoie une réponse
  vide (refus passager qui faisait perdre des échéances OAT / BTP / Bonos) ; après trois
  symboles refusés d'affilée, CNBC est mis de côté par le coupe-circuit ; le message d'erreur
  indique ce que CNBC a renvoyé.

## 1.5.4 — revue du matin : clôtures et chiffres cohérents
- **Correctif** : les graphiques de la daily macro réutilisent les séries du bandeau au lieu
  de les relire. Avant, une seconde lecture (autre fenêtre, autre moment, autre alternative de
  la chaîne) pouvait afficher un autre cours : Euro Stoxx 50 6 222,60 sur le graphique contre
  6 173,37 dans le bandeau, Bund 3,530 % contre 3,486 %.
- **Clôtures uniquement** (`closing_prices` dans `daily_macro.yaml`) : un cours du jour dont
  la séance n'est pas close (ex. OAT en séance le matin) est ignoré dans les bandeaux, les
  graphiques et le contrôle croisé ; la vue indique les lignes concernées.

## 1.5.3
- Graphique VIX sur 3 mois par défaut (les autres restent en YTD).
- OAT 10 ans ajoutée sur le graphique du Bund 10 ans (même axe, en %).

## 1.5.2 — graphiques plus précis
- Graduations de dates adaptées à la fenêtre (lundis sur 2 à 6 mois : un VIX sur 3 mois se
  lit semaine par semaine), communes au dashboard et au PNG.
- Axe vertical plus dense, décimales selon l'amplitude ; plus haut et plus bas de la période
  étiquetés ; réticule et info-bulle date / valeur exactes dans le dashboard.

## 1.5.1 — graphiques façon Investing, courbes d'OAS
- Graphiques de la daily macro présentés comme un historique de cours (Investing) : zone
  remplie, échelle ajustée aux cours, pointillé et étiquette du dernier cours sur l'axe de
  droite, séparateur de milliers ; variation de la séance toujours mise en évidence.
- Graphique crédit : courbes d'OAS High Yield Euro et US (ICE BofA, comme sur FRED) à la
  place de l'écart HY-IG (gardé en variante commentée dans `daily_macro.yaml`).

## 1.5.0 — spreads de crédit et graphiques de la daily macro
- **Spreads HY-IG** Euro et US (OAS cash, en pb) et **G spread Euro IG / Bund 5 ans** dans le
  bandeau Spreads. OAS ICE BofA via FRED en gratuit (US HY, US IG, Euro HY) ; Euro IG et
  rendement Euro IG : Bloomberg, sinon saisie manuelle (aucune source gratuite fiable).
- Nouvelle source `fredapi:` (API officielle FRED, clé gratuite `FRED_API_KEY`) en secours de
  `fred:` quand `fred.stlouisfed.org` est bloqué ; sondée par *Tester la connexion*.
- **Graphiques de la daily macro** (section `charts`) : Euro Stoxx 50, S&P 500, Bund 10 ans,
  HY-IG, Brent en YTD, VIX sur 6 mois ; dernier niveau et variation de la séance mis en
  évidence ; période modifiable par graphique dans le dashboard (ou date de début libre) ;
  PNG des graphiques téléchargeable et écrit par `market-monitor daily-macro`.
- Saisie manuelle : propose les jambes des spreads publiés quand elles n'ont pas d'autre
  source ; formulaire en grille de quatre.

## 1.4.0 — format de la revue, MSCI EM officiel, iTraxx
- **Daily macro au format de la revue** : bandeaux Indices (Euro Stoxx 50, CAC 40, S&P 500,
  Nasdaq 100, Nikkei 225, CSI 300), Taux 10 ans par pays (États-Unis, Allemagne, France, Italie,
  Royaume-Uni, Japon), Marchés clés (Brent, EUR/USD, VIX, MSCI EM, Or, Bitcoin) et nouveau
  bandeau Spreads (OAT-Bund, iTraxx Main, iTraxx Crossover) ; DAX, FTSE MIB et BTP-Bund retirés
  des bandeaux (toujours dans le dashboard). Variation 1J seule.
- **Options d'affichage** dans `daily_macro.yaml` : `format` (séparateur de milliers, unités
  collées, « bps »/« bp ») et, par ligne ou par bandeau, `label`, `decimals`, `suffix`,
  `change` (VIX en %), `change_decimals` ; appliquées au texte, au PNG et à l'Excel. Par défaut,
  rendu inchangé.
- **MSCI EM** : niveau officiel MSCI (`msci:891800`, ~1 742) au lieu de l'ETF EEM ; secours
  sur le future ICE `MME=F`, signalé comme proxy seulement quand il sert (proxy par alternative
  d'une chaîne). Nouvelle source `msci:` (indices MSCI, variantes prix / net / brut).
- Nouveaux instruments : CSI 300, Gilt 10 ans et JGB 10 ans (CNBC puis Stooq hors Bloomberg).
- **Saisie manuelle** (provider `manual`, fichier `data/manual_quotes.csv`) pour l'iTraxx sans
  Bloomberg : encart dans la vue *Daily macro*, commandes `market-monitor quote set | list |
  import | delete`, import d'un historique Excel ; Bloomberg reste prioritaire.
- PNG : largeur de tuile identique dans tous les bandeaux, hauteur ajustée au nombre de colonnes.
- Correctif de test : le script de page réelle ne remplace plus `MarketMonitor.from_settings`
  pour les tests suivants.
- 299 tests (19 nouveaux), 95 % de couverture.

## 1.3.0 — fiabilité de la revue publiée
- **Cache : une série, une source.** Une chaîne de secours `a|b` ne recolle plus deux sources :
  le cache mémorise l'alternative qui a servi et recharge toute la fenêtre quand une autre
  répond (avant : faux mouvement de plusieurs pb à la jointure, sans avertissement). La source
  préférée reprend la main dès qu'elle revient ; les caches v1.2 des chaînes sont reconstruits
  une fois. L'avertissement « source de secours » n'est plus perdu par le cache.
- **Mouvements suspects** : un 1J à plus de 8 écarts-types (`quality.suspect_abs_z`) est traité
  comme une donnée à vérifier, pas comme un mouvement.
- **Contrôle croisé** des lignes publiées et des candidats aux mouvements marquants avec une
  seconde source (autre provider ou autre alternative de la chaîne) : écart de niveau ou de 1J
  → ligne suspecte ; un spread hérite d'une jambe suspecte.
- **Changements de contrat** Brent, WTI et TTF (champ `roll` du référentiel, calendriers ICE /
  NYMEX / ICE Endex) : 1J et 1S signalés, jours de roll exclus de la volatilité.
- **Courbe US via CNBC** en tête de chaîne (`cnbc:US2Y|fred:DGS2|2YY=F`, `cnbc:US10Y|^TNX`…) :
  une seule source et une seule date pour les pentes 2s10s et 5s30s.
- **Jambes décalées** : un spread ou une pente dont une jambe est en retard le signale.
- Lignes suspectes ou en changement de contrat (†) : exclues des mouvements marquants,
  neutres dans la heatmap, sans alerte de marché ; nouvelle règle d'alerte `suspect`.
- Export : « à vérifier » dans le texte, † et 1J en ambre sur le PNG, ligne surlignée et
  commentée dans l'Excel, colonnes *Source effective*, *Contrôle 2e source*, *Suspect*,
  *Roll 1J* et *À vérifier* dans *Données* ; section « À vérifier avant diffusion » complète.
- Dashboard : † et 1J en ambre dans les tableaux, rubrique « À vérifier avant diffusion » dans
  *Qualité des données*, mouvements marquants contrôlés comme dans l'export.
- 280 tests (29 nouveaux), dont un scénario de bout en bout sur le référentiel complet.

## 1.2.3 — lignes Bloomberg seules masquées
- Un instrument qu'aucun provider actif ne sert (iTraxx sans Bloomberg) est retiré des
  watchlists, de l'historique, des corrélations, de l'export daily macro et des alertes ; il
  réapparaît automatiquement avec Bloomberg.

## 1.2.2 — taux souverains via CNBC
- Nouvelle source `cnbc:` (barres quotidiennes CNBC) en tête des chaînes Bund / OAT / BTP /
  Bonos : une seule source pour les quatre pays, spreads cohérents.
- Page anti-robot de Stooq reconnue : Stooq est ignoré par le coupe-circuit au lieu d'être
  interrogé pour chaque ticker.
- *Tester la connexion* sonde aussi CNBC.

## 1.2.1 — réseau d'entreprise filtré
- Chaînes de secours `a|b` dans le provider `free` : Bund Stooq → Bundesbank, UST 2 ans
  FRED → future `2YY=F` ; le repli est signalé en avertissement.
- Coupe-circuit : une source injoignable est ignorée 10 minutes (un seul délai d'attente au
  lieu d'un par ticker) ; une seule nouvelle tentative pour les sources publiques.
- Le dashboard recharge le référentiel quand un fichier YAML ou la version change, sans
  redémarrer Streamlit ; version affichée dans la barre latérale.
- *Tester la connexion* : sondes en parallèle, Bundesbank et STOXX ajoutés, 401 = joignable
  (clé requise), page de blocage du proxy détectée, code HTTP affiché.
- `market-monitor ecb-series FLOW MOTIF` : recherche de séries BCE.

## 1.2.0 — sources gratuites pour les taux
- Nouvelles sources sans clé dans le provider `free` : Stooq (`stooq:`), Bundesbank
  (`bbk:`), FRED (`fred:`), STOXX (`stoxx:`), avec la même politique SSL que les autres.
- Référentiel : rendements Bund / OAT / BTP / Bonos 2-5-10-30 ans via Stooq (spreads et
  pentes de nouveau calculés sans Bloomberg), UST 2 ans via FRED, VSTOXX via STOXX,
  immobilier Stoxx 600 via l'ETF iShares (proxy).
- *Tester la connexion* sonde aussi Stooq et FRED.

## 1.1.0 — connexion réseau et interface
- Écran *Connexion réseau* au lancement du dashboard : aucune donnée n'est chargée avant
  *Lancer le chargement* ; modes standard / certificat d'entreprise / contournement SSL,
  bouton *Tester la connexion* (FMP, BCE, Yahoo). Désactivable par `ui.network_gate: false`.
- Barre latérale : mode de connexion actif et bouton *Changer la connexion* ; bandeau
  *Contourner le SSL* quand la plupart des séries échouent sur le certificat.
- Interface retravaillée : barre de titre avec puces d'état, tuiles d'en-tête, mouvements
  marquants en cartes, titres de section soulignés, tableaux encadrés, chrome Streamlit allégé.
- `.streamlit/config.toml`, `.gitignore` et `.env.example` versionnés.

## 1.0.1 — proxy d'entreprise
- Contournement des proxys d'inspection SSL d'entreprise (`market_monitor.network`) : section
  `network` de `config.yaml` (`ca_bundle`, `insecure_ssl`) et variables
  `MARKET_MONITOR_CA_BUNDLE` / `MARKET_MONITOR_INSECURE_SSL`, appliquées à FMP, à la BCE et à
  Yahoo (session `requests` imposée à yfinance à la place de `curl_cffi`).
- `doctor` : ligne « Réseau » (configuration, variables de proxy) et, avec `--online`, sonde de
  connectivité qui qualifie la panne (certificat, proxy, DNS, délai, filtrage).

## 1.0.0 — phase 7 : tests, documentation, finitions
- `market-monitor doctor [--online]` : diagnostic de l'installation (fichiers, cache, providers).
- Journal tournant `logs/market_monitor.log` (1 Mo × 5), idempotent sous Streamlit.
- Ligne de commande réorganisée (table de commandes, codes retour documentés), sortie UTF-8
  forcée pour les redirections sous Windows.
- Scripts Windows : `install`, `doctor`, `dashboard`, `daily_macro` (tâche planifiée), `check`.
- Intégration continue Windows + Linux, Python 3.11 / 3.12 (ruff, mypy, pytest).
- Tests de bout en bout sur la configuration complète du dépôt (73 instruments, données
  synthétiques) : moteur, export, alertes, corrélations, ligne de commande, `main()` Streamlit.
- README consolidé : démarrage rapide, routine du matin, intégration au terminal, dépannage,
  limites et évolutions.
- Exports paresseux `from market_monitor import MarketMonitor, load_settings`.

## 0.6.0 — phase 6 : alertes
- Règles YAML `zscore`, `level` (avec franchissement), `change`, `stale` ; gravités et escalade.
- Bandeau d'alertes, vue Alertes, bloc interne dans le texte et feuille Excel de la daily macro.
- `market-monitor alerts --json --fail-on` pour les tâches planifiées.

## 0.5.0 — phase 5 : export daily macro
- Excel auditable (variations en formules sur la feuille *Données*), PNG des bandeaux,
  texte en langage de marché, section « À vérifier avant diffusion ».
- Vue Daily macro avec téléchargements ; `market-monitor daily-macro`.

## 0.4.0 — phase 4 : historiques et corrélations
- Comparaison base 100 / variation cumulée en pb / points, statistiques de période.
- Corrélations quotidiennes ou hebdomadaires, variation sur un mois, clustering, paire glissante.

## 0.3.0 — phase 3 : dashboard
- Vue d'ensemble : mouvements marquants, heatmap de z-scores, tableaux par classe d'actifs,
  qualité des données ; page réutilisable dans le terminal.

## 0.2.0 — phase 2 : référentiel et performances
- Référentiel YAML (73 instruments), dérivés (spreads, pentes), watchlists.
- Variations 1J / 1S / MTD / YTD en % ou pb, z-scores hors échantillon, données périmées.

## 0.1.0 — phase 1 : couche de données
- Interface `DataProvider`, providers Bloomberg (blpapi), FMP, gratuit (BCE + yfinance).
- Cache parquet horodaté, fallback par instrument, configuration YAML + `.env`.
