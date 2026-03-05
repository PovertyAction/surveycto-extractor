"""
Seed Dataset Generator
======================
Reads questions.json (form-level metadata) and emits a Stata .do file
that creates a 1-row schema seed dataset.

Usage (standalone):
    python -m surveycto_extractor.main --survey <key> --phases seed

The generated .do file:
  - Uses only information from the JSON (no .dta needed)
  - Declares explicit Stata storage types for every variable
  - Expands repeat groups to N iterations (configured or auto-resolved)
  - Expands select_multiple questions into binary choice columns
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional


# ── Type mapping ─────────────────────────────────────────────────────────────

def _stata_type(q_type: str, constraint: Optional[str] = None) -> str:
    """Return Stata storage type for a SurveyCTO question type."""
    mapping = {
        "integer":      "long",
        "decimal":      "double",
        "date":         "long",
        "datetime":     "double",
        "time":         "long",
        "select_one":   "long",
        "repeat_count": "long",
        "calculate":    "long",
        "geopoint":     "double",
        "geotrace":     "str64",
        "geoshape":     "str64",
        "barcode":      "str20",
        "image":        "str20",
        "audio":        "str20",
        "video":        "str20",
        "file":         "str20",
    }
    if q_type in mapping:
        return mapping[q_type]

    if q_type == "text":
        # Try to infer max length from constraint like ". <= 10" or "string-length(.) <= 50"
        if constraint:
            m = re.search(r'<=\s*(\d+)', constraint)
            if m:
                length = int(m.group(1))
                return f"str{min(length, 2045)}"
        return "str32"

    if q_type == "select_multiple":
        # Binaries are byte
        return "byte"

    # Fallback
    return "str32"


def _stata_value(q_type: str, choices: Optional[List[Dict]] = None) -> str:
    """Return a synthetic value literal for a Stata gen statement."""
    if q_type == "integer":
        return "1"
    if q_type == "decimal":
        return "1"
    if q_type == "date":
        return "td(01jan2024)"
    if q_type == "datetime":
        return "tc(01jan2024 00:00:00)"
    if q_type == "time":
        return "0"
    if q_type == "select_one":
        # Use first choice value (integer) if available
        if choices:
            first = choices[0]
            val = str(first.get("value", "1")).strip()
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

def _gen_line(var_name: str, stata_type: str, value: str, label: str) -> str:
    """Build a single gen + label variable statement."""
    safe_label = label.replace('"', "'").replace("\n", " ").strip()
    if len(safe_label) > 80:
        safe_label = safe_label[:77] + "..."
    lines = [f"gen {stata_type} {var_name} = {value}"]
    if safe_label:
        lines.append(f'label variable {var_name} "{safe_label}"')
    return "\n".join(lines)


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
) -> Path:
    """
    Generate a Stata .do file that creates a 1-row seed dataset.

    Args:
        questions_json_path: Path to questions.json produced by Phase 2
        output_do_path: Where to write the .do file
        survey_name: Human-readable survey name for the header comment
        repeat_defaults: Dict of {repeat_group_name: n_iterations} overrides
        seed_dta_stata_path: Stata path string for the save command (may use globals)
        data_path: Optional path to existing .dta — used to infer max iterations
            for variable-driven repeat groups (e.g. repeat_count=${hh_size}).
            Safe to omit for new forms with no submissions yet.

    Returns:
        Path to the written .do file
    """
    if repeat_defaults is None:
        repeat_defaults = {}

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
    lines = [
        f"* Seed dataset: {survey_name}",
        "* Generated by surveycto_extractor — run this in Stata to create a 1-row schema seed",
        "* Do NOT edit by hand; regenerate via: python -m surveycto_extractor.main --phases seed",
        "",
        "clear",
        "set obs 1",
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

            # Emit the _count variable itself
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
            for suffix, comp_label in [
                ("_lat", "latitude"), ("_lon", "longitude"),
                ("_alt", "altitude"), ("_acc", "accuracy")
            ]:
                comp_var = var_name + suffix
                gen_line = _gen_line(comp_var, "double", "0", f"{label} ({comp_label})")
                lines.append(gen_line)
            lines.append("")
            continue

        # ── select_multiple → binary columns, one per choice
        if q_type == "select_multiple":
            for ci, choice in enumerate(choices):
                choice_val = str(choice.get("value", ci + 1)).strip()
                choice_label = str(choice.get("label", "")).strip()
                # SurveyCTO exports negative choice values with - replaced by _
                # e.g. choice value -97 → column name var__97 (double underscore)
                stata_suffix = choice_val.replace('-', '_')
                col_var = f"{var_name}_{stata_suffix}"
                col_label = f"{label}: {choice_label}" if choice_label else f"{label}: {choice_val}"
                # First choice = 1 (seed has at least one selected), rest = 0
                val = "1" if ci == 0 else "0"
                gen_line = _gen_line(col_var, "byte", val, col_label)
                lines.append(gen_line)
            lines.append("")
            continue

        # ── Variables inside a repeat group → emit N iterations
        # Determine if this variable is inside a repeat group
        repeat_parent = _find_repeat_parent(group_path, repeat_counts)

        if repeat_parent:
            n = repeat_counts[repeat_parent]
            stata_type = _stata_type(q_type, constraint)
            base_value = _stata_value(q_type, choices if choices else None)
            base_value = _format_value(stata_type, base_value)

            for i in range(1, n + 1):
                iter_var = f"{var_name}_{i}"
                iter_label = f"{label} (iteration {i})"
                gen_line = _gen_line(iter_var, stata_type, base_value, iter_label)
                lines.append(gen_line)
                fmt = _format_value_with_format(iter_var, q_type)
                if fmt:
                    lines.append(fmt)
            lines.append("")
            continue

        # ── Regular (non-repeat) variable
        stata_type = _stata_type(q_type, constraint)
        value = _stata_value(q_type, choices if choices else None)
        value = _format_value(stata_type, value)

        gen_line = _gen_line(var_name, stata_type, value, label)
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
