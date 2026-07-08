"""
pulldata() CSV loader and indexed lookup
=========================================

Scans a list of questions for ``pulldata('<name>', ...)`` references,
locates each ``<name>.csv`` in the configured search directories, loads
them via pandas, and exposes an indexed lookup callable shaped to match
the SurveyCTO ``pulldata`` signature:

    pulldata(csv_name, return_col, key_col, key_value) -> value | ''

Behaviour matches SurveyCTO's runtime:

- Static source (``pulldata('foo', ...)``): the CSV name is known at
  load time; missing CSVs → :class:`MissingPullDataError` on construction.
- Dynamic source (``pulldata(${var}, ...)``): the CSV name resolves only
  at row-generation time. The loader scans the ``data/`` directory at
  init for any ``*.csv`` it might need; unknown names at runtime return
  empty string (also matches SurveyCTO).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd


# Static-source pulldata call: pulldata('csv', ...)
_STATIC_RX = re.compile(r"pulldata\(\s*'([^']+)'\s*,", re.IGNORECASE)

# Dynamic-source pulldata call: pulldata(${var}, ...)
_DYNAMIC_RX = re.compile(r"pulldata\(\s*\$\{([^}]+)\}\s*,", re.IGNORECASE)

# search()-appearance choice source: search('csv', 'matches', ...). The choice
# list is populated from this CSV at runtime, so it is just as mandatory for a
# faithful simulation as a pulldata() call -- without it the select's real
# options don't exist and the simulated column is wrong.
_SEARCH_RX = re.compile(r"search\(\s*'([^']+)'", re.IGNORECASE)


class MissingPullDataError(RuntimeError):
    """Raised when a referenced pulldata CSV cannot be found in any search dir."""


class PullDataTable:
    """One loaded CSV with key-column → row-index lookups."""

    __slots__ = ("name", "df", "_indexes")

    def __init__(self, name: str, df: pd.DataFrame):
        self.name = name
        self.df = df
        # Lazy per-key-column lookups. {key_col: {key_value_str: row_index}}
        self._indexes: Dict[str, Dict[str, int]] = {}

    def lookup(self, col: str, key_col: str, key_value: Any) -> str:
        if col not in self.df.columns or key_col not in self.df.columns:
            return ""
        idx = self._indexes.get(key_col)
        if idx is None:
            # First-occurrence wins (matches SurveyCTO's documented
            # pulldata behaviour for duplicate keys). The dict
            # comprehension we previously used overwrote with the last
            # occurrence.
            idx = {}
            for i, v in enumerate(self.df[key_col].tolist()):
                k = _norm(v)
                if k not in idx:
                    idx[k] = i
            self._indexes[key_col] = idx
        row_i = idx.get(_norm(key_value))
        if row_i is None:
            return ""
        val = self.df.iloc[row_i][col]
        if pd.isna(val):
            return ""
        return str(val) if not isinstance(val, str) else val


def _norm(value: Any) -> str:
    """Canonical string form for key matching. Pandas reads numeric CSV
    cells as floats; SurveyCTO compares stringwise. We coerce so that
    ``42``, ``'42'``, ``42.0``, and ``'42.0'`` all match the same row."""
    if value is None:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return repr(value)
    s = str(value).strip()
    if not s:
        return ""
    # Strings that parse as floats normalise via the float branch so
    # the float-stored side ('1.0') and the string-stored side ('1')
    # land on the same key.
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    if f != f:  # NaN
        return ""
    if f.is_integer():
        return str(int(f))
    return repr(f)


def scan_pulldata_refs(questions: Iterable[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Walk every expression-bearing column in ``questions`` and collect
    every ``pulldata`` reference. Returns ``{"static": {names}, "dynamic": {vars}}``.

    Dynamic source names are the variable names appearing inside ``${...}``
    in the first arg position; resolution happens at row-generation time
    when those variables hold actual CSV-name values.

    ``search('csv', ...)`` appearances are folded into ``static``: the choice
    list of that select is populated from ``csv.csv``, so it is mandatory for a
    faithful simulation exactly like a ``pulldata()`` source.
    """
    static: Set[str] = set()
    dynamic: Set[str] = set()
    for q in questions:
        for key in ("relevance", "constraint", "calculation", "choice_filter"):
            expr = q.get(key)
            if not expr:
                continue
            for m in _STATIC_RX.finditer(expr):
                static.add(m.group(1))
            for m in _DYNAMIC_RX.finditer(expr):
                dynamic.add(m.group(1))
        # group_relevances is a list of strings
        for expr in q.get("group_relevances") or []:
            for m in _STATIC_RX.finditer(expr):
                static.add(m.group(1))
            for m in _DYNAMIC_RX.finditer(expr):
                dynamic.add(m.group(1))
        # search()-appearance choice sources are mandatory too
        appearance = q.get("appearance")
        if appearance:
            for m in _SEARCH_RX.finditer(appearance):
                static.add(m.group(1))
    return {"static": static, "dynamic": dynamic}


