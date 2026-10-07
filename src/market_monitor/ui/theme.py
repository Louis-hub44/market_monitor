"""Design tokens: blue-graphite desk screen, Bloomberg amber as the single accent.

Up / down use teal and coral rather than pure green / red (readable for the most
common colour-vision deficiencies, and calmer on a screen kept open all day).
"""

from __future__ import annotations

import math

BACKGROUND = "#131E28"
PANEL = "#1B2835"
RULE = "#2A3A49"
TEXT = "#E3E9EF"
MUTED = "#8A9AAB"
ACCENT = "#F0A830"
UP = "#3FA796"
DOWN = "#E06C5A"
NEUTRAL_TILE = "#22303D"
CHECK_BG = "#5C4A1F"  # dark amber: value to check before use
INFO = "#6CB4E8"
SEVERITY_COLORS = {"critical": DOWN, "warning": ACCENT, "info": INFO}
#: categorical palette for comparison lines (teal / coral are reserved for up / down)
SERIES_COLORS = ["#F0A830", "#6CB4E8", "#B79CE0", "#F28DB2", "#8FD3C1", "#E8D58A",
                 "#9DB0C2", "#C8E07A", "#F5B98A", "#7F8FE8"]

FONT_FAMILY = "'IBM Plex Sans Condensed', 'Arial Narrow', 'Segoe UI', sans-serif"
FONT_URL = (
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@400;500;600&display=swap"
)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colours (``t`` in [0, 1]) -> ``#rrggbb``."""
    t = min(max(t, 0.0), 1.0)
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    mix = (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))
    return "#{:02x}{:02x}{:02x}".format(*mix)


def heat_color(z: float, clip: float = 3.0, base: str = NEUTRAL_TILE) -> str:
    """Diverging colour for a z-score, saturated at ``|z| = clip``; NaN -> ``base``."""
    if z is None or (isinstance(z, float) and math.isnan(z)):
        return base
    return blend(base, UP if z > 0 else DOWN, abs(z) / clip)


def sign_color(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)) or x == 0:
        return MUTED
    return UP if x > 0 else DOWN


def plotly_colorscale(steps: int = 11) -> list[list[float | str]]:
    """Plotly colourscale matching :func:`heat_color` on [-clip, clip]."""
    scale: list[list[float | str]] = []
    for i in range(steps):
        pos = i / (steps - 1)
        z = (pos - 0.5) * 2  # in [-1, 1]
        scale.append([pos, heat_color(z, clip=1.0)])
    return scale


