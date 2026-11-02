"""Design tokens of the terminal (shared look with Market Monitor).

Blue-graphite desk screen, Bloomberg amber as the single accent; up / down in teal and
coral rather than pure green / red (readable for the common colour-vision deficiencies).
"""

from __future__ import annotations

from html import escape

BACKGROUND = "#131E28"
PANEL = "#1B2835"
PANEL_2 = "#22303D"
RULE = "#2A3A49"
TEXT = "#E3E9EF"
MUTED = "#8A9AAB"
ACCENT = "#F0A830"
UP = "#3FA796"
DOWN = "#E06C5A"
INFO = "#6CB4E8"
#: categorical series (teal / coral reserved for up / down)
SERIES = ["#F0A830", "#6CB4E8", "#B79CE0", "#8FD3C1", "#F28DB2", "#E8D58A"]
ESTR_COLOR, IBOR_COLOR = INFO, ACCENT

FONT_FAMILY = "'IBM Plex Sans Condensed', 'Arial Narrow', 'Segoe UI', sans-serif"
MONO_FAMILY = "'IBM Plex Mono', 'Consolas', monospace"
FONT_URL = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@400;500;600"
            "&family=IBM+Plex+Mono:wght@400;500&display=swap")


def _rgb(c: str) -> tuple[int, int, int]:
    h = c.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colours (``t`` in [0, 1])."""
    t = min(max(t, 0.0), 1.0)
    (r1, g1, b1), (r2, g2, b2) = _rgb(a), _rgb(b)
    return f"#{round(r1 + (r2 - r1) * t):02x}{round(g1 + (g2 - g1) * t):02x}{round(b1 + (b2 - b1) * t):02x}"


def plotly_layout(title: str = "", y_title: str = "", height: int = 360) -> dict:
    """Layout dict applied to every Plotly figure of the dashboard."""
    axis = {"gridcolor": RULE, "zerolinecolor": blend(RULE, MUTED, 0.4), "linecolor": RULE,
            "tickfont": {"color": MUTED, "size": 11}, "title": {"font": {"color": MUTED, "size": 11}}}
    return {
        "title": {"text": title, "font": {"color": TEXT, "size": 14}, "x": 0.01, "xanchor": "left"},
        "paper_bgcolor": PANEL, "plot_bgcolor": PANEL, "height": height,
        "font": {"family": FONT_FAMILY, "color": TEXT, "size": 12},
        "margin": {"l": 48, "r": 20, "t": 46, "b": 40},
        "xaxis": axis, "yaxis": {**axis, "title": {"text": y_title, "font": {"color": MUTED, "size": 11}}},
        "legend": {"orientation": "h", "y": -0.18, "font": {"color": MUTED, "size": 11}, "bgcolor": "rgba(0,0,0,0)"},
        "hovermode": "x unified",
        "hoverlabel": {"bgcolor": BACKGROUND, "bordercolor": RULE, "font": {"family": FONT_FAMILY, "color": TEXT}},
    }


# ---------------------------------------------------------------- html bits
def chip(text: str, kind: str = "") -> str:
    """Small rounded label; ``kind`` in ``"" | ok | warn | ko``."""
    return f'<span class="rba-chip {kind}">{escape(text)}</span>'


def kpi_tiles(items: list[tuple[str, str, str]]) -> str:
    """Row of KPI tiles from ``(label, value, sub)`` triples."""
    tiles = "".join(f'<div class="rba-kpi"><div class="lbl">{escape(lbl)}</div><div class="val">{escape(val)}</div>'
                    f'<div class="sub">{escape(sub)}</div></div>' for lbl, val, sub in items)
    return f'<div class="rba-kpis">{tiles}</div>'


def section(title: str, sub: str = "") -> str:
    s = f'<span class="sub">{escape(sub)}</span>' if sub else ""
    return f'<div class="rba-section">{escape(title)}{s}</div>'


def page_css() -> str:
    """Global CSS injected once per run."""
    return f"""
<style>
@import url('{FONT_URL}');
html, body, [class*="st-"], .stMarkdown {{ font-family: {FONT_FAMILY}; }}
[data-testid="stDataFrame"] {{ font-variant-numeric: tabular-nums; border: 1px solid {RULE}; border-radius: 8px; }}
[data-testid="stIconMaterial"], span[class*="material-symbols"] {{ font-family: "Material Symbols Rounded" !important; }}
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stAppDeployButton"] {{ visibility: hidden; }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1680px; }}
[data-testid="stSidebar"] {{ background: {PANEL}; border-right: 1px solid {RULE}; }}
.rba-side-title {{ color: {MUTED}; font-size: .72rem; font-weight: 600; letter-spacing: .12em;
                   text-transform: uppercase; margin: .9rem 0 .35rem 0; }}

/* top bar */
.rba-topbar {{ display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: .8rem;
               padding-bottom: .7rem; margin-bottom: .9rem; border-bottom: 1px solid {RULE}; }}
