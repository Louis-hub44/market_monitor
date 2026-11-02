"""Test de fumée du point d'entrée.

Motivation : un `streamlit run` qui répond HTTP 200 ne prouve rien — le script
s'exécute côté session, et une erreur d'API (icône invalide, argument obsolète)
n'apparaît que dans le navigateur. Ce test exécute réellement `app.py`.

Il aurait attrapé la régression `icon="◧"` : Streamlit n'accepte qu'un emoji ou
une icône Material `:material/nom:`, pas un caractère géométrique Unicode.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ["OFFLINE_MODE"] = "true"

import pytest  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_app_entrypoint_runs_without_exception():
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
    app.run()
    assert not app.exception, f"app.py a levé : {app.exception}"


def test_page_icons_are_valid_streamlit_icons():
    """Garde-fou : toute icône doit être un emoji ou `:material/...:`."""
    from streamlit.string_util import validate_icon_or_emoji

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    icons = [
        line.split('icon="')[1].split('"')[0]
        for line in source.splitlines()
        if 'icon="' in line
    ]
    assert icons, "aucune icône trouvée — le test ne vérifie plus rien"
    for icon in icons:
        validate_icon_or_emoji(icon)      # lève StreamlitAPIException si invalide


def test_page_config_icon_is_valid():
    from streamlit.string_util import validate_icon_or_emoji

    source = (ROOT / "app.py").read_text(encoding="utf-8")
    line = next(l for l in source.splitlines() if "page_icon=" in l)
    icon = line.split('page_icon="')[1].split('"')[0]
    validate_icon_or_emoji(icon)


def test_navigation_declares_all_views():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    for view in ("dashboard", "portfolio_view", "equity", "fixed_income",
                 "diversified", "events", "data_quality"):
        assert f"views/{view}.py" in source
        assert (ROOT / "views" / f"{view}.py").exists()
