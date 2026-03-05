"""
Survey lookup tool — project-agnostic version.

Auto-discovers survey documentation by scanning the docs directory for
variable_dictionary.json and questions.json files.

Usage:
  python search_survey.py --var <variable_name> [--context N]
  python search_survey.py --choice-list <list_name>
  python search_survey.py --search <text>

Options:
  --context N   Also print N adjacent questions on each side in survey order,
                plus resolve the relevance gate ("why is this asked").
                Default: 0 (off). Recommended: 3.

For --var lookups, performs a two-pass search:
  1. variable_dictionary.json  (keyed by Stata dataset name)
     -> resolves the SurveyCTO original_variable_name if it differs
  2. questions.json             (keyed by SurveyCTO form name)
     -> provides constraint, choices, relevance (not in the dictionary)
This ensures variables with name mismatches between SurveyCTO and Stata are found.

SETUP:
  Option A — Auto-discovery (recommended):
    Set DOCS below to your project's survey_documentation root directory.
    The script will find all *_variable_dictionary.json / *_questions.json pairs.

  Option B — Manual SURVEY_FILES dict:
    Comment out the _auto_discover_surveys call and set SURVEY_FILES directly
    (see commented example below).
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION — update DOCS to match your project layout
# ---------------------------------------------------------------------------
# This script lives at .claude/skills/survey-expert/search_survey.py
# REPO_ROOT is 3 levels up (skills/ -> .claude/ -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[3]

# TODO: Update this path to your project's survey_documentation directory.
# Examples:
#   REPO_ROOT / "docs" / "ltfu" / "survey_documentation"   (g2r-main LTFU)
#   REPO_ROOT / "8. Baseline" / "survey_documentation"      (brac_try)
DOCS = REPO_ROOT / "docs" / "survey_documentation"  # TODO: adjust


def _auto_discover_surveys(docs_dir: Path) -> dict:
    """
    Walk docs_dir, find *_variable_dictionary.json files, pair with questions.json.
    Returns dict: {label: {questions: Path, vardict: Path}}
    """
    survey_files = {}
    if not docs_dir.exists():
        return survey_files
    for vdict_path in sorted(docs_dir.rglob("*_variable_dictionary.json")):
        stem  = vdict_path.stem  # e.g. "girls_variable_dictionary"
        label = stem.replace("_variable_dictionary", "")
        q_path = vdict_path.parent / f"{label}_questions.json"
        if not q_path.exists():
            for candidate in vdict_path.parent.glob("*_questions.json"):
                q_path = candidate
                break
        if q_path.exists():
            survey_files[label] = {"questions": q_path, "vardict": vdict_path}
    return survey_files


SURVEY_FILES = _auto_discover_surveys(DOCS)

# ---------------------------------------------------------------------------
# Option B: override SURVEY_FILES manually if auto-discovery doesn't work.
# Example for brac_try:
#
# DOCS_GIRLS = REPO_ROOT / "8. Baseline" / "survey_documentation" / "girls"
# DOCS_NORMS = REPO_ROOT / "8. Baseline" / "survey_documentation" / "norms"
# SURVEY_FILES = {
#     "girls": {
#         "questions": DOCS_GIRLS / "girls_questions.json",
#         "vardict":   DOCS_GIRLS / "girls_variable_dictionary.json",
#     },
#     "norms": {
#         "questions": DOCS_NORMS / "norms_questions.json",
#         "vardict":   DOCS_NORMS / "norms_variable_dictionary.json",
#     },
# }
# ---------------------------------------------------------------------------


def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sentinel_note(choices, constraint):
    """Extract sentinel codes from choices list (negative values) or constraint string."""
    sentinels = []
    if choices:
        for c in choices:
            try:
                v = int(c["value"])
                if v < 0:
                    sentinels.append(f"{v} ({c['label'].strip()})")
            except (ValueError, KeyError):
                pass
    if constraint and not sentinels:
        for m in re.finditer(r"\.\s*=\s*(-\d+)", constraint):
            sentinels.append(m.group(1))
    return ", ".join(sentinels) if sentinels else "none"


def fmt_choices(choices):
    if not choices:
        return "n/a"
    parts = [f"  {c['value']:>4} = {c['label'].strip()}" for c in choices]
    return "\n" + "\n".join(parts)


def extract_gate_vars(relevance: str) -> list:
    """Parse all ${variable_name} references from a SurveyCTO relevance expression."""
    return re.findall(r"\$\{([^}]+)\}", relevance or "")


def fmt_gate_summary(gate_q: dict) -> str:
    """One-line summary of a gate variable: type, choices/constraint, its own relevance."""
    qt = (gate_q.get("question_text") or "").strip()
    cl = gate_q.get("choice_list") or ""
    choices = gate_q.get("choices") or []
    constraint = gate_q.get("constraint") or ""
    own_rel = gate_q.get("relevance") or ""
    grp_rels = gate_q.get("group_relevances") or []

    # Build a compact choices/constraint note
    if choices:
        choice_str = ", ".join(
            f"{c['value']}={c['label'].strip()}" for c in choices
        )
        type_note = f"[{cl}: {choice_str}]"
    elif constraint:
        type_note = f"[constraint: {constraint}]"
    else:
        type_note = f"[{gate_q.get('type', '?')}]"

    # Resolve own gate condition
    if own_rel:
        gate_note = f"asked when: {own_rel}"
    elif grp_rels:
        gate_note = f"asked when: {'; '.join(grp_rels)}"
    else:
        gate_note = "always asked"

    return f"    {qt}\n      {type_note}\n      {gate_note}"


def print_why_asked(q: dict, q_index: dict) -> None:
    """Resolve all gate variables in the relevance expression and print their details."""
    relevance = q.get("relevance") or ""
    grp_rels = q.get("group_relevances") or []

    # Collect all gate vars from both relevance and group_relevances
    gate_vars = extract_gate_vars(relevance)
    for gr in grp_rels:
        gate_vars += extract_gate_vars(gr)
    gate_vars = list(dict.fromkeys(gate_vars))  # deduplicate, preserve order

    if not gate_vars:
        return

    print(f"  why asked  :")
    for gvar in gate_vars:
        gate_q = q_index.get(gvar)
        if gate_q:
            print(f"    {gvar}:")
            print(fmt_gate_summary(gate_q))
        else:
            print(f"    {gvar}: (not found in questions.json)")


def print_context(questions: list, target_idx: int, n: int) -> None:
    """Print N adjacent questions on each side of target_idx in compact form."""
    if n <= 0:
        return

    start = max(0, target_idx - n)
    end   = min(len(questions), target_idx + n + 1)

    print(f"\n  --- adjacent questions (survey order, context={n}) ---")
    for i in range(start, end):
        q = questions[i]
        vn  = q.get("variable_name", "?")
        qt  = (q.get("question_text") or "").strip()[:65]
        rel = q.get("relevance") or ""

        marker = ">>>" if i == target_idx else "   "
        print(f"  {marker} [{i:4d}] {vn}")
        print(f"           Q: {qt}")
        if rel:
            print(f"           relevance: {rel}")


def print_question(q: dict, survey_label: str, stata_name: str = None,
                   questions: list = None, target_idx: int = None,
                   context: int = 0, q_index: dict = None) -> None:
    """Print one question record with optional context and why-asked sections."""
    print(f"\n{'-'*60}")
    form_name = q.get("variable_name", "?")
    if stata_name and stata_name != form_name:
        print(f"[{survey_label}]  stata: {stata_name}  (form: {form_name})")
    else:
        print(f"[{survey_label}]  {form_name}")

    stata_type = q.get("_stata_type")
    if stata_type:
        print(f"  stata_type : {stata_type}")
    non_null = q.get("_non_null_count")
    if non_null is not None:
        print(f"  non_null   : {non_null:,}")
    print(f"  form_type  : {q.get('type', '?')}")
    print(f"  question   : {(q.get('question_text') or '').strip()}")
    print(f"  choice_list: {q.get('choice_list') or 'none'}")
    print(f"  choices    : {fmt_choices(q.get('choices'))}")
    print(f"  sentinels  : {sentinel_note(q.get('choices'), q.get('constraint'))}")
    print(f"  constraint : {q.get('constraint') or 'none'}")
    print(f"  relevance  : {q.get('relevance') or 'none'}")
    grp = q.get("group_relevances") or []
    if grp:
        print(f"  grp_relev  : {'; '.join(grp)}")
    print(f"  skip_logic : {q.get('stata_skip_logic') or 'none'}")
    path = q.get("group_path") or []
    print(f"  group_path : {' / '.join(path) if isinstance(path, list) else path}")

    # Why asked: resolve gate variables from relevance expression
    if q_index is not None:
        print_why_asked(q, q_index)

    # Adjacent questions in survey order
    if questions is not None and target_idx is not None and context > 0:
        print_context(questions, target_idx, context)


def _questions_by_name(questions: list) -> dict:
    """Build {variable_name: question_record} index."""
    return {q["variable_name"]: q for q in questions if "variable_name" in q}


def search_var(stata_name: str, context: int = 0) -> bool:
    """
    Two-pass lookup:
      Pass 1 — variable_dictionary (Stata name -> original SurveyCTO name + Stata type)
      Pass 2 — questions.json (SurveyCTO name -> constraint, choices, relevance)
    Falls back to direct questions.json search if variable_dictionary is missing.
    """
    found = False
    for label, files in SURVEY_FILES.items():
        qpath = files["questions"]
        vpath = files["vardict"]
        if not qpath.exists():
            continue

        questions = load_json(qpath)
        q_index   = _questions_by_name(questions)

        # Pass 1: variable_dictionary lookup
        original_name = stata_name
        stata_type    = None

        non_null_count = None
        if vpath.exists():
            vardict = load_json(vpath).get("variables", {})
            entry   = vardict.get(stata_name)
            if entry:
                stata_type     = entry.get("stata", {}).get("type")
                non_null_count = entry.get("non_null_count")
                orig           = entry.get("survey", {}).get("original_variable_name")
                if orig:
                    original_name = orig

        # Pass 2: questions.json lookup by original SurveyCTO name
        q = q_index.get(original_name)
        if q is None and original_name != stata_name:
            q = q_index.get(stata_name)
        if q is None:
            continue

        # Find index for context printing
        try:
            target_idx = next(i for i, x in enumerate(questions)
                              if x.get("variable_name") == q.get("variable_name"))
        except StopIteration:
            target_idx = None

        if stata_type:
            q = dict(q, _stata_type=stata_type)
        if non_null_count is not None:
            q = dict(q, _non_null_count=non_null_count)

        print_question(
            q, label,
            stata_name  = stata_name if stata_name != original_name else None,
            questions   = questions,
            target_idx  = target_idx,
            context     = context,
            q_index     = q_index,
        )
        found = True

    return found


def search_choice_list(list_name: str, context: int = 0) -> bool:
    found = False
    for label, files in SURVEY_FILES.items():
        qpath = files["questions"]
        if not qpath.exists():
            continue
        questions = load_json(qpath)
        q_index   = _questions_by_name(questions)
        for i, q in enumerate(questions):
            if q.get("choice_list") == list_name:
                print_question(q, label, questions=questions, target_idx=i,
                               context=context, q_index=q_index)
                found = True
    return found


def search_text(text: str, context: int = 0) -> bool:
    text_lower = text.lower()
    found = False
    for label, files in SURVEY_FILES.items():
        qpath = files["questions"]
        if not qpath.exists():
            continue
        questions = load_json(qpath)
        q_index   = _questions_by_name(questions)
        for i, q in enumerate(questions):
            qt = q.get("question_text") or ""
            vn = q.get("variable_name") or ""
            if text_lower in qt.lower() or text_lower in vn.lower():
                print_question(q, label, questions=questions, target_idx=i,
                               context=context, q_index=q_index)
                found = True
    return found


def main():
    if not SURVEY_FILES:
        print(f"WARNING: No survey files found under {DOCS}", file=sys.stderr)
        print("Update DOCS or SURVEY_FILES at the top of search_survey.py.", file=sys.stderr)

    parser = argparse.ArgumentParser(description="Survey variable lookup")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--var",         metavar="NAME", help="Look up a Stata variable by exact name")
    grp.add_argument("--choice-list", metavar="LIST", help="List all variables using a choice list")
    grp.add_argument("--search",      metavar="TEXT", help="Search question text or variable names")
    parser.add_argument("--context", metavar="N", type=int, default=0,
                        help="Show N adjacent questions on each side + resolve relevance gate (default: 0)")
    args = parser.parse_args()

    if args.var:
        ok = search_var(args.var, context=args.context)
    elif args.choice_list:
        ok = search_choice_list(args.choice_list, context=args.context)
    else:
        ok = search_text(args.search, context=args.context)

    if not ok:
        print("No matches found.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
