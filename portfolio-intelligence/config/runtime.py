"""Configuration modifiable à chaud depuis l'interface.

`config/settings.py` lit l'environnement au démarrage : c'est le socle, figé
pour la durée du processus. Ce module apporte la couche au-dessus — ce que
l'utilisateur peut changer sans redémarrer ni éditer un fichier :

* la clé API FMP ;
* la politique SSL (certificat d'entreprise, contournement).

Priorité : **override d'interface > environnement (.env) > défaut**.

Persistance
-----------
Les réglages sont écrits dans `.local_config.json` à la racine, fichier ignoré
par git et créé en permissions restreintes (0600) là où le système le permet.

⚠️ La clé API y est stockée **en clair**, exactement comme dans `.env`. C'est
acceptable sur un poste personnel ou un conteneur dédié ; ça ne l'est pas sur
un poste partagé ou un répertoire réseau. Dans ce cas, laisser la persistance
désactivée : la clé ne vit alors qu'en mémoire, le temps de la session.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG_PATH = Path(os.getenv("LOCAL_CONFIG_FILE", ROOT / ".local_config.json"))

#: Clés autorisées dans le fichier local — tout le reste est ignoré.
ALLOWED_KEYS = ("fmp_api_key", "insecure_ssl", "ca_bundle")


def load_local_config() -> dict[str, Any]:
    """Réglages persistés, ou dictionnaire vide si aucun/illisible."""
    if not LOCAL_CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Configuration locale illisible, ignorée : %s", exc)
        return {}
    return {k: v for k, v in raw.items() if k in ALLOWED_KEYS}


def save_local_config(**values: Any) -> Path:
    """Met à jour le fichier local (fusion, pas remplacement)."""
    current = load_local_config()
    current.update({k: v for k, v in values.items() if k in ALLOWED_KEYS})
    LOCAL_CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    try:
        LOCAL_CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:  # pragma: no cover - Windows / systèmes de fichiers exotiques
        pass
    return LOCAL_CONFIG_PATH


def clear_local_config() -> bool:
    """Supprime les réglages persistés (retour à l'environnement seul)."""
    try:
        LOCAL_CONFIG_PATH.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover
        log.warning("Suppression impossible : %s", exc)
        return False


def is_persisted() -> bool:
    return LOCAL_CONFIG_PATH.exists()


def mask_key(key: str | None) -> str:
    """Représentation affichable d'une clé — jamais la clé entière."""
    if not key:
        return "non configurée"
    key = key.strip()
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-4:]}"
