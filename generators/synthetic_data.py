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

Output: ``<output_dir>/<survey>_synthetic.csv`` + a strip-log
(``<survey>_synthetic.strip.log``) listing expressions the evaluator
could not interpret. CLI in ``main.py`` exposes ``--phases synthetic``
with ``--rows`` and ``--seed`` flags.
"""

from __future__ import annotations

import csv
import datetime
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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
# than respondent input. The walker fills these from a per-respondent run
# context rather than sampling.
_METADATA_TYPES = {
    "start", "end", "today", "deviceid", "subscriberid", "simserial",
    "simid", "phonenumber", "devicephonenum", "username", "caseid",
    "audio audit", "text audit", "speed violations count",
    "speed violations list",
}

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


def _build_run_context(rng: random.Random, index: int) -> RunContext:
    base_dt = datetime.datetime(2026, 5, 11, 9, 0, 0)
    start = base_dt + datetime.timedelta(
        days=rng.randint(0, 30), seconds=rng.randint(0, 8 * 3600)
    )
    duration = rng.randint(600, 7200)
    end = start + datetime.timedelta(seconds=duration)
    submission = end + datetime.timedelta(seconds=rng.randint(0, 600))
    device_n = rng.randint(100000, 999999)
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
        caseid=f"case-{index:04d}",
        key=f"uuid:{uuid.UUID(int=rng.getrandbits(128))}",
        formdef_version=f"2026{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_innermost_repeat(group_path: List[str], repeats: Set[str]) -> Optional[str]:
    for g in reversed(group_path or []):
        if g in repeats:
            return g
    return None


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
        if value.is_integer():
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
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
    if q_type in ("audio audit", "text audit"):
        return f"media-{rng.randint(1000, 9999)}.m4a"
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
) -> WalkResult:
    """Generate one respondent's row."""

    row: Dict[str, Any] = {}
    repeat_counts: Dict[str, int] = {}

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

        # ── repeat_count: resolve N for this respondent. If the repeat
        # group is gated by a relevance that evaluated false, the count
        # column is blank and the repeat block contributes nothing.
        if q_type == "repeat_count":
            repeat_name = q.get("repeat_group_name") or ""
            ctx = _make_ctx(var_name, [])
            if not _is_relevant(q, ctx, strip_log, var_name):
                repeat_counts[repeat_name] = 0
                row[var_name] = ""
                continue
            n = _resolve_repeat_count(
                q.get("calculation"), ctx, strip_log, var_name, rng, constraint,
            )
            repeat_counts[repeat_name] = n
            row[var_name] = n
            continue

        # ── Variables inside a repeat: iterate N times with wide-suffix keys
        repeat_parent = _find_innermost_repeat(group_path, repeats)
        if repeat_parent:
            n = repeat_counts.get(repeat_parent, 1)
            for i in range(1, n + 1):
                key = f"{var_name}_{i}"
                stack = [(repeat_parent, i)]
                ctx = _make_ctx(key, stack)
                if not _is_relevant(q, ctx, strip_log, key):
                    row[key] = ""
                    if q_type == "select_multiple":
                        for c in choices:
                            cv = str(c.get("value", "")).strip()
                            if not cv:
                                continue
                            row[f"{var_name}_{_choice_suffix(cv)}_{i}"] = ""
                    continue
                value = _compute_value(
                    q, q_type, choices, constraint, ctx, rng, runctx,
                    strip_log, key,
                )
                if q_type == "select_multiple":
                    # Store the formatted space-separated string in the row
                    # so downstream `selected(${var}, X)` inside the same
                    # repeat (which resolves `${var}` -> `var_<i>` via the
                    # repeat stack) can tokenise it correctly. The column
                    # builder omits this `var_<i>` cell from CSV; it's
                    # internal-only.
                    csv_str = _format_for_csv(value)
                    row[key] = csv_str
                    selected_tokens = set(csv_str.split())
                    for c in choices:
                        cv = str(c.get("value", "")).strip()
                        if not cv:
                            continue
                        bin_key = f"{var_name}_{_choice_suffix(cv)}_{i}"
                        row[bin_key] = 1 if cv in selected_tokens else 0
                else:
                    row[key] = value
            continue

        # ── Top-level variables (no repeat)
        ctx = _make_ctx(var_name, [])
        if not _is_relevant(q, ctx, strip_log, var_name):
            row[var_name] = ""
            if q_type == "select_multiple":
                for c in choices:
                    cv = str(c.get("value", "")).strip()
                    if not cv:
                        continue
                    row[f"{var_name}_{_choice_suffix(cv)}"] = ""
            continue
        value = _compute_value(
            q, q_type, choices, constraint, ctx, rng, runctx, strip_log, var_name,
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
) -> Any:
    """Compute a Python value for one (relevance-true) question."""

    # Calculate: evaluate the expression
    if q_type == "calculate":
        calc = question.get("calculation")
        if not calc:
            return ""
        return safe_evaluate(
            calc, ctx, default="",
            on_error=lambda e, exc: strip_log.record(var_label, "calculation", e, exc),
        )

    # Metadata types: synthesised from run-context
    if q_type in _METADATA_TYPES:
        return _metadata_value(q_type, var_label, runctx, rng)

    # select_one / select_multiple: narrow choices by choice_filter if present
    narrowed_choices = choices
    cf_expr = question.get("choice_filter")
    if cf_expr and choices:
        narrowed_choices = _narrow_choices(
            choices, cf_expr, ctx, strip_log, var_label,
        )
        if not narrowed_choices:
            narrowed_choices = choices  # fall back to full list

    if q_type == "select_multiple":
        return sample_python_value(q_type, narrowed_choices, constraint, rng)
    return sample_python_value(q_type, narrowed_choices, constraint, rng)


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
) -> List[str]:
    """Materialise the column list for the wide CSV in form order, with
    repeat-suffix unrolling and select_multiple indicator columns.

    Variables sharing a name with a SurveyCTO export metadata column
    (e.g. a form-defined ``duration`` calculate) reuse that slot rather
    than producing a duplicate column — the form's value, if any, will
    fill the metadata column at write time.
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

        if q_type == "repeat_count":
            _push(var_name)
            continue

        repeat_parent = _find_innermost_repeat(group_path, repeats)
        if repeat_parent:
            n = max_iter.get(repeat_parent, 1)
            for i in range(1, n + 1):
                # select_multiple inside a repeat: SurveyCTO export emits ONLY
                # the per-choice indicator columns (no `var_<iter>` parent).
                if q_type == "select_multiple":
                    for c in choices:
                        cv = str(c.get("value", "")).strip()
                        if not cv:
                            continue
                        _push(f"{var_name}_{_choice_suffix(cv)}_{i}")
                else:
                    _push(f"{var_name}_{i}")
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
    """Project a walk-result row into the final wide column order. Form-side
    values win over run-context defaults — so a form's ``duration`` calculate
    overrides the synthesised metadata value when its expression evaluates
    cleanly, and falls back to the synth default when the calc is empty /
    unsupported.
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
            form_v = _format_for_csv(row[c])
            out[c] = form_v if form_v != "" else runctx_defaults.get(c, "")
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
    allow_missing_pulldata: bool = False,
) -> Path:
    """Walk the form and write ``n_rows`` synthetic respondents to a wide CSV.

    Returns the path of the written CSV. Writes a sibling strip-log at
    ``output_csv_path.with_suffix('.strip.log')`` listing any expressions
    the evaluator could not interpret.
    """
    if n_rows < 1:
        n_rows = 1
    rng = random.Random(seed)

    with questions_json_path.open("r", encoding="utf-8") as f:
        questions: List[Dict[str, Any]] = json.load(f)

    # Set of repeat-group names (used to detect repeat parentage during walking)
    repeats: Set[str] = {
        q.get("repeat_group_name", "")
        for q in questions
        if q.get("type") == "repeat_count"
    }
    repeats.discard("")

    # Choice lookup for choice-label() etc. (not needed in Phase A subset,
    # but plumb it through so Phase B doesn't need a signature change).
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

    # Load pulldata CSVs once
    try:
        tables = load_pulldata_tables(
            questions, search_dirs=pulldata_search_dirs,
            allow_missing=allow_missing_pulldata,
        )
    except MissingPullDataError as exc:
        raise SystemExit(f"[synthetic] {exc}") from exc
    pulldata_lookup = make_lookup(tables) if tables else None

    strip_log = StripLog()

    # Pass 1: walk all respondents in memory, tracking max iterations per repeat
    walk_results: List[WalkResult] = []
    max_iter: Dict[str, int] = {}
    for i in range(n_rows):
        runctx = _build_run_context(rng, i + 1)
        result = _walk_one(
            questions, repeats, rng, runctx, pulldata_lookup, choices_lookup,
            var_to_choice_list, strip_log,
        )
        # Store run-context inside the result for the writer
        result.row["__runctx__"] = runctx
        walk_results.append(result)
        for k, v in result.repeat_counts.items():
            if v > max_iter.get(k, 0):
                max_iter[k] = v

    # Pass 2: assemble columns and write CSV
    column_order = _build_column_order(questions, repeats, max_iter)

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with output_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=column_order, extrasaction="ignore")
        writer.writeheader()
        for result in walk_results:
            runctx = result.row.pop("__runctx__")
            writer.writerow(_row_to_csv_dict(result.row, runctx, column_order))

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
