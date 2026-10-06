# Historique des versions

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
