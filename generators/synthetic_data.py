"""
Synthetic SurveyCTO-export CSV generator
========================================

Walks a parsed SurveyCTO form (``<survey>_questions.json``) and emits a
wide CSV that mirrors what SurveyCTO will export when real submissions
arrive. The output's purpose is HFC and cleaning-pipeline dry-runs
*before* real data — same columns, same skip-induced blanks, same
``pulldata``-populated cells, same calculate-derived values.

Per respondent the walker:

1. Iterates ``questions.json`` in order, maintaining a partial row dict.
2. Resolves repeat counts dynamically (the count variable's value is
   only known after upstream questions have been answered).
3. Composes effective relevance as (all group_relevances ∧ own relevance)
   and skips the question — leaves the cell blank — when false.
4. For ``calculate`` questions, evaluates the calculation against the
   current row state via :mod:`transformers.expression_evaluator`.
5. For other types, samples a type-correct value via
   :mod:`generators.sampling`, respecting any extractable constraint
   bounds and the narrowed choice list.
6. ``select_multiple`` is unrolled to both the parent ``"v1 v2"`` cell
   AND per-choice indicator columns (``var_<value>`` with 0/1) — the
   exact wide-export shape SurveyCTO produces.
7. Inside a repeat group, variables are wide-suffixed (``var_1, var_2,
   ...``). Per-iteration ``index()`` is exposed to the evaluator so
   constraint expressions that branch on iteration work correctly.
   Nested repeats expand over the full enclosing chain (``var_1_1,
   var_1_2, ...``) with one inner count per outer iteration
   (``inner_count_1, ...``); see the suffix model below.

Output: ``<output_dir>/<survey>_synthetic.csv`` + a strip-log
(``<survey>_synthetic.strip.log``) listing expressions the evaluator
could not interpret. CLI in ``main.py`` exposes ``--phases synthetic``
with ``--rows`` and ``--seed`` flags.
"""

from __future__ import annotations

import csv
import datetime
import json
import math
import random
import re
import uuid
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from extractors.pulldata_loader import (
    MissingPullDataError,
    load_pulldata_tables,
    make_lookup,
)
from generators.sampling import (
    format_date_for_csv,
    format_datetime_for_csv,
    format_time_for_csv,
    numeric_bounds,
    sample_python_value,
)
from transformers.expression_evaluator import EvalContext, safe_evaluate


# ── SurveyCTO export metadata columns ─────────────────────────────────────────

SURVEYCTO_METADATA_COLS = [
    "CompletionDate",
    "SubmissionDate",
    "starttime",
    "endtime",
    "deviceid",
    "subscriberid",
    "simid",
    "devicephonenum",
    "username",
    "duration",
    "caseid",
]

KEY_COL = "KEY"
FORMDEF_VERSION_COL = "formdef_version"


# Question types whose values come from device / session metadata rather
# than respondent input. Multi-word SurveyCTO types like ``text audit``
# are split by ``parsers/type_parser.py`` into ``(base_type, choice_list)``
# (e.g. ``("text", "audit")``). Detect them via ``(type, choice_list)``
# tuples; the single-word entries pair with ``None``.
_METADATA_TYPE_PAIRS: Set[Tuple[str, Optional[str]]] = {
    ("start", None), ("end", None), ("today", None),
    ("deviceid", None), ("subscriberid", None), ("simserial", None),
    ("simid", None), ("phonenumber", None), ("devicephonenum", None),
    ("username", None), ("caseid", None),
    ("text", "audit"), ("audio", "audit"),
    ("speed", "violations count"), ("speed", "violations list"),
}


def _metadata_kind(question: Dict[str, Any]) -> Optional[str]:
    """Return a discriminator string for metadata-type questions, or None.

    Returns the original SurveyCTO type (e.g. ``"text audit"``) so the
    value synthesiser can pick the right shape (audit fields get media
    filenames, ``today``/``start``/``end`` get datestrings, etc.).
    """
    t = question.get("type", "") or ""
    cl = question.get("choice_list")
    if (t, None) in _METADATA_TYPE_PAIRS:
        return t
    if cl and (t, cl) in _METADATA_TYPE_PAIRS:
        return f"{t} {cl}"
    return None

# Types whose columns appear in the SurveyCTO export but aren't typically
# populated by the respondent: emit the column, leave it blank.
_BLANK_TYPES: Set[str] = {"comments"}


# ── Walk state ────────────────────────────────────────────────────────────────

@dataclass
class WalkResult:
    """One respondent's row plus the per-repeat-group iteration counts
    actually used (so the column unroller knows how wide to be)."""

    row: Dict[str, Any] = field(default_factory=dict)
    repeat_counts: Dict[str, int] = field(default_factory=dict)


@dataclass
class StripLog:
    entries: List[Tuple[str, str, str, str]] = field(default_factory=list)
    """[(variable_name, expression_kind, expression, error_message)]"""

    def record(self, var_name: str, kind: str, expr: str, exc: Exception) -> None:
        self.entries.append((var_name, kind, expr, str(exc)))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(f"# Synthetic-data evaluator strip-log\n")
            f.write(f"# {len(self.entries)} expressions could not be evaluated.\n\n")
            for var, kind, expr, msg in self.entries:
                f.write(f"[{var}] {kind}: {expr!r}\n  -> {msg}\n")


# ── Run-context for metadata + ids ────────────────────────────────────────────

@dataclass
class RunContext:
    """Per-respondent injected values for metadata-type questions."""

    submission_date: datetime.datetime
    start_time: datetime.datetime
    end_time: datetime.datetime
    deviceid: str
    subscriberid: str
    simid: str
    devicephonenum: str
    username: str
    duration: int
    caseid: str
    key: str
    formdef_version: str
    # Form-level identifiers used to build SurveyCTO-shaped audit-URL
    # values. Both fall back to a placeholder when the form's settings
    # sheet doesn't carry the field.
    form_id: str = "synthetic_form"
    server_host: str = "synthetic.surveycto.com"
    # Mutable per-respondent counter: snapshots of elapsed seconds for
    # successive ``once(duration())`` calls inside ``calculate_here``.
    # Real SurveyCTO returns the cumulative seconds AT THE MOMENT the
    # field is first reached, so each downstream calc_here yields a
    # larger value than upstream ones. Walker advances this; subtraction
    # between two snapshots gives module duration.
    duration_progress: int = 0


