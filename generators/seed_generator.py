"""
Seed Dataset Generator
======================
Reads questions.json (form-level metadata) and emits a Stata .do file
that creates a seed dataset with one or more rows. By default produces
a 1-row schema seed; with `--rows N` produces N rows of plausible
type-correct values.

Usage (standalone):
    python -m surveycto_extractor.main --survey <key> --phases seed
    python -m surveycto_extractor.main --survey <key> --phases seed --rows 5 --seed 42

The generated .do file:
  - Uses only information from the JSON (no .dta needed)
  - Declares explicit Stata storage types (from the vendored xlsform.md
    type catalog at coding_guidelines/surveycto_refs/_type_catalog.json)
  - Honours constraint expressions where they are simple numeric bounds
  - Expands repeat groups to N iterations (configured or auto-resolved)
  - Expands select_multiple questions into binary choice columns
"""

import json
import random
import re
from pathlib import Path
from typing import Dict, List, Optional


# ── Type catalog (lazy-loaded from xlsform.md) ───────────────────────────────

_TYPE_CATALOG_PATH = (Path(__file__).resolve().parent.parent
                      / "coding_guidelines" / "surveycto_refs"
                      / "_type_catalog.json")
_TYPE_CATALOG: Optional[Dict[str, Dict]] = None


def _load_type_catalog() -> Dict[str, Dict]:
    """Load the vendored type catalog; rebuild via build_type_catalog.py."""
    global _TYPE_CATALOG
    if _TYPE_CATALOG is None:
        if _TYPE_CATALOG_PATH.exists():
            with _TYPE_CATALOG_PATH.open("r", encoding="utf-8") as f:
                _TYPE_CATALOG = json.load(f)
        else:
            _TYPE_CATALOG = {}
    return _TYPE_CATALOG


# ── Type mapping ─────────────────────────────────────────────────────────────

def _stata_type(q_type: str, constraint: Optional[str] = None) -> str:
    """Return Stata storage type for a SurveyCTO question type.

    Uses the catalog at coding_guidelines/surveycto_refs/_type_catalog.json
    (built by generators/build_type_catalog.py from the vendored xlsform.md)
    as the source of truth; falls back to a small hardcoded table when the
    catalog is missing or the type is not listed.
    """
    catalog = _load_type_catalog()
    if q_type == "text":
        # Try to infer max length from constraint like ". <= 10" or "string-length(.) <= 50"
        if constraint:
            m = re.search(r'<=\s*(\d+)', constraint)
            if m:
                length = int(m.group(1))
                return f"str{min(length, 2045)}"
        return "str32"

    entry = catalog.get(q_type)
    if entry and entry.get("stata_type"):
        return entry["stata_type"]

    # repeat_count is synthetic — not in the catalog
    if q_type == "repeat_count":
        return "long"

    # Fallback: minimal hardcoded table
    fallback = {
        "integer": "long", "decimal": "double", "date": "long",
        "datetime": "double", "time": "long", "select_one": "long",
        "calculate": "long", "geopoint": "double", "select_multiple": "byte",
    }
    return fallback.get(q_type, "str32")


# ── Constraint-aware bound extraction ────────────────────────────────────────

def _numeric_bounds(constraint: Optional[str]) -> tuple:
    """Extract (lower, upper) numeric bounds from a constraint expression.

    Recognises clauses like `. >= N`, `. > N`, `. <= N`, `. < N`,
    `var >= N`, etc. on either side of `and`. Inclusive on >= and <=,
    pinned by +/-1 for strict > / <. Returns (None, None) if no clean
    bound can be extracted.
    """
    if not constraint:
        return (None, None)

    lo = None
    hi = None
    # `. op N` or `var op N` — match every relational clause
    for m in re.finditer(
        r'(?:\.|[A-Za-z_]\w*)\s*(>=|<=|>(?!=)|<(?!=))\s*(-?\d+(?:\.\d+)?)',
        constraint
    ):
        op, val = m.group(1), float(m.group(2))
        if op == '>=':
            lo = val if lo is None else max(lo, val)
        elif op == '>':
            v = val + 1
            lo = v if lo is None else max(lo, v)
        elif op == '<=':
            hi = val if hi is None else min(hi, val)
        elif op == '<':
            v = val - 1
            hi = v if hi is None else min(hi, v)
    return (lo, hi)


