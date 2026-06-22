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
import math
import os
import re
import sys
import time
import importlib.util
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

try:
    import networkx as nx
    _NX_AVAILABLE = True
except ImportError:
    nx = None
    _NX_AVAILABLE = False

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
# Lightweight TF-IDF index (no external dependencies)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase alpha-numeric tokens (>= 2 chars)."""
    return _TOKEN_RE.findall(text.lower())


class _TfidfIndex:
    """Minimal TF-IDF index for cosine-similarity search over text documents.

    Each document is a (doc_id, text) pair.  At build time we compute IDF
    weights and per-document TF-IDF norms.  Queries are scored by cosine
    similarity against every document (fast enough for < 50 k docs).
    """

    __slots__ = ("_doc_ids", "_doc_tfs", "_idf", "_doc_norms")

    def __init__(self, docs: list[tuple[str, str]]):
        self._doc_ids: list[str] = []
        self._doc_tfs: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}
        self._doc_norms: list[float] = []
        self._build(docs)

    # -- build --------------------------------------------------------------

    def _build(self, docs: list[tuple[str, str]]):
        n = len(docs)
        if n == 0:
            return

        # Term frequency per document + document frequency
        df: dict[str, int] = {}
        for doc_id, text in docs:
            tokens = _tokenize(text)
            tf: dict[str, float] = {}
            for tok in tokens:
                tf[tok] = tf.get(tok, 0.0) + 1.0
            # Normalize TF by document length (prevents long docs dominating)
            doc_len = len(tokens) or 1
            for tok in tf:
                tf[tok] /= doc_len
            self._doc_ids.append(doc_id)
            self._doc_tfs.append(tf)
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1

        # IDF with smoothing: log((N + 1) / (df + 1)) + 1
        self._idf = {
            tok: math.log((n + 1) / (freq + 1)) + 1
            for tok, freq in df.items()
        }

        # Precompute document norms
        for tf in self._doc_tfs:
            norm_sq = sum(
                (w * self._idf.get(tok, 0.0)) ** 2 for tok, w in tf.items()
            )
            self._doc_norms.append(math.sqrt(norm_sq) or 1.0)

    # -- query --------------------------------------------------------------

    def query(self, text: str, max_results: int = 20,
              min_score: float = 0.01) -> list[tuple[str, float]]:
        """Return top doc_ids ranked by cosine similarity to *text*."""
        tokens = _tokenize(text)
        if not tokens or not self._doc_ids:
            return []

        # Query TF (raw counts, normalized)
        q_tf: dict[str, float] = {}
        for tok in tokens:
            q_tf[tok] = q_tf.get(tok, 0.0) + 1.0
        q_len = len(tokens)
        for tok in q_tf:
            q_tf[tok] /= q_len

        # Query TF-IDF vector and norm
        q_vec = {tok: w * self._idf.get(tok, 0.0) for tok, w in q_tf.items()}
        q_norm = math.sqrt(sum(v ** 2 for v in q_vec.values())) or 1.0

        # Score each document
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
# Data store
# ---------------------------------------------------------------------------

