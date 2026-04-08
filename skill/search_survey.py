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
import math
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Lightweight TF-IDF index (shared with mcp/survey_server.py)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase alpha-numeric tokens (>= 2 chars)."""
    return _TOKEN_RE.findall(text.lower())


class _TfidfIndex:
    """Minimal TF-IDF index for cosine-similarity search over text documents."""

    __slots__ = ("_doc_ids", "_doc_tfs", "_idf", "_doc_norms")

    def __init__(self, docs: list[tuple[str, str]]):
        self._doc_ids: list[str] = []
        self._doc_tfs: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}
        self._doc_norms: list[float] = []
        self._build(docs)

    def _build(self, docs: list[tuple[str, str]]):
        n = len(docs)
        if n == 0:
            return
        df: dict[str, int] = {}
        for doc_id, text in docs:
            tokens = _tokenize(text)
            tf: dict[str, float] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0.0) + 1.0
            doc_len = len(tokens) or 1
            for tok in tf:
                tf[tok] /= doc_len
            self._doc_ids.append(doc_id)
            self._doc_tfs.append(tf)
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1
        self._idf = {
            tok: math.log((n + 1) / (freq + 1)) + 1
            for tok, freq in df.items()
        }
        for tf in self._doc_tfs:
            norm_sq = sum(
                (w * self._idf.get(tok, 0.0)) ** 2 for tok, w in tf.items()
            )
            self._doc_norms.append(math.sqrt(norm_sq) or 1.0)

    def query(self, text: str, max_results: int = 20,
              min_score: float = 0.01) -> list[tuple[str, float]]:
        """Return top doc_ids ranked by cosine similarity to *text*."""
        tokens = _tokenize(text)
        if not tokens or not self._doc_ids:
            return []
        q_tf: dict[str, float] = {}
        for tok in tokens:
            q_tf[tok] = q_tf.get(tok, 0.0) + 1.0
        q_len = len(tokens)
        for tok in q_tf:
            q_tf[tok] /= q_len
        q_vec = {tok: w * self._idf.get(tok, 0.0) for tok, w in q_tf.items()}
        q_norm = math.sqrt(sum(v ** 2 for v in q_vec.values())) or 1.0
        scored: list[tuple[str, float]] = []
        for i, tf in enumerate(self._doc_tfs):
            dot = sum(q_vec.get(tok, 0.0) * w * self._idf.get(tok, 0.0)
                      for tok, w in tf.items() if tok in q_vec)
            if dot <= 0:
                continue
            score = dot / (self._doc_norms[i] * q_norm)
            if score >= min_score:
                scored.append((self._doc_ids[i], score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:max_results]


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


MAX_DISPLAY_LEN = 200  # cap long text (consent relevance, group_relevances, etc.)


def _truncate(text: str, limit: int = MAX_DISPLAY_LEN) -> str:
    """Truncate text with a character count suffix if it exceeds limit."""
    if not text or len(text) <= limit:
        return text
    return text[:limit] + f" [...{len(text) - limit} more chars]"


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

    return f"    {_truncate(qt)}\n      {type_note}\n      {gate_note}"


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
            print(f"           relevance: {_truncate(rel)}")


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
    d_min = q.get("_data_min")
    d_max = q.get("_data_max")
    if d_min is not None and d_max is not None:
        print(f"  data_range : {d_min} to {d_max}")

    # Repeat group summary — ALWAYS shown first if applicable; critical for position-vs-code decisions
    ri = q.get("_repeat_info")
    if ri is not None:
        max_s = f"of {ri['max_iter']} (max observed)" if ri["max_iter"] else "of ?"
        print(f"  *** REPEAT GROUP ***")
        print(f"  repeat     : iteration {ri['iteration']} {max_s}")
        print(f"  repeat_grp : {ri['base']}  (count var: {ri['count_key']})")
        print(f"  pos_vs_code: VERIFY -- if count is constant across HHs: position==code (safe).")
        print(f"               If count varies by HH (select_multiple-gated repeat): position!=code")
        print(f"               (need routing variable to map position -> choice code).")
        print(f"               Check: tabulate {ri['count_key']} to see if count varies.")

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


def _filter_surveys(survey_filter: str = None) -> dict:
    """Return SURVEY_FILES filtered by --survey flag. Supports partial match."""
    if not survey_filter:
        return SURVEY_FILES
    matched = {k: v for k, v in SURVEY_FILES.items()
               if survey_filter in k}
    if not matched:
        print(f"WARNING: --survey '{survey_filter}' matched no surveys. "
              f"Available: {', '.join(SURVEY_FILES.keys())}", file=sys.stderr)
    return matched


def search_var(stata_name: str, context: int = 0, survey_filter: str = None) -> bool:
    """
    Two-pass lookup:
      Pass 1 — variable_dictionary (Stata name -> original SurveyCTO name + Stata type)
      Pass 2 — questions.json (SurveyCTO name -> constraint, choices, relevance)
    Falls back to direct questions.json search if variable_dictionary is missing.
    """
    found = False
    for label, files in _filter_surveys(survey_filter).items():
        qpath = files["questions"]
        vpath = files["vardict"]
        if not qpath.exists():
            continue

        questions = load_json(qpath)
        q_index   = _questions_by_name(questions)

        # Pass 1: variable_dictionary lookup
        original_name  = stata_name
        stata_type     = None
        non_null_count = None
        data_min       = None
        data_max       = None
        repeat_info    = None   # filled below if variable is from a repeat group

        if vpath.exists():
            vardict = load_json(vpath).get("variables", {})
            entry   = vardict.get(stata_name)
            if entry:
                stata_type     = entry.get("stata", {}).get("type")
                non_null_count = entry.get("non_null_count")
                data_min       = entry.get("data_min")
                data_max       = entry.get("data_max")
                orig           = entry.get("survey", {}).get("original_variable_name")
                if orig:
                    original_name = orig

                # Repeat group detection — surface position-vs-code info prominently
                repeat_iter = entry.get("repeat_iteration")
                if repeat_iter is not None:
                    # Derive max iteration from all variables sharing the same form name
                    sibling_iters = [
                        v.get("repeat_iteration")
                        for k, v in vardict.items()
                        if isinstance(v, dict)
                        and v.get("survey", {}).get("original_variable_name") == original_name
                        and isinstance(v.get("repeat_iteration"), int)
                    ]
                    max_iter = max(sibling_iters) if sibling_iters else None

                    # Find the _count variable: search all vardict entries with
                    # type=="repeat_count" and a repeat_group_base that appears in
                    # the target variable's group_path.
                    gpath = entry.get("survey", {}).get("group_path", "") or ""
                    gpath_str = gpath if isinstance(gpath, str) else "/".join(gpath)
                    gpath_parts = [p.strip() for p in gpath_str.replace("/", " ").split()]

                    count_key   = None
                    count_entry = {}
                    for ck, cv in vardict.items():
                        if not isinstance(cv, dict):
                            continue
                        if cv.get("survey", {}).get("type") == "repeat_count":
                            base = cv.get("repeat_metadata", {}).get("repeat_group_base", "")
                            if base and base in gpath_parts:
                                count_key   = ck
                                count_entry = cv
                                break
                    # Fallback: walk group_path from innermost outward, try <part>_count
                    if count_key is None:
                        for part in reversed(gpath_parts):
                            candidate = f"{part}_count"
                            if candidate in vardict:
                                count_key   = candidate
                                count_entry = vardict[candidate]
                                break

                    # A FIXED repeat count means the count var is non-null for ALL HHs
                    # (i.e. the repeat always runs the same number of times).
                    # VARIABLE count = the repeat is driven by a prior select_multiple.
                    total_obs     = load_json(vpath).get("dataset", {}).get("n_observations")
                    count_nonnull = count_entry.get("non_null_count") if count_entry else None
                    fixed = (
                        count_nonnull is not None
                        and total_obs is not None
                        and count_nonnull == total_obs
                    )
                    base_display = (
                        count_entry.get("repeat_metadata", {}).get("repeat_group_base")
                        or (count_key.replace("_count", "") if count_key else "?")
                    )
                    repeat_info = {
                        "iteration": repeat_iter,
                        "max_iter":  max_iter,
                        "base":      base_display,
                        "count_key": count_key or "?",
                        "fixed":     fixed,
                    }

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
        if data_min is not None and data_max is not None:
            q = dict(q, _data_min=data_min, _data_max=data_max)
        if repeat_info is not None:
            q = dict(q, _repeat_info=repeat_info)

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


def search_choice_list(list_name: str, context: int = 0, survey_filter: str = None) -> bool:
    found = False
    for label, files in _filter_surveys(survey_filter).items():
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


def search_text(text: str, context: int = 0, survey_filter: str = None) -> bool:
    found = False
    for label, files in _filter_surveys(survey_filter).items():
        qpath = files["questions"]
        vpath = files["vardict"]
        if not qpath.exists():
            continue
        questions = load_json(qpath)
        q_index   = _questions_by_name(questions)

        # Build TF-IDF index: one document per question, text = var_name + question_text
        vardict = {}
        if vpath.exists():
            vardict = load_json(vpath).get("variables", {})

        tfidf_docs: list[tuple[str, str]] = []
        for q in questions:
            vn = q.get("variable_name") or ""
            qt = q.get("question_text") or ""
            tfidf_docs.append((vn, vn.replace("_", " ") + " " + qt))
        # Also index vardict variables not in questions (Stata-renamed vars)
        q_varnames = {d[0] for d in tfidf_docs}
        for var_name, entry in vardict.items():
            if var_name in q_varnames or not isinstance(entry, dict):
                continue
            survey_data = entry.get("survey", {})
            qt = survey_data.get("question_text") or ""
            form_name = survey_data.get("original_variable_name") or ""
            tfidf_docs.append((
                var_name,
                var_name.replace("_", " ") + " " + qt + " " + form_name.replace("_", " "),
            ))

        idx = _TfidfIndex(tfidf_docs)
        hits = idx.query(text, max_results=20)

        for var_name, _score in hits:
            q = q_index.get(var_name)
            if q is None:
                continue
            try:
                target_idx = next(
                    i for i, x in enumerate(questions)
                    if x.get("variable_name") == var_name
                )
            except StopIteration:
                target_idx = None
            print_question(q, label, questions=questions, target_idx=target_idx,
                           context=context, q_index=q_index)
            found = True
    return found


def gate_chain(stata_name: str, survey_filter: str = None) -> bool:
    """Build the full composed gate chain for a variable.

    Walks from group-level relevances (outermost) through the variable's
    own relevance, recursively resolving each ${ref} to its question text,
    choice list, and own gate condition.  Returns an indented tree.
    """
    found = False
    for label, files in _filter_surveys(survey_filter).items():
        qpath = files["questions"]
        vpath = files["vardict"]
        if not qpath.exists():
            continue

        questions = load_json(qpath)
        q_index = _questions_by_name(questions)

        # Find variable in vardict
        original_name = stata_name
        vardict = {}
        entry = {}
        if vpath.exists():
            raw = load_json(vpath)
            vardict = raw.get("variables", {})
            entry = vardict.get(stata_name, {})
            orig = entry.get("survey", {}).get("original_variable_name")
            if orig:
                original_name = orig

        # Get question from questions.json
        q = q_index.get(original_name)
        if q is None and original_name != stata_name:
            q = q_index.get(stata_name)
        if q is None and not entry:
            continue

        found = True

        # Collect all conditions: group relevances (outer->inner) + own
        grp_rels = []
        own_rel = ""
        if q:
            grp_rels = q.get("group_relevances") or []
            own_rel = q.get("relevance") or ""
        else:
            grp_rels = entry.get("survey", {}).get("group_relevances") or []

        all_conditions = list(grp_rels) + ([own_rel] if own_rel else [])

        lines = [f"[{label}] Gate chain for: {stata_name}"]

        if not all_conditions:
            lines.append("  (always asked -- no skip logic)")
            print("\n".join(lines))
            continue

        # Cache for resolved gate variables
        resolved_cache = {}

        def _resolve_var(vname):
            if vname in resolved_cache:
                return resolved_cache[vname]
            gq = q_index.get(vname)
            info = {"name": vname}
            if gq:
                info["question"] = (gq.get("question_text") or "").strip()[:80]
                info["type"] = gq.get("type") or "?"
                gchoices = gq.get("choices") or []
                gcl = gq.get("choice_list") or ""
                if gchoices:
                    cstr = ", ".join(
                        f"{c['value']}={c['label'].strip()}"
                        for c in gchoices[:6]
                    )
                    info["choices"] = f"{gcl}: {cstr}"
                else:
                    info["choices"] = ""
                info["own_relevance"] = gq.get("relevance") or ""
            else:
                # Try vardict
                ve = vardict.get(vname, {})
                if ve:
                    sd = ve.get("survey", {})
                    info["question"] = (sd.get("question_text") or "").strip()[:80]
                    info["type"] = sd.get("type") or "?"
                    info["choices"] = ""
                    info["own_relevance"] = ""
                else:
                    info["question"] = "(not found in survey)"
                    info["type"] = "?"
                    info["choices"] = ""
                    info["own_relevance"] = ""
            resolved_cache[vname] = info
            return info

        indent = 0
        for i, cond in enumerate(all_conditions):
            is_group = i < len(grp_rels)
            prefix = "  " * indent
            source = "GROUP" if is_group else "VAR"

            refs = re.findall(r"\$\{([^}]+)\}", cond)
            display_cond = re.sub(r"\$\{([^}]+)\}", r"\1", cond)

            lines.append(f"{prefix}[{source}] {display_cond}")

            for ref in refs:
                rinfo = _resolve_var(ref)
                qt = rinfo.get("question", "")
                choices = rinfo.get("choices", "")
                own = rinfo.get("own_relevance", "")

                lines.append(f"{prefix}  ^ {ref}: {qt}")
                if choices:
                    lines.append(f"{prefix}    [{choices}]")
                if own:
                    own_display = re.sub(r"\$\{([^}]+)\}", r"\1", own)
                    lines.append(f"{prefix}    asked when: {own_display}")
                else:
                    lines.append(f"{prefix}    always asked")

            indent += 1

        # Final line: the target variable itself
        prefix = "  " * indent
        survey_data = entry.get("survey", {}) if entry else {}
        qt = (survey_data.get("question_text") or (q.get("question_text") if q else "") or "").strip()[:80]
        lines.append(f"{prefix}>>> {stata_name}: {qt}")

        # Non-null vs total
        non_null = entry.get("non_null_count")
        total = None
        if vpath.exists():
            total = load_json(vpath).get("dataset", {}).get("n_observations")
        if non_null is not None and total is not None:
            lines.append(f"{prefix}    non_null: {non_null:,} / {total:,}")

        print("\n".join(lines))

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
    grp.add_argument("--gate-chain",  metavar="NAME", help="Show full composed skip logic tree for a variable")
    parser.add_argument("--context", metavar="N", type=int, default=0,
                        help="Show N adjacent questions on each side + resolve relevance gate (default: 0)")
    parser.add_argument("--survey", metavar="KEY", default=None,
                        help="Filter to surveys whose key contains KEY (e.g. 'endline', 'ltfu', 'c2')")
    args = parser.parse_args()

    if args.var:
        ok = search_var(args.var, context=args.context, survey_filter=args.survey)
    elif args.choice_list:
        ok = search_choice_list(args.choice_list, context=args.context, survey_filter=args.survey)
    elif args.gate_chain:
        ok = gate_chain(args.gate_chain, survey_filter=args.survey)
    else:
        ok = search_text(args.search, context=args.context, survey_filter=args.survey)

    if not ok:
        print("No matches found.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