def _text_max_length(constraint: Optional[str]) -> Optional[int]:
    """Look for `string-length(.) <= N` or `<= N` patterns in the constraint."""
    if not constraint:
        return None
    m = re.search(r'<=\s*(\d+)', constraint)
    if m:
        return int(m.group(1))
    return None


# ── Per-type random value generator ──────────────────────────────────────────

def _random_value(
    q_type: str,
    choices: Optional[List[Dict]],
    constraint: Optional[str],
    rng: random.Random,
) -> str:
    """Return a Stata literal for one randomly-sampled, type-correct value.

    For integer / decimal: samples within constraint bounds when extractable;
    otherwise uses safe defaults. For select_one: draws uniformly from choice
    values. For select_multiple: callers iterate per-binary-column. For
    dates/times: draws within a reasonable window. For strings: short seeded
    token, optionally length-capped by `string-length(.) <= N`.
    """
    if q_type == "integer":
        lo, hi = _numeric_bounds(constraint)
        lo = int(lo) if lo is not None else 0
        hi = int(hi) if hi is not None else max(lo + 1, 100)
        if lo > hi:
            lo, hi = hi, lo
        return str(rng.randint(lo, hi))

    if q_type == "decimal":
        lo, hi = _numeric_bounds(constraint)
        lo = lo if lo is not None else 0.0
        hi = hi if hi is not None else max(lo + 1.0, 100.0)
        if lo > hi:
            lo, hi = hi, lo
        return f"{rng.uniform(lo, hi):.4f}"

    if q_type == "date":
        # Random date between 2020-01-01 and today + 30 days
        day_offset = rng.randint(0, 5 * 365)
        # Stata date counts days since 1960-01-01. 2020-01-01 = 21915.
        return str(21915 + day_offset)

    if q_type == "datetime":
        # Random datetime in ms since 1960-01-01. ~21915 days * 86_400_000 ms.
        ms_offset = rng.randint(0, 5 * 365 * 86_400_000)
        return str(21915 * 86_400_000 + ms_offset)

    if q_type == "time":
        # Random second-of-day in ms (Stata stores as %tcHH:MM:SS).
        return str(rng.randint(0, 86_400) * 1000)

    if q_type == "select_one":
        if choices:
            ints = [
                str(c.get("value", "")).strip()
                for c in choices
                if str(c.get("value", "")).strip().lstrip("-").isdigit()
            ]
            if ints:
                return rng.choice(ints)
        return "1"

    if q_type in ("repeat_count", "calculate"):
        return "1"

    if q_type == "geopoint":
        # Caller splits into 4 component vars; this default is used per
        # component. Random lat/lon in a sensible range.
        return f"{rng.uniform(-90, 90):.6f}"

    if q_type in ("barcode", "image", "audio", "video", "file"):
        return f'"seed_file_{rng.randint(1, 9999)}"'

    if q_type == "text":
        n = _text_max_length(constraint)
        token = f"seed_{rng.randint(0, 9999)}"
        if n is not None:
            return f'"{token[:n]}"'
        return f'"{token}"'

    if q_type == "select_multiple":
        return "0"

    return '"seed"'


# Legacy entry point kept for backward compatibility — fixed placeholder
# values, not sampled. Used for the n_rows == 1 default so the schema
# seed remains byte-identical to pre-Tier-5 output.
def _stata_value(q_type: str, choices: Optional[List[Dict]] = None) -> str:
    """Return a synthetic value literal (fixed placeholders, no rng)."""
    if q_type in ("integer", "decimal"):
        return "1"
    if q_type == "date":
        return "td(01jan2024)"
    if q_type == "datetime":
        return "tc(01jan2024 00:00:00)"
    if q_type == "time":
        return "0"
    if q_type == "select_one":
        if choices:
            val = str(choices[0].get("value", "1")).strip()
            if val and val.lstrip("-").isdigit():
                return val
        return "1"
    if q_type in ("repeat_count", "calculate"):
        return "1"
    if q_type == "geopoint":
        return "0"
    if q_type in ("barcode", "image", "audio", "video", "file"):
        return '"seed_file"'
    if q_type == "text":
        return '"seed"'
    if q_type == "select_multiple":
        return "0"
    return '"seed"'


