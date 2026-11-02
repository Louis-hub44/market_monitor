"""Composants d'interface réutilisables.

Tous lisent la palette **au moment de l'appel** (et non à l'import) : c'est ce
qui permet de basculer clair/sombre sans redémarrer l'application.
"""

from __future__ import annotations

import streamlit as st

from config.theme import (
    active_mode,
    categorical_colors,
    css,
    palette,
    quality_colors,
)
from models.datapoint import DataPoint
from models.enums import Confidence, DataQuality


def inject_css() -> None:
    st.markdown(css(active_mode()), unsafe_allow_html=True)


def theme_toggle(container=None) -> str:
    """Sélecteur de thème. Renvoie le mode actif.

    Le thème des composants natifs (grilles, menus) vient de
    `.streamlit/config.toml` et n'est pas modifiable à chaud par Streamlit ;
    ce sélecteur pilote tout ce que l'application dessine elle-même — CSS,
    graphiques, palettes. Les deux sont accordés sur le mode sombre par défaut.
    """
    target = container or st
    current = st.session_state.get("pi_theme", "dark")
    choice = target.radio(
        "Thème", ["Sombre", "Clair"], index=0 if current == "dark" else 1,
        horizontal=True, label_visibility="collapsed", key="pi_theme_radio",
    )
    mode = "dark" if choice == "Sombre" else "light"
    if mode != current:
        st.session_state["pi_theme"] = mode
        st.rerun()
    return mode


def page_header(eyebrow: str, title: str, subtitle: str = "") -> None:
    st.markdown(
        f'<div class="pi-eyebrow">{eyebrow}</div>'
        f'<div class="pi-title">{title}</div>'
        f'<div class="pi-sub">{subtitle}</div><div class="pi-rule"></div>',
        unsafe_allow_html=True,
    )


def block_header(title: str, note: str = "") -> None:
    """Titre de bloc : libellé, filet horizontal, annotation à droite."""
    st.markdown(
        f'<div class="pi-block"><div class="s-title">{title}</div>'
        f'<div class="s-line"></div>'
        + (f'<div class="s-note">{note}</div>' if note else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def identity_band(cells: list[tuple[str, str]], name: str, meta: str) -> None:
    """Bande d'identité du portefeuille, en tête de page."""
    body = (
        f'<div class="b-cell b-main"><div class="b-name">{name}</div>'
        f'<div class="b-meta">{meta}</div></div>'
    )
    for label, value in cells:
        body += (f'<div class="b-cell"><div class="b-label">{label}</div>'
                 f'<div class="b-value">{value}</div></div>')
    st.markdown(f'<div class="pi-band">{body}</div>', unsafe_allow_html=True)


def allocation_legend(data: dict[str, float]) -> None:
    """Légende compacte accompagnant la barre d'allocation empilée."""
    colors = categorical_colors(data.keys())
    items = [
        f'<div class="l-item"><span class="l-dot" style="background:{color}"></span>'
        f'{key} <span class="l-val">{value:.1%}</span></div>'
        for (key, value), color in zip(data.items(), colors)
    ]
    st.markdown(f'<div class="pi-legend">{"".join(items)}</div>', unsafe_allow_html=True)


def quality_badge(quality: DataQuality | str, note: str = "") -> str:
    q = quality.value if isinstance(quality, DataQuality) else str(quality)
    color = quality_colors().get(q, palette().ink_soft)
    title = f' title="{note}"' if note else ""
    return f'<span class="pi-badge" style="color:{color}"{title}>{q}</span>'


def fmt(value: float | None, kind: str = "pct", digits: int = 2) -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value:+.{digits}%}" if abs(value) < 100 else "—"
    if kind == "pct0":
        return f"{value:.{digits}%}"
    if kind == "num":
        return f"{value:,.{digits}f}".replace(",", " ")
    if kind == "money":
        return f"{value:,.0f}".replace(",", " ")
    if kind == "bp":
        return f"{value:+,.0f} bp"
    return str(value)


def kpi(label: str, value: str, delta: str | None = None, foot: str = "",
        quality: DataQuality | str | None = None, positive: bool | None = None) -> None:
    cls = "" if positive is None else ("pos" if positive else "neg")
    delta_html = f'<div class="k-delta {cls}">{delta}</div>' if delta else ""
    badge = f" {quality_badge(quality)}" if quality else ""
    st.markdown(
        f'<div class="pi-kpi"><div class="k-label">{label}</div>'
        f'<div class="k-value {cls}">{value}</div>{delta_html}'
        f'<div class="k-foot">{foot}{badge}</div></div>',
        unsafe_allow_html=True,
    )


def kpi_row(items: list[dict]) -> None:
    cols = st.columns(len(items))
    for col, item in zip(cols, items):
        with col:
            kpi(**item)


def risk_rail(rows: list[tuple[str, float]], max_abs: float | None = None) -> None:
    """Barres horizontales d'impact — signature visuelle des pages de risque."""
    if not rows:
        st.caption("Aucun impact calculable avec les données disponibles.")
        return
    p = palette()
    scale = max_abs or max(abs(v) for _, v in rows) or 1.0
    html = []
    for name, value in rows:
        width = min(abs(value) / scale * 100, 100)
        color = p.negative if value < 0 else p.positive
        html.append(
            f'<div class="pi-rail"><div class="r-name">{name}</div>'
            f'<div class="r-bar"><div class="r-fill" style="width:{width:.1f}%;background:{color}"></div></div>'
            f'<div class="r-val" style="color:{color}">{value:+.2%}</div></div>'
        )
    st.markdown("".join(html), unsafe_allow_html=True)


def datapoint_line(label: str, dp: DataPoint, kind: str = "num") -> None:
    value = fmt(dp.value, kind) if dp.value is not None else "non disponible"
    st.markdown(
        f'**{label}** · {value} {quality_badge(dp.quality, dp.note)} '
        f'<span class="mut">{dp.source}</span>',
        unsafe_allow_html=True,
    )


def confidence_note(confidence: Confidence, coverage: float, assumptions: list[str]) -> None:
    labels = {Confidence.HIGH: "élevée", Confidence.MEDIUM: "moyenne", Confidence.LOW: "faible"}
    st.caption(
        f"Confiance {labels[confidence]} · couverture du calcul {coverage:.0%}"
        + (f" · hypothèses de modèle : {', '.join(assumptions[:3])}" if assumptions else "")
    )


def empty_state(message: str, hint: str = "") -> None:
    st.info(message + (f"\n\n{hint}" if hint else ""))


def warning_list(warnings: list[str]) -> None:
    if not warnings:
        return
    with st.expander(f"Avertissements ({len(warnings)})"):
        for w in warnings:
            st.write("— " + w)
