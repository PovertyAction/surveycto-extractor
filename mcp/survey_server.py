"""
SurveyCTO Survey Expert -- MCP Server (optional add-on).

Keeps survey metadata in memory for instant variable lookups.
Drop-in complement to the search_survey.py skill when query
volume is high (10-50+ lookups per cleaning module).

Graceful degradation:
  - No config.py          -> server starts, tools explain what's needed
  - DATASETS empty        -> same
  - JSON files missing    -> skips that survey, loads what's available
  - Malformed JSON        -> skips with warning
  - Some surveys loaded   -> tools work on what's available

Setup:
  1. pip install "mcp[cli]"
  2. Add to your project's .mcp.json:
     {
       "mcpServers": {
         "survey-expert": {
           "command": "python",
           "args": ["<path-to>/surveycto_extractor/mcp/survey_server.py"]
         }
       }
     }
  3. The server auto-discovers config.py in its parent directory.
     Override with SURVEY_CONFIG env var if needed.
"""

import json
import os
import re
import sys
import importlib.util
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config discovery (never raises — returns None on failure)
# ---------------------------------------------------------------------------

def _find_config() -> Optional[Path]:
    """Locate config.py: env var SURVEY_CONFIG, or parent of mcp/."""
    env = os.environ.get("SURVEY_CONFIG")
    if env:
        p = Path(env)
        if p.exists():
            return p
        print(f"[survey-expert] WARNING: SURVEY_CONFIG={env} not found", file=sys.stderr)
    default = Path(__file__).resolve().parent.parent / "config.py"
    if default.exists():
        return default
    print(
        f"[survey-expert] config.py not found at {default}. "
        f"Server will start with no data. Set SURVEY_CONFIG env var "
        f"or run Phase 4 (create_variable_dictionaries.py) first.",
        file=sys.stderr,
    )
    return None