def _build_run_context(
    rng: random.Random, index: int, caseid: Optional[str] = None,
    form_settings: Optional[Dict[str, Any]] = None,
) -> RunContext:
    base_dt = datetime.datetime(2026, 5, 11, 9, 0, 0)
    start = base_dt + datetime.timedelta(
        days=rng.randint(0, 30), seconds=rng.randint(0, 8 * 3600)
    )
    duration = rng.randint(600, 7200)
    end = start + datetime.timedelta(seconds=duration)
    submission = end + datetime.timedelta(seconds=rng.randint(0, 600))
    device_n = rng.randint(100000, 999999)
    # Prefer the real settings.version when available so synth's
    # ``formdef_version`` matches what the form authors stamped (and
    # what downstream HFC code may filter on); otherwise sample a
    # recent YYYYMMDD.
    settings = form_settings or {}
    settings_version = (settings.get("version") or "").strip() if settings else ""
    if settings_version:
        # SurveyCTO normalises the value in the export; strip a ``.0``
        # suffix in case pandas read it as a float.
        if settings_version.endswith(".0"):
            settings_version = settings_version[:-2]
        formdef_version_val = settings_version
    else:
        formdef_version_val = (
            base_dt - datetime.timedelta(days=rng.randint(0, 3 * 365))
        ).strftime("%Y%m%d")
    form_id = (settings.get("form_id") or "synthetic_form").strip() if settings else "synthetic_form"
    return RunContext(
        submission_date=submission,
        start_time=start,
        end_time=end,
        deviceid=f"collect:{device_n}",
        subscriberid=str(rng.randint(10**14, 10**15 - 1)),
        simid=str(rng.randint(10**14, 10**15 - 1)),
        devicephonenum=f"+1555{rng.randint(1000000, 9999999)}",
        username=f"enum{rng.randint(1, 50):03d}",
        duration=duration,
        caseid=caseid if caseid is not None else f"case-{index:04d}",
        # version=4 sets the RFC 4122 variant/version bits; real SurveyCTO
        # KEY values are v4 UUIDs and HFC checks may assert this.
        key=f"uuid:{uuid.UUID(int=rng.getrandbits(128), version=4)}",
        formdef_version=formdef_version_val,
        form_id=form_id,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

# pulldata('table', 'col', 'key_col', ${caseid}) — capture (table, key_col)
# for any call whose lookup expression is the form's caseid variable. Accept
# both ``${caseid}`` and ``${case_id}`` (the underscore variant some forms use).
_CASEID_PULLDATA_RX = re.compile(
    r"""pulldata\(\s*['"]([^'"]+)['"]\s*,\s*['"][^'"]+['"]\s*,\s*"""
    r"""['"]([^'"]+)['"]\s*,\s*\$\{\s*case_?id\s*\}\s*\)""",
    re.IGNORECASE,
)


def _build_caseid_pool(
    questions: List[Dict[str, Any]],
    tables: Dict[str, Any],
) -> List[str]:
    """Collect caseid candidates from the pulldata table the form looks up
    with ``${caseid}`` as the key. Returns unique stringified key values,
    or ``[]`` if no caseid-keyed pulldata reference exists or the table
    didn't load.

    If the form references multiple ``(table, key_col)`` pairs (rare but
    possible — e.g. a form that pulls from both ``cases.csv`` and
    ``preloads.csv`` keyed on ``${caseid}``), we use only the first pair
    encountered in form-order. Merging pools across tables would produce
    caseids unresolvable in some of them (a caseid from ``cases.csv``
    looked up against ``preloads.csv`` returns blank cells with no
    explanation). A stderr warning lists the ignored extras.
    """
    pairs: List[Tuple[str, str]] = []
    seen_pairs: Set[Tuple[str, str]] = set()
    for q in questions:
        for field_name in ("calculation", "relevance", "constraint", "choice_filter"):
            expr = q.get(field_name)
            if not expr:
                continue
            for m in _CASEID_PULLDATA_RX.finditer(str(expr)):
                pair = (m.group(1), m.group(2))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    pairs.append(pair)
    if not pairs:
        return []
    chosen_tbl, chosen_col = pairs[0]
    if len(pairs) > 1:
        import sys
        extras = ", ".join(f"{t}[{c}]" for t, c in pairs[1:])
        print(
            f"[WARN] Multiple caseid-keyed pulldata tables found; using "
            f"{chosen_tbl}[{chosen_col}], ignoring: {extras}",
            file=sys.stderr,
        )
    tbl = tables.get(chosen_tbl) if tables else None
    if tbl is None or chosen_col not in tbl.df.columns:
        return []
    pool: List[str] = []
    seen: Set[str] = set()
    for v in tbl.df[chosen_col].tolist():
        if v is None:
            continue
        if isinstance(v, float) and math.isnan(v):
            continue
        s = str(v).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        pool.append(s)
    return pool


# ── search() pulldata choice expansion ────────────────────────────────────────
# SurveyCTO's ``search('CSV', 'matches', col, val[, col, val]*)`` appearance
# directive populates a select's choices at run time from a media-bundle
# CSV. The static XLSForm choice list typically contains a placeholder row
# (e.g. ``peer_id`` / ``peer_name``) plus a few sentinel rows (``0``, ``-88``).
# At run time SurveyCTO replaces the placeholder with rows from the CSV,
# using the placeholder row's ``value`` and ``label`` text as the names of
# the CSV columns to read.

_SEARCH_HEAD_RX = re.compile(r"search\s*\(", re.IGNORECASE)


def _extract_search_call(appearance: str) -> Optional[Tuple[str, str]]:
    """Locate the first ``search(...)`` call in ``appearance`` and return
    ``(csv_name, raw_args)`` where ``raw_args`` is the comma-separated
    text **after** the CSV-name argument, with the outer ``search(`` and
    matching ``)`` stripped.

    Walks the argument list paren-aware so nested calls (e.g.
    ``search('roster', 'matches', col, if(${x}=1, ${y}, 0))``) are not
    truncated at the first inner ``)``. Returns ``None`` if no search
    call is present or the parentheses are unbalanced.
    """
    head = _SEARCH_HEAD_RX.search(appearance)
    if not head:
        return None
    i = head.end()
    n = len(appearance)
    depth = 1
    in_str: Optional[str] = None
    body_start = i
    while i < n and depth > 0:
        ch = appearance[i]
        if in_str:
            if ch == in_str and appearance[i - 1] != "\\":
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return None
    body = appearance[body_start:i]
    # First top-level comma separates the CSV name from the rest.
    parts = _split_top_level(body, ",")
    if not parts:
        return None
    csv_name = parts[0].strip().strip("'\"")
    raw_args = ",".join(parts[1:]).strip()
    return (csv_name, raw_args)


def _split_top_level(s: str, sep: str) -> List[str]:
    """Split ``s`` on ``sep`` only when the separator is at paren depth 0
    and not inside a quoted string. Preserves nested expressions intact."""
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    in_str: Optional[str] = None
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            buf.append(ch)
            if ch == in_str and (i == 0 or s[i - 1] != "\\"):
                in_str = None
        elif ch in ("'", '"'):
            buf.append(ch)
            in_str = ch
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


def _parse_search_appearance(appearance: Optional[str]) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """Parse a ``search('csv', 'matches', col, val, col, val, ...)`` directive.

    Returns ``(csv_name, [(col, value_expr), ...])`` or ``None`` if the
    appearance doesn't contain a search call. The 'matches' literal is
    optional in SurveyCTO syntax for a no-filter search; we accept both
    bare ``search('csv')`` and the filtered form. Value expressions are
    returned verbatim so the caller can evaluate ``${var}`` refs against
    the current row state. Nested function calls inside value
    expressions (e.g. ``if(${x}=1, ${y}, 0)``) are preserved intact.
    """
    if not appearance:
        return None
    extracted = _extract_search_call(str(appearance))
    if extracted is None:
        return None
    csv_name, raw_args = extracted
    matches: List[Tuple[str, str]] = []
    if raw_args:
        # Strip a leading "'matches'," if present (it's the SurveyCTO
        # search-mode token that precedes the col/val pairs).
        raw_args = re.sub(r"^\s*['\"]matches['\"]\s*,?\s*", "", raw_args)
        parts = [p.strip() for p in _split_top_level(raw_args, ",") if p.strip()]
        for i in range(0, len(parts) - 1, 2):
            col = parts[i].strip().strip("'\"")
            val = parts[i + 1].strip()
            # Strip outer quotes only when they wrap the entire expr; a
            # nested expr like ``if(${x}=1, '${y}', 0)`` must stay verbatim.
            if (len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]
                    and "(" not in val):
                val = val[1:-1]
            matches.append((col, val))
    return (csv_name, matches)