def _format_value(stata_type: str, value_str: str) -> str:
    """Wrap value in quotes if the Stata type is a string."""
    if stata_type.startswith("str"):
        # value_str already quoted if it came from _stata_value for strings
        if not value_str.startswith('"'):
            return f'"{value_str}"'
    return value_str


# ── Repeat count resolution ───────────────────────────────────────────────────

def _max_iterations_from_data(data_path: "Path") -> Dict[str, int]:
    """
    Scan a Stata dataset for the maximum observed iteration per variable base name.

    Looks for columns matching <base>_<N> where N is a pure integer suffix.
    Returns {base_variable_name: max_N_observed}.

    Filters out select_multiple binaries: if the base (or base stripped of a
    trailing underscore, which appears for negative choice values like _97)
    is itself a column in the dataset, it is a binary dummy, not a repeat column.

    If the file does not exist (new form, no submissions yet) returns {}.
    Requires pyreadstat; if unavailable also returns {}.
    """
    try:
        import pyreadstat
    except ImportError:
        return {}

    if not Path(data_path).exists():
        return {}

    try:
        df, _ = pyreadstat.read_dta(str(data_path), metadataonly=True)
        col_set = set(df.columns)
    except Exception:
        return {}

    base_max: Dict[str, int] = {}
    for col in col_set:
        m = re.match(r'^(.+?)_(\d+)$', col)
        # Also handle digit appended directly without underscore (e.g. "f_hr_fn_r1")
        # but require the digit suffix to be short (1-2 digits) to avoid false
        # positives like "income2023" or "year2024"
        if not m:
            m = re.match(r'^(.+?[a-z_])(\d{1,2})$', col)
        if not m:
            continue
        base, num = m.group(1), int(m.group(2))
        # Filter select_multiple binaries: parent variable exists as its own column,
        # including the trailing-underscore variant (e.g. internet_use__97 → base
        # is internet_use_ whose stripped form internet_use IS a column)
        if base in col_set or base.rstrip('_') in col_set:
            continue
        base_max[base] = max(base_max.get(base, 0), num)

    return base_max


def _resolve_repeat_count(calculation: Optional[str], repeat_defaults: Dict[str, int],
                           repeat_name: str, data_counts: Optional[Dict[str, int]] = None) -> int:
    """
    Determine number of iterations for a repeat group.

    Priority:
      1. repeat_defaults (explicit config override) — always wins
      2. data_counts (max observed iteration in real .dta) — if data available
      3. Form calculation: literal integer → use it
      4. Variable reference or complex expression → 1 (safe seed default)
    """
    # Tier 1 — explicit config override
    if repeat_name in repeat_defaults:
        return repeat_defaults[repeat_name]

    # Tier 2 — from real data (only for variable-driven repeats)
    # For literal-integer repeats the formula is authoritative; skip data tier.
    calc = (calculation or "").strip()
    is_literal = calc.lstrip("-").isdigit()
    if not is_literal and data_counts and repeat_name in data_counts:
        return data_counts[repeat_name]

    if not calc:
        return 1

    # Tier 3 — literal integer in form
    if is_literal:
        return max(1, int(calc))

    # Tier 4 — variable reference or complex expression
    return 1


# ── Line builders ─────────────────────────────────────────────────────────────

def _gen_line(var_name: str, stata_type: str, value: str, label: str,
              extra_replaces: Optional[List[str]] = None) -> str:
    """Build gen + label variable + optional per-row replace statements.

    extra_replaces is a list of (row_index, value) tuples rendered as
    `replace var = value in K` lines, used when n_rows > 1.
    """
    safe_label = label.replace('"', "'").replace("\n", " ").strip()
    if len(safe_label) > 80:
        safe_label = safe_label[:77] + "..."
    lines = [f"gen {stata_type} {var_name} = {value}"]
    if safe_label:
        lines.append(f'label variable {var_name} "{safe_label}"')
    if extra_replaces:
        lines.extend(extra_replaces)
    return "\n".join(lines)