def _load_config_module(path: Path) -> Optional[object]:
    """Import config.py dynamically. Returns None on failure."""
    try:
        spec = importlib.util.spec_from_file_location("config", str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        print(f"[survey-expert] Failed to load {path}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

MAX_DISPLAY = 200


def _trunc(text: str, limit: int = MAX_DISPLAY) -> str:
    if not text or len(text) <= limit:
        return text
    return text[:limit] + f" [...{len(text) - limit} chars]"


def _fmt_sentinel_compact(sentinels: dict | None) -> str:
    """Compact sentinel summary for batch output."""
    if not sentinels:
        return "none"
    parts = []
    if sentinels.get("raw_int_detail"):
        codes = ",".join(sentinels["raw_int_detail"].keys())
        parts.append(f"raw_int:{codes}")
    elif sentinels.get("raw_int"):
        parts.append(f"raw_int({sentinels['raw_int']})")
    if sentinels.get("string_detail"):
        codes = ",".join(sentinels["string_detail"].keys())
        parts.append(f"string:{codes}")
    elif sentinels.get("string"):
        parts.append(f"string({sentinels['string']})")
    if sentinels.get("ext_missing_detail"):
        codes = ",".join(f".{k}" for k in sentinels["ext_missing_detail"].keys())
        parts.append(f"ext_missing:{codes}")
    elif sentinels.get("ext_missing"):
        parts.append(f"ext_missing({sentinels['ext_missing']})")
    if sentinels.get("type_mismatch"):
        parts.append("type_mismatch")
    if sentinels.get("calculate_risk"):
        parts.append("calculate_risk")
    return "; ".join(parts) if parts else "none"


def _fmt_sentinel_full(sentinels: dict | None) -> str:
    """Detailed sentinel summary for single lookup."""
    if not sentinels:
        return "none"
    parts = []
    if sentinels.get("raw_int_detail"):
        detail = sentinels["raw_int_detail"]
        codes = ", ".join(f"{k} ({v} obs)" for k, v in detail.items())
        parts.append(f"raw_int: {codes}")
    elif sentinels.get("raw_int"):
        parts.append(f"raw_int: {sentinels['raw_int']} obs")
    if sentinels.get("string_detail"):
        detail = sentinels["string_detail"]
        codes = ", ".join(f'"{k}" ({v} obs)' for k, v in detail.items())
        parts.append(f"string: {codes}")
    elif sentinels.get("string"):
        parts.append(f"string: {sentinels['string']} obs")
    if sentinels.get("ext_missing_detail"):
        detail = sentinels["ext_missing_detail"]
        codes = ", ".join(f".{k} ({v} obs)" for k, v in detail.items())
        parts.append(f"ext_missing: {codes}")
    elif sentinels.get("ext_missing"):
        parts.append(f"ext_missing: {sentinels['ext_missing']} obs")
    if sentinels.get("type_mismatch"):
        parts.append("type_mismatch: form says numeric but Stata has string")
    if sentinels.get("calculate_risk"):
        parts.append("calculate_risk: unexpected negative values in calculate field")
    return "; ".join(parts) if parts else "none"


def _sentinel_from_choices(choices: list[dict] | None, constraint: str | None) -> str:
    """Extract sentinel codes defined in the form (from choices or constraint)."""
    codes = []
    if choices:
        for c in choices:
            try:
                v = int(c.get("value", ""))
                if v < 0:
                    codes.append(f"{v} ({c.get('label', '').strip()})")
            except (ValueError, TypeError):
                pass
    if constraint and not codes:
        for m in re.finditer(r"\.\s*=\s*(-\d+)", constraint):
            codes.append(m.group(1))
    return ", ".join(codes) if codes else "none"


def _is_sentinel_value(value) -> bool:
    """Check if a choice value is a sentinel code (negative integer)."""
    try:
        return int(value) < 0
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------------

class SurveyStore:
    """In-memory store for survey metadata with auto-reload on file changes."""

    def __init__(self, datasets_config: dict | None = None):
        self._config = datasets_config or {}
        self._mtimes: dict[str, float] = {}

        # Per-survey loaded data
        self._vardicts: dict[str, dict] = {}
        self._questions: dict[str, list] = {}
        self._q_index: dict[str, dict] = {}
        self._dataset_meta: dict[str, dict] = {}

        # Pre-computed indexes (rebuilt on load)
        self._max_iters: dict[str, dict[str, int]] = {}
        self._count_vars: dict[str, dict[str, str]] = {}
        self._q_order: dict[str, dict[str, int]] = {}

        self._load_all()

    @property
    def is_empty(self) -> bool:
        return not any(self._vardicts.values()) and not any(self._questions.values())

    @property
    def survey_labels(self) -> list[str]:
        return sorted(set(list(self._vardicts.keys()) + list(self._questions.keys())))

    def _empty_message(self) -> str:
        """Helpful message when no data is loaded."""
        if not self._config:
            return (
                "No survey data loaded.\n\n"
                "The MCP server could not find config.py or DATASETS is empty.\n"
                "To fix:\n"
                "  1. Ensure config.py exists (copy from config.template.py)\n"
                "  2. Add entries to DATASETS with questions_json and output_json paths\n"
                "  3. Run create_variable_dictionaries.py to generate the JSON files\n"
                "  4. Restart the Claude Code session to reload the MCP server"
            )
        # Config exists but no data loaded — JSONs probably don't exist yet
        missing = []
        for label, cfg in self._config.items():
            v_path = Path(cfg.get("output_json", ""))
            q_path = Path(cfg.get("questions_json", ""))
            if not v_path.exists() and not q_path.exists():
                missing.append(label)
        if missing:
            return (
                f"No survey data loaded. JSON files not found for: {', '.join(missing)}.\n\n"
                f"Run the extractor pipeline first:\n"
                f"  python main.py --survey <KEY>                    # Phase 1-3\n"
                f"  python create_variable_dictionaries.py --survey <KEY>  # Phase 4\n"
                f"Then restart the Claude Code session."
            )
        return "No survey data loaded (unknown reason). Check server stderr for details."

    # -- Survey filtering -----------------------------------------------

    def _filtered_labels(self, survey: str | None) -> list[str]:
        """Return survey labels, filtered by partial match if survey is given."""
        labels = self.survey_labels
        if not survey:
            return labels
        matched = [l for l in labels if survey.lower() in l.lower()]
        if not matched:
            return []  # caller handles the "no match" message
        return matched

    def _no_survey_match_msg(self, survey: str) -> str:
        available = ", ".join(self.survey_labels) if self.survey_labels else "(none)"
        return f"No survey matching '{survey}'. Available: {available}"

    # -- Loading --------------------------------------------------------

    def _mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _load_all(self):
        for label, cfg in self._config.items():
            self._load_survey(label, cfg)
        n_surveys = len([l for l in self._vardicts if self._vardicts[l]])
        n_vars = sum(len(v) for v in self._vardicts.values())
        n_qs = sum(len(q) for q in self._questions.values())
        if n_surveys:
            print(
                f"[survey-expert] Loaded {n_surveys} survey(s): "
                f"{n_vars:,} variables, {n_qs:,} questions",
                file=sys.stderr,
            )
        else:
            print(
                "[survey-expert] No survey data loaded. "
                "Tools will return setup instructions.",
                file=sys.stderr,
            )

    def _load_survey(self, label: str, cfg: dict):
        q_path = Path(cfg.get("questions_json", ""))
        v_path = Path(cfg.get("output_json", ""))

        # Variable dictionary (primary)
        if v_path.exists():
            try:
                with open(v_path, encoding="utf-8") as f:
                    raw = json.load(f)
                self._vardicts[label] = raw.get("variables", {})
                self._dataset_meta[label] = {
                    "dataset": raw.get("dataset", {}),
                    "summary": raw.get("summary", {}),
                }
                self._mtimes[str(v_path)] = self._mtime(v_path)
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"[survey-expert] WARNING: Failed to load {v_path}: {exc}",
                    file=sys.stderr,
                )
                self._vardicts[label] = {}
                self._dataset_meta[label] = {}
        else:
            self._vardicts[label] = {}
            self._dataset_meta[label] = {}

        # Questions (supplement)
        if q_path.exists():
            try:
                with open(q_path, encoding="utf-8") as f:
                    questions = json.load(f)
                self._questions[label] = questions
                self._q_index[label] = {
                    q["variable_name"]: q
                    for q in questions
                    if "variable_name" in q
                }
                self._mtimes[str(q_path)] = self._mtime(q_path)
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"[survey-expert] WARNING: Failed to load {q_path}: {exc}",
                    file=sys.stderr,
                )
                self._questions[label] = []
                self._q_index[label] = {}
        else:
            self._questions[label] = []
            self._q_index[label] = {}

        # Build indexes
        self._build_indexes(label)

    def _build_indexes(self, label: str):
        vardict = self._vardicts.get(label, {})

        # Max repeat iterations per form variable name
        max_iters: dict[str, int] = {}
        for entry in vardict.values():
            if not isinstance(entry, dict):
                continue
            ri = entry.get("repeat_iteration")
            if isinstance(ri, int):
                fname = entry.get("survey", {}).get("original_variable_name", "")
                if fname and ri > max_iters.get(fname, 0):
                    max_iters[fname] = ri
        self._max_iters[label] = max_iters

        # Count variables per repeat group base
        count_vars: dict[str, str] = {}
        for var_name, entry in vardict.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("survey", {}).get("type") == "repeat_count":
                base = entry.get("repeat_metadata", {}).get("repeat_group_base", "")
                if base:
                    count_vars[base] = var_name
        self._count_vars[label] = count_vars

        # Question order index
        q_order: dict[str, int] = {}
        for i, q in enumerate(self._questions.get(label, [])):
            vn = q.get("variable_name")
            if vn:
                q_order[vn] = i
        self._q_order[label] = q_order

    def _check_reload(self):
        for label, cfg in self._config.items():
            q_path = Path(cfg.get("questions_json", ""))
            v_path = Path(cfg.get("output_json", ""))
            for p in (q_path, v_path):
                key = str(p)
                if self._mtime(p) != self._mtimes.get(key, 0.0):
                    print(f"[survey-expert] Reloading {label}...", file=sys.stderr)
                    self._load_survey(label, cfg)
                    break

    # -- Query helpers --------------------------------------------------

    def _get_repeat_info(self, label: str, entry: dict) -> dict | None:
        """Build repeat info dict for a vardict entry, all O(1)."""
        ri = entry.get("repeat_iteration")
        if ri is None:
            return None
        survey = entry.get("survey", {})
        form_name = survey.get("original_variable_name", "")
        group_path = survey.get("group_path", "") or ""
        gpath_parts = [p.strip() for p in group_path.replace("/", " ").split() if p.strip()]

        max_iter = self._max_iters.get(label, {}).get(form_name)

        # Find count variable from pre-computed index
        count_key = None
        for part in reversed(gpath_parts):
            count_key = self._count_vars.get(label, {}).get(part)
            if count_key:
                break
        # Fallback: try <part>_count
        if not count_key:
            for part in reversed(gpath_parts):
                candidate = f"{part}_count"
                if candidate in self._vardicts.get(label, {}):
                    count_key = candidate
                    break

        rmeta = entry.get("repeat_metadata", {})
        base_name = rmeta.get("repeat_group_base") or (
            count_key.replace("_count", "") if count_key else "?"
        )
        return {
            "iteration": ri,
            "max_iter": max_iter,
            "base": base_name,
            "count_key": count_key or "?",
        }

    def _resolve_gates(self, q: dict, q_index: dict) -> list[str]:
        """Resolve gate variables from relevance expressions."""
        relevance = q.get("relevance") or ""
        grp_rels = q.get("group_relevances") or []

        gate_vars = re.findall(r"\$\{([^}]+)\}", relevance)
        for gr in grp_rels:
            gate_vars += re.findall(r"\$\{([^}]+)\}", gr)
        gate_vars = list(dict.fromkeys(gate_vars))

        if not gate_vars:
            return []

        lines = ["  why asked:"]
        for gvar in gate_vars:
            gq = q_index.get(gvar)
            if gq:
                gqt = (gq.get("question_text") or "").strip()[:80]
                gchoices = gq.get("choices") or []
                gcl = gq.get("choice_list") or ""
                if gchoices:
                    cstr = ", ".join(
                        f"{c['value']}={c['label'].strip()}" for c in gchoices[:8]
                    )
                    note = f"[{gcl}: {cstr}]"
                else:
                    note = f"[{gq.get('type', '?')}]"
                own_rel = gq.get("relevance") or ""
                gate_cond = f"asked when: {own_rel}" if own_rel else "always asked"
                lines.append(f"    {gvar}: {gqt}")
                lines.append(f"      {note}")
                lines.append(f"      {gate_cond}")
            else:
                lines.append(f"    {gvar}: (not in questions.json)")
        return lines

    # -- Public query methods -------------------------------------------

    def lookup_var(self, stata_name: str, context: int = 0,
                   survey: str | None = None) -> str:
        """Full single-variable lookup: vardict primary + questions.json supplement."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []

        for label in labels:
            entry = self._vardicts.get(label, {}).get(stata_name)
            if entry is None:
                continue

            survey_data = entry.get("survey", {})
            form_name = survey_data.get("original_variable_name", stata_name)
            q = self._q_index.get(label, {}).get(form_name)

            lines = []

            # Header
            header = f"[{label}] {stata_name}"
            if form_name != stata_name:
                header += f"  (form: {form_name})"
            lines.append(header)

            # Core vardict fields
            stata_type = entry.get("stata", {}).get("type", "?")
            non_null = entry.get("non_null_count")
            nn_str = f"{non_null:,}" if isinstance(non_null, int) else str(non_null)
            lines.append(f"  stata_type   : {stata_type}")
            lines.append(f"  non_null     : {nn_str}")

            # Data range (integer/decimal/calculate/date only)
            d_min = entry.get("data_min")
            d_max = entry.get("data_max")
            if d_min is not None and d_max is not None:
                lines.append(f"  data_range   : {d_min} to {d_max}")

            lines.append(f"  form_type    : {survey_data.get('type', '?')}")
            lines.append(f"  question     : {(survey_data.get('question_text') or '').strip()}")

            # Choices (from vardict, prefer questions.json for full list)
            cl = survey_data.get("choice_list")
            choices = survey_data.get("choices")
            q_choices = q.get("choices") if q else None
            display_choices = q_choices if q_choices and isinstance(q_choices, list) else (
                choices if isinstance(choices, list) else None
            )

            if cl:
                lines.append(f"  choice_list  : {cl}")
            if display_choices:
                lines.append(f"  choices      :")
                for c in display_choices:
                    lines.append(f"    {str(c.get('value','')):>6} = {c.get('label','').strip()}")
            elif isinstance(choices, dict):
                lines.append(f"  choice_code  : {choices.get('value', '?')} = {choices.get('label', '').strip()}")
            elif not cl:
                lines.append(f"  choices      : n/a")

            # Form-defined sentinels (from choices/constraint)
            constraint = q.get("constraint") if q else None
            form_sents = _sentinel_from_choices(display_choices, constraint)
            lines.append(f"  form_sents   : {form_sents}")

            # Data-detected sentinels (from vardict sentinel scan)
            lines.append(f"  data_sents   : {_fmt_sentinel_full(entry.get('sentinels'))}")

            # Constraint (questions.json only)
            if constraint:
                lines.append(f"  constraint   : {constraint}")

            # Skip logic (prefer iteration-specific for repeat vars)
            skip_iter = survey_data.get("skip_logic_iteration_specific")
            skip = survey_data.get("stata_skip_logic")
            if skip_iter and skip_iter != skip:
                lines.append(f"  skip_logic   : {skip_iter}")
                lines.append(f"  skip_template: {skip}")
            elif skip:
                lines.append(f"  skip_logic   : {skip}")
            else:
                lines.append(f"  skip_logic   : none")

            # Group relevances
            grp_rels = survey_data.get("group_relevances") or []
            if grp_rels:
                lines.append(f"  grp_relevance: {'; '.join(str(r) for r in grp_rels)}")

            lines.append(f"  group_path   : {survey_data.get('group_path', '') or 'top-level'}")

            # Calculation (questions.json only)
            if q:
                calc = q.get("calculation")
                if calc:
                    lines.append(f"  calculation  : {calc}")
                if q.get("required"):
                    lines.append(f"  required     : yes")

            # Repeat group info (pre-computed indexes = O(1))
            rinfo = self._get_repeat_info(label, entry)
            if rinfo:
                lines.append(f"  *** REPEAT GROUP ***")
                lines.append(
                    f"  repeat       : iteration {rinfo['iteration']}"
                    f" of {rinfo['max_iter'] or '?'} (max observed)"
                )
                lines.append(
                    f"  repeat_grp   : {rinfo['base']}"
                    f"  (count var: {rinfo['count_key']})"
                )
                lines.append(
                    f"  pos_vs_code  : VERIFY -- tabulate {rinfo['count_key']}"
                    f" to check if count is constant (position==code)"
                    f" or varies (position!=code, need routing variable)."
                )

            # Select multiple info
            if entry.get("is_select_multiple"):
                lines.append(f"  *** SELECT_MULTIPLE ***")
                lines.append(f"  choice_code  : {entry.get('choice_code', '?')}")
                lines.append(f"  choice_label : {entry.get('choice_label', '?')}")

            # Context: adjacent questions + gate resolution
            if context > 0 and q:
                questions = self._questions.get(label, [])
                q_idx = self._q_order.get(label, {}).get(form_name)
                if q_idx is not None:
                    start = max(0, q_idx - context)
                    end = min(len(questions), q_idx + context + 1)
                    lines.append(f"")
                    lines.append(
                        f"  --- adjacent questions (survey order, context={context}) ---"
                    )
                    for i in range(start, end):
                        aq = questions[i]
                        vn = aq.get("variable_name", "?")
                        qt = (aq.get("question_text") or "").strip()[:65]
                        rel = aq.get("relevance") or ""
                        marker = ">>>" if i == q_idx else "   "
                        lines.append(f"  {marker} [{i:4d}] {vn}")
                        lines.append(f"           Q: {qt}")
                        if rel:
                            lines.append(f"           relevance: {_trunc(rel)}")

                # Gate resolution
                gate_lines = self._resolve_gates(q, self._q_index.get(label, {}))
                lines.extend(gate_lines)

            results.append("\n".join(lines))

        if not results:
            suggestions = self._suggest_similar(stata_name, survey)
            scope = f" in survey '{survey}'" if survey else " in any loaded survey"
            msg = f"No match for '{stata_name}'{scope}."
            if suggestions:
                msg += f"\n  Did you mean: {', '.join(suggestions[:5])}"
            return msg
        return "\n\n".join(results)

    def lookup_vars_batch(self, names: list[str],
                          survey: str | None = None) -> str:
        """Batch lookup -- compact vardict-first format."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        blocks = []
        n_found = 0

        for name in names:
            found = False
            for label in labels:
                entry = self._vardicts.get(label, {}).get(name)
                if entry is None:
                    continue
                found = True
                n_found += 1
                survey_data = entry.get("survey", {})
                form_name = survey_data.get("original_variable_name", name)
                stata_type = entry.get("stata", {}).get("type", "?")
                non_null = entry.get("non_null_count", "?")
                form_type = survey_data.get("type", "?")
                qt = (survey_data.get("question_text") or "").strip()[:80]

                skip = (
                    survey_data.get("skip_logic_iteration_specific")
                    or survey_data.get("stata_skip_logic")
                    or "none"
                )

                sent_str = _fmt_sentinel_compact(entry.get("sentinels"))

                repeat_str = ""
                rinfo = self._get_repeat_info(label, entry)
                if rinfo:
                    repeat_str = (
                        f"\n  repeat: iter {rinfo['iteration']}"
                        f"/{rinfo['max_iter'] or '?'} ({rinfo['base']})"
                    )

                sm_str = ""
                if entry.get("is_select_multiple"):
                    sm_str = (
                        f"\n  select_multiple: code={entry.get('choice_code', '?')}"
                        f" ({entry.get('choice_label', '?')})"
                    )

                header = f"=== [{label}] {name} ==="
                if form_name != name:
                    header += f"  (form: {form_name})"

                range_str = ""
                d_min = entry.get("data_min")
                d_max = entry.get("data_max")
                if d_min is not None and d_max is not None:
                    range_str = f"\n  range: {d_min} to {d_max}"

                block = (
                    f"{header}\n"
                    f"  {form_type} | stata:{stata_type} | non_null:{non_null}\n"
                    f"  Q: {qt}\n"
                    f"  skip: {_trunc(skip, 120)}\n"
                    f"  sentinels: {sent_str}"
                    f"{range_str}{repeat_str}{sm_str}"
                )
                blocks.append(block)
                break  # first matching survey in scope

            if not found:
                blocks.append(f"=== {name} ===\n  NOT FOUND")

        scope = f" (survey filter: {survey})" if survey else ""
        header = f"Batch lookup: {len(names)} variables, {n_found} found{scope}"
        return header + "\n\n" + "\n\n".join(blocks)

    def search(self, query: str, max_results: int = 20,
               survey: str | None = None) -> str:
        """Search variable names and question text across surveys."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        query_lower = query.lower()
        matches = []

        for label in labels:
            for var_name, entry in self._vardicts.get(label, {}).items():
                if not isinstance(entry, dict):
                    continue
                survey_data = entry.get("survey", {})
                qt = (survey_data.get("question_text") or "").lower()
                form_name = (survey_data.get("original_variable_name") or "").lower()
                if query_lower in var_name.lower() or query_lower in qt or query_lower in form_name:
                    form_type = survey_data.get("type", "?")
                    non_null = entry.get("non_null_count", "?")
                    qt_display = (survey_data.get("question_text") or "").strip()[:70]
                    skip = survey_data.get("stata_skip_logic") or ""
                    skip_brief = _trunc(skip, 50) if skip else ""
                    sent = _fmt_sentinel_compact(entry.get("sentinels"))

                    line = f"  [{label}] {var_name:30s} | {form_type:15s} | n={non_null}"
                    if sent != "none":
                        line += f" | SENT:{sent}"
                    if skip_brief:
                        line += f" | skip:{skip_brief}"
                    line += f"\n{'':33s}  Q: {qt_display}"
                    matches.append(line)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            scope = f" in survey '{survey}'" if survey else ""
            return f"No matches for '{query}'{scope}."

        scope = f" (survey filter: {survey})" if survey else ""
        header = f"Found {len(matches)} match(es) for '{query}'{scope}:"
        return header + "\n" + "\n".join(matches)

    def get_choice_list(self, list_name: str,
                        survey: str | None = None) -> str:
        """Return choice list values and all variables using it."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []

        for label in labels:
            using_vars = []
            choices = None

            for var_name, entry in self._vardicts.get(label, {}).items():
                if not isinstance(entry, dict):
                    continue
                survey_data = entry.get("survey", {})
                if survey_data.get("choice_list") == list_name:
                    using_vars.append(var_name)
                    if choices is None:
                        c = survey_data.get("choices")
                        if c and isinstance(c, list):
                            choices = c

            # Supplement from questions.json for full choice list
            if choices is None:
                for q in self._questions.get(label, []):
                    if q.get("choice_list") == list_name and q.get("choices"):
                        choices = q["choices"]
                        break

            if not using_vars:
                continue

            lines = [f"[{label}] Choice list: {list_name}"]
            if choices:
                lines.append("  Choices:")
                for c in choices:
                    v = c.get("value", "?")
                    lbl = c.get("label", "").strip()
                    lines.append(f"    {str(v):>6} = {lbl}")
                sents = [c for c in choices if _is_sentinel_value(c.get("value"))]
                if sents:
                    lines.append("  Sentinel codes in this list:")
                    for c in sents:
                        lines.append(
                            f"    {str(c['value']):>6} = {c.get('label', '').strip()}"
                        )
            else:
                lines.append("  Choices: (external csv or not embedded in form)")

            lines.append(f"  Variables using this list ({len(using_vars)}):")
            for vn in using_vars[:50]:
                entry = self._vardicts.get(label, {}).get(vn, {})
                qt = (entry.get("survey", {}).get("question_text") or "").strip()[:55]
                sent = _fmt_sentinel_compact(entry.get("sentinels"))
                sent_note = f" | SENT:{sent}" if sent != "none" else ""
                lines.append(f"    {vn:30s} | {qt}{sent_note}")
            if len(using_vars) > 50:
                lines.append(f"    ... and {len(using_vars) - 50} more")

            results.append("\n".join(lines))

        if not results:
            scope = f" in survey '{survey}'" if survey else ""
            return f"Choice list '{list_name}' not found{scope}."
        return "\n\n".join(results)

    def get_info(self) -> str:
        """Dataset overview for all loaded surveys."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        lines = ["Loaded surveys:"]

        for label in sorted(self._dataset_meta):
            meta = self._dataset_meta[label]
            ds = meta.get("dataset", {})
            sm = meta.get("summary", {})
            vardict = self._vardicts.get(label, {})

            if not ds and not sm and not vardict:
                lines.append(f"\n  [{label}]  (no data loaded)")
                continue

            n_obs = ds.get("n_observations", "?")
            nn_str = f"{n_obs:,}" if isinstance(n_obs, int) else str(n_obs)

            n_sent = sum(
                1 for v in vardict.values()
                if isinstance(v, dict) and v.get("sentinels")
            )
            n_repeat = sum(
                1 for v in vardict.values()
                if isinstance(v, dict) and v.get("repeat_iteration") is not None
            )

            lines.append(f"")
            lines.append(f"  [{label}]")
            lines.append(f"    observations       : {nn_str}")
            lines.append(f"    total_variables    : {sm.get('total_variables', '?')}")
            lines.append(f"    matched_to_form    : {sm.get('matched_to_questions', '?')}")
            lines.append(f"    unmatched          : {sm.get('unmatched', '?')}")
            lines.append(f"    from_repeat_groups : {sm.get('from_repeat_groups', '?')}")
            lines.append(f"    repeat_vars_loaded : {n_repeat}")
            lines.append(f"    select_multiple    : {sm.get('select_multiple_choices', '?')}")
            lines.append(f"    vars_with_sentinels: {n_sent}")

            if self._count_vars.get(label):
                lines.append(f"    repeat_groups:")
                for base, count_var in sorted(self._count_vars[label].items()):
                    lines.append(f"      {base:25s} count_var: {count_var}")

        return "\n".join(lines)

    def get_gate_chain(self, stata_name: str,
                       survey: str | None = None) -> str:
        """Build the full composed gate chain for a variable.

        Walks from group-level relevances (outermost) through the variable's
        own relevance, recursively resolving each ${ref} to its question text,
        choice list, and own gate chain.  Returns an indented tree.
        """
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []
        for label in labels:
            entry = self._vardicts.get(label, {}).get(stata_name)
            if entry is None:
                continue

            survey_data = entry.get("survey", {})
            form_name = survey_data.get("original_variable_name", stata_name)
            q = self._q_index.get(label, {}).get(form_name)
            q_index = self._q_index.get(label, {})

            # Collect all conditions: group relevances (outer->inner) + own
            grp_rels = []
            if q:
                grp_rels = q.get("group_relevances") or []
            else:
                grp_rels = survey_data.get("group_relevances") or []

            own_rel = ""
            if q:
                own_rel = q.get("relevance") or ""

            all_conditions = list(grp_rels) + ([own_rel] if own_rel else [])

            # Build tree lines
            lines = [f"[{label}] Gate chain for: {stata_name}"]
            if not all_conditions:
                lines.append("  (always asked — no skip logic)")
                results.append("\n".join(lines))
                continue

            # Track resolved vars to avoid cycles
            resolved_cache: dict[str, dict] = {}

            def _resolve_var(vname: str) -> dict:
                """Resolve a gate variable to its metadata."""
                if vname in resolved_cache:
                    return resolved_cache[vname]
                gq = q_index.get(vname)
                info: dict = {"name": vname}
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
                    own = gq.get("relevance") or ""
                    info["own_relevance"] = own
                    info["group_relevances"] = gq.get("group_relevances") or []
                else:
                    # Try vardict
                    ve = self._vardicts.get(label, {}).get(vname, {})
                    if ve:
                        sd = ve.get("survey", {})
                        info["question"] = (sd.get("question_text") or "").strip()[:80]
                        info["type"] = sd.get("type") or "?"
                        info["choices"] = ""
                        info["own_relevance"] = ""
                        info["group_relevances"] = []
                    else:
                        info["question"] = "(not found in survey)"
                        info["type"] = "?"
                        info["choices"] = ""
                        info["own_relevance"] = ""
                        info["group_relevances"] = []
                resolved_cache[vname] = info
                return info

            indent = 0
            for i, cond in enumerate(all_conditions):
                is_group = i < len(grp_rels)
                prefix = "  " * indent
                source = "GROUP" if is_group else "VAR"

                # Extract referenced variables from condition
                refs = re.findall(r"\$\{([^}]+)\}", cond)
                # Clean condition for display (replace ${x} with x)
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
            qt = (survey_data.get("question_text") or "").strip()[:80]
            lines.append(f"{prefix}>>> {stata_name}: {qt}")

            # Non-null vs total
            non_null = entry.get("non_null_count")
            total = None
            meta = self._dataset_meta.get(label, {})
            total = meta.get("dataset", {}).get("n_observations")
            if non_null is not None and total is not None:
                nn = f"{non_null:,}" if isinstance(non_null, int) else str(non_null)
                tt = f"{total:,}" if isinstance(total, int) else str(total)
                lines.append(f"{prefix}    non_null: {nn} / {tt}")

            results.append("\n".join(lines))

        if not results:
            suggestions = self._suggest_similar(stata_name, survey)
            scope = f" in survey '{survey}'" if survey else " in any loaded survey"
            msg = f"No match for '{stata_name}'{scope}."
            if suggestions:
                msg += f"\n  Did you mean: {', '.join(suggestions[:5])}"
            return msg
        return "\n\n".join(results)

    def _suggest_similar(self, name: str, survey: str | None = None,
                         max_suggestions: int = 5) -> list[str]:
        """Find variables with similar names (prefix/substring match)."""
        labels = self._filtered_labels(survey)
        name_lower = name.lower()
        candidates = []
        for label in labels:
            for var_name in self._vardicts.get(label, {}):
                vl = var_name.lower()
                if name_lower in vl or vl in name_lower:
                    candidates.append(var_name)
                elif len(name_lower) >= 5 and name_lower[:5] == vl[:5]:
                    candidates.append(var_name)
                if len(candidates) >= max_suggestions * 3:
                    break
        candidates.sort(key=lambda x: abs(len(x) - len(name)))
        return candidates[:max_suggestions]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = FastMCP(
    "survey-expert",
    instructions=(
        "Survey metadata lookup server for SurveyCTO-based research projects. "
        "Use lookup_variable for detailed single-variable investigation. "
        "Use lookup_variables for batch checks (e.g., all variables in a cleaning module). "
        "Use search_questions to discover variables by keyword. "
        "Use get_choice_list to see all choices and variables sharing a choice list. "
        "Use get_gate_chain to see the full composed skip logic tree for a variable "
        "(critical for verifying zeroing/missing logic in cleaning code). "
        "Use get_survey_info for a dataset overview before diving in. "
        "All tools accept an optional 'survey' parameter to filter by survey key "
        "(partial match, e.g., survey='ltfu' matches 'ltfu_hh' and 'ltfu_adult')."
    ),
)

# Global store, initialized lazily
_store: SurveyStore | None = None


def _get_store() -> SurveyStore:
    """Get or create the survey store. Never raises."""
    global _store
    if _store is None:
        config_path = _find_config()
        datasets = {}
        if config_path:
            mod = _load_config_module(config_path)
            if mod:
                datasets = getattr(mod, "DATASETS", {})
                if datasets:
                    print(
                        f"[survey-expert] Config: {config_path} "
                        f"({len(datasets)} dataset(s))",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[survey-expert] Config loaded but DATASETS is empty.",
                        file=sys.stderr,
                    )
        _store = SurveyStore(datasets)
    return _store


@server.tool()
def lookup_variable(name: str, context: int = 0,
                    survey: str = "") -> str:
    """Look up a single Stata variable by name.

    Returns full metadata: question text, type, Stata type, skip logic,
    form-defined sentinel codes, data-detected sentinels, choices, constraint,
    repeat group info, and select_multiple info.

    Set context > 0 to also show adjacent questions in survey order and
    resolve gate variables (why the question is asked). Recommended: context=3.

    Use the survey parameter to filter to a specific survey (partial match,
    e.g., survey="ltfu" matches "ltfu_hh" and "ltfu_adult").

    Use this for deep investigation of a specific variable.
    For checking many variables at once, use lookup_variables instead.
    """
    return _get_store().lookup_var(
        name, context=context, survey=survey or None
    )


@server.tool()
def lookup_variables(names: list[str], survey: str = "") -> str:
    """Look up multiple Stata variables at once (batch mode).

    Returns compact metadata per variable: form type, Stata type,
    non-null count, question text, skip logic, sentinel summary,
    repeat group info, and select_multiple info.

    Use this when checking a batch of variables -- e.g., all variables
    in a cleaning module (typically 10-50 variables). Much more efficient
    than calling lookup_variable repeatedly.

    Use the survey parameter to filter to a specific survey (partial match).

    For deeper investigation of a specific variable found in batch results,
    follow up with lookup_variable.
    """
    return _get_store().lookup_vars_batch(names, survey=survey or None)


@server.tool()
def search_questions(query: str, max_results: int = 20,
                     survey: str = "") -> str:
    """Search across variable names and question text by keyword.

    Searches loaded surveys. Returns matching variables with type,
    non-null count, sentinel status, skip logic, and question text.

    Use the survey parameter to filter to a specific survey (partial match).

    Use to discover related variables, find variables by topic, or
    locate all variants of a question (e.g., _poly, _other_specify).
    """
    return _get_store().search(
        query, max_results=max_results, survey=survey or None
    )


@server.tool()
def get_choice_list(list_name: str, survey: str = "") -> str:
    """Get all choices in a named choice list and all variables using it.

    Returns the full choice list (values and labels), highlights sentinel
    codes (negative values), and lists all variables referencing this list
    with their sentinel status.

    Use the survey parameter to filter to a specific survey (partial match).

    Use to understand the domain of a categorical variable, verify sentinel
    codes in the instrument, or find related variables sharing choices.
    """
    return _get_store().get_choice_list(
        list_name, survey=survey or None
    )


@server.tool()
def get_gate_chain(name: str, survey: str = "") -> str:
    """Show the full composed skip logic tree for a variable.

    Walks from outermost group relevance through the variable's own
    relevance condition, resolving each referenced variable to its
    question text, choice list, and own gate condition.

    Use this to understand WHY a variable has missing/zero values —
    critical for verifying zeroing and missing-value logic in cleaning code.

    Use the survey parameter to filter to a specific survey (partial match).
    """
    return _get_store().get_gate_chain(
        name, survey=survey or None
    )


@server.tool()
def get_survey_info() -> str:
    """Get an overview of all loaded surveys.

    Returns per-survey: observation count, variable count, matched/unmatched
    counts, repeat group summary, select_multiple count, and number of
    variables with detected sentinel issues.

    Use this to understand the scope of the dataset before diving in.
    """
    return _get_store().get_info()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _get_store()
    server.run()
