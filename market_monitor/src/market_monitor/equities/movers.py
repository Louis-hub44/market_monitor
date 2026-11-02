r"""Top movers of an index on its last closed session, contributions, breadth, sectors.

For a member :math:`i` with close :math:`P_{i,t}` on the index's session :math:`t` and
:math:`P_{i,t-1}` on the previous one:

* return :math:`r_i = P_{i,t}/P_{i,t-1} - 1`, converted to the index currency for a foreign
  listing (Stoxx 600: :math:`(1+r_i)(X_t/X_{t-1}) - 1`, :math:`X` = value of one unit in EUR);
* weight on :math:`t-1`, drifted from the composition date :math:`d_0` with the prices:
  :math:`w_{i,t-1} \propto w_{i,d_0}\,P_{i,t-1}/P_{i,d_0}`;
* contribution in index points :math:`c_i = w_{i,t-1}\,r_i\,I_{t-1}`; the sum is compared
  with the index's own move :math:`I_t - I_{t-1}` (the residual tells how good the weights
  are: free float, stale composition, missing prices);
* volume against its 20-session average, close against the 50 / 200-session averages,
  new 52-week high / low (close above every close of the previous 52 weeks).

Safeguards: a member whose market held the session but has no print for it is *not quoted*
(not counted); a member whose market was closed (local holiday) is left out; a move beyond
``max_abs_return_pct``, a split reported on the session, a move matching a split ratio or a
zero volume goes to "À vérifier" - never to the movers, the breadth or the contributions.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from market_monitor.equities import calendars
from market_monitor.equities.constituents import Composition
from market_monitor.equities.panel import EquityPanel

#: market whose calendar defines an index's session
INDEX_MARKETS = {"CAC": "XPAR", "SX5E": "XETR", "SXXP": "XETR", "DAX": "XETR", "FTSEMIB": "XMIL",
                 "IBEX": "XMAD", "SPX": "XNYS", "NDX": "XNAS"}
SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10)
SPLIT_TOLERANCE = 0.01
VOLUME_SESSIONS = 20
STATUS_OK, STATUS_NOT_QUOTED, STATUS_CLOSED, STATUS_CHECK, STATUS_NO_PRICE = (
    "ok", "non coté", "place fermée", "à vérifier", "sans cours")
MEMBER_COLUMNS = ["ticker", "name", "sector", "mic", "currency", "close", "date", "return_pct",
                  "volume_ratio", "weight", "contribution", "contribution_pct", "above_ma50", "above_ma200",
                  "new_high", "new_low", "status", "reason"]


@dataclass
class IndexMovers:
    index: str
    name: str
    session: date | None
    previous: date | None
    members: pd.DataFrame
    composition_as_of: date | None = None
    weights: str = "none"
    index_change_pts: float = math.nan
    index_change_pct: float = math.nan
    index_level: float = math.nan
    notes: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def counted(self) -> pd.DataFrame:
        return self.members[self.members["status"] == STATUS_OK]

    def top(self, n: int = 10) -> pd.DataFrame:
        """``n`` largest rises (members up on the session only)."""
        ok = self.counted
        return ok[ok["return_pct"] > 0].sort_values("return_pct", ascending=False).head(n)

    def flop(self, n: int = 10) -> pd.DataFrame:
        """``n`` largest falls (members down on the session only)."""
        ok = self.counted
        return ok[ok["return_pct"] < 0].sort_values("return_pct", ascending=True).head(n)

    @property
    def contributions_total(self) -> float:
        values = pd.to_numeric(self.counted["contribution"], errors="coerce").dropna()
        return float(values.sum()) if not values.empty else math.nan

    @property
    def contributions_pct(self) -> float:
        values = pd.to_numeric(self.counted["contribution_pct"], errors="coerce").dropna()
        return float(values.sum()) if not values.empty else math.nan

    def contributors(self, n: int = 10) -> pd.DataFrame:
        """``n`` largest positive and ``n`` largest negative contributions (pp of the index)."""
        ok = self.counted.assign(_c=pd.to_numeric(self.counted["contribution_pct"], errors="coerce"))
        ok = ok.dropna(subset=["_c"])
        best, worst = ok[ok["_c"] > 0].nlargest(n, "_c"), ok[ok["_c"] < 0].nsmallest(n, "_c")
        return pd.concat([best, worst]).sort_values("_c").drop(columns="_c")

    @property
    def residual(self) -> float:
        return self.index_change_pts - self.contributions_total

    def breadth(self) -> dict[str, float]:
        ok = self.counted
        def share(column: str) -> float:
            known = ok[column].dropna()
            return 100.0 * float(known.astype(bool).mean()) if not known.empty else math.nan
        return {
            "counted": len(ok), "members": len(self.members),
            "advancers": int((ok["return_pct"] > 0).sum()), "decliners": int((ok["return_pct"] < 0).sum()),
            "unchanged": int((ok["return_pct"] == 0).sum()),
            "above_ma50": share("above_ma50"), "above_ma200": share("above_ma200"),
            "new_highs": int(ok["new_high"].fillna(False).astype(bool).sum()),
            "new_lows": int(ok["new_low"].fillna(False).astype(bool).sum()),
        }

    def sectors(self) -> pd.DataFrame:
        """Per sector: members counted, equal-weight and weighted average return, contribution."""
        ok = self.counted.assign(sector=lambda t: t["sector"].replace("", "Autres"))
        rows = []
        for sector, group in ok.groupby("sector"):
            weights = group["weight"].fillna(0.0)
            weighted = (float((group["return_pct"] * weights).sum() / weights.sum())
                        if weights.sum() > 0 else math.nan)
            rows.append({"sector": sector, "members": len(group),
                         "average_pct": float(group["return_pct"].mean()), "weighted_pct": weighted,
                         "contribution": _total(group["contribution"]),
                         "contribution_pct": _total(group["contribution_pct"])})
        out = pd.DataFrame(rows, columns=["sector", "members", "average_pct", "weighted_pct", "contribution",
                                          "contribution_pct"])
        return out.sort_values("average_pct", ascending=False).reset_index(drop=True)


def _total(values: pd.Series) -> float:
    return float(pd.to_numeric(values, errors="coerce").sum(min_count=1))


def index_market(composition: Composition) -> str:
    if composition.index in INDEX_MARKETS:
        return INDEX_MARKETS[composition.index]
    mics = [m.mic for m in composition.members if m.mic]
    return max(set(mics), key=mics.count) if mics else calendars.market_of("", composition.currency)


def index_session(composition: Composition, as_of: date, now: datetime | None = None) -> date:
    """Last closed session of the index's market on or before ``as_of``."""
    mic = index_market(composition)
    if now is not None and now.date() <= as_of:
        return calendars.last_closed_session(mic, now)
    return calendars.session_on_or_before(mic, as_of)