def _resolve_search_choices(
    question: Dict[str, Any],
    tables: Dict[str, Any],
    row: Optional[Dict[str, Any]] = None,
    apply_matches: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """If ``question`` has a ``search()`` appearance and the named CSV is
    loaded, return a list of synthetic choice dicts derived from CSV rows.

    The placeholder choice row (typically the first non-numeric-value
    entry in the static choice list) is replaced by one ``{value, label}``
    per CSV row, using the placeholder's ``value``/``label`` fields as
    the CSV column names to read. Sentinel rows (``0``, ``-88``, ...) are
    preserved.

    When ``apply_matches`` is True, the per-respondent ``matches`` filter
    is evaluated against ``row`` and only matching CSV rows are kept.
    Set False at column-order-build time to get the full possible
    indicator-column space.
    """
    parsed = _parse_search_appearance(question.get("appearance"))
    if not parsed:
        return None
    csv_name, matches = parsed
    table = tables.get(csv_name) if tables else None
    if table is None:
        return None

    df = table.df
    static_choices = question.get("choices") or []
    # Find the placeholder row: a static row whose ``value`` field is a
    # column name in the CSV. That field name doubles as the CSV column
    # to read for synthetic choice values; ``label`` does the same for
    # labels. Sentinels (rows whose ``value`` isn't a CSV column) survive.
    placeholder = None
    sentinels: List[Dict[str, Any]] = []
    csv_cols = set(df.columns)
    for c in static_choices:
        v = str(c.get("value", "")).strip()
        if placeholder is None and v in csv_cols:
            placeholder = c
        else:
            sentinels.append(c)
    if placeholder is None:
        return None
    val_col = str(placeholder["value"]).strip()
    lbl_col = str(placeholder.get("label", val_col)).strip()
    if lbl_col not in csv_cols:
        lbl_col = val_col

    # Apply ``matches`` against the row state. Skip clauses whose value
    # expression doesn't resolve (e.g. unsupported function) so we don't
    # silently filter to nothing.
    sub = df
    if apply_matches and matches and row is not None:
        for col, val_expr in matches:
            if col not in df.columns:
                continue
            resolved = _resolve_match_value(val_expr, row)
            if resolved is None:
                continue
            sub = sub[sub[col].astype(str).str.strip() == str(resolved).strip()]
            if len(sub) == 0:
                break

    dynamic: List[Dict[str, Any]] = []
    for _, r in sub.iterrows():
        v = r[val_col]
        if v is None:
            continue
        if isinstance(v, float) and math.isnan(v):
            continue
        sv = str(v).strip()
        if not sv:
            continue
        sl = "" if lbl_col == val_col else str(r.get(lbl_col, "")).strip()
        dynamic.append({"value": sv, "label": sl})
    # Sentinels at the end so they don't dominate the indicator column
    # ordering of the export header.
    return dynamic + sentinels


def _resolve_match_value(expr: str, row: Dict[str, Any]) -> Optional[str]:
    """Resolve a search() match-value expression against the current row.

    Handles bare quoted/unquoted literals and ``${var}`` references.
    Returns None for unresolvable expressions (nested function calls
    like ``if(...)``, ``concat(...)``) so the caller can skip the
    clause rather than filter against a raw expression string.
    """
    s = str(expr).strip()
    if not s:
        return None
    m = re.fullmatch(r"\$\{\s*([A-Za-z_][\w]*)\s*\}", s)
    if m:
        v = row.get(m.group(1), "")
        return str(v) if v not in (None, "") else None
    # A bare literal (already unquoted by _parse_search_appearance, or a
    # number) — usable as-is. Anything containing parens / operators is a
    # nested expression we can't evaluate here.
    if "(" in s or ")" in s:
        return None
    return s


# ── Nested-repeat suffix model ─────────────────────────────────────────────────
# A field's wide-export column suffix is the chain of iteration indices of ALL
# its enclosing repeat groups, outermost-first: a field in repeats [outer, inner]
# at iterations (o, i) is column ``var_o_i``. A repeat group's ``repeat_count``
# variable lives at its PARENT level (its own ``group_path`` excludes the repeat
# it counts), so it carries only the ancestor suffix: the inner count is
# ``inner_count_o`` (one per outer iteration); a top-level count is unsuffixed.
#
# This mirrors the resolver in ``create_variable_dictionaries._build_repeat_tree``
# (depth → ``_{p1}_..._{iter}`` suffix pattern), so the simulated wide-export
# contract matches what the variable dictionary reconstructs from real data.


def _repeat_chain(group_path: List[str], repeats: Set[str]) -> List[str]:
    """Ordered list of enclosing repeat groups for a field, outermost-first.

    Filters ``group_path`` (which lists every enclosing group, repeats and
    plain groups alike) down to just the repeat groups. The length is the
    field's repeat nesting depth; the list drives both suffix construction
    and per-iteration expansion.
    """
    return [g for g in (group_path or []) if g in repeats]


def _suffix(combo: Tuple[int, ...]) -> str:
    """``(2, 3) -> '_2_3'``; ``() -> ''`` (top-level, no suffix)."""
    return "".join(f"_{i}" for i in combo)


def _enumerate_combos(
    chain: List[str], counts: Dict[str, Dict[Tuple[int, ...], int]]
) -> Iterator[Tuple[int, ...]]:
    """Yield every iteration-index tuple for a field's repeat ``chain`` using
    the per-respondent resolved ``counts``.

    ``counts[R][prefix]`` is the resolved iteration count for repeat ``R``
    under the ancestor-index ``prefix`` (the tuple of indices of R's
    enclosing repeats). Counts are therefore ragged: each outer iteration
    can have its own inner count. An empty chain yields a single empty
    tuple (top-level field, no expansion).
    """

    def _rec(prefix: Tuple[int, ...], depth: int) -> Iterator[Tuple[int, ...]]:
        if depth == len(chain):
            yield prefix
            return
        n = counts.get(chain[depth], {}).get(prefix, 0)
        for i in range(1, n + 1):
            yield from _rec(prefix + (i,), depth + 1)

    yield from _rec((), 0)


def _grid_combos(
    chain: List[str], max_iter: Dict[str, int]
) -> Iterator[Tuple[int, ...]]:
    """Yield the rectangular column grid for a field's repeat ``chain``.

    Unlike the per-respondent (ragged) :func:`_enumerate_combos`, the column
    header is rectangular: every enclosing repeat ``R`` contributes
    ``1..max_iter[R]`` independently (``product`` over levels), matching how
    SurveyCTO pads its wide export. Outer indices vary slowest, so the
    columns come out ``var_1_1, var_1_2, ..., var_2_1, ...``.
    """
    if not chain:
        yield ()
        return
    ranges = [range(1, max_iter.get(c, 1) + 1) for c in chain]
    yield from product(*ranges)


def _is_relevant(
    question: Dict[str, Any],
    ctx: EvalContext,
    strip_log: StripLog,
    var_label: str,
) -> bool:
    """Effective relevance = all group_relevances ∧ own relevance.

    Errors / unsupported functions default to True (show the question),
    matching the "structurally include rather than silently hide" principle.
    """

    def _on_err(expr: str, exc: Exception) -> None:
        strip_log.record(var_label, "relevance", expr, exc)

    for gr in question.get("group_relevances") or []:
        if gr is None or str(gr).strip() == "":
            continue
        v = safe_evaluate(gr, ctx, default=True, on_error=_on_err)
        if not _truthy(v):
            return False
    rel = question.get("relevance")
    if rel is None or str(rel).strip() == "":
        return True
    v = safe_evaluate(rel, ctx, default=True, on_error=_on_err)
    return _truthy(v)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v) and v == v
    if isinstance(v, str):
        return v != "" and v.lower() != "false"
    return v is not None


