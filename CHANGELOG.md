# Historique des versions

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
