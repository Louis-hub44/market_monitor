# Terminal — monorepo

Mini terminal de marché maison, sans Bloomberg. Chaque projet est autonome : il a son propre
`pyproject.toml` (ou `requirements.txt`), ses tests, sa documentation et son workflow de CI
(`.github/workflows/<projet>.yml`, déclenché uniquement par les changements de son dossier).

| Dossier | Rôle |
| --- | --- |
| `core/` | Socle partagé : session HTTP unique derrière le proxy d'entreprise, `check-network` |
| `market_monitor/` | Écran d'accueil multi-actifs, séance de la veille, daily macro (v1.6.6) |
| `rates_bond_analytics/` | Courbes €STR / EURIBOR / souveraines, analytics obligataires YAS, swaps, scénarios de courbe (v1.1) |
| `portfolio-intelligence/` | Plateforme d'analyse de portefeuilles Equity / Fixed Income / Diversifié (V7) |
| `docs/stress_engine/` | Conception du moteur de stress tests et de scénarios (en cours) |

Installation d'un projet : se placer dans son dossier et suivre son README
(par exemple `cd rates_bond_analytics && pip install -e ".[dev]"`).