def _format_for_csv(value: Any) -> str:
    """Coerce a typed sampled value into the string form SurveyCTO would
    write to its export CSV."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime.datetime):
        return format_datetime_for_csv(value)
    if isinstance(value, datetime.date):
        return format_date_for_csv(value)
    if isinstance(value, datetime.time):
        return format_time_for_csv(value)
    if isinstance(value, float):
        # SurveyCTO writes empty cells for undefined calculates; NaN
        # propagating from division-by-zero must not surface as the
        # literal token "nan".
        if value != value:  # NaN check
            return ""
        if value.is_integer():
            return str(int(value))
        # ``%.10g`` preserves significant digits for very small numbers
        # (``0.00001`` would otherwise truncate to ``"0"`` via ``%.4f``)
        # while still trimming trailing zeros for normal-magnitude floats.
        return f"{value:.10g}"
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v != "")
    return str(value)


def _resolve_repeat_count(
    calc: Optional[str],
    ctx: EvalContext,
    strip_log: StripLog,
    var_name: str,
    rng: random.Random,
    constraint: Optional[str],
) -> int:
    """Pick an iteration count for a repeat group at row-generation time.

    Order: explicit literal in calculation → evaluated expression
    referencing prior answers → sampled within constraint bounds (or
    1..5 fallback)."""
    if calc:
        s = calc.strip()
        if s.lstrip("-").isdigit():
            return max(1, int(s))
        try:
            v = safe_evaluate(s, ctx, default=None,
                              on_error=lambda e, exc: strip_log.record(
                                  var_name, "repeat_count_calc", e, exc))
            if v is not None:
                try:
                    iv = int(float(v))
                    if iv >= 0:
                        return max(1, iv)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
    lo, hi = numeric_bounds(constraint)
    lo_i = int(lo) if lo is not None else 1
    hi_i = int(hi) if hi is not None else 5
    if lo_i > hi_i:
        lo_i, hi_i = hi_i, lo_i
    lo_i = max(1, lo_i)
    return rng.randint(lo_i, max(lo_i, hi_i))


def _metadata_value(q_type: str, q_name: str, runctx: RunContext, rng: random.Random) -> str:
    if q_type == "start":
        return runctx.start_time.strftime("%b %d, %Y %I:%M:%S %p")
    if q_type == "end":
        return runctx.end_time.strftime("%b %d, %Y %I:%M:%S %p")
    if q_type == "today":
        return runctx.submission_date.strftime("%Y-%m-%d")
    if q_type == "deviceid":
        return runctx.deviceid
    if q_type == "subscriberid":
        return runctx.subscriberid
    if q_type in ("simserial", "simid"):
        return runctx.simid
    if q_type in ("phonenumber", "devicephonenum"):
        return runctx.devicephonenum
    if q_type == "username":
        return runctx.username
    if q_type == "caseid":
        return runctx.caseid
    # Audit fields are exported by SurveyCTO as full attachment URLs
    # of the form:
    #   text audit:  https://<host>/api/v2/forms/<form_id>/submissions/<key>/attachments/TA_<uuid>.csv
    #   audio audit: https://<host>/api/v2/forms/<form_id>/submissions/<key>/attachments/AA_<uuid>_AFTER_<seconds>S.m4a
    # ``key`` has the form ``uuid:<uuid>``; we strip the ``uuid:`` prefix
    # when embedding it in the bare ``TA_`` / ``AA_`` filename to match
    # the empirical format observed in real SurveyCTO production exports.
    key_uuid = runctx.key[5:] if runctx.key.startswith("uuid:") else runctx.key
    base = f"https://{runctx.server_host}/api/v2/forms/{runctx.form_id}/submissions/{runctx.key}/attachments"
    if q_type == "text audit":
        return f"{base}/TA_{key_uuid}.csv"
    if q_type == "audio audit":
        # Real exports include the second-offset when audit was captured.
        # We don't know which audio_audit instance this is, so randomise
        # within the respondent's duration window.
        secs = rng.randint(60, max(60, runctx.duration))
        return f"{base}/AA_{key_uuid}_AFTER_{secs}S.m4a"
    if q_type in ("speed violations count",):
        return "0"
    if q_type == "speed violations list":
        return ""
    return ""


# ── Per-respondent walk ───────────────────────────────────────────────────────

def _walk_one(
    questions: List[Dict[str, Any]],
    repeats: Set[str],
    rng: random.Random,
    runctx: RunContext,
    pulldata_lookup: Optional[Callable[[str, str, str, Any], Any]],
    choices_lookup: Optional[Dict[str, List[Dict[str, Any]]]],
    var_to_choice_list: Optional[Dict[str, str]],
    strip_log: StripLog,
    force_values: Optional[Dict[str, str]] = None,
    geo_bbox: Optional[Tuple[float, float, float, float]] = None,
    pulldata_tables: Optional[Dict[str, Any]] = None,
) -> WalkResult:
    """Generate one respondent's row.

    ``force_values`` maps ``variable_name -> string value``. Forced
    variables **bypass their own relevance check** so a gated cascade
    populates regardless of upstream sampling (the typical use case is
    pinning a consent variable so the consent-gated section fills in
    every row). The forced value is pre-seeded into the row dict at
    walk start so questions earlier in the form whose relevance
    references the forced variable see the value at evaluation time.

    Repeat-scope caveat: pre-seeding writes ``row[fv_name]`` only
    (unsuffixed). Inside a repeat iteration ``i``, ``ctx.get_var`` tries
    ``row[fv_name_i]`` first and falls back to the unsuffixed value when
    the suffixed key is absent — so an in-repeat question whose
    relevance references a force-targeted variable that's normally
    defined per-iteration will see the pre-seeded value as a fallback.
    Acceptable in practice because force_values are typically used on
    top-level variables (consent flags), not repeat-scoped ones.

    ``geo_bbox`` overrides the global default for geopoint sampling.
    """

    row: Dict[str, Any] = {}
    # Per-respondent resolved repeat counts, keyed by repeat name then by the
    # ancestor-index tuple (the indices of the repeat's enclosing repeats).
    # Top-level repeats key on the empty tuple ``()``; an inner repeat nested
    # under one outer level keys on ``(o,)`` so each outer iteration can carry
    # its own inner count (ragged rosters).
    counts: Dict[str, Dict[Tuple[int, ...], int]] = {}
    force_values = force_values or {}
    # Pre-seed force_values into the row so questions earlier in the
    # form that reference a force-targeted variable in their ``relevance``
    # see the forced value (e.g. ``text_audit`` at position 4 referencing
    # ``${consentsurvey}=1`` where ``consentsurvey`` is at position 102).
    # The walker still re-asserts the forced value when it reaches the
    # variable's own position, so this is idempotent.
    for _fv_name, _fv_value in force_values.items():
        row[_fv_name] = _fv_value

    def _make_ctx(current_var: Optional[str], repeat_stack: List[Tuple[str, int]]):
        return EvalContext(
            row=row,
            choices=choices_lookup,
            pulldata_lookup=pulldata_lookup,
            repeat_stack=list(repeat_stack),
            rng=rng,
            now=runctx.submission_date,
            current_var=current_var,
            var_to_choice_list=var_to_choice_list,
            duration_secs=runctx.duration,
        )

    for q in questions:
        q_type = q.get("type", "")
        var_name = q.get("variable_name", "")
        if not var_name:
            continue
        if q_type in _BLANK_TYPES:
            row[var_name] = ""
            continue
        group_path = q.get("group_path") or []
        choices = q.get("choices") or []
        constraint = q.get("constraint")
        # Expand search()-appearance choice lists from pulldata. Done per
        # respondent so the matches-clause filter sees the current row.
        # Falls back to the static list when no search() applies or the
        # named CSV isn't loaded.
        if q_type in ("select_one", "select_multiple") and pulldata_tables and q.get("appearance"):
            dynamic = _resolve_search_choices(q, pulldata_tables, row=row, apply_matches=True)
            if dynamic:
                choices = dynamic

        # ── repeat_count: resolve N once per ancestor iteration. A top-level
        # repeat resolves a single count (key ``()``); an inner repeat nested
        # under outer repeats resolves one count per outer-iteration tuple, so
        # the count column is wide-suffixed by the ancestors (``inner_count_o``).
        # If a repeat is gated by a relevance that evaluated false for some
        # combination, that count is 0 and the block contributes nothing there.
        if q_type == "repeat_count":
            repeat_name = q.get("repeat_group_name") or ""
            ancestors = _repeat_chain(group_path, repeats)
            slot = counts.setdefault(repeat_name, {})
            for combo in _enumerate_combos(ancestors, counts):
                col = f"{var_name}{_suffix(combo)}"
                ctx = _make_ctx(col, list(zip(ancestors, combo)))
                if not _is_relevant(q, ctx, strip_log, col):
                    slot[combo] = 0
                    row[col] = ""
                    continue
                n = _resolve_repeat_count(
                    q.get("calculation"), ctx, strip_log, col, rng, constraint,
                )
                slot[combo] = n
                row[col] = n
            continue

        # ── Variables inside one or more repeats: expand over the full chain
        # of enclosing repeats. A field in [outer, inner] gets a column per
        # (outer, inner) iteration pair, keyed ``var_o_i``; a singly-nested
        # field keeps the original ``var_i`` shape.
        chain = _repeat_chain(group_path, repeats)
        if chain:
            forced = force_values.get(var_name)
            for combo in _enumerate_combos(chain, counts):
                suffix = _suffix(combo)
                key = f"{var_name}{suffix}"
                ctx = _make_ctx(key, list(zip(chain, combo)))
                # --force-value bypasses relevance: the user is explicitly
                # asking for the variable to be populated, so we treat it
                # as relevant and write the forced value. This is the only
                # way to make a gated-cascade actually populate when the
                # gating ancestor would have randomly evaluated false.
                if forced is None and not _is_relevant(q, ctx, strip_log, key):
                    row[key] = ""
                    if q_type == "select_multiple":
                        for c in choices:
                            cv = str(c.get("value", "")).strip()
                            if not cv:
                                continue
                            row[f"{var_name}_{_choice_suffix(cv)}{suffix}"] = ""
                    continue
                if var_name in force_values:
                    value = force_values[var_name]
                else:
                    value = _compute_value(
                        q, q_type, choices, constraint, ctx, rng, runctx,
                        strip_log, key, geo_bbox,
                    )
                if q_type == "select_multiple":
                    # Store the formatted space-separated string in the row
                    # so downstream `selected(${var}, X)` inside the same
                    # repeat (which resolves `${var}` -> the suffixed key via
                    # the repeat stack) can tokenise it correctly. The column
                    # builder omits this parent cell from CSV; it's
                    # internal-only.
                    csv_str = _format_for_csv(value)
                    row[key] = csv_str
                    selected_tokens = set(csv_str.split())
                    for c in choices:
                        cv = str(c.get("value", "")).strip()
                        if not cv:
                            continue
                        bin_key = f"{var_name}_{_choice_suffix(cv)}{suffix}"
                        row[bin_key] = 1 if cv in selected_tokens else 0
                else:
                    row[key] = value
            continue

        # ── Top-level variables (no repeat)
        ctx = _make_ctx(var_name, [])
        forced = force_values.get(var_name)
        # --force-value bypasses relevance (see in-repeat branch above).
        if forced is None and not _is_relevant(q, ctx, strip_log, var_name):
            row[var_name] = ""
            if q_type == "select_multiple":
                for c in choices:
                    cv = str(c.get("value", "")).strip()
                    if not cv:
                        continue
                    row[f"{var_name}_{_choice_suffix(cv)}"] = ""
            continue
        if forced is not None:
            value = forced
        else:
            value = _compute_value(
                q, q_type, choices, constraint, ctx, rng, runctx,
                strip_log, var_name, geo_bbox,
            )
        if q_type == "select_multiple":
            # Store the formatted space-separated string (not the raw list)
            # so downstream `selected(${var}, X)` calls tokenise correctly
            # via `_to_string(...) .split()`.
            csv_str = _format_for_csv(value)
            row[var_name] = csv_str
            selected_tokens = set(csv_str.split())
            for c in choices:
                cv = str(c.get("value", "")).strip()
                if not cv:
                    continue
                bin_key = f"{var_name}_{_choice_suffix(cv)}"
                row[bin_key] = 1 if cv in selected_tokens else 0
        else:
            row[var_name] = value

    # Collapse the ragged per-ancestor counts to the max iterations seen for
    # each repeat in this respondent. Pass 1 aggregates these into the global
    # ``max_iter`` that sizes the rectangular column grid.
    repeat_counts = {
        r: (max(slot.values()) if slot else 0) for r, slot in counts.items()
    }
    return WalkResult(row=row, repeat_counts=repeat_counts)


def _choice_suffix(value: str) -> str:
    """SurveyCTO renames negative choice values: ``-99`` → ``_99`` so the
    multi-select indicator column becomes ``var__99`` (double-underscore)."""
    return value.replace("-", "_")


def _compute_value(
    question: Dict[str, Any],
    q_type: str,
    choices: List[Dict[str, Any]],
    constraint: Optional[str],
    ctx: EvalContext,
    rng: random.Random,
    runctx: RunContext,
    strip_log: StripLog,
    var_label: str,
    geo_bbox: Optional[Tuple[float, float, float, float]] = None,
) -> Any:
    """Compute a Python value for one (relevance-true) question."""

    # Calculate / calculate_here: evaluate the expression. calculate_here
    # is SurveyCTO-specific (fixed-location calculate). Two practical
    # differences from plain calculate:
    #   1. The typical formula is ``once(format-date-time(now(), '...'))``
    #      to capture a module-start timestamp (~95% of real-world use).
    #   2. The other common pattern is ``once(duration())`` to capture
    #      cumulative elapsed seconds at that field. SurveyCTO returns
    #      successively larger values as the form progresses — we mirror
    #      that here by advancing ``runctx.duration_progress`` per call,
    #      so HFC code that does ``end_modN - start_modN`` gets a
    #      sensible non-zero module duration.
    if q_type in ("calculate", "calculate_here"):
        calc = question.get("calculation")
        if not calc:
            return ""
        if q_type == "calculate_here" and re.search(r"\bonce\s*\(\s*duration\s*\(", calc):
            remaining = max(0, runctx.duration - runctx.duration_progress)
            advance = max(1, int(remaining * rng.uniform(0.05, 0.20))) if remaining > 0 else 0
            runctx.duration_progress = min(
                runctx.duration_progress + advance, runctx.duration
            )
            return runctx.duration_progress
        return safe_evaluate(
            calc, ctx, default="",
            on_error=lambda e, exc: strip_log.record(var_label, "calculation", e, exc),
        )

    # Metadata types: synthesised from run-context. Detection uses the
    # (type, choice_list) tuple form so multi-word types like
    # "text audit" -- which `parsers/type_parser.py` splits into
    # ("text", "audit") -- are matched correctly.
    meta_kind = _metadata_kind(question)
    if meta_kind is not None:
        return _metadata_value(meta_kind, var_label, runctx, rng)

    # select_one / select_multiple: narrow choices by choice_filter if present
    narrowed_choices = choices
    cf_expr = question.get("choice_filter")
    if cf_expr and choices:
        narrowed_choices = _narrow_choices(
            choices, cf_expr, ctx, strip_log, var_label,
        )
        if not narrowed_choices:
            narrowed_choices = choices  # fall back to full list

    sampler_kwargs = {}
    if geo_bbox is not None:
        sampler_kwargs["geo_bbox"] = geo_bbox
    app = question.get("appearance")
    if app:
        sampler_kwargs["appearance"] = app
    return sample_python_value(q_type, narrowed_choices, constraint, rng, **sampler_kwargs)


def _narrow_choices(
    choices: List[Dict[str, Any]],
    cf_expr: str,
    parent_ctx: EvalContext,
    strip_log: StripLog,
    var_label: str,
) -> List[Dict[str, Any]]:
    """Evaluate ``choice_filter`` against each candidate choice row."""
    out: List[Dict[str, Any]] = []
    for c in choices:
        ctx = EvalContext(
            row=parent_ctx.row,
            choices=parent_ctx.choices,
            pulldata_lookup=parent_ctx.pulldata_lookup,
            repeat_stack=list(parent_ctx.repeat_stack),
            rng=parent_ctx.rng,
            now=parent_ctx.now,
            current_var=parent_ctx.current_var,
            choice_row=c,
        )
        v = safe_evaluate(
            cf_expr, ctx, default=True,
            on_error=lambda e, exc: strip_log.record(var_label, "choice_filter", e, exc),
        )
        if _truthy(v):
            out.append(c)
    return out


# ── Column-set assembly ───────────────────────────────────────────────────────

def _build_column_order(
    questions: List[Dict[str, Any]],
    repeats: Set[str],
    max_iter: Dict[str, int],
    pulldata_tables: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Materialise the column list for the wide CSV in form order, with
    repeat-suffix unrolling and select_multiple indicator columns.

    Variables sharing a name with a SurveyCTO export metadata column
    (e.g. a form-defined ``duration`` calculate) reuse that slot rather
    than producing a duplicate column — the form's value, if any, will
    fill the metadata column at write time.

    When a select question has a ``search('CSV', ...)`` appearance and
    the named CSV is loaded, the choice list is expanded to include one
    indicator column per CSV row (no matches-clause filter at header
    time — every possible ID gets a slot, matching real SurveyCTO
    export shape).
    """
    cols: List[str] = []
    seen: Set[str] = set()

    def _push(name: str) -> None:
        if name in seen:
            return
        cols.append(name)
        seen.add(name)

    for c in SURVEYCTO_METADATA_COLS:
        _push(c)

    for q in questions:
        q_type = q.get("type", "")
        var_name = q.get("variable_name", "")
        if not var_name:
            continue

        group_path = q.get("group_path") or []
        choices = q.get("choices") or []
        if q_type in ("select_one", "select_multiple") and pulldata_tables and q.get("appearance"):
            dynamic = _resolve_search_choices(q, pulldata_tables, apply_matches=False)
            if dynamic:
                choices = dynamic

        if q_type == "repeat_count":
            # A repeat's count variable lives at its PARENT level, so it is
            # suffixed by the count's own enclosing repeats (its ancestors),
            # NOT by the repeat it counts. A top-level count is unsuffixed; an
            # inner count nested under one outer repeat emits ``count_1`` ..
            # ``count_<max outer>``.
            ancestors = _repeat_chain(group_path, repeats)
            for combo in _grid_combos(ancestors, max_iter):
                _push(f"{var_name}{_suffix(combo)}")
            continue

        chain = _repeat_chain(group_path, repeats)
        if chain:
            for combo in _grid_combos(chain, max_iter):
                suffix = _suffix(combo)
                # select_multiple inside a repeat: SurveyCTO export emits ONLY
                # the per-choice indicator columns (no parent cell).
                # This is NOT a bug — it's how real SurveyCTO wide exports are
                # shaped. Empirically verified against a production wide
                # export on a `select_multiple` inside a repeat group:
                # `<var>_<value>_<i>` indicators exist; the `<var>_<i>` parent
                # does not. Reviewers periodically flag this as
                # missing-output — please keep this comment so the design
                # intent is obvious.
                if q_type == "select_multiple":
                    for c in choices:
                        cv = str(c.get("value", "")).strip()
                        if not cv:
                            continue
                        _push(f"{var_name}_{_choice_suffix(cv)}{suffix}")
                else:
                    _push(f"{var_name}{suffix}")
        else:
            # Top-level select_multiple: emit the parent space-separated
            # cell AND per-choice indicator columns. SurveyCTO's "Publish ..."
            # form setting varies per question (some publish parent only,
            # some indicators only, some both) — emitting both is the safest
            # superset for HFC dry-runs.
            _push(var_name)
            if q_type == "select_multiple":
                for c in choices:
                    cv = str(c.get("value", "")).strip()
                    if not cv:
                        continue
                    _push(f"{var_name}_{_choice_suffix(cv)}")

    _push(KEY_COL)
    _push(FORMDEF_VERSION_COL)
    return cols


