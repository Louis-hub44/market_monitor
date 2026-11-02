# terminal-core

Socle partagé du terminal. Il contient une seule chose : **la session HTTP unique**, utilisée
derrière un proxy d'inspection SSL d'entreprise. Tous les projets (Rates & Bond Analytics,
Portfolio Intelligence, moteur de stress) passent par elle.

```bash
pip install -e core            # avant les autres projets
check-network                  # ou : python core/scripts/check_network.py
```

## Politique

| Sujet | Règle |
| --- | --- |
| Proxy | `TERMINAL_HTTPS_PROXY`, `TERMINAL_HTTP_PROXY`, `TERMINAL_NO_PROXY` (ou bloc `network` d'une configuration). À défaut, les variables standard `HTTPS_PROXY` / `NO_PROXY`. Les identifiants d'un proxy authentifié sont masqués dans tous les affichages. |
| Confiance TLS (`TERMINAL_TRUST=auto`) | 1. **truststore** : magasin de certificats du poste, où l'informatique installe la racine du proxy. Un CA d'entreprise fourni y est **ajouté**. 2. Sinon le **bundle CA de l'entreprise**, fusionné avec certifi. 3. Sinon **certifi**. Modes forcés : `system`, `ca`, `certifi`. |
| CA d'entreprise | `TERMINAL_CA_BUNDLE`, puis les variables historiques `PI_CA_BUNDLE`, `RATES_CA_BUNDLE`, `MARKET_MONITOR_CA_BUNDLE`, puis `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`. |
| `verify=False` | **Impossible.** `SecureSession` lève `InsecureTLSError` sur `session.verify = False` comme sur `get(..., verify=False)`. `TERMINAL_TRUST=insecure` est refusé. Les anciens contournements (`PI_INSECURE_SSL`, `MARKET_MONITOR_INSECURE_SSL`, `RATES_NETWORK_MODE=insecure`) sont ignorés, avec un avertissement. |
| Délai | `TERMINAL_TIMEOUT_S` (20 s par défaut), appliqué quand l'appelant n'en donne pas. |
| yfinance | `apply_to_yfinance(get_session())` impose la session au singleton `YfData`. |

```python
from terminal_core import get_session

session = get_session()          # partagée dans le processus, réglages lus dans l'environnement
resp = session.get("https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT")
```

## Diagnostic

`check-network` affiche la configuration effective (confiance, proxy, certificats trouvés sur
le poste), puis sonde ECB, Bundesbank, Stooq, FRED (sources critiques du moteur de stress),
CNBC, FMP et Yahoo. Chaque échec est qualifié, avec le correctif associé :

| Cause | Lecture |
| --- | --- |
| `ssl` | certificat refusé : proxy d'inspection, voir truststore ou `TERMINAL_CA_BUNDLE` |
| `proxy` | proxy injoignable ou non renseigné |
| `proxy_auth` | HTTP 407, identifiants du proxy requis |
| `blocked` | hôte filtré : page de blocage, tunnel refusé (403) ou HTTP 403/429 |
| `dns`, `timeout` | réseau coupé, pare-feu |
| `http` | la source répond en erreur 5xx : panne du fournisseur |

Le verdict distingue une panne de **poste**, où toutes les sources échouent pour la même cause,
d'une panne de **source**, où certaines répondent. Codes retour : 0 si les sources critiques
répondent, 1 si au moins une est en échec, 2 si la configuration est invalide. Option `--json`
pour une tâche planifiée.

`terminal_core.guards.insecure_tls_uses(paths)` sert de garde-fou dans les suites de tests des
projets : il détecte `verify=False`, `x.verify = False` et `ssl._create_unverified_context`.
