"""Series imported from CSV / Excel files (desk exports, manual histories, the morning banners).

Spec in the mapping::

    US.HY.OAS:
      file: {path: data/series/historique_bandeaux.csv, column: "HY US OAS", scale: 0.0001}

Two layouts are recognised automatically:

* **wide**: one date column and one column per series (``date,Bund 10Y,OAT 10Y,…``);
  ``column`` names the series column;
* **long**: ``date, <id column>, <value column>[, …]`` with one row per (date, series), as in
  ``historique_bandeaux.csv`` (``date,instrument,niveau,variation,source``); ``column`` is the
  value of the id column. The id / value / date columns are detected (``instrument``, ``id``,
  ``ticker``, ``serie`` / ``niveau``, ``value``, ``valeur``, ``close``, ``level`` / ``date``) or
  given explicitly (``id_column``, ``value_column``, ``date_column``).

Decimal commas, ``;`` separators, ISO or day-first dates and ``.xlsx`` sheets (``sheet``) are
handled. A relative ``path`` is resolved against the provider ``root`` (the project root by
default). Files are read once per provider instance.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from rates_analytics.data.mapping import MarketDataMap
from rates_analytics.data.provider import Capability, DataProvider, empty_history
from rates_analytics.exceptions import DataProviderError

logger = logging.getLogger(__name__)

DATE_NAMES = ("date", "dates", "observation_date", "time_period", "jour")
ID_NAMES = ("instrument", "id", "ticker", "serie", "série", "series", "name", "nom")
VALUE_NAMES = ("niveau", "value", "valeur", "close", "level", "obs_value", "last", "px_last")


def _norm(name: Any) -> str:
    return str(name).strip().lower()


def _pick(columns: Sequence[str], explicit: str | None, candidates: Sequence[str]) -> str | None:
    if explicit:
        for c in columns:
            if _norm(c) == _norm(explicit):
                return c
        raise DataProviderError(f"column {explicit!r} not found in {list(columns)}")
    for name in candidates:
        for c in columns:
            if _norm(c) == name:
                return c
    return None


def _numbers(values: pd.Series) -> pd.Series:
    if not pd.api.types.is_numeric_dtype(values):
        values = (values.astype(str).str.replace(" ", "", regex=False).str.replace(" ", "", regex=False)
                  .str.replace(",", ".", regex=False))
    return pd.to_numeric(values, errors="coerce")


def _dates(values: pd.Series) -> pd.DatetimeIndex:
    text = values.astype(str).str.strip()
    iso = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    other = pd.to_datetime(text[iso.isna()], dayfirst=True, errors="coerce", format="mixed")
    iso.loc[iso.isna()] = other
    return pd.DatetimeIndex(iso).normalize()


def read_table(path: Path, sheet: str | int | None = None) -> pd.DataFrame:
    """CSV (``,`` or ``;``) or Excel sheet as a raw frame."""
    if not path.is_file():
        raise DataProviderError(f"series file not found: {path}")
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        try:
            return pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
        except ImportError as exc:  # pragma: no cover - openpyxl missing
            raise DataProviderError("reading Excel files needs openpyxl (pip install openpyxl)") from exc
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        header = fh.readline()
    sep = ";" if header.count(";") > header.count(",") else ("\t" if "\t" in header else ",")
    return pd.read_csv(path, sep=sep, dtype=str, encoding="utf-8-sig")


def extract_series(frame: pd.DataFrame, column: str, *, date_column: str | None = None,
                   id_column: str | None = None, value_column: str | None = None) -> pd.Series:
    """One series (float, by date) out of a wide or long table."""
    cols = list(frame.columns)
    dcol = _pick(cols, date_column, DATE_NAMES) or cols[0]
    icol = _pick(cols, id_column, ID_NAMES)
    if icol is not None and column not in cols:
        vcol = _pick(cols, value_column, VALUE_NAMES)
        if vcol is None:
            raise DataProviderError(f"long table without value column (one of {VALUE_NAMES})")
        rows = frame[frame[icol].astype(str).str.strip() == column]
        values, idx = _numbers(rows[vcol]), _dates(rows[dcol])
    else:
        match = _pick(cols, column, ())
        if match is None:  # pragma: no cover - _pick raises first
            raise DataProviderError(f"column {column!r} not found")
        values, idx = _numbers(frame[match]), _dates(frame[dcol])
    s = pd.Series(values.to_numpy(dtype=float), index=idx)
    s = s[s.index.notna()].dropna().sort_index()
    s = s[~s.index.duplicated(keep="last")]   # a later correction of the same day wins
    s.index.name = "date"
    return s


class FileProvider(DataProvider):
    """Serves the ids mapped with a ``file:`` spec from local CSV / Excel files."""

    name = "file"
    capabilities = frozenset({Capability.HISTORY, Capability.QUOTES})

    def __init__(self, mapping: MarketDataMap | None = None, root: str | Path | None = None,
                 quote_lookback_days: int = 10) -> None:
        super().__init__(mapping, quote_lookback_days)
        self.root = Path(root) if root is not None else Path.cwd()
        self._tables: dict[tuple[Path, Any], pd.DataFrame] = {}

    def _table(self, spec: Mapping[str, Any]) -> pd.DataFrame:
        path = Path(str(spec["path"])).expanduser()
        if not path.is_absolute():
            path = self.root / path
        key = (path, spec.get("sheet"))
        if key not in self._tables:
            self._tables[key] = read_table(path, spec.get("sheet"))
        return self._tables[key]

    def get_history(self, ids: Sequence[str], start: date, end: date) -> pd.DataFrame:
        cols: dict[str, pd.Series] = {}
        failures: list[str] = []
        for mid in ids:
            spec = self.mapping.spec(mid, self.name)
            if spec is None:
                continue
            if "path" not in spec or "column" not in spec:
                failures.append(f"{mid}: file spec needs 'path' and 'column'")
                continue
            try:
                s = extract_series(self._table(spec), str(spec["column"]), date_column=spec.get("date_column"),
                                   id_column=spec.get("id_column"), value_column=spec.get("value_column"))
            except (DataProviderError, OSError, ValueError) as exc:
                failures.append(f"{mid}: {exc}")
                continue
            s = s.loc[pd.Timestamp(start):pd.Timestamp(end)]
            if not s.empty:
                cols[mid] = s * float(spec.get("scale", 1.0))
        if not cols:
            if failures:
                raise DataProviderError("file series failed – " + " | ".join(failures[:3]))
            return empty_history()
        if failures:
            logger.warning("file provider: %d id(s) failed, e.g. %s", len(failures), failures[0])
        out = pd.concat(cols, axis=1)
        out.index.name = "date"
        return out