def _row_to_csv_dict(
    row: Dict[str, Any], runctx: RunContext, column_order: List[str]
) -> Dict[str, str]:
    """Project a walk-result row into the final wide column order.

    Form-side values win when present and non-empty. For known metadata
    columns (``duration``, ``caseid``, ``username``, etc.), if the form
    defines a calculate by that name but it evaluates to empty (eval
    error, unsupported function, ...), we fall back to the synthesised
    run-context value rather than emit a blank cell. This favors HFC
    usability over strict form fidelity in the rare case where a
    form-defined metadata calc fails to evaluate.

    Non-metadata columns pass through an empty ``row[c]`` as an empty
    cell — a form ``calculate`` that legitimately evaluates to empty
    stays empty.
    """
    runctx_defaults: Dict[str, str] = {
        "CompletionDate": runctx.end_time.strftime("%b %d, %Y %I:%M:%S %p"),
        "SubmissionDate": runctx.submission_date.strftime("%b %d, %Y %I:%M:%S %p"),
        "starttime": runctx.start_time.strftime("%b %d, %Y %I:%M:%S %p"),
        "endtime": runctx.end_time.strftime("%b %d, %Y %I:%M:%S %p"),
        "deviceid": runctx.deviceid,
        "subscriberid": runctx.subscriberid,
        "simid": runctx.simid,
        "devicephonenum": runctx.devicephonenum,
        "username": runctx.username,
        "duration": str(runctx.duration),
        "caseid": runctx.caseid,
        KEY_COL: runctx.key,
        FORMDEF_VERSION_COL: runctx.formdef_version,
    }

    out: Dict[str, str] = {}
    for c in column_order:
        if c in row:
            formatted = _format_for_csv(row[c])
            if formatted == "" and c in runctx_defaults:
                out[c] = runctx_defaults[c]
            else:
                out[c] = formatted
        else:
            out[c] = runctx_defaults.get(c, "")
    return out