def load_pulldata_tables(
    questions: Iterable[Dict[str, Any]],
    search_dirs: List[Path],
) -> Dict[str, PullDataTable]:
    """Load every CSV referenced by ``pulldata()`` in ``questions``.

    Static-source refs are mandatory: when the form references
    ``pulldata('foo', ...)`` and ``foo.csv`` is not in any search dir,
    raises :class:`MissingPullDataError`. Anyone exercising a form for
    HFC dry-runs needs the pulldata sources -- the form is designed
    around them.

    For dynamic-source refs, we also greedy-load every ``*.csv`` in the
    search dirs so runtime lookups can resolve ``pulldata(${csv_var}, …)``
    against whatever the form happens to point at. Files that fail to
    parse are skipped with a warning.

    Returns ``{csv_name_without_ext: PullDataTable}``.
    """
    refs = scan_pulldata_refs(questions)
    tables: Dict[str, PullDataTable] = {}

    # 1. Static references — required
    missing: List[str] = []
    for name in sorted(refs["static"]):
        path = _find_csv(name, search_dirs)
        if path is None:
            missing.append(name)
            continue
        try:
            tables[name] = PullDataTable(name, _read_csv(path))
        except Exception as exc:
            raise MissingPullDataError(
                f"pulldata source {name!r} found at {path} but failed to "
                f"load: {exc}"
            ) from exc

    if missing:
        search_str = ", ".join(str(d) for d in search_dirs)
        raise MissingPullDataError(
            f"missing pulldata CSV(s): {', '.join(missing)}. "
            f"Searched: {search_str}. Drop the CSVs into one of those "
            f"directories."
        )

    # 2. Dynamic-source refs — best-effort, also greedily preload any other
    #    CSVs in the search dirs in case form variables point at them.
    if refs["dynamic"]:
        for d in search_dirs:
            if not d.exists():
                continue
            for csv_path in d.glob("*.csv"):
                stem = csv_path.stem
                if stem in tables:
                    continue
                try:
                    tables[stem] = PullDataTable(stem, _read_csv(csv_path))
                except Exception as exc:
                    print(
                        f"  [pulldata] WARNING: failed to load {csv_path}: {exc}"
                    )

    return tables


def make_lookup(tables: Dict[str, PullDataTable]):
    """Return a callable matching the SurveyCTO ``pulldata`` signature."""

    def _lookup(csv_name: str, col: str, key_col: str, key_value: Any) -> str:
        tbl = tables.get(str(csv_name).strip())
        if tbl is None:
            return ""
        return tbl.lookup(str(col).strip(), str(key_col).strip(), key_value)

    return _lookup


def _find_csv(name: str, search_dirs: List[Path]) -> Optional[Path]:
    for d in search_dirs:
        candidate = d / f"{name}.csv"
        if candidate.exists():
            return candidate
    return None


def _read_csv(path: Path) -> pd.DataFrame:
    """Read a pulldata CSV with safe defaults. Keep everything as object
    so numeric-looking IDs aren't silently coerced (SurveyCTO treats them
    as text)."""
    return pd.read_csv(path, dtype=object, keep_default_na=False, na_values=[""])