.rba-title {{ color: {ACCENT}; font-size: 1.9rem; font-weight: 600; line-height: 1.05; letter-spacing: .01em; }}
.rba-title span {{ color: {MUTED}; font-weight: 400; font-size: 1rem; margin-left: .6rem; }}
.rba-chips {{ display: flex; flex-wrap: wrap; gap: .4rem; }}
.rba-chip {{ display: inline-block; padding: .16rem .62rem; border-radius: 999px; font-size: .78rem;
             border: 1px solid {RULE}; background: {BACKGROUND}; color: {MUTED}; white-space: nowrap; }}
.rba-chip.ok {{ color: {UP}; border-color: {blend(RULE, UP, .5)}; }}
.rba-chip.warn {{ color: {ACCENT}; border-color: {blend(RULE, ACCENT, .55)}; }}
.rba-chip.ko {{ color: {DOWN}; border-color: {blend(RULE, DOWN, .55)}; }}

/* KPI tiles */
.rba-kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .6rem; margin: .2rem 0 1rem 0; }}
.rba-kpi {{ background: {PANEL}; border: 1px solid {RULE}; border-radius: 10px; padding: .55rem .85rem; }}
.rba-kpi .lbl {{ color: {MUTED}; font-size: .72rem; letter-spacing: .07em; text-transform: uppercase; }}
.rba-kpi .val {{ color: {TEXT}; font-size: 1.25rem; font-weight: 600; margin-top: .1rem;
                 font-family: {MONO_FAMILY}; font-variant-numeric: tabular-nums; }}
.rba-kpi .sub {{ color: {MUTED}; font-size: .78rem; margin-top: .05rem; min-height: 1em; }}

.rba-section {{ color: {TEXT}; font-weight: 600; font-size: 1.02rem; margin: .9rem 0 .45rem 0;
                padding-left: .55rem; border-left: 3px solid {ACCENT}; }}
.rba-section .sub {{ color: {MUTED}; font-weight: 400; font-size: .86rem; margin-left: .6rem; }}
.rba-note {{ background: {blend(PANEL, ACCENT, .08)}; border: 1px solid {blend(RULE, ACCENT, .45)};
             border-radius: 10px; padding: .6rem .9rem; color: {TEXT}; font-size: .9rem; margin: .2rem 0 .9rem 0; }}
.rba-note b {{ color: {ACCENT}; }}

/* tabs */
.stTabs [data-baseweb="tab-list"] {{ gap: .2rem; border-bottom: 1px solid {RULE}; }}
.stTabs [data-baseweb="tab"] {{ padding: .45rem .95rem; border-radius: 8px 8px 0 0; color: {MUTED};
                                font-weight: 500; letter-spacing: .02em; }}
.stTabs [aria-selected="true"] {{ color: {ACCENT}; background: {PANEL}; }}
.stTabs [data-baseweb="tab-highlight"] {{ background-color: {ACCENT}; }}

[data-testid="stExpander"] details {{ border-color: {RULE}; border-radius: 10px; background: {PANEL}; }}
.stButton button, .stDownloadButton button {{ border-radius: 8px; font-weight: 500; }}
.stButton button[kind="primary"] {{ color: {BACKGROUND}; }}
[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 12px; }}
[data-testid="stMetric"] {{ background: {PANEL}; border: 1px solid {RULE}; border-radius: 10px; padding: .5rem .8rem; }}
[data-testid="stMetricValue"] {{ font-family: {MONO_FAMILY}; font-size: 1.2rem; }}

/* start screen */
.rba-hero {{ text-align: center; margin: 3vh auto 1.4rem auto; max-width: 720px; }}
.rba-brand {{ color: {ACCENT}; font-size: 2.5rem; font-weight: 600; letter-spacing: .01em; }}
.rba-brand-sub {{ color: {MUTED}; font-size: .95rem; letter-spacing: .14em; text-transform: uppercase; }}
.rba-hero-txt {{ color: {MUTED}; font-size: 1rem; margin-top: .7rem; line-height: 1.5; }}
.rba-hero-txt b {{ color: {TEXT}; }}
.rba-card-title {{ color: {TEXT}; font-weight: 600; font-size: 1.05rem; margin: .1rem 0 .35rem 0; }}
.rba-probes {{ display: flex; flex-wrap: wrap; gap: .45rem; margin: .6rem 0; }}
.rba-probe {{ border-radius: 8px; padding: .3rem .7rem; font-size: .85rem; border: 1px solid {RULE}; background: {BACKGROUND}; }}
.rba-probe.ok {{ color: {UP}; border-color: {blend(RULE, UP, .5)}; }}
.rba-probe.ko {{ color: {DOWN}; border-color: {blend(RULE, DOWN, .5)}; }}
.rba-probe b {{ color: {TEXT}; margin-right: .3rem; }}

@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
"""
