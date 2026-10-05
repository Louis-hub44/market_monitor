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
    """Global CSS injected once per page (font, tabular figures, compact tables)."""
    return f"""
<style>
@import url('{FONT_URL}');
html, body, [class*="st-"], .stMarkdown, .stDataFrame {{ font-family: {FONT_FAMILY}; }}
[data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; }}
.stMarkdown div.mm-title {{ color: {ACCENT}; font-size: 2.1rem; font-weight: 600; line-height: 1.1;
                           margin: 0; letter-spacing: .01em; }}
.stMarkdown div.mm-asof {{ color: {MUTED}; font-size: 1rem; margin: .25rem 0 .8rem 0; }}
.mm-movers {{ display: flex; flex-wrap: wrap; gap: .5rem 1.6rem; margin: .2rem 0 1.2rem 0;
              padding: .7rem 0; border-top: 1px solid {RULE}; border-bottom: 1px solid {RULE}; }}
.mm-mover {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
.mm-mover .name {{ color: {TEXT}; font-weight: 500; }}
.mm-mover .chg {{ font-weight: 600; margin-left: .35rem; }}
.mm-mover .z {{ color: {MUTED}; margin-left: .35rem; }}
.mm-alert {{ border-left: 3px solid; padding: .15rem 0 .15rem .6rem; margin: .25rem 0; }}
.mm-alert .sev {{ font-weight: 600; margin-right: .5rem; }}
h3.mm-class {{ font-size: 1.05rem; color: {TEXT}; font-weight: 600; margin: 1rem 0 .3rem 0; }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
"""