class SurveyStore:
    """In-memory store for survey metadata with auto-reload on file changes."""

    _RELOAD_DEBOUNCE_SECS = 2.0  # min seconds between file-change checks

    def __init__(self, datasets_config: dict | None = None):
        self._config = datasets_config or {}
        self._mtimes: dict[str, float] = {}
        self._last_reload_check: float = 0.0

        # Per-survey loaded data
        self._vardicts: dict[str, dict] = {}
        self._questions: dict[str, list] = {}
        self._q_index: dict[str, dict] = {}
        self._dataset_meta: dict[str, dict] = {}

        # Pre-computed indexes (rebuilt on load)
        self._max_iters: dict[str, dict[str, int]] = {}
        self._count_vars: dict[str, dict[str, str]] = {}
        self._q_order: dict[str, dict[str, int]] = {}
        self._tfidf: dict[str, _TfidfIndex] = {}
        self._choice_list_index: dict[str, dict[str, list[str]]] = {}
        self._graphs: dict[str, object] = {}  # networkx MultiDiGraph per survey
        self._repeat_trees: dict[str, dict] = {}  # repeat group topology per survey

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
                self._graphs.pop(label, None)
        else:
            self._vardicts[label] = {}
            self._dataset_meta[label] = {}
            self._graphs.pop(label, None)

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

        # Load variable graph (convention: *_variable_graph.json next to vardict)
        if _NX_AVAILABLE:
            graph_path = v_path.with_name(
                v_path.stem.replace("_variable_dictionary", "_variable_graph") + ".json"
            )
            if graph_path.exists():
                try:
                    with open(graph_path, encoding="utf-8") as f:
                        raw_graph = json.load(f)
                    self._repeat_trees[label] = raw_graph.pop("repeat_groups", {})
                    self._graphs[label] = nx.node_link_graph(raw_graph)
                    self._mtimes[str(graph_path)] = self._mtime(graph_path)
                    rpt_msg = ""
                    if self._repeat_trees[label]:
                        rpt_msg = f", {len(self._repeat_trees[label])} repeat group(s)"
                    print(
                        f"[survey-expert] Graph loaded for {label}: "
                        f"{self._graphs[label].number_of_nodes()} nodes, "
                        f"{self._graphs[label].number_of_edges()} edges{rpt_msg}",
                        file=sys.stderr,
                    )
                except (json.JSONDecodeError, OSError) as exc:
                    self._graphs.pop(label, None)
                    self._repeat_trees.pop(label, None)
                    print(
                        f"[survey-expert] WARNING: Failed to load graph {graph_path}: {exc}",
                        file=sys.stderr,
                    )
            else:
                self._graphs.pop(label, None)
                self._repeat_trees.pop(label, None)

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

        # Choice list -> variables index (for fast get_choice_list)
        cl_index: dict[str, list[str]] = {}
        for var_name, entry in vardict.items():
            if not isinstance(entry, dict):
                continue
            cl = entry.get("survey", {}).get("choice_list")
            if cl:
                cl_index.setdefault(cl, []).append(var_name)
        self._choice_list_index[label] = cl_index

        # TF-IDF search index: one document per variable
        # Document text = variable name (underscores as spaces) + question text
        #                 + original form name (underscores as spaces)
        tfidf_docs: list[tuple[str, str]] = []
        for var_name, entry in vardict.items():
            if not isinstance(entry, dict):
                continue
            survey_data = entry.get("survey", {})
            qt = survey_data.get("question_text") or ""
            form_name = survey_data.get("original_variable_name") or ""
            doc_text = (
                var_name.replace("_", " ") + " " +
                qt + " " +
                form_name.replace("_", " ")
            )
            tfidf_docs.append((var_name, doc_text))
        self._tfidf[label] = _TfidfIndex(tfidf_docs)

    def _check_reload(self):
        now = time.monotonic()
        if now - self._last_reload_check < self._RELOAD_DEBOUNCE_SECS:
            return
        self._last_reload_check = now
        for label, cfg in self._config.items():
            q_path = Path(cfg.get("questions_json", ""))
            v_path = Path(cfg.get("output_json", ""))
            # Also check graph file (convention-based path)
            paths = [q_path, v_path]
            if cfg.get("output_json"):
                gp = v_path.with_name(
                    v_path.stem.replace("_variable_dictionary", "_variable_graph") + ".json"
                )
                paths.append(gp)
            for p in paths:
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
            # choice_filter (data-structure: constrains which choice codes
            # can land in the dataset for this variable)
            choice_filter = survey_data.get("choice_filter") or (q.get("choice_filter") if q else None)
            if choice_filter:
                lines.append(f"  choice_filter: {str(choice_filter).strip()}")
            if display_choices:
                # If any choice carries extra non-core keys, those are filter
                # dimensions from the choices worksheet — surface them inline.
                core_keys = {'value', 'label'}
                extra_keys = []
                for c in display_choices:
                    if isinstance(c, dict):
                        for k in c.keys():
                            if k not in core_keys and k not in extra_keys:
                                extra_keys.append(k)
                lines.append(f"  choices      :")
                for c in display_choices:
                    if not isinstance(c, dict):
                        continue
                    base = f"    {str(c.get('value','')):>6} = {str(c.get('label','')).strip()}"
                    if extra_keys:
                        dims = [f"{k}={c[k]}" for k in extra_keys if k in c]
                        if dims:
                            base += "   [" + ", ".join(dims) + "]"
                    lines.append(base)
            elif isinstance(choices, dict):
                lines.append(f"  choice_code  : {choices.get('value', '?')} = {choices.get('label', '').strip()}")
            elif not cl:
                lines.append(f"  choices      : n/a")

            # Form-defined sentinels (from choices/constraint)
            constraint = survey_data.get("constraint") or (q.get("constraint") if q else None)
            form_sents = _sentinel_from_choices(display_choices, constraint)
            lines.append(f"  form_sents   : {form_sents}")

            # Data-detected sentinels (from vardict sentinel scan)
            lines.append(f"  data_sents   : {_fmt_sentinel_full(entry.get('sentinels'))}")

            # Constraint (raw + Stata-converted)
            if constraint:
                lines.append(f"  constraint   : {constraint}")
            stata_constraint = (
                survey_data.get("stata_constraint")
                or (q.get("stata_constraint") if q else None)
            )
            if stata_constraint:
                lines.append(f"  stata_constr : {stata_constraint}")

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

            # Calculation (vardict preferred — only set for type:calculate;
            # fall back to questions.json which has it for any field).
            calc = survey_data.get("calculation") or (q.get("calculation") if q else None)
            if calc:
                lines.append(f"  calculation  : {calc}")
            if q and q.get("required"):
                lines.append(f"  required     : yes")

            # references — first-hop deps parsed from this var's expressions
            refs = survey_data.get("references") or (q.get("references") if q else None)
            if refs:
                lines.append(f"  references   : {', '.join(refs)}")

            # XML contract — deterministic "where it comes from" provenance, present
            # only when the dictionary was enriched with a compiled-XForm contract
            # (enrich_with_contract.py). Purely additive: renders nothing otherwise.
            contract = entry.get("contract")
            if contract:
                kind = contract.get("kind")
                if kind == "matched":
                    if contract.get("node_path"):
                        lines.append(f"  xml_node     : {contract['node_path']}")
                    ds = contract.get("data_source")
                    if ds:
                        if ds.get("kind") == "pulldata":
                            lines.append(
                                f"  comes_from   : pulldata('{ds.get('dataset')}', "
                                f"key={ds.get('key')})"
                            )
                        elif ds.get("kind") == "search":
                            lines.append(
                                f"  comes_from   : search('{ds.get('dataset')}', "
                                f"filter={ds.get('filter')})"
                            )
                    if contract.get("is_select_multiple") and contract.get("choice_code") is not None:
                        cl = contract.get("choice_label")
                        lines.append(
                            f"  xml_decode   : code {contract['choice_code']}"
                            + (f" = {cl}" if cl else "")
                        )
                    if contract.get("resolved_by", "").startswith("xml"):
                        rb = contract["resolved_by"]
                        note = " (heuristic string-key match)" if rb == "xml-heuristic" else ""
                        lines.append(f"  xml_resolved : matched by XML (fuzzy missed){note}")
                    if contract.get("corrected_from"):
                        lines.append(
                            f"  xml_corrected: was '{contract['corrected_from']}' "
                            f"(fuzzy false-positive, XML wins)"
                        )
                elif kind in ("system", "legacy"):
                    lines.append(f"  xml_status   : {kind}")

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
                non_null = entry.get("non_null_count")
                non_null = non_null if non_null is not None else "?"
                form_type = str(survey_data.get("type") or "?")
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

                # XML contract provenance (only when the dict was enriched). Surface
                # the high-value signals in batch too: a false-positive correction
                # and where the value comes from. Additive -- empty when absent.
                contract_str = ""
                contract = entry.get("contract")
                if contract and contract.get("kind") == "matched":
                    bits = []
                    if contract.get("corrected_from"):
                        bits.append(f"xml_corrected (was '{contract['corrected_from']}')")
                    elif contract.get("resolved_by"):
                        bits.append(f"resolved_by={contract['resolved_by']}")
                    ds = contract.get("data_source")
                    if ds:
                        bits.append(f"from {ds.get('kind')}('{ds.get('dataset')}')")
                    if bits:
                        contract_str = "\n  contract: " + " | ".join(bits)
                elif contract and contract.get("kind") in ("system", "legacy"):
                    contract_str = f"\n  contract: {contract['kind']}"

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
                    f"{range_str}{repeat_str}{sm_str}{contract_str}"
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
        """Search variable names and question text across surveys (TF-IDF ranked)."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        # Collect TF-IDF hits across all matching surveys
        scored: list[tuple[float, str, str]] = []  # (score, label, var_name)
        for label in labels:
            idx = self._tfidf.get(label)
            if idx is None:
                continue
            for var_name, score in idx.query(query, max_results=max_results):
                scored.append((score, label, var_name))

        # Sort by score descending, keep top max_results
        scored.sort(key=lambda x: x[0], reverse=True)
        scored = scored[:max_results]

        if not scored:
            scope = f" in survey '{survey}'" if survey else ""
            return f"No matches for '{query}'{scope}."

        # Format output
        matches = []
        for score, label, var_name in scored:
            entry = self._vardicts.get(label, {}).get(var_name, {})
            survey_data = entry.get("survey", {})
            form_type = str(survey_data.get("type") or "?")
            non_null = entry.get("non_null_count")
            non_null = non_null if non_null is not None else "?"
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
            # Use pre-built choice list index instead of scanning all variables
            using_vars = self._choice_list_index.get(label, {}).get(list_name, [])
            choices = None

            for var_name in using_vars:
                if choices is not None:
                    break
                entry = self._vardicts.get(label, {}).get(var_name, {})
                if isinstance(entry, dict):
                    c = entry.get("survey", {}).get("choices")
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

    # -- Variable neighborhood graph -------------------------------------

    def _resolve_form_name(self, stata_name: str,
                           survey: str | None = None) -> tuple[str | None, str | None]:
        """Resolve a Stata variable name to its form-level name and survey label."""
        labels = self._filtered_labels(survey)
        for label in labels:
            entry = self._vardicts.get(label, {}).get(stata_name)
            if isinstance(entry, dict):
                form_name = entry.get("survey", {}).get("original_variable_name") or stata_name
                return form_name, label
        return None, None

    def get_neighborhood(self, name: str, depth: int = 1,
                         survey: str | None = None) -> str:
        """Return the variable neighborhood from the relationship graph."""
        self._check_reload()
        if not _NX_AVAILABLE:
            return ("Variable graph requires networkx. "
                    "Install: pip install networkx")
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        # Check if any graphs are loaded
        available = [l for l in labels if l in self._graphs]
        if not available:
            return ("No variable graph loaded. Generate it by running:\n"
                    "  python create_variable_dictionaries.py --survey <KEY>")

        # Resolve to form-level name
        form_name, matched_label = self._resolve_form_name(name, survey)
        if form_name is None:
            # Try the name directly as a form-level node
            for label in available:
                if name in self._graphs[label]:
                    form_name = name
                    matched_label = label
                    break
        if form_name is None:
            suggestions = self._suggest_similar(name, survey)
            msg = f"Variable '{name}' not found."
            if suggestions:
                msg += f"\n  Did you mean: {', '.join(suggestions[:5])}"
            return msg

        G = self._graphs.get(matched_label)
        if G is None or form_name not in G:
            return f"Variable '{form_name}' not in graph for {matched_label}."

        # Get neighborhood: all nodes within `depth` hops (undirected view)
        undirected = G.to_undirected(as_view=True)
        neighborhood = nx.single_source_shortest_path_length(undirected, form_name, cutoff=depth)
        neighbor_nodes = set(neighborhood.keys())

        # Get the target node attributes
        target_attrs = G.nodes[form_name]

        # Collect neighbors grouped by relationship type.
        # For depth=1, group by direct edge type/direction.
        # For depth>1, group by the first-hop edge that leads to each neighbor.
        edge_groups: dict[str, set[str]] = {
            "calculates_from": set(),
            "calculated_by": set(),
            "gated_by": set(),
            "gates": set(),
            "group_gated_by": set(),
            "group_gates": set(),
            "constrained_by": set(),
            "constrains": set(),
            "repeat_sibling": set(),
        }

        def _classify_out(etype: str, target: str):
            if etype == "calculates_from":
                edge_groups["calculated_by"].add(target)
            elif etype == "gated_by":
                edge_groups["gates"].add(target)
            elif etype == "group_gated_by":
                edge_groups["group_gates"].add(target)
            elif etype == "constrained_by":
                edge_groups["constrains"].add(target)
            elif etype == "repeat_sibling":
                edge_groups["repeat_sibling"].add(target)

        def _classify_in(etype: str, target: str):
            if etype == "calculates_from":
                edge_groups["calculates_from"].add(target)
            elif etype == "gated_by":
                edge_groups["gated_by"].add(target)
            elif etype == "group_gated_by":
                edge_groups["group_gated_by"].add(target)
            elif etype == "constrained_by":
                edge_groups["constrained_by"].add(target)
            elif etype == "repeat_sibling":
                edge_groups["repeat_sibling"].add(target)

        if depth <= 1:
            # Direct edges only — fast path
            for _, v, d in G.out_edges(form_name, data=True):
                if v in neighbor_nodes:
                    _classify_out(d.get("type", ""), v)
            for u, _, d in G.in_edges(form_name, data=True):
                if u in neighbor_nodes:
                    _classify_in(d.get("type", ""), u)
        else:
            # Multi-hop: classify each neighbor by the first-hop edge
            # that leads to it from form_name
            first_hop_out = {}  # neighbor -> set of edge types via out
            for _, v, d in G.out_edges(form_name, data=True):
                first_hop_out.setdefault(v, set()).add(d.get("type", ""))
            first_hop_in = {}   # neighbor -> set of edge types via in
            for u, _, d in G.in_edges(form_name, data=True):
                first_hop_in.setdefault(u, set()).add(d.get("type", ""))

            # Single BFS to get all shortest paths at once (reuse undirected view)
            all_paths = nx.single_source_shortest_path(undirected, form_name, cutoff=depth)
            for node, path in all_paths.items():
                if node == form_name or len(path) < 2:
                    continue
                first_step = path[1]
                for etype in first_hop_out.get(first_step, set()):
                    _classify_out(etype, node)
                for etype in first_hop_in.get(first_step, set()):
                    _classify_in(etype, node)

        # Format output
        lines = [
            f"[{matched_label}] Variable neighborhood: {form_name} (depth={depth})",
            f"  type: {target_attrs.get('type', '?')}",
            f"  group_path: {target_attrs.get('group_path', '')}",
            f"  repeat_depth: {target_attrs.get('repeat_depth', 0)}",
        ]
        rg = target_attrs.get("repeat_group")
        if rg:
            lines.append(f"  repeat_group: {rg}")
            # Append repeat tree context if available
            tree = self._repeat_trees.get(matched_label, {})
            rg_info = tree.get(rg)
            if rg_info:
                lines.append(f"  repeat_tree: depth={rg_info['depth']}, "
                             f"count_var={rg_info['count_var']}, "
                             f"count_expr={rg_info.get('count_expr', '?')}, "
                             f"max_iter={rg_info['max_iterations']}, "
                             f"suffix={rg_info['stata_suffix_pattern']}")
                if rg_info.get("parent"):
                    lines.append(f"  repeat_parent: {rg_info['parent']}")
                lines.append(f"  join_key: {rg_info['join_key_note']}")
        sv = target_attrs.get("stata_vars", [])
        if sv:
            lines.append(f"  stata_vars: {', '.join(sv)}")

        # Relationship sections with risk semantics
        sections = [
            ("Calculates from (inputs -- sentinel contamination risk)",
             "calculates_from"),
            ("Calculated by (downstream -- changing this changes them)",
             "calculated_by"),
            ("Gated by (missing-by-logic if gate is false)",
             "gated_by"),
            ("Gates (changing this can make these disappear)",
             "gates"),
            ("Group-gated by (structural gate -- affects whole group)",
             "group_gated_by"),
            ("Group-gates (structural -- these groups depend on this)",
             "group_gates"),
            ("Constrained by (validation -- defines valid ranges)",
             "constrained_by"),
            ("Constrains (validation -- these depend on valid values here)",
             "constrains"),
            ("Repeat siblings (same repeat iteration -- handle together)",
             "repeat_sibling"),
        ]

        for header, key in sections:
            members = edge_groups.get(key, [])
            if not members:
                continue
            lines.append(f"\n  {header}:")
            for m in sorted(members):
                m_attrs = G.nodes.get(m, {})
                m_type = m_attrs.get("type", "?")
                m_depth = m_attrs.get("repeat_depth", 0)
                m_svars = m_attrs.get("stata_vars", [])
                sv_str = f" -> {', '.join(m_svars)}" if m_svars else ""
                depth_str = f" [repeat_depth={m_depth}]" if m_depth > 0 else ""
                lines.append(f"    {m} ({m_type}){depth_str}{sv_str}")

        if all(not edge_groups.get(k) for _, k in sections):
            lines.append("\n  (no relationships found)")

        return "\n".join(lines)

    def get_repeat_structure(self, survey: str | None = None) -> str:
        """Return the repeat group topology tree for one or more surveys."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []
        for label in labels:
            tree = self._repeat_trees.get(label, {})
            if not tree:
                results.append(f"[{label}] No repeat groups (or graph not generated).")
                continue

            lines = [f"[{label}] Repeat group topology ({len(tree)} group(s)):"]

            # Sort by depth then name for consistent display
            for rg_name, info in sorted(tree.items(),
                                        key=lambda x: (x[1]["depth"], x[0])):
                indent = "  " * info["depth"]
                lines.append(f"\n{indent}{rg_name}:")
                lines.append(f"{indent}  parent: {info['parent'] or '(root)'}")
                lines.append(f"{indent}  depth: {info['depth']}")
                lines.append(f"{indent}  count_var: {info['count_var']}")
                lines.append(f"{indent}  count_expr: {info.get('count_expr', '?')}")
                lines.append(f"{indent}  max_iterations: {info['max_iterations']}")
                lines.append(f"{indent}  n_variables: {info['n_variables']}")
                if info.get("relevance"):
                    lines.append(f"{indent}  relevance: {info['relevance']}")
                lines.append(f"{indent}  stata_suffix: {info['stata_suffix_pattern']}")
                lines.append(f"{indent}  join_key: {info['join_key_note']}")

            results.append("\n".join(lines))

        return "\n\n".join(results)


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
        "Use get_variable_neighborhood to see all variables connected to a target "
        "(calculation inputs, gates, repeat siblings) with risk semantics. "
        "IMPORTANT: before writing a cleaning module, call get_variable_neighborhood on "
        "key variables to understand sentinel contamination paths and gating dependencies. "
        "Use get_repeat_structure to see the repeat group hierarchy (nesting, count "
        "variables, max iterations, join keys) before writing reshape or merge code. "
        "Use get_survey_info for a dataset overview before diving in. "
        "All tools accept an optional 'survey' parameter to filter by survey key "
        "(partial match, e.g., survey='endline' matches 'endline_hh' and 'endline_adult')."
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
    e.g., survey="endline" matches "endline_hh" and "endline_adult").

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
    """Search across variable names and question text using TF-IDF ranking.

    Accepts natural-language queries (e.g., "crop sales quantity") and
    matches individual words against variable names, question text, and
    original form names.  Results are ranked by relevance, so abbreviated
    variable names (crpsale_qty) surface when the question text mentions
    the full words (crop, sale, quantity).

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


@server.tool()
def get_variable_neighborhood(name: str, depth: int = 1,
                              survey: str = "") -> str:
    """Show the relationship neighborhood of a variable from the dependency graph.

    Returns all variables connected to the target within `depth` hops,
    grouped by relationship type with risk semantics:
    - Calculates from: inputs whose sentinel contamination propagates here
    - Calculated by: downstream variables that change if this one changes
    - Gated by: gate variables that control whether this one has data
    - Gates: variables that disappear if this one is recoded
    - Constrained by: variables that define valid ranges
    - Repeat siblings: variables in the same repeat iteration

    For variables inside repeat groups, includes repeat tree context:
    parent group, count variable, count expression, max iterations,
    Stata suffix pattern, and join key note.

    Accepts a Stata column name (e.g., crpsale_qty_1_2) -- automatically
    resolves to the form-level variable (crpsale_qty).

    Requires the variable graph to be generated first:
      python create_variable_dictionaries.py --survey <KEY>

    Use the survey parameter to filter to a specific survey (partial match).
    """
    return _get_store().get_neighborhood(
        name, depth=depth, survey=survey or None
    )


@server.tool()
def get_repeat_structure(survey: str = "") -> str:
    """Show the repeat group topology tree for a survey.

    Returns the hierarchy of repeat groups with:
    - Parent-child nesting relationships
    - Count variable and count expression (what drives the iteration count)
    - Max iterations observed in the data
    - Number of form-level variables in each repeat
    - Stata suffix pattern (how iterations map to column name suffixes)
    - Join key note (how to merge across repeat levels)

    Essential for writing reshape, merge, or cross-level aggregation code.

    Use the survey parameter to filter to a specific survey (partial match).
    """
    return _get_store().get_repeat_structure(survey=survey or None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _get_store()
    server.run()