def compute_movers(
    composition: Composition,
    panel: EquityPanel,
    session: date,
    *,
    index_levels: pd.Series | None = None,
    fx_to_eur: Mapping[str, str] | None = None,
    max_abs_return_pct: float = 25.0,
) -> IndexMovers:
    """Movers of ``composition`` on ``session`` (or the last session loaded, if earlier)."""
    closes, volumes = panel.field("last"), panel.field("volume")
    mic = index_market(composition)
    out = IndexMovers(composition.index, composition.name, None, None, pd.DataFrame(columns=MEMBER_COLUMNS),
                      composition.as_of, composition.weights)
    tickers = [t for t in composition.tickers if t in closes.columns]
    if not composition.members:
        out.notes.append("composition vide : market-monitor constituents refresh")
        return out
    if not tickers:
        out.notes.append("aucun cours chargé pour ces titres : actualiser les cours des titres")
        return out
    loaded = closes[tickers].loc[: pd.Timestamp(session)]
    quoted = loaded.notna().sum(axis=1)
    usable = quoted[quoted >= max(1, len(tickers) // 2)]
    if usable.empty:
        out.notes.append("pas assez de cours chargés : actualiser les cours des titres")
        return out
    t = min(pd.Timestamp(session), usable.index[-1])
    if t < pd.Timestamp(session):
        out.notes.append(f"cours des titres chargés jusqu'au {t:%d/%m} (séance attendue : {session:%d/%m}) : "
                         "actualiser les cours des titres")
    previous = pd.Timestamp(calendars.previous_session(mic, t.date()))
    out.session, out.previous = t.date(), previous.date()
    _index_move(out, index_levels, t, previous)
    rows = [_member_row(m, closes, volumes, panel, t, previous, composition, fx_to_eur or {},
                        max_abs_return_pct) for m in composition.members]
    members = pd.DataFrame(rows, columns=MEMBER_COLUMNS).set_index("ticker", drop=False)
    _contributions(members, composition, closes, previous, out.index_level - out.index_change_pts
                   if not math.isnan(out.index_change_pts) else math.nan)
    out.members = members
    _notes(out, composition, members)
    return out


def _index_move(out: IndexMovers, levels: pd.Series | None, t: pd.Timestamp, previous: pd.Timestamp) -> None:
    if levels is None:
        return
    series = levels.dropna()
    if pd.Timestamp(t) in series.index and pd.Timestamp(previous) in series.index:
        now, before = float(series.loc[t]), float(series.loc[previous])
        out.index_level, out.index_change_pts = now, now - before
        out.index_change_pct = 100.0 * (now / before - 1.0) if before else math.nan


def _member_row(member, closes: pd.DataFrame, volumes: pd.DataFrame, panel: EquityPanel, t: pd.Timestamp,
                previous: pd.Timestamp, composition: Composition, fx: Mapping[str, str],
                max_abs: float) -> dict[str, object]:
    row: dict[str, object] = {c: math.nan for c in MEMBER_COLUMNS}
    row.update(ticker=member.ticker, name=member.name, sector=member.sector, mic=member.mic,
               currency=member.currency, weight=member.weight, status=STATUS_OK, reason="",
               date=None, above_ma50=None, above_ma200=None, new_high=None, new_low=None)
    if member.ticker not in closes.columns:
        row.update(status=STATUS_NO_PRICE, reason="aucun cours chargé")
        return row
    series = closes[member.ticker].loc[:t].dropna()
    market = calendars.market_of(member.mic, member.currency)
    if not calendars.is_session(market, t.date()):
        row.update(status=STATUS_CLOSED, reason=f"place fermée le {t:%d/%m}")
        return row
    if series.empty or series.index[-1] != t:
        last = f"dernière cotation le {series.index[-1]:%d/%m}" if not series.empty else "aucune cotation"
        row.update(status=STATUS_NOT_QUOTED, reason=f"non coté le {t:%d/%m} ({last})")
        return row
    before = series.loc[:t - pd.Timedelta(days=1)]
    if before.empty:
        row.update(status=STATUS_NO_PRICE, reason="pas de clôture précédente")
        return row
    close, prev = float(series.iloc[-1]), float(before.iloc[-1])
    local = close / prev - 1.0
    ret = local
    foreign = composition.currency == "EUR" and member.currency not in ("", "EUR")
    symbol = fx.get(member.currency) if foreign else None
    if symbol and symbol in closes.columns:
        rate = closes[symbol].dropna()
        at_t, at_prev = rate.loc[:t], rate.loc[:before.index[-1]]
        if not at_t.empty and not at_prev.empty:
            ret = (1.0 + local) * float(at_t.iloc[-1]) / float(at_prev.iloc[-1]) - 1.0
    row.update(close=close, date=t.date(), return_pct=100.0 * ret)
    volume = (volumes[member.ticker].loc[:t].dropna() if member.ticker in volumes.columns
              else pd.Series(dtype=float))
    if len(volume) > VOLUME_SESSIONS and volume.index[-1] == t:
        average = float(volume.iloc[-VOLUME_SESSIONS - 1:-1].mean())
        row["volume_ratio"] = float(volume.iloc[-1]) / average if average > 0 else math.nan
    for n in (50, 200):
        if len(series) >= n:
            row[f"above_ma{n}"] = bool(close > float(series.tail(n).mean()))
    year = series.loc[t - pd.DateOffset(years=1): t - pd.Timedelta(days=1)]
    if len(year) >= 60:
        row["new_high"], row["new_low"] = bool(close > float(year.max())), bool(close < float(year.min()))
    reasons = _suspect(member.ticker, local, close, prev, volume, panel, t, before.index[-1], max_abs)
    if reasons:
        row.update(status=STATUS_CHECK, reason=" ; ".join(reasons))
    return row


def _suspect(ticker: str, ret: float, close: float, prev: float, volume: pd.Series, panel: EquityPanel,
             t: pd.Timestamp, prev_day: pd.Timestamp, max_abs: float) -> list[str]:
    reasons = []
    if abs(100.0 * ret) > max_abs:
        reasons.append(f"variation de {100 * ret:+.1f} % au-delà du seuil de {max_abs:g} %".replace(".", ","))
    cov = panel.coverage(ticker)
    if cov is not None:
        days = [d for d in cov.splits if prev_day < pd.Timestamp(d) <= t]
        if days:
            reasons.append(f"split signalé par Yahoo le {pd.Timestamp(days[-1]):%d/%m}")
    ratio = close / prev if prev else math.nan
    if abs(ret) > 0.3 and any(abs(ratio * k - 1.0) < SPLIT_TOLERANCE for k in SPLIT_RATIOS):
        reasons.append("rapport de cours typique d'un split ou regroupement")
    if not volume.empty and volume.index[-1] == t and float(volume.iloc[-1]) == 0.0:
        reasons.append("volume nul")
    return reasons


def _contributions(members: pd.DataFrame, composition: Composition, closes: pd.DataFrame,
                   previous: pd.Timestamp, index_prev: float) -> None:
    """Weights drifted from the composition date to ``previous``, contribution in points."""
    if not composition.weighted:
        return
    base_day = pd.Timestamp(composition.as_of) if composition.as_of else None
    drifted = {}
    for ticker, weight in members["weight"].items():
        if weight is None or (isinstance(weight, float) and math.isnan(weight)) or ticker not in closes:
            continue
        series = closes[ticker].dropna()
        at_prev = series.loc[:previous]
        at_base = series.loc[:base_day] if base_day is not None else pd.Series(dtype=float)
        known = not at_prev.empty and not at_base.empty
        factor = float(at_prev.iloc[-1]) / float(at_base.iloc[-1]) if known else 1.0
        drifted[ticker] = float(weight) * factor
    total = sum(drifted.values())
    if not total:
        return
    for ticker, w in drifted.items():
        members.at[ticker, "weight"] = 100.0 * w / total
        ret = pd.to_numeric(members.at[ticker, "return_pct"], errors="coerce")
        if members.at[ticker, "status"] != STATUS_OK or pd.isna(ret):
            continue
        members.at[ticker, "contribution_pct"] = (w / total) * float(ret)        # pp of the index
        if not math.isnan(index_prev):
            members.at[ticker, "contribution"] = (w / total) * float(ret) / 100.0 * index_prev


def _notes(out: IndexMovers, composition: Composition, members: pd.DataFrame) -> None:
    for status in (STATUS_CHECK,):
        for _, row in members[members["status"] == status].iterrows():
            out.checks.append(f"{row['name']} ({row['ticker']}) : {row['reason']}")
    not_quoted = members[members["status"] == STATUS_NOT_QUOTED]
    if not not_quoted.empty:
        names = ", ".join(not_quoted["name"].head(8))
        out.notes.append(f"{len(not_quoted)} titre(s) non coté(s) sur la séance, non comptés : {names}"
                         + ("…" if len(not_quoted) > 8 else ""))
    closed = members[members["status"] == STATUS_CLOSED]
    if not closed.empty:
        out.notes.append(f"{len(closed)} titre(s) sur une place fermée ce jour-là (jour férié local)")
    missing = members[members["status"] == STATUS_NO_PRICE]
    if not missing.empty:
        out.notes.append(f"{len(missing)} titre(s) sans cours chargé")
    if composition.as_of and out.session and (out.session - composition.as_of).days > 45:
        out.notes.append(f"composition du {composition.as_of:%d/%m/%Y} : à rafraîchir")
    if composition.weights == "marketcap":
        out.notes.append("poids estimés par la capitalisation totale (approximation du flottant)")
    if not composition.weighted:
        out.notes.append("pas de poids : contributions non calculées")
    elif (not math.isnan(out.index_change_pts) and out.index_change_pts
          and not math.isnan(out.contributions_total)):
        gap = abs(out.residual) / abs(out.index_change_pts)
        if gap > 0.15:
            text = (f"contributions : {out.contributions_total:+.1f} pts contre "
                    f"{out.index_change_pts:+.1f} pts pour l'indice (écart {100 * gap:.0f} %)")
            out.notes.append(text.replace(".", ",") + " - poids à rafraîchir")


# ------------------------------------------------------------ second source
SecondSource = Callable[[list[str], date, date], Mapping[str, pd.DataFrame]]


def verify_members(movers: IndexMovers, tickers: list[str], second: SecondSource,
                   tolerance_pp: float = 1.0) -> dict[str, str]:
    """Compare the session's return of ``tickers`` with a second source (FMP...).

    Returns ticker -> ``"ok"`` / ``"écart …"`` / ``"indisponible"``; a gap moves the member to
    "À vérifier".
    """
    if movers.session is None or movers.previous is None or not tickers:
        return {}
    start = movers.previous - pd.Timedelta(days=7)
    try:
        found = second(tickers, start.date() if hasattr(start, "date") else start, movers.session)
    except Exception:  # noqa: BLE001 - the second source is optional by nature
        return {t: "indisponible" for t in tickers}
    out: dict[str, str] = {}
    for ticker in tickers:
        frame = found.get(ticker)
        primary = (float(movers.members.at[ticker, "return_pct"])  # type: ignore[arg-type]
                   if ticker in movers.members.index else math.nan)
        if frame is None or frame.empty or "last" not in frame:
            out[ticker] = "indisponible"
            continue
        closes = frame["last"].dropna()
        at_t = closes.loc[: pd.Timestamp(movers.session)]
        at_prev = closes.loc[: pd.Timestamp(movers.previous)]
        if at_t.empty or at_prev.empty or at_t.index[-1] != pd.Timestamp(movers.session):
            out[ticker] = "indisponible"
            continue
        other = 100.0 * (float(at_t.iloc[-1]) / float(at_prev.iloc[-1]) - 1.0)
        if np.isfinite(primary) and abs(other - primary) > tolerance_pp:
            out[ticker] = f"écart : {primary:+.2f} % contre {other:+.2f} % (2e source)".replace(".", ",")
            movers.members.at[ticker, "status"] = STATUS_CHECK
            movers.members.at[ticker, "reason"] = out[ticker]
            movers.checks.append(f"{movers.members.at[ticker, 'name']} ({ticker}) : {out[ticker]}")
        else:
            out[ticker] = "ok"
    return out