def _value_for_row(
    q_type: str,
    choices: Optional[List[Dict]],
    constraint: Optional[str],
    stata_type: str,
    rng: random.Random,
) -> str:
    """Generate one value formatted appropriately for the Stata type."""
    raw = _random_value(q_type, choices, constraint, rng)
    return _format_value(stata_type, raw)


def _gen_values(
    var_name: str,
    stata_type: str,
    q_type: str,
    choices: Optional[List[Dict]],
    constraint: Optional[str],
    label: str,
    n_rows: int,
    rng: random.Random,
) -> str:
    """Emit gen + per-row replace statements for one variable across n_rows.

    When n_rows == 1, uses the legacy deterministic placeholder (`1`, `"seed"`,
    etc.) so the default 1-row schema seed remains byte-identical to the
    pre-Tier-5 output. When n_rows > 1, samples constraint-aware values via
    the rng.
    """
    if n_rows == 1:
        first = _format_value(stata_type, _stata_value(q_type, choices))
        return _gen_line(var_name, stata_type, first, label)
    first = _value_for_row(q_type, choices, constraint, stata_type, rng)
    replaces: List[str] = []
    for i in range(2, n_rows + 1):
        v = _value_for_row(q_type, choices, constraint, stata_type, rng)
        replaces.append(f"replace {var_name} = {v} in {i}")
    return _gen_line(var_name, stata_type, first, label, extra_replaces=replaces)


def _format_value_with_format(var_name: str, q_type: str) -> Optional[str]:
    """Return a format/display format statement if needed."""
    if q_type == "date":
        return f"format {var_name} %td"
    if q_type == "datetime":
        return f"format {var_name} %tc"
    return None


# ── Main generator ────────────────────────────────────────────────────────────

