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
| `stress_engine/` | Moteur de stress tests, scénarios et VaR : facteurs de risque, projection des positions (phase 2) |
| `docs/stress_engine/` | Conception et plan du moteur de stress |

Installation : `core` d'abord (session réseau partagée), puis le projet voulu, depuis son dossier :

```bash
pip install -e core
cd rates_bond_analytics && pip install -e ".[dev]"
cd ../stress_engine && pip install -e ../rates_bond_analytics -e ".[dev]"
```

Réseau d'entreprise : toutes les requêtes passent par `core/terminal_core/network.py`
(proxy, magasin de certificats du poste, certificat d'entreprise ; jamais `verify=False`).
Diagnostic : `check-network`.