# ── Top-level entry point ─────────────────────────────────────────────────────

def generate_synthetic_csv(
    questions_json_path: Path,
    output_csv_path: Path,
    pulldata_search_dirs: List[Path],
    survey_name: str = "Survey",
    n_rows: int = 5,
    seed: int = 0,
    force_values: Optional[Dict[str, str]] = None,
    geo_bbox: Optional[Tuple[float, float, float, float]] = None,
    form_settings: Optional[Dict[str, Any]] = None,
) -> Path:
    """Walk the form and write ``n_rows`` synthetic respondents to a wide CSV.

    Architecture: two passes per respondent, but only the second pass
    retains the row in memory (and only one row at a time, which is then
    written straight to disk). The first pass discards the row dict after
    extracting `repeat_counts` so we can compute the global ``max_iter``
    per repeat group before writing the CSV header. Memory is O(n_cols)
    rather than O(n_rows x n_cols).

    Each respondent gets a deterministic seed derived from ``seed``;
    within that, two further RNGs split run-context generation from
    question sampling, so adding or removing a metadata field doesn't
    shift the byte-output of question-level samples at the same seed.

    ``force_values`` maps ``variable_name -> string``; when the walker
    reaches a listed variable that would otherwise be sampled, the
    forced value is used. Useful for ensuring consent-gated sections
    populate during HFC dry-runs.

    ``geo_bbox`` overrides the global geopoint sampling box (per-survey
    config; defaults to global).
    """
    if n_rows < 1:
        n_rows = 1
    master = random.Random(seed)
    respondent_seeds = [master.randrange(2 ** 63) for _ in range(n_rows)]

    with questions_json_path.open("r", encoding="utf-8") as f:
        questions: List[Dict[str, Any]] = json.load(f)

    # Set of repeat-group names (used to detect repeat parentage during walking)
    repeats: Set[str] = {
        q.get("repeat_group_name", "")
        for q in questions
        if q.get("type") == "repeat_count"
    }
    repeats.discard("")

    # Choice lookup for choice-label() / jr:choice-name(); plus var_name
    # -> choice_list so the evaluator can resolve the right list from a
    # ${var} reference.
    choices_lookup: Dict[str, List[Dict[str, Any]]] = {}
    var_to_choice_list: Dict[str, str] = {}
    for q in questions:
        cl = q.get("choice_list")
        if cl and q.get("choices") and cl not in choices_lookup:
            choices_lookup[cl] = q["choices"]
        if cl:
            vn = (q.get("variable_name") or "").strip()
            if vn:
                var_to_choice_list[vn] = cl

    # Load pulldata CSVs once. Pulldata is obligatory: when the form
    # references pulldata, the synthetic CSV is essentially useless
    # without it (gated cascades won't populate, dynamic choices can't
    # expand, caseid sampling can't resolve). Hard-error rather than
    # silently producing an empty-looking CSV.
    try:
        tables = load_pulldata_tables(
            questions, search_dirs=pulldata_search_dirs,
        )
    except MissingPullDataError as exc:
        raise SystemExit(f"[synthetic] {exc}") from exc
    pulldata_lookup = make_lookup(tables) if tables else None

    # Caseid sampling: if the form looks up any pulldata table with
    # ``${caseid}`` as the key, draw caseid values from that table's key
    # column so pulldata lookups resolve. Otherwise fall back to the
    # synthetic ``case-NNNN`` form. Picked once, deterministically, off
    # the master seed -- so re-running with the same seed selects the
    # same caseids in the same order.
    caseid_pool = _build_caseid_pool(questions, tables or {})
    if caseid_pool:
        pool_rng = random.Random(f"{seed}::caseid_pool")
        shuffled = list(caseid_pool)
        pool_rng.shuffle(shuffled)
        # Without replacement when pool >= n_rows; cycle otherwise.
        caseid_per_row = [shuffled[i % len(shuffled)] for i in range(n_rows)]
    else:
        caseid_per_row = [None] * n_rows

    def _walk_for_respondent(rs: int, i: int, log: StripLog) -> WalkResult:
        # Two RNGs per respondent, derived from the per-respondent seed:
        # `meta_rng` -> run-context only; `sample_rng` -> sampling + the
        # evaluator's random() / uuid(). Adding a metadata field changes
        # meta_rng draws but leaves sample_rng intact -> sampling stays
        # byte-stable across edits to the run-context generator.
        meta_rng = random.Random(f"{rs}::meta")
        sample_rng = random.Random(f"{rs}::sample")
        rctx = _build_run_context(meta_rng, i + 1, caseid=caseid_per_row[i], form_settings=form_settings)
        return _walk_one(
            questions, repeats, sample_rng, rctx, pulldata_lookup,
            choices_lookup, var_to_choice_list, log,
            force_values=force_values, geo_bbox=geo_bbox,
            pulldata_tables=tables,
        )

    # ── Pass 1: collect repeat_counts per respondent; drop rows.
    max_iter: Dict[str, int] = {}
    p1_log = StripLog()  # pass-1 strip-log discarded so we don't double-report
    for i, rs in enumerate(respondent_seeds):
        result = _walk_for_respondent(rs, i, p1_log)
        for k, v in result.repeat_counts.items():
            if v > max_iter.get(k, 0):
                max_iter[k] = v
        # row dropped at end of scope

    column_order = _build_column_order(questions, repeats, max_iter, pulldata_tables=tables)

    # ── Pass 2: walk and write streaming. Each row is materialised once,
    # written to disk, then released.
    strip_log = StripLog()
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with output_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=column_order, extrasaction="ignore")
        writer.writeheader()
        for i, rs in enumerate(respondent_seeds):
            meta_rng = random.Random(f"{rs}::meta")
            sample_rng = random.Random(f"{rs}::sample")
            rctx = _build_run_context(meta_rng, i + 1, caseid=caseid_per_row[i], form_settings=form_settings)
            result = _walk_one(
                questions, repeats, sample_rng, rctx, pulldata_lookup,
                choices_lookup, var_to_choice_list, strip_log,
                force_values=force_values, geo_bbox=geo_bbox,
                pulldata_tables=tables,
            )
            writer.writerow(_row_to_csv_dict(result.row, rctx, column_order))

    # Strip-log: write fresh, or remove a stale one from a previous run that
    # had unsupported functions which are now supported.
    log_path = output_csv_path.with_suffix(".strip.log")
    if strip_log.entries:
        strip_log.write(log_path)
        print(
            f"[OK] Synthetic CSV written: {output_csv_path.name} "
            f"({n_rows} rows, {len(column_order)} columns, "
            f"{len(strip_log.entries)} strip-log entries -> {log_path.name})"
        )
    else:
        if log_path.exists():
            log_path.unlink()
        print(
            f"[OK] Synthetic CSV written: {output_csv_path.name} "
            f"({n_rows} rows, {len(column_order)} columns)"
        )
    return output_csv_path
