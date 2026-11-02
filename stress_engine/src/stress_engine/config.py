"""Chargement des fichiers de configuration YAML du moteur (taxonomie, EUREX, proxys de fonds)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class StressConfigError(ValueError):
    """Configuration du moteur invalide."""


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise StressConfigError(f"fichier de configuration introuvable : {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise StressConfigError(f"{p} : un dictionnaire YAML est attendu")
    return data


@dataclass(frozen=True)
class EngineConfig:
    """Les trois fichiers de configuration, déjà lus."""

    factors: Mapping[str, Any]
    eurex: Mapping[str, Any]
    funds: Mapping[str, Any]
    directory: Path = field(default=CONFIG_DIR)

    @classmethod
    def load(cls, directory: str | Path | None = None) -> EngineConfig:
        d = Path(directory) if directory is not None else CONFIG_DIR
        return cls(factors=load_yaml(d / "risk_factors.yaml"), eurex=load_yaml(d / "eurex.yaml"),
                   funds=load_yaml(d / "fund_proxies.yaml"), directory=d)
