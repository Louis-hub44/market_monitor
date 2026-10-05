# Historique des versions

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
