"""Système de design : deux palettes, un seul vocabulaire.

Parti pris
----------
Mode **sombre par défaut** : c'est le mode de travail d'une salle de marché, et
sur un écran regardé huit heures par jour il réduit la fatigue. Le fond n'est
pas noir pur mais un bleu-ardoise profond (`#0F1319`) — le noir absolu écrase
les contrastes et fait vibrer le texte fin. Un mode clair complet reste
disponible pour l'impression et les projections en salle de réunion.

Couleurs *catégorielles* vs *séquentielles*
-------------------------------------------
Distinction structurante, et la raison pour laquelle les graphiques se lisaient
mal avant : un dégradé de bleu (séquentiel) sert à représenter une **grandeur
ordonnée** (une intensité, une durée). Il est inutilisable pour distinguer des
**catégories** sans ordre naturel — onze secteurs, quinze pays. Chaque famille
a donc sa palette :

* `categorical` — dix teintes de tons distincts, écartées en luminance pour
  rester discernables même en niveaux de gris ;
* `sequential` — un dégradé, pour les intensités ;
* `diverging` — rouge/bleu centré sur zéro, pour les impacts signés ;
* `asset_classes` / `quality` — couleurs **sémantiques** fixes : une classe
  d'actif garde sa teinte d'un écran à l'autre, sinon la mémoire visuelle du
  gérant ne sert à rien.

Les couleurs de signe (vert/rouge) ne sont jamais réutilisées pour une
catégorie : dans cette application, le rouge signifie une perte, rien d'autre.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MODE = "dark"

FONT_STACK = "'IBM Plex Sans', 'Segoe UI', system-ui, sans-serif"
MONO_STACK = "'IBM Plex Mono', 'SFMono-Regular', Consolas, monospace"


@dataclass(frozen=True)
class Palette:
    name: str
    ink: str            # texte principal
    ink_soft: str       # texte secondaire
    paper: str          # fond application
    surface: str        # cartes et graphiques
    surface_alt: str    # survol, lignes alternées
    rule: str           # filets
    grid: str           # grilles de graphique
    accent: str         # accent de marque, unique
    accent_soft: str
    positive: str
    negative: str
    warning: str
    neutral: str
    categorical: tuple[str, ...]
    sequential: tuple[str, ...]
    diverging: tuple[str, ...]
    asset_classes: dict[str, str] = field(default_factory=dict)
    quality: dict[str, str] = field(default_factory=dict)

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"

    def category(self, index: int) -> str:
        return self.categorical[index % len(self.categorical)]


# --------------------------------------------------------------------- #
# Mode sombre
# --------------------------------------------------------------------- #
DARK = Palette(
    name="dark",
    ink="#E9EEF4",
    ink_soft="#94A3B4",
    paper="#0F1319",
    surface="#161C24",
    surface_alt="#1D2530",
    rule="#2A333F",
    grid="#232C37",
    accent="#4F9BD8",
    accent_soft="#1E3245",
    positive="#35AE83",
    negative="#E2596C",
    warning="#DCA53F",
    neutral="#7C8A9A",
    categorical=(
        "#4F9BD8",  # bleu
        "#E58A5B",  # orange
        "#45B8C4",  # turquoise
        "#A88BD6",  # violet
        "#D9A13B",  # ambre
        "#7FB84F",  # olive
        "#C9749B",  # rose
        "#5C7FA8",  # bleu ardoise
        "#B5894F",  # brun
        "#8FA3B8",  # gris bleuté
    ),
    sequential=("#1A2733", "#22415A", "#2B5C80", "#3578A6", "#4F9BD8", "#83BCE6"),
    diverging=("#E2596C", "#B4535F", "#5E4650", "#2F4453", "#3578A6", "#4F9BD8"),
    asset_classes={
        "EQUITY": "#4F9BD8",
        "GOV_BOND": "#45B8C4",
        "CREDIT": "#A88BD6",
        "CASH": "#7C8A9A",
        "FX": "#D9A13B",
        "COMMODITY": "#E58A5B",
        "FUND": "#7FB84F",
        "OTHER": "#5C7FA8",
    },
    quality={
        "VERIFIED": "#35AE83",
        "API_CONFIRMED": "#4F9BD8",
        "CACHED": "#DCA53F",
        "ESTIMATED": "#E58A5B",
        "UNVERIFIED": "#E2596C",
    },
)

# --------------------------------------------------------------------- #
# Mode clair
# --------------------------------------------------------------------- #
LIGHT = Palette(
    name="light",
    ink="#131A22",
    ink_soft="#5A6672",
    paper="#F6F7F9",
    surface="#FFFFFF",
    surface_alt="#F1F4F7",
    rule="#DCE1E7",
    grid="#E6EAEF",
    accent="#1F5C8B",
    accent_soft="#E3EDF5",
    positive="#1B7A5A",
    negative="#B23A48",
    warning="#B8802B",
    neutral="#8A94A0",
    categorical=(
        "#1F5C8B",
        "#C2652F",
        "#1E8C99",
        "#7A5CB0",
        "#B07A1E",
        "#5C9130",
        "#A64C74",
        "#41638A",
        "#8A6134",
        "#5A6E85",
    ),
    sequential=("#E3EDF5", "#B9D2E4", "#8AB4D2", "#5B95BE", "#2F72A3", "#1F5C8B"),
    diverging=("#B23A48", "#D08A91", "#EDE3E4", "#DDE7EE", "#7BA7C4", "#1F5C8B"),
    asset_classes={
        "EQUITY": "#1F5C8B",
        "GOV_BOND": "#1E8C99",
        "CREDIT": "#7A5CB0",
        "CASH": "#8A94A0",
        "FX": "#B07A1E",
        "COMMODITY": "#C2652F",
        "FUND": "#5C9130",
        "OTHER": "#41638A",
    },
    quality={
        "VERIFIED": "#1B7A5A",
        "API_CONFIRMED": "#1F5C8B",
        "CACHED": "#B8802B",
        "ESTIMATED": "#A2681F",
        "UNVERIFIED": "#B23A48",
    },
)

PALETTES: dict[str, Palette] = {"dark": DARK, "light": LIGHT}


# --------------------------------------------------------------------- #
# Accès au mode courant
# --------------------------------------------------------------------- #
def active_mode() -> str:
    """Mode courant, lu dans l'état de session Streamlit s'il existe.

    La fonction reste utilisable hors Streamlit (tests, batch) : elle retombe
    alors silencieusement sur le mode par défaut.
    """
    try:
        import streamlit as st

        return st.session_state.get("pi_theme", DEFAULT_MODE)
    except Exception:  # pragma: no cover - hors contexte Streamlit
        return DEFAULT_MODE


def palette(mode: str | None = None) -> Palette:
    return PALETTES.get(mode or active_mode(), PALETTES[DEFAULT_MODE])


def asset_class_colors(mode: str | None = None) -> dict[str, str]:
    return palette(mode).asset_classes


def quality_colors(mode: str | None = None) -> dict[str, str]:
    return palette(mode).quality


def categorical_colors(keys, mode: str | None = None) -> list[str]:
    """Couleurs de catégories, stables pour un même jeu de clés.

    Les classes d'actif conservent leur couleur sémantique ; les autres clés
    reçoivent une teinte de la palette catégorielle, attribuée par ordre
    d'apparition — deux graphiques triés pareil donnent donc les mêmes couleurs.
    """
    pal = palette(mode)
    colors: list[str] = []
    cursor = 0
    for key in keys:
        semantic = pal.asset_classes.get(str(key).upper())
        if semantic:
            colors.append(semantic)
        else:
            colors.append(pal.category(cursor))
            cursor += 1
    return colors


# --------------------------------------------------------------------- #
# Plotly
# --------------------------------------------------------------------- #
def plotly_template(mode: str | None = None) -> dict:
    p = palette(mode)
    return {
        "layout": {
            "font": {"family": FONT_STACK, "size": 12, "color": p.ink},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "colorway": list(p.categorical),
            "margin": {"l": 56, "r": 24, "t": 36, "b": 40},
            "hoverlabel": {
                "font": {"family": MONO_STACK, "size": 12, "color": p.ink},
                "bgcolor": p.surface_alt,
                "bordercolor": p.rule,
            },
            "xaxis": {"gridcolor": p.grid, "zerolinecolor": p.rule, "linecolor": p.rule,
                      "tickfont": {"family": MONO_STACK, "size": 11, "color": p.ink_soft}},
            "yaxis": {"gridcolor": p.grid, "zerolinecolor": p.neutral, "linecolor": p.rule,
                      "tickfont": {"family": MONO_STACK, "size": 11, "color": p.ink_soft}},
            "legend": {"orientation": "h", "y": -0.2, "font": {"size": 11, "color": p.ink_soft}},
            "title": {"font": {"size": 13.5, "color": p.ink}, "x": 0, "xanchor": "left"},
        }
    }


# --------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------- #
def css(mode: str | None = None) -> str:
    """Feuille de style injectée dans l'application.

    Les surcharges `!important` sur les conteneurs Streamlit sont nécessaires
    pour que le fond suive notre palette : le thème natif est défini dans
    `.streamlit/config.toml` et n'est pas modifiable à chaud.
    """
    p = palette(mode)
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

:root {{
  --pi-ink: {p.ink}; --pi-ink-soft: {p.ink_soft}; --pi-paper: {p.paper};
  --pi-surface: {p.surface}; --pi-surface-alt: {p.surface_alt};
  --pi-rule: {p.rule}; --pi-accent: {p.accent}; --pi-accent-soft: {p.accent_soft};
  --pi-pos: {p.positive}; --pi-neg: {p.negative}; --pi-warn: {p.warning};
}}

html, body, [class*="css"] {{ font-family: {FONT_STACK}; }}
.stApp, [data-testid="stAppViewContainer"] {{ background: {p.paper} !important; color: {p.ink}; }}
[data-testid="stHeader"] {{ background: {p.paper} !important; }}
section[data-testid="stSidebar"] {{
  background: {p.surface} !important; border-right: 1px solid {p.rule};
}}
section[data-testid="stSidebar"] * {{ color: {p.ink}; }}
h1, h2, h3, h4, h5, p, li, label, span, div[data-testid="stMarkdownContainer"] {{ color: {p.ink}; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {{ color: {p.ink_soft} !important; }}
hr {{ border-color: {p.rule}; }}

/* En-tête de page */
.pi-eyebrow {{
  font-family: {MONO_STACK}; font-size: 11px; letter-spacing: .14em;
  text-transform: uppercase; color: {p.accent}; margin-bottom: 2px;
}}
.pi-title {{ font-size: 26px; font-weight: 600; margin: 0 0 4px 0; color: {p.ink}; }}
.pi-sub {{ color: {p.ink_soft}; font-size: 13px; margin-bottom: 12px; }}
.pi-rule {{ border-top: 1px solid {p.rule}; margin: 8px 0 18px 0; }}

/* Bande d'identité */
.pi-band {{
  display: flex; align-items: stretch; gap: 0; background: {p.surface};
  border: 1px solid {p.rule}; border-radius: 5px; border-left: 3px solid {p.accent};
  margin-bottom: 16px; overflow: hidden;
}}
.pi-band .b-cell {{ padding: 11px 20px; border-right: 1px solid {p.rule}; }}
.pi-band .b-cell:last-child {{ border-right: none; }}
.pi-band .b-name {{ font-size: 15px; font-weight: 600; line-height: 1.3; color: {p.ink}; }}
.pi-band .b-meta {{
  font-family: {MONO_STACK}; font-size: 10.5px; letter-spacing: .07em;
  text-transform: uppercase; color: {p.ink_soft}; margin-top: 3px;
}}
.pi-band .b-label {{
  font-family: {MONO_STACK}; font-size: 10px; letter-spacing: .10em;
  text-transform: uppercase; color: {p.ink_soft};
}}
.pi-band .b-value {{
  font-family: {MONO_STACK}; font-variant-numeric: tabular-nums;
  font-size: 19px; font-weight: 600; line-height: 1.35; color: {p.ink};
}}

/* Cartes KPI */
.pi-kpi {{
  background: {p.surface}; border: 1px solid {p.rule}; border-radius: 5px;
  padding: 12px 14px 10px 14px; height: 100%;
}}
.pi-kpi .k-label {{
  font-family: {MONO_STACK}; font-size: 10.5px; letter-spacing: .10em;
  text-transform: uppercase; color: {p.ink_soft};
}}
.pi-kpi .k-value {{
  font-family: {MONO_STACK}; font-variant-numeric: tabular-nums;
  font-size: 23px; font-weight: 600; line-height: 1.25; margin-top: 4px; color: {p.ink};
}}
.pi-kpi .k-delta {{ font-family: {MONO_STACK}; font-size: 12px; }}
.pi-kpi .k-foot {{ font-size: 10.5px; color: {p.ink_soft}; margin-top: 6px; }}

.pos, .pi-kpi .k-value.pos {{ color: {p.positive} !important; }}
.neg, .pi-kpi .k-value.neg {{ color: {p.negative} !important; }}
.mut {{ color: {p.ink_soft} !important; }}

/* Pastille de qualité */
.pi-badge {{
  display: inline-block; font-family: {MONO_STACK}; font-size: 9.5px;
  letter-spacing: .08em; padding: 1px 6px; border-radius: 3px;
  border: 1px solid currentColor; text-transform: uppercase;
}}

/* Titre de bloc */
.pi-block {{ display: flex; align-items: baseline; gap: 12px; margin: 6px 0 10px 0; }}
.pi-block .s-title {{ font-size: 14px; font-weight: 600; white-space: nowrap; color: {p.ink}; }}
.pi-block .s-line {{ flex: 1; border-top: 1px solid {p.rule}; }}
.pi-block .s-note {{ font-size: 11px; color: {p.ink_soft}; white-space: nowrap; }}

/* Rail de risque */
.pi-rail {{ display: flex; align-items: center; gap: 10px; padding: 5px 0; }}
.pi-rail .r-name {{ flex: 0 0 240px; font-size: 12.5px; color: {p.ink}; }}
.pi-rail .r-bar {{
  flex: 1; height: 8px; background: {p.surface_alt}; border-radius: 3px; position: relative;
}}
.pi-rail .r-fill {{ position: absolute; top: 0; left: 0; height: 8px; border-radius: 3px; background: {p.negative}; }}
.pi-rail .r-val {{
  flex: 0 0 84px; text-align: right; font-family: {MONO_STACK};
  font-variant-numeric: tabular-nums; font-size: 12.5px; font-weight: 500; color: {p.ink};
}}

/* Légende d'allocation */
.pi-legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 8px; }}
.pi-legend .l-item {{ display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: {p.ink}; }}
.pi-legend .l-dot {{ width: 9px; height: 9px; border-radius: 2px; }}
.pi-legend .l-val {{
  font-family: {MONO_STACK}; font-variant-numeric: tabular-nums; color: {p.ink_soft};
}}

/* Tableaux et onglets */
div[data-testid="stDataFrame"] {{ font-family: {MONO_STACK}; font-variant-numeric: tabular-nums; }}
.stTabs [data-baseweb="tab"] {{ font-size: 13px; color: {p.ink_soft}; }}
.stTabs [aria-selected="true"] {{ color: {p.accent} !important; }}
[data-testid="stExpander"] details {{
  background: {p.surface}; border: 1px solid {p.rule}; border-radius: 5px;
}}
</style>
"""