def page_css() -> str:
    """Global CSS injected once per page (font, tabular figures, cards, sidebar, start screen)."""
    return f"""
<style>
@import url('{FONT_URL}');
html, body, [class*="st-"], .stMarkdown, .stDataFrame {{ font-family: {FONT_FAMILY}; }}
[data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}
/* keep Streamlit's icon font (ligatures) despite the global font override above */
[data-testid="stIconMaterial"], span[class*="material-symbols"] {{
    font-family: "Material Symbols Rounded" !important; }}

/* ---- chrome: quieter Streamlit frame, tighter page */
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stAppDeployButton"] {{
    visibility: hidden; }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1600px; }}
[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {RULE}; }}
.mm-side-title {{ color: {MUTED}; font-size: .72rem; font-weight: 600; letter-spacing: .12em;
                  text-transform: uppercase; margin: .8rem 0 .4rem 0; }}

/* ---- top bar */
.mm-topbar {{ display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap;
              gap: .8rem; padding-bottom: .8rem; margin-bottom: 1rem; border-bottom: 1px solid {RULE}; }}
.stMarkdown div.mm-title {{ color: {ACCENT}; font-size: 2.1rem; font-weight: 600; line-height: 1.05;
                           margin: 0; letter-spacing: .01em; }}
.mm-tagline {{ color: {MUTED}; font-size: .92rem; margin-top: .2rem; }}
.mm-chips {{ display: flex; flex-wrap: wrap; gap: .4rem; }}
.mm-chip {{ display: inline-block; padding: .18rem .65rem; border-radius: 999px; font-size: .8rem;
            border: 1px solid {RULE}; background: {BACKGROUND}; color: {MUTED}; white-space: nowrap; }}
.mm-chip.ok {{ color: {UP}; border-color: {blend(RULE, UP, .5)}; }}
.mm-chip.warn {{ color: {ACCENT}; border-color: {blend(RULE, ACCENT, .5)}; }}

/* ---- KPI tiles */
.mm-kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .7rem;
            margin: .2rem 0 1rem 0; }}
.mm-kpi {{ background: {PANEL}; border: 1px solid {RULE}; border-radius: 10px; padding: .6rem .9rem; }}
.mm-kpi .lbl {{ color: {MUTED}; font-size: .74rem; letter-spacing: .06em; text-transform: uppercase; }}
.mm-kpi .val {{ color: {TEXT}; font-size: 1.2rem; font-weight: 600; margin-top: .15rem;
                font-variant-numeric: tabular-nums; }}
.mm-kpi.warn .val {{ color: {ACCENT}; }}
.stMarkdown div.mm-asof {{ color: {MUTED}; font-size: 1rem; margin: .25rem 0 .8rem 0; }}

/* ---- movers */
.mm-section {{ color: {TEXT}; font-weight: 600; font-size: 1.02rem; margin: .6rem 0 .4rem 0;
               padding-left: .55rem; border-left: 3px solid {ACCENT}; }}
.mm-movers {{ display: flex; flex-wrap: wrap; gap: .5rem; margin: .2rem 0 1.2rem 0; }}
.mm-mover {{ font-variant-numeric: tabular-nums; white-space: nowrap; background: {PANEL};
             border: 1px solid {RULE}; border-radius: 8px; padding: .4rem .75rem; }}
.mm-mover .name {{ color: {TEXT}; font-weight: 500; }}
.mm-mover .chg {{ font-weight: 600; margin-left: .45rem; }}
.mm-mover .z {{ color: {MUTED}; margin-left: .45rem; font-size: .88rem; }}

/* ---- alerts, tables */
.mm-alert {{ border-left: 3px solid; padding: .15rem 0 .15rem .6rem; margin: .25rem 0; }}
.mm-alert .sev {{ font-weight: 600; margin-right: .5rem; }}
h3.mm-class {{ font-size: 1.02rem; color: {TEXT}; font-weight: 600; margin: 1.1rem 0 .35rem 0;
               padding-left: .55rem; border-left: 3px solid {ACCENT}; }}
[data-testid="stDataFrame"] {{ border: 1px solid {RULE}; border-radius: 8px; overflow: hidden; }}
[data-testid="stExpander"] details {{ border-color: {RULE}; border-radius: 10px; background: {PANEL}; }}

/* ---- widgets */
.stButton button, .stDownloadButton button {{ border-radius: 8px; font-weight: 500; }}
.stButton button[kind="primary"] {{ color: {BACKGROUND}; }}
[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 12px; }}

/* ---- start screen */
.mm-hero {{ text-align: center; margin: 4vh auto 1.6rem auto; max-width: 640px; }}
.mm-brand {{ color: {ACCENT}; font-size: 2.6rem; font-weight: 600; letter-spacing: .01em; }}
.mm-hero-sub {{ color: {MUTED}; font-size: 1.02rem; margin-top: .4rem; line-height: 1.5; }}
.mm-hero-sub b {{ color: {TEXT}; }}
.mm-card-title {{ color: {TEXT}; font-weight: 600; font-size: 1.1rem; margin-bottom: .4rem; }}
.mm-probes {{ display: flex; flex-wrap: wrap; gap: .45rem; margin: .6rem 0; }}
.mm-probe {{ border-radius: 8px; padding: .3rem .7rem; font-size: .86rem; border: 1px solid {RULE};
             background: {BACKGROUND}; }}
.mm-probe.ok {{ color: {UP}; border-color: {blend(RULE, UP, .5)}; }}
.mm-probe.ko {{ color: {DOWN}; border-color: {blend(RULE, DOWN, .5)}; }}
.mm-probe b {{ color: {TEXT}; margin-right: .3rem; }}

@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
"""