def generate_seed_dofile(
    questions_json_path: Path,
    output_do_path: Path,
    survey_name: str = "Survey",
    repeat_defaults: Optional[Dict[str, int]] = None,
    seed_dta_stata_path: str = "",
    data_path: Optional[Path] = None,
    n_rows: int = 1,
    seed: int = 0,
) -> Path:
    """
    Generate a Stata .do file that creates a seed dataset.

    Args:
        questions_json_path: Path to questions.json produced by Phase 2
        output_do_path: Where to write the .do file
        survey_name: Human-readable survey name for the header comment
        repeat_defaults: Dict of {repeat_group_name: n_iterations} overrides
        seed_dta_stata_path: Stata path string for the save command (may use globals)
        data_path: Optional path to existing .dta — used to infer max iterations
            for variable-driven repeat groups (e.g. repeat_count=${hh_size}).
            Safe to omit for new forms with no submissions yet.
        n_rows: Number of rows in the generated dataset. Default 1 produces
            the original schema seed (one row of placeholder values). Larger
            values emit per-row `replace` statements with constraint-aware
            random values drawn from `random.Random(seed)`.
        seed: Random seed for reproducibility. Same seed → byte-identical
            do-file output.

    Returns:
        Path to the written .do file
    """
    if repeat_defaults is None:
        repeat_defaults = {}
    if n_rows < 1:
        n_rows = 1
    rng = random.Random(seed)

    with open(questions_json_path, "r", encoding="utf-8") as f:
        questions: List[Dict] = json.load(f)

    # Scan real data for max observed iterations per base variable name
    base_max = _max_iterations_from_data(data_path) if data_path else {}

    # Map repeat_group_name → max observed N, by checking base variable names
    # of questions that belong to each repeat group (via group_path)
    data_max_by_group: Dict[str, int] = {}
    if base_max:
        for count_q in questions:
            if count_q.get("type") != "repeat_count":
                continue
            repeat_name = count_q.get("repeat_group_name", "")
            if not repeat_name:
                continue
            for q in questions:
                if repeat_name in (q.get("group_path") or []):
                    base = q.get("variable_name", "")
                    if base and base in base_max:
                        data_max_by_group[repeat_name] = max(
                            data_max_by_group.get(repeat_name, 0),
                            base_max[base]
                        )

    # Build lookup: repeat_group_name → n_iterations
    # First pass: identify repeat_count questions and resolve counts
    repeat_counts: Dict[str, int] = {}
    for q in questions:
        if q.get("type") == "repeat_count":
            repeat_name = q.get("repeat_group_name", "")
            if repeat_name:
                n = _resolve_repeat_count(
                    q.get("calculation"), repeat_defaults, repeat_name,
                    data_counts=data_max_by_group,
                )
                repeat_counts[repeat_name] = n

    if data_max_by_group:
        print("  [data] max iterations per repeat group: " +
              ", ".join(f"{k}={v}" for k, v in data_max_by_group.items()))

    # ── Build do-file lines ───────────────────────────────────────────────────
    header_purpose = (
        "1-row schema seed (default)"
        if n_rows == 1
        else f"{n_rows}-row seeded dataset (seed={seed})"
    )
    lines = [
        f"* Seed dataset: {survey_name}",
        f"* Generated by surveycto_extractor — {header_purpose}",
        "* Do NOT edit by hand; regenerate via: python -m surveycto_extractor.main --phases seed",
    ]
    if n_rows > 1:
        lines.append(
            "* Values are constraint-aware where bounds are extractable from "
            "the form. Not real data; do NOT use for analysis."
        )
    lines += [
        "",
        "clear",
        f"set obs {n_rows}",
        "",
    ]

    current_section = None

    for q in questions:
        q_type = q.get("type", "")
        var_name = q.get("variable_name", "")
        label = q.get("question_text", "") or ""
        choices = q.get("choices") or []
        constraint = q.get("constraint")
        group_path = q.get("group_path", [])

        if not var_name:
            continue

        # Section separator comment
        section = group_path[0] if group_path else "(top level)"
        if section != current_section:
            current_section = section
            lines.append(f"\n* ── Section: {section} {'─' * max(0, 60 - len(section))}")

        # ── repeat_count variable → skip gen (Stata dataset stores count as var_count)
        if q_type == "repeat_count":
            repeat_name = q.get("repeat_group_name", var_name[:-6])
            n = repeat_counts.get(repeat_name, 1)
            lines.append(f"\n* ── Repeat: {repeat_name} (N={n} iteration{'s' if n != 1 else ''}) {'─' * max(0, 40 - len(repeat_name))}")

            # Emit the _count variable itself.
            # Count is a structural integer that should be constant across
            # all seeded rows (it tells Stata how many iterations exist),
            # so we don't randomize it row-by-row.
            stata_type = "long"
            gen_line = _gen_line(var_name, stata_type, str(n), label)
            lines.append(gen_line)
            fmt = _format_value_with_format(var_name, q_type)
            if fmt:
                lines.append(fmt)
            lines.append("")
            continue

        # ── geopoint → 4 component variables
        if q_type == "geopoint":
            for suffix, comp_label, comp_range in [
                ("_lat", "latitude",  (-90.0, 90.0)),
                ("_lon", "longitude", (-180.0, 180.0)),
                ("_alt", "altitude",  (0.0, 5000.0)),
                ("_acc", "accuracy",  (1.0, 50.0)),
            ]:
                comp_var = var_name + suffix
                if n_rows == 1:
                    # Legacy: literal 0 placeholder
                    gen_line = _gen_line(comp_var, "double", "0", f"{label} ({comp_label})")
                else:
                    first = f"{rng.uniform(*comp_range):.6f}"
                    replaces = []
                    for i in range(2, n_rows + 1):
                        replaces.append(f"replace {comp_var} = {rng.uniform(*comp_range):.6f} in {i}")
                    gen_line = _gen_line(comp_var, "double", first, f"{label} ({comp_label})",
                                         extra_replaces=replaces or None)
                lines.append(gen_line)
            lines.append("")
            continue

        # ── Variables inside a repeat group → emit N iterations
        # Must check BEFORE select_multiple so the combined case
        # (select_multiple inside a repeat) gets iteration suffixes.
        repeat_parent = _find_repeat_parent(group_path, repeat_counts)

        if repeat_parent:
            n = repeat_counts[repeat_parent]

            # select_multiple inside repeat → double suffix: var_choiceval_iter
            if q_type == "select_multiple" and choices:
                for ci, choice in enumerate(choices):
                    choice_val = str(choice.get("value", ci + 1)).strip()
                    choice_label = str(choice.get("label", "")).strip()
                    stata_suffix = choice_val.replace('-', '_')
                    base_col = f"{var_name}_{stata_suffix}"
                    for i in range(1, n + 1):
                        iter_var = f"{base_col}_{i}"
                        iter_label = f"{label}: {choice_label} (iteration {i})" if choice_label else f"{label}: {choice_val} (iteration {i})"
                        if n_rows == 1:
                            # Legacy: first choice = 1, rest = 0
                            first = "1" if ci == 0 else "0"
                            gen_line = _gen_line(iter_var, "byte", first, iter_label)
                        else:
                            # Multi-row: Bernoulli per row, first choice is
                            # weighted up so each seed row has at least one
                            # selected most of the time.
                            first = "1" if (ci == 0 and rng.random() < 0.7) else str(int(rng.random() < 0.5))
                            replaces = []
                            for r in range(2, n_rows + 1):
                                replaces.append(f"replace {iter_var} = {int(rng.random() < 0.5)} in {r}")
                            gen_line = _gen_line(iter_var, "byte", first, iter_label,
                                                 extra_replaces=replaces or None)
                        lines.append(gen_line)
                lines.append("")
                continue

            stata_type = _stata_type(q_type, constraint)
            for i in range(1, n + 1):
                iter_var = f"{var_name}_{i}"
                iter_label = f"{label} (iteration {i})"
                gen_line = _gen_values(iter_var, stata_type, q_type, choices,
                                       constraint, iter_label, n_rows, rng)
                lines.append(gen_line)
                fmt = _format_value_with_format(iter_var, q_type)
                if fmt:
                    lines.append(fmt)
            lines.append("")
            continue

        # ── select_multiple (not in repeat) → binary columns, one per choice
        if q_type == "select_multiple":
            for ci, choice in enumerate(choices):
                choice_val = str(choice.get("value", ci + 1)).strip()
                choice_label = str(choice.get("label", "")).strip()
                # SurveyCTO exports negative choice values with - replaced by _
                # e.g. choice value -97 → column name var__97 (double underscore)
                stata_suffix = choice_val.replace('-', '_')
                col_var = f"{var_name}_{stata_suffix}"
                col_label = f"{label}: {choice_label}" if choice_label else f"{label}: {choice_val}"
                if n_rows == 1:
                    # Legacy: first choice = 1, rest = 0
                    first = "1" if ci == 0 else "0"
                    gen_line = _gen_line(col_var, "byte", first, col_label)
                else:
                    first = "1" if ci == 0 else str(int(rng.random() < 0.5))
                    replaces = []
                    for r in range(2, n_rows + 1):
                        replaces.append(f"replace {col_var} = {int(rng.random() < 0.5)} in {r}")
                    gen_line = _gen_line(col_var, "byte", first, col_label,
                                         extra_replaces=replaces or None)
                lines.append(gen_line)
            lines.append("")
            continue

        # ── Regular (non-repeat) variable
        stata_type = _stata_type(q_type, constraint)
        gen_line = _gen_values(var_name, stata_type, q_type, choices,
                               constraint, label, n_rows, rng)
        lines.append(gen_line)
        fmt = _format_value_with_format(var_name, q_type)
        if fmt:
            lines.append(fmt)
        lines.append("")

    # Save command
    lines.append("")
    if seed_dta_stata_path:
        lines.append(f'save "{seed_dta_stata_path}", replace')
    else:
        lines.append('* Uncomment and set path to save:')
        lines.append('* save "path/to/seed.dta", replace')

    lines.append("")

    # Write file
    output_do_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_do_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[OK] Seed do-file written: {output_do_path.name} ({len(questions)} questions processed)")
    return output_do_path


def _find_repeat_parent(group_path: List[str], repeat_counts: Dict[str, int]) -> Optional[str]:
    """
    Return the innermost repeat group name that this variable is nested under,
    or None if the variable is not inside any repeat.
    """
    if not group_path:
        return None
    for group_name in reversed(group_path):
        if group_name in repeat_counts:
            return group_name
    return None
