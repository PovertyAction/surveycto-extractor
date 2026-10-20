"""SurveyCTO survey expert: lookups over the extractor's outputs.

One module behind both front ends: the survey-expert MCP server
(``mcp_server/survey_server.py``) wraps ``SurveyStore``, and ``surveycto-skill``
copies this file into a project as the skill's ``search_survey.py``, the shell
fallback. It is standard library only, so that copy runs with no package
installed. ``tests/test_survey.py`` holds it to that.

A store reads, per survey, ``<key>_variable_dictionary.json`` (primary),
``<key>_questions.json`` and ``<key>_variable_graph.json`` next to it, and
reloads them when they change. Every output gates data values by the column's
``pii.status``: a flagged column shows counts, never values.

Shell use::

    python search_survey.py [--dir DOCS] [--survey KEY] <command>

with commands ``var``, ``search``, ``choices``, ``gates``, ``neighborhood``,
``group``, ``lineage``, ``repeats`` and ``info`` (``--help`` lists their
arguments).
"""

import argparse
import difflib
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

# The PII statuses whose values no output shows; a copy of pii.FLAGGED, kept here
# so this module stays standard library only (a test holds the two equal).
_FLAGGED = {"contains_pii", "may_contain_pii"}


def _shows_values(status: str) -> bool:
    return status not in _FLAGGED


_UNCLASSIFIED = (
    "built before PII classification, so nothing gates its values; "
    "re-run surveycto-vardict"
)
_OLD_QUESTIONS = (
    "written by an older surveycto-extract (no gates or data_source); "
    "re-run surveycto-extract, then surveycto-vardict"
)


_REF = re.compile(r"\$\{([^}]+)\}")


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _source_line(source: dict) -> str:
    """Describe a questions.json ``data_source``: where a value is read from."""
    if source["kind"] == "pulldata":
        # A pulldata() written without its 4th argument matches on nothing.
        match = source["match"] or "(no value given)"
        return (
            f"pulldata: column {_unquote(source['value'])} of dataset "
            f"{source['dataset']}, the row where {source['key']} = {match}"
        )
    filters = ", ".join(
        f"{c} = {v}" if v is not None else c for c, v in source["match"].items()
    )
    return f"search: a row of dataset {source['dataset']}" + (
        f" where {filters}" if filters else ""
    )


class _Unclassified(Exception):
    """A variable dictionary with no ``summary.pii``."""


def _pii_line(entry: dict) -> str | None:
    """Return ``status (basis)`` for a column's pii block, or None without one."""
    block = entry.get("pii")
    if not block:
        return None
    return f"{block['status']} ({', '.join(block['basis'])})"


# ---------------------------------------------------------------------------
# Variable graph (the node-link JSON surveycto-vardict writes)
# ---------------------------------------------------------------------------


class _Graph:
    """The variable graph, read without networkx.

    The breadth-first search reaches the same nodes at the same distances as
    networkx (``tests/test_pipeline_e2e.py`` checks it). Where shortest paths tie,
    it follows the file's edge order; networkx's undirected view iterates a set,
    so its pick changed with the process's hash seed.
    """

    def __init__(self, raw: dict):
        self.nodes: dict[str, dict] = {}
        self.succ: dict[str, dict[str, list[dict]]] = {}
        self.pred: dict[str, dict[str, list[dict]]] = {}
        for node in raw["nodes"]:
            attrs = {k: v for k, v in node.items() if k != "id"}
            self.nodes[node["id"]] = attrs
            self.succ[node["id"]] = {}
            self.pred[node["id"]] = {}
        # networkx 3.4 (py3.10) writes "links"; 3.6 writes "edges".
        for edge in raw["edges"] if "edges" in raw else raw["links"]:
            u, v = edge["source"], edge["target"]
            data = {k: x for k, x in edge.items() if k not in ("source", "target")}
            self.succ[u].setdefault(v, []).append(data)
            self.pred[v].setdefault(u, []).append(data)

    def __contains__(self, node) -> bool:
        return node in self.nodes

    def neighbors(self, node: str) -> list[str]:
        """Return a node's neighbors in the undirected view: successors, then
        predecessors that are not also successors.
        """
        return [
            *self.succ[node],
            *(p for p in self.pred[node] if p not in self.succ[node]),
        ]

    def shortest_paths(self, source: str, cutoff: int) -> dict[str, list[str]]:
        """Return ``nx.single_source_shortest_path`` on the undirected view."""
        paths = {source: [source]}
        level = [source]
        for _ in range(cutoff):
            nxt = []
            for v in level:
                for w in self.neighbors(v):
                    if w not in paths:
                        paths[w] = [*paths[v], w]
                        nxt.append(w)
            if not nxt:
                break
            level = nxt
        return paths

    def out_edges(self, node: str):
        return [(node, v, d) for v, ds in self.succ[node].items() for d in ds]

    def in_edges(self, node: str):
        return [(u, node, d) for u, ds in self.pred[node].items() for d in ds]


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
        codes = ",".join(f".{k}" for k in sentinels["ext_missing_detail"])
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

# [^\W_] = any Unicode letter/digit but not underscore. The old [a-z0-9] was
# ASCII-only, so accented Spanish/French question text ("cuántos niños")
# shredded into fragments and never matched an agent's query. (#29.4)
_TOKEN_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens (>= 2 chars), accent-insensitive.

    NFKD-normalises and strips combining marks so accented and unaccented
    forms match both ways (an agent typing "ninos" hits "niños"), then
    casefolds; non-Latin scripts are preserved.
    """
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return _TOKEN_RE.findall(norm.casefold())


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
            tok: math.log((n + 1) / (freq + 1)) + 1 for tok, freq in df.items()
        }

        # Precompute document norms
        for tf in self._doc_tfs:
            norm_sq = sum((w * self._idf.get(tok, 0.0)) ** 2 for tok, w in tf.items())
            self._doc_norms.append(math.sqrt(norm_sq) or 1.0)

    # -- query --------------------------------------------------------------

    def query(
        self, text: str, max_results: int = 20, min_score: float = 0.01
    ) -> list[tuple[str, float]]:
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
        q_norm = math.sqrt(sum(v**2 for v in q_vec.values())) or 1.0

        # Score each document
        scored: list[tuple[str, float]] = []
        for i, tf in enumerate(self._doc_tfs):
            dot = sum(
                q_vec.get(tok, 0.0) * w * self._idf.get(tok, 0.0)
                for tok, w in tf.items()
                if tok in q_vec
            )
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
        """Initialize the store from a DATASETS config and load all surveys."""
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
        self._graphs: dict[str, _Graph] = {}  # variable graph per survey
        self._repeat_trees: dict[str, dict] = {}  # repeat group topology per survey
        # Columns of a withheld select_multiple are shown by position, since their
        # names carry the list's codes: column -> alias, and back.
        self._alias: dict[str, dict[str, str]] = {}
        self._unalias: dict[str, dict[str, str]] = {}
        # Surveys refused, with why: a dictionary built before the PII pass, or a
        # questions.json from an older extract.
        self._refused: dict[str, str] = {}

        self._load_all()

    @property
    def is_empty(self) -> bool:
        """Return True when no survey data has been loaded, or every dictionary
        was refused (its questions alone would answer without the PII gate).
        """
        if not any(self._vardicts.values()) and self._refused:
            return True
        return not any(self._vardicts.values()) and not any(self._questions.values())

    @property
    def survey_labels(self) -> list[str]:
        """Return the sorted survey keys that have loaded data."""
        return sorted(set(list(self._vardicts.keys()) + list(self._questions.keys())))

    def _empty_message(self) -> str:
        """Return a helpful message shown when no data is loaded."""
        if self._refused:
            return "\n".join(
                f"[{label}] refused {why}"
                for label, why in sorted(self._refused.items())
            )
        if not self._config:
            return (
                "No survey data loaded.\n\n"
                "The MCP server could not find config.toml/config.py or DATASETS is empty.\n"
                "To fix:\n"
                "  1. Ensure config.toml exists (run `surveycto-init` to create one)\n"
                "  2. Add entries to DATASETS with questions_json and output_json paths\n"
                "  3. Run `surveycto-vardict` to generate the JSON files\n"
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
                f"  uv run surveycto-extract --survey <KEY>                    # Phase 1-3\n"
                f"  uv run surveycto-vardict --survey <KEY>  # Phase 4\n"
                f"Then restart the Claude Code session."
            )
        return (
            "No survey data loaded (unknown reason). Check server stderr for details."
        )

    # -- Survey filtering -----------------------------------------------

    def _filtered_labels(self, survey: str | None) -> list[str]:
        """Return survey labels, filtered by partial match if survey is given."""
        labels = self.survey_labels
        if not survey:
            return labels
        matched = [lbl for lbl in labels if survey.lower() in lbl.lower()]
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
        n_surveys = len([lbl for lbl in self._vardicts if self._vardicts[lbl]])
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
        # Only build a Path from a NON-EMPTY config value. Path("") is Path("."),
        # whose .exists() is True and .with_name(...) raises ValueError -- so a
        # DATASETS entry missing output_json used to crash the whole server on
        # startup instead of degrading that one survey. (#29.2)
        q_path = Path(cfg["questions_json"]) if cfg.get("questions_json") else None
        v_path = Path(cfg["output_json"]) if cfg.get("output_json") else None

        # Variable dictionary (primary)
        if v_path and v_path.exists():
            try:
                with open(v_path, encoding="utf-8") as f:
                    raw = json.load(f)
                if "pii" not in raw.get("summary", {}):
                    # Built before the PII pass: it may hold flagged columns'
                    # ranges and no statuses to gate them, so none of it is served.
                    raise _Unclassified(v_path)
                self._refused.pop(label, None)
                self._vardicts[label] = raw.get("variables", {})
                self._dataset_meta[label] = {
                    "dataset": raw.get("dataset", {}),
                    "summary": raw.get("summary", {}),
                }
                self._mtimes[str(v_path)] = self._mtime(v_path)
            except _Unclassified:
                print(
                    f"[survey-expert] REFUSED {v_path}: {_UNCLASSIFIED}",
                    file=sys.stderr,
                )
                self._refused[label] = f"{v_path}: {_UNCLASSIFIED}"
                self._vardicts[label] = {}
                self._dataset_meta[label] = {}
                self._mtimes[str(v_path)] = self._mtime(v_path)
            except (json.JSONDecodeError, OSError) as exc:
                # Keep any previously-loaded data instead of wiping it: a reload
                # firing mid-write (create_variable_dictionaries still writing)
                # would otherwise serve "no data" until the next debounce. The
                # mtime is NOT recorded, so the next check retries. (#29.3)
                print(
                    f"[survey-expert] WARNING: Failed to load {v_path} "
                    f"(keeping previous data): {exc}",
                    file=sys.stderr,
                )
                self._vardicts.setdefault(label, {})
                self._dataset_meta.setdefault(label, {})
        else:
            self._vardicts[label] = {}
            self._dataset_meta[label] = {}
            self._graphs.pop(label, None)

        # Questions (supplement)
        if q_path and q_path.exists():
            try:
                with open(q_path, encoding="utf-8") as f:
                    questions = json.load(f)
                self._mtimes[str(q_path)] = self._mtime(q_path)
                if any("gates" not in q or "data_source" not in q for q in questions):
                    # An older extract: group and lineage answers would be wrong.
                    why = f"{q_path}: {_OLD_QUESTIONS}"
                    print(f"[survey-expert] REFUSED {why}", file=sys.stderr)
                    self._refused[label] = why
                    self._vardicts[label] = {}
                    self._dataset_meta[label] = {}
                    questions = []
                self._questions[label] = questions
                self._q_index[label] = {
                    q["variable_name"]: q for q in questions if "variable_name" in q
                }
            except (json.JSONDecodeError, OSError) as exc:
                print(
                    f"[survey-expert] WARNING: Failed to load {q_path} "
                    f"(keeping previous data): {exc}",
                    file=sys.stderr,
                )
                self._questions.setdefault(label, [])
                self._q_index.setdefault(label, {})
        else:
            self._questions[label] = []
            self._q_index[label] = {}

        # Build indexes
        self._build_indexes(label)

        # Load variable graph (convention: *_variable_graph.json next to vardict)
        if v_path:
            graph_path = v_path.with_name(
                v_path.stem.replace("_variable_dictionary", "_variable_graph") + ".json"
            )
            if graph_path.exists():
                try:
                    with open(graph_path, encoding="utf-8") as f:
                        raw_graph = json.load(f)
                    self._repeat_trees[label] = raw_graph.pop("repeat_groups", {})
                    self._graphs[label] = _Graph(raw_graph)
                    self._mtimes[str(graph_path)] = self._mtime(graph_path)
                    rpt_msg = ""
                    if self._repeat_trees[label]:
                        rpt_msg = f", {len(self._repeat_trees[label])} repeat group(s)"
                    print(
                        f"[survey-expert] Graph loaded for {label}: "
                        f"{len(self._graphs[label].nodes)} nodes{rpt_msg}",
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

        # A withheld select_multiple's binaries carry its codes in their names;
        # show them as <question>_<n> (n: order of first appearance) plus the
        # repeat instance, as surveycto-python-dict does.
        withheld = {
            q["variable_name"]
            for q in self._questions.get(label, [])
            if q.get("choices_withheld")
        } | {
            e["survey"]["original_variable_name"]
            for e in vardict.values()
            if (e.get("survey") or {}).get("choices_withheld")
        }
        alias: dict[str, str] = {}
        positions: dict[str, dict] = {}
        for var_name, entry in vardict.items():
            survey_data = entry.get("survey") or {}
            question = survey_data.get("original_variable_name")
            if (
                question not in withheld
                or survey_data.get("type") != "select_multiple"
                or var_name == question
            ):
                continue
            choice = survey_data.get("choices")
            code = (entry.get("contract") or {}).get("choice_code") or (
                choice.get("value") if isinstance(choice, dict) else var_name
            )
            seen = positions.setdefault(question, {})
            n = seen.setdefault(code, len(seen) + 1)
            ri = entry.get("repeat_iteration")
            alias[var_name] = f"{question}_<{n}>" + (f"_{ri}" if ri else "")
        self._alias[label] = alias
        self._unalias[label] = {v: k for k, v in alias.items()}

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
        exported = set()
        for var_name, entry in vardict.items():
            if not isinstance(entry, dict):
                continue
            survey_data = entry.get("survey", {})
            qt = survey_data.get("question_text") or ""
            form_name = survey_data.get("original_variable_name") or ""
            exported.add(form_name)
            doc_text = (
                alias.get(var_name, var_name).replace("_", " ")
                + " "
                + qt
                + " "
                + form_name.replace("_", " ")
            )
            tfidf_docs.append((var_name, doc_text))
        # Form questions with no dataset column (notes, unexported fields) are
        # searchable too, keyed "q:<name>".
        for q in self._questions.get(label, []):
            name = q.get("variable_name")
            if name and name not in exported:
                text = name.replace("_", " ") + " " + (q.get("question_text") or "")
                tfidf_docs.append((f"q:{name}", text))
        self._tfidf[label] = _TfidfIndex(tfidf_docs)

    def _shown(self, label: str, column: str) -> str:
        """Return the name a column is shown by (an alias for a withheld list's)."""
        return self._alias.get(label, {}).get(column, column)

    def _column(self, label: str, name: str) -> str:
        """Return the column a shown name stands for."""
        return self._unalias.get(label, {}).get(name, name)

    def _check_reload(self):
        now = time.monotonic()
        if now - self._last_reload_check < self._RELOAD_DEBOUNCE_SECS:
            return
        self._last_reload_check = now
        for label, cfg in self._config.items():
            # Skip empty config values (Path("") is Path(".") -- statting the
            # cwd would trigger a spurious reload every check). (#29.2)
            paths = []
            if cfg.get("questions_json"):
                paths.append(Path(cfg["questions_json"]))
            if cfg.get("output_json"):
                v_path = Path(cfg["output_json"])
                paths.append(v_path)
                gp = v_path.with_name(
                    v_path.stem.replace("_variable_dictionary", "_variable_graph")
                    + ".json"
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
        gpath_parts = [
            p.strip() for p in group_path.replace("/", " ").split() if p.strip()
        ]

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
                elif gq.get("constraint"):
                    note = f"[constraint: {gq['constraint']}]"
                else:
                    note = f"[{gq.get('type', '?')}]"
                own_rel = gq.get("relevance") or ""
                gate_grps = gq.get("group_relevances") or []
                if own_rel:
                    gate_cond = f"asked when: {own_rel}"
                elif gate_grps:
                    gate_cond = f"asked when: {'; '.join(gate_grps)}"
                else:
                    gate_cond = "always asked"
                lines.append(f"    {gvar}: {gqt}")
                lines.append(f"      {note}")
                lines.append(f"      {gate_cond}")
            else:
                lines.append(f"    {gvar}: (not in questions.json)")
        return lines

    # -- Public query methods -------------------------------------------

    def lookup_var(
        self, stata_name: str, context: int = 0, survey: str | None = None
    ) -> str:
        """Full single-variable lookup: vardict primary + questions.json supplement."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []

        for label in labels:
            column = self._column(label, stata_name)
            entry = self._vardicts.get(label, {}).get(column)
            if entry is None:
                continue

            survey_data = entry.get("survey", {})
            form_name = survey_data.get("original_variable_name", stata_name)
            q = self._q_index.get(label, {}).get(form_name)
            shown = self._shown(label, column)
            aliased = shown != column

            lines = []

            # Header
            header = f"[{label}] {shown}"
            if form_name != shown:
                header += f"  (form: {form_name})"
            lines.append(header)

            # Core vardict fields
            stata_type = entry.get("stata", {}).get("type", "?")
            non_null = entry.get("non_null_count")
            nn_str = f"{non_null:,}" if isinstance(non_null, int) else str(non_null)
            lines.append(f"  stata_type   : {stata_type}")
            lines.append(f"  non_null     : {nn_str}")
            if entry.get("missing_rate") is not None:
                lines.append(f"  missing_rate : {entry['missing_rate']:.1%}")

            # Data range (integer/decimal/calculate/date only)
            d_min = entry.get("data_min")
            d_max = entry.get("data_max")
            if d_min is not None and d_max is not None:
                lines.append(f"  data_range   : {d_min} to {d_max}")

            lines.append(f"  form_type    : {survey_data.get('type', '?')}")
            lines.append(
                f"  question     : {(survey_data.get('question_text') or '').strip()}"
            )
            pii = _pii_line(entry)
            if pii:
                lines.append(f"  pii          : {pii}")
                if not _shows_values(entry["pii"]["status"]):
                    lines.append(
                        "  PII: print only counts, null rates and KEYs for this "
                        "column, never its values"
                    )

            # Choices (from vardict, prefer questions.json for full list)
            cl = survey_data.get("choice_list")
            choices = survey_data.get("choices")
            q_choices = q.get("choices") if q else None
            display_choices = (
                q_choices
                if q_choices and isinstance(q_choices, list)
                else (choices if isinstance(choices, list) else None)
            )

            if cl:
                lines.append(f"  choice_list  : {cl}")
            # choice_filter (data-structure: constrains which choice codes
            # can land in the dataset for this variable)
            choice_filter = survey_data.get("choice_filter") or (
                q.get("choice_filter") if q else None
            )
            if choice_filter:
                lines.append(f"  choice_filter: {str(choice_filter).strip()}")
            withheld = survey_data.get("choices_withheld") or (
                q.get("choices_withheld") if q else None
            )
            if withheld:
                lines.append(
                    f"  choices_withheld: {', '.join(withheld)} (the list is read "
                    "from data; refer to it, never reconstruct it)"
                )
            if display_choices:
                # If any choice carries extra non-core keys, those are filter
                # dimensions from the choices worksheet — surface them inline.
                core_keys = {"value", "label"}
                extra_keys = []
                for c in display_choices:
                    if isinstance(c, dict):
                        for k in c:
                            if k not in core_keys and k not in extra_keys:
                                extra_keys.append(k)
                lines.append("  choices      :")
                for c in display_choices:
                    if not isinstance(c, dict):
                        continue
                    base = f"    {str(c.get('value', '')):>6} = {str(c.get('label', '')).strip()}"
                    if extra_keys:
                        dims = [f"{k}={c[k]}" for k in extra_keys if k in c]
                        if dims:
                            base += "   [" + ", ".join(dims) + "]"
                    lines.append(base)
            elif isinstance(choices, dict) and not aliased:
                lines.append(
                    f"  choice_code  : {choices.get('value', '?')} = {(choices.get('label') or '').strip()}"
                )
            elif not cl:
                lines.append("  choices      : n/a")

            # Form-defined sentinels (from choices/constraint)
            constraint = survey_data.get("constraint") or (
                q.get("constraint") if q else None
            )
            form_sents = _sentinel_from_choices(display_choices, constraint)
            lines.append(f"  form_sents   : {form_sents}")

            # Data-detected sentinels (from vardict sentinel scan)
            lines.append(
                f"  data_sents   : {_fmt_sentinel_full(entry.get('sentinels'))}"
            )

            # Constraint (raw + Stata-converted)
            if constraint:
                lines.append(f"  constraint   : {constraint}")
            stata_constraint = survey_data.get("stata_constraint") or (
                q.get("stata_constraint") if q else None
            )
            if stata_constraint:
                lines.append(f"  stata_constr : {stata_constraint}")

            # The form's own relevance, before the Stata translation below.
            if q and q.get("relevance"):
                lines.append(f"  relevance    : {q['relevance']}")

            # Skip logic (prefer iteration-specific for repeat vars)
            skip_iter = survey_data.get("skip_logic_iteration_specific")
            skip = survey_data.get("stata_skip_logic")
            if skip_iter and skip_iter != skip:
                lines.append(f"  skip_logic   : {skip_iter}")
                lines.append(f"  skip_template: {skip}")
            elif skip:
                lines.append(f"  skip_logic   : {skip}")
            else:
                lines.append("  skip_logic   : none")

            # Group relevances
            grp_rels = survey_data.get("group_relevances") or []
            if grp_rels:
                lines.append(f"  grp_relevance: {'; '.join(str(r) for r in grp_rels)}")

            lines.append(
                f"  group_path   : {survey_data.get('group_path', '') or 'top-level'}"
            )

            # Calculation (vardict preferred — only set for type:calculate;
            # fall back to questions.json which has it for any field).
            calc = survey_data.get("calculation") or (
                q.get("calculation") if q else None
            )
            if calc:
                lines.append(f"  calculation  : {calc}")
            if q and q.get("required"):
                lines.append("  required     : yes")

            # references — first-hop deps parsed from this var's expressions
            refs = survey_data.get("references") or (q.get("references") if q else None)
            if refs:
                lines.append(f"  references   : {', '.join(refs)}")
            source = q.get("data_source") if q else None
            if source and not (entry.get("contract") or {}).get("data_source"):
                lines.append(f"  comes_from   : {_source_line(source)}")

            # XML contract — deterministic "where it comes from" provenance, present
            # only when the dictionary was enriched with a compiled-XForm contract
            # (the XML overlay in surveycto-vardict). Purely additive: renders nothing otherwise.
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
                    if (
                        contract.get("is_select_multiple")
                        and contract.get("choice_code") is not None
                        and not aliased
                    ):
                        cl = contract.get("choice_label")
                        lines.append(
                            f"  xml_decode   : code {contract['choice_code']}"
                            + (f" = {cl}" if cl else "")
                        )
                    if contract.get("resolved_by", "").startswith("xml"):
                        rb = contract["resolved_by"]
                        note = (
                            " (heuristic string-key match)"
                            if rb == "xml-heuristic"
                            else ""
                        )
                        lines.append(
                            f"  xml_resolved : matched by XML (fuzzy missed){note}"
                        )
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
                lines.append("  *** REPEAT GROUP ***")
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
                lines.append("  *** SELECT_MULTIPLE ***")
                lines.append(f"  choice_code  : {entry.get('choice_code', '?')}")
                lines.append(f"  choice_label : {entry.get('choice_label', '?')}")

            # Context: adjacent questions + gate resolution
            if context > 0 and q:
                questions = self._questions.get(label, [])
                q_idx = self._q_order.get(label, {}).get(form_name)
                if q_idx is not None:
                    start = max(0, q_idx - context)
                    end = min(len(questions), q_idx + context + 1)
                    lines.append("")
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
            # A form question answers from questions.json: by its columns when it
            # has several (repeat instances, select_multiple binaries), else as a
            # question with no dataset column (a note, a field never exported).
            for label in labels:
                q = self._q_index.get(label, {}).get(stata_name)
                if not q:
                    continue
                columns = [
                    self._shown(label, col)
                    for col, e in self._vardicts.get(label, {}).items()
                    if (e.get("survey") or {}).get("original_variable_name")
                    == stata_name
                ]
                if columns:
                    more = (
                        f", ... ({len(columns) - 10} more)" if len(columns) > 10 else ""
                    )
                    results.append(
                        f"[{label}] {stata_name} is a form question stored in "
                        f"{len(columns)} columns; look one up: "
                        f"{', '.join(columns[:10])}{more}"
                    )
                else:
                    results.append(self._form_question(label, q))
        if not results:
            suggestions = self._suggest_similar(stata_name, survey)
            scope = f" in survey '{survey}'" if survey else " in any loaded survey"
            msg = f"No match for '{stata_name}'{scope}."
            if suggestions:
                msg += f"\n  Did you mean: {', '.join(suggestions[:5])}"
            return msg
        return "\n\n".join(results)

    @staticmethod
    def _form_question(label: str, q: dict) -> str:
        """Render a questions.json entry that no dataset column holds."""
        lines = [
            f"[{label}] {q['variable_name']}  (form question; no dataset column)",
            f"  form_type    : {q.get('type', '?')}",
            f"  question     : {(q.get('question_text') or '').strip()}",
        ]
        for key, tag in (
            ("relevance", "relevance    "),
            ("constraint", "constraint   "),
            ("calculation", "calculation  "),
            ("choice_list", "choice_list  "),
        ):
            if q.get(key):
                lines.append(f"  {tag}: {q[key]}")
        if q.get("choices_withheld"):
            lines.append(
                f"  choices_withheld: {', '.join(q['choices_withheld'])} (the list "
                "is read from data; refer to it, never reconstruct it)"
            )
        for c in q.get("choices") or []:
            lines.append(f"    {str(c.get('value', '')):>6} = {c.get('label', '')}")
        return "\n".join(lines)

    def lookup_vars_batch(self, names: list[str], survey: str | None = None) -> str:
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
                column = self._column(label, name)
                entry = self._vardicts.get(label, {}).get(column)
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
                        bits.append(
                            f"xml_corrected (was '{contract['corrected_from']}')"
                        )
                    elif contract.get("resolved_by"):
                        bits.append(f"resolved_by={contract['resolved_by']}")
                    ds = contract.get("data_source")
                    if ds:
                        bits.append(f"from {ds.get('kind')}('{ds.get('dataset')}')")
                    if bits:
                        contract_str = "\n  contract: " + " | ".join(bits)
                elif contract and contract.get("kind") in ("system", "legacy"):
                    contract_str = f"\n  contract: {contract['kind']}"

                shown = self._shown(label, column)
                header = f"=== [{label}] {shown} ==="
                if form_name != shown:
                    header += f"  (form: {form_name})"

                range_str = ""
                d_min = entry.get("data_min")
                d_max = entry.get("data_max")
                if d_min is not None and d_max is not None:
                    range_str = f"\n  range: {d_min} to {d_max}"

                pii_str = ""
                if entry.get("pii"):
                    pii_str = f"\n  pii: {_pii_line(entry)}"
                    if not _shows_values(entry["pii"]["status"]):
                        pii_str += " -- print counts and KEYs only, never values"
                withheld = survey_data.get("choices_withheld")
                if withheld:
                    pii_str += (
                        f"\n  choices_withheld: {', '.join(withheld)} (refer to "
                        "the list, never reconstruct it)"
                    )

                block = (
                    f"{header}\n"
                    f"  {form_type} | stata:{stata_type} | non_null:{non_null}\n"
                    f"  Q: {qt}\n"
                    f"  skip: {_trunc(skip, 120)}\n"
                    f"  sentinels: {sent_str}"
                    f"{range_str}{repeat_str}{sm_str}{contract_str}{pii_str}"
                )
                blocks.append(block)
                break  # first matching survey in scope

            if not found:
                blocks.append(f"=== {name} ===\n  NOT FOUND")

        scope = f" (survey filter: {survey})" if survey else ""
        header = f"Batch lookup: {len(names)} variables, {n_found} found{scope}"
        return header + "\n\n" + "\n\n".join(blocks)

    def search(
        self, query: str, max_results: int = 20, survey: str | None = None
    ) -> str:
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
            if var_name.startswith("q:"):
                q = self._q_index[label][var_name[2:]]
                qt_display = (q.get("question_text") or "").strip()[:70]
                matches.append(
                    f"  [{label}] {var_name[2:]:30s} | {q.get('type', '?'):15s} "
                    f"| form question, no dataset column\n{'':33s}  Q: {qt_display}"
                )
                continue
            entry = self._vardicts.get(label, {}).get(var_name, {})
            var_name = self._shown(label, var_name)
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
            status = (entry.get("pii") or {}).get("status")
            if status in _FLAGGED:
                line += f" | PII:{status}"
            if skip_brief:
                line += f" | skip:{skip_brief}"
            line += f"\n{'':33s}  Q: {qt_display}"
            matches.append(line)

        scope = f" (survey filter: {survey})" if survey else ""
        header = f"Found {len(matches)} match(es) for '{query}'{scope}:"
        return header + "\n" + "\n".join(matches)

    def get_choice_list(self, list_name: str, survey: str | None = None) -> str:
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
            form_users = [
                q
                for q in self._questions.get(label, [])
                if q.get("choice_list") == list_name
            ]
            if choices is None:
                for q in form_users:
                    if q.get("choices"):
                        choices = q["choices"]
                        break

            if not using_vars and not form_users:
                continue

            withheld = next(
                (
                    w
                    for w in [
                        *(
                            self._vardicts[label][v]["survey"].get("choices_withheld")
                            for v in using_vars
                        ),
                        *(q.get("choices_withheld") for q in form_users),
                    ]
                    if w
                ),
                None,
            )

            lines = [f"[{label}] Choice list: {list_name}"]
            if withheld:
                lines.append(
                    f"  Choices withheld: {', '.join(withheld)} (the list is read "
                    "from data; refer to it, never reconstruct it)"
                )
            if not using_vars:
                lines.append(
                    "  (no dataset column uses this list; questions: "
                    + ", ".join(q["variable_name"] for q in form_users)
                    + ")"
                )
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
            elif not withheld:
                lines.append("  Choices: (external csv or not embedded in form)")

            lines.append(f"  Variables using this list ({len(using_vars)}):")
            for vn in using_vars[:50]:
                entry = self._vardicts.get(label, {}).get(vn, {})
                qt = (entry.get("survey", {}).get("question_text") or "").strip()[:55]
                sent = _fmt_sentinel_compact(entry.get("sentinels"))
                sent_note = f" | SENT:{sent}" if sent != "none" else ""
                lines.append(f"    {self._shown(label, vn):30s} | {qt}{sent_note}")
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
                1
                for v in vardict.values()
                if isinstance(v, dict) and v.get("sentinels")
            )
            n_repeat = sum(
                1
                for v in vardict.values()
                if isinstance(v, dict) and v.get("repeat_iteration") is not None
            )

            lines.append("")
            lines.append(f"  [{label}]")
            lines.append(f"    observations       : {nn_str}")
            lines.append(f"    total_variables    : {sm.get('total_variables', '?')}")
            # Prefer the post-XML-overlay counts when enrichment has run --
            # matched_to_questions/unmatched are the stale pre-XML fuzzy counts
            # and understate matched / overstate unmatched for enriched
            # dictionaries. (#24)
            if "matched_after_xml" in sm:
                _matched = sm["matched_after_xml"]
                _total = sm.get("total_variables")
                _unmatched = (
                    _total - _matched
                    if isinstance(_total, int) and isinstance(_matched, int)
                    else sm.get("unmatched", "?")
                )
                lines.append(f"    matched_to_form    : {_matched} (post-XML)")
                lines.append(f"    unmatched          : {_unmatched}")
            else:
                lines.append(
                    f"    matched_to_form    : {sm.get('matched_to_questions', '?')}"
                )
                lines.append(f"    unmatched          : {sm.get('unmatched', '?')}")
            lines.append(
                f"    from_repeat_groups : {sm.get('from_repeat_groups', '?')}"
            )
            lines.append(f"    repeat_vars_loaded : {n_repeat}")
            lines.append(
                f"    select_multiple    : {sm.get('select_multiple_choices', '?')}"
            )
            lines.append(f"    vars_with_sentinels: {n_sent}")

            if self._count_vars.get(label):
                lines.append("    repeat_groups:")
                for base, count_var in sorted(self._count_vars[label].items()):
                    lines.append(f"      {base:25s} count_var: {count_var}")

        return "\n".join(lines)

    def get_gate_chain(self, stata_name: str, survey: str | None = None) -> str:
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
            column = self._column(label, stata_name)
            entry = self._vardicts.get(label, {}).get(column)
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
            lines = [f"[{label}] Gate chain for: {self._shown(label, column)}"]
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
                            f"{c['value']}={c['label'].strip()}" for c in gchoices[:6]
                        )
                        info["choices"] = f"{gcl}: {cstr}"
                    elif gq.get("constraint"):
                        info["choices"] = f"constraint: {gq['constraint']}"
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
                    groups = rinfo.get("group_relevances", [])

                    lines.append(f"{prefix}  ^ {ref}: {qt}")
                    if choices:
                        lines.append(f"{prefix}    [{choices}]")
                    if own or groups:
                        gate = own or "; ".join(groups)
                        own_display = re.sub(r"\$\{([^}]+)\}", r"\1", gate)
                        lines.append(f"{prefix}    asked when: {own_display}")
                    else:
                        lines.append(f"{prefix}    always asked")

                indent += 1

            # Final line: the target variable itself
            prefix = "  " * indent
            qt = (survey_data.get("question_text") or "").strip()[:80]
            lines.append(f"{prefix}>>> {self._shown(label, column)}: {qt}")

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

    def _suggest_similar(
        self, name: str, survey: str | None = None, max_suggestions: int = 5
    ) -> list[str]:
        """Find variables with similar names (prefix/substring match)."""
        labels = self._filtered_labels(survey)
        name_lower = name.lower()
        candidates = []
        for label in labels:
            for var_name in self._vardicts.get(label, {}):
                vl = var_name.lower()
                if (
                    name_lower in vl
                    or vl in name_lower
                    or len(name_lower) >= 5
                    and name_lower[:5] == vl[:5]
                ):
                    candidates.append(self._shown(label, var_name))
                if len(candidates) >= max_suggestions * 3:
                    break
        candidates.sort(key=lambda x: abs(len(x) - len(name)))
        return candidates[:max_suggestions]

    # -- Variable neighborhood graph -------------------------------------

    def _resolve_form_name(
        self, stata_name: str, survey: str | None = None
    ) -> tuple[str | None, str | None]:
        """Resolve a Stata variable name to its form-level name and survey label."""
        labels = self._filtered_labels(survey)
        for label in labels:
            entry = self._vardicts.get(label, {}).get(self._column(label, stata_name))
            if isinstance(entry, dict):
                form_name = (
                    entry.get("survey", {}).get("original_variable_name") or stata_name
                )
                return form_name, label
        return None, None

    def get_neighborhood(
        self, name: str, depth: int = 1, survey: str | None = None
    ) -> str:
        """Return the variable neighborhood from the relationship graph."""
        self._check_reload()
        if self.is_empty:
            return self._empty_message()

        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        # Check if any graphs are loaded
        available = [lbl for lbl in labels if lbl in self._graphs]
        if not available:
            return (
                "No variable graph loaded. Generate it by running:\n"
                "  uv run surveycto-vardict --survey <KEY>"
            )

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
        all_paths = G.shortest_paths(form_name, cutoff=depth)
        neighbor_nodes = set(all_paths)

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
            for _, v, d in G.out_edges(form_name):
                if v in neighbor_nodes:
                    _classify_out(d.get("type", ""), v)
            for u, _, d in G.in_edges(form_name):
                if u in neighbor_nodes:
                    _classify_in(d.get("type", ""), u)
        else:
            # Multi-hop: classify each neighbor by the first-hop edge
            # that leads to it from form_name
            first_hop_out = {}  # neighbor -> set of edge types via out
            for _, v, d in G.out_edges(form_name):
                first_hop_out.setdefault(v, set()).add(d.get("type", ""))
            first_hop_in = {}  # neighbor -> set of edge types via in
            for u, _, d in G.in_edges(form_name):
                first_hop_in.setdefault(u, set()).add(d.get("type", ""))

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
                lines.append(
                    f"  repeat_tree: depth={rg_info['depth']}, "
                    f"count_var={rg_info['count_var']}, "
                    f"count_expr={rg_info.get('count_expr', '?')}, "
                    f"max_iter={rg_info['max_iterations']}, "
                    f"suffix={rg_info['stata_suffix_pattern']}"
                )
                if rg_info.get("parent"):
                    lines.append(f"  repeat_parent: {rg_info['parent']}")
                lines.append(f"  join_key: {rg_info['join_key_note']}")
        sv = target_attrs.get("stata_vars", [])
        if sv:
            lines.append(
                f"  stata_vars: {', '.join(self._shown(matched_label, v) for v in sv)}"
            )

        # Relationship sections with risk semantics
        sections = [
            (
                "Calculates from (inputs -- sentinel contamination risk)",
                "calculates_from",
            ),
            (
                "Calculated by (downstream -- changing this changes them)",
                "calculated_by",
            ),
            ("Gated by (missing-by-logic if gate is false)", "gated_by"),
            ("Gates (changing this can make these disappear)", "gates"),
            (
                "Group-gated by (structural gate -- affects whole group)",
                "group_gated_by",
            ),
            ("Group-gates (structural -- these groups depend on this)", "group_gates"),
            ("Constrained by (validation -- defines valid ranges)", "constrained_by"),
            (
                "Constrains (validation -- these depend on valid values here)",
                "constrains",
            ),
            (
                "Repeat siblings (same repeat iteration -- handle together)",
                "repeat_sibling",
            ),
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
                shown = [self._shown(matched_label, v) for v in m_svars]
                sv_str = f" -> {', '.join(shown)}" if m_svars else ""
                depth_str = f" [repeat_depth={m_depth}]" if m_depth > 0 else ""
                lines.append(f"    {m} ({m_type}){depth_str}{sv_str}")

        if all(not edge_groups.get(k) for _, k in sections):
            lines.append("\n  (no relationships found)")

        return "\n".join(lines)

    def get_group(self, group: str, survey: str | None = None) -> str:
        """List a group's questions in form order, with their type and condition:
        the group's own gate, the enclosing groups' and each subgroup's once.
        """
        self._check_reload()
        if self.is_empty:
            return self._empty_message()
        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []
        for label in labels:
            members = [
                q
                for q in self._questions.get(label, [])
                if group in (q.get("group_path") or [])
            ]
            if not members:
                continue
            lines = [f"[{label}] {group}: {len(members)} questions"]
            own = next(
                (g for q in members for g in q["gates"] if g["group"] == group), None
            )
            if own:
                lines[0] += f", asked if {own['relevance']}"
            # The groups it sits in gate it too, outermost first.
            path = members[0]["group_path"]
            outer = path[: path.index(group)]
            lines += [
                f"  inside {g['group']}, asked if {g['relevance']}"
                for g in members[0]["gates"]
                if g["group"] in outer
            ]
            shown: set[str] = set()
            for q in members:
                inner = q["group_path"][q["group_path"].index(group) + 1 :]
                # Each subgroup's condition once, where its questions start.
                for g in q["gates"]:
                    if g["group"] in inner and g["group"] not in shown:
                        shown.add(g["group"])
                        lines.append(f"  -- {g['group']}: if {g['relevance']}")
                where = f" [{'/'.join(inner)}]" if inner else ""
                when = f"  if {q['relevance']}" if q["relevance"] else ""
                lines.append(f"  {q['variable_name']} ({q['type']}){where}{when}")
            results.append("\n".join(lines))

        if not results:
            groups = sorted(
                {
                    g
                    for label in labels
                    for q in self._questions.get(label, [])
                    for g in q.get("group_path") or []
                }
            )
            close = difflib.get_close_matches(group, groups, n=5)
            return f"No group named {group!r}." + (
                f" Close: {', '.join(close)}" if close else ""
            )
        return "\n\n".join(results)

    def trace_lineage(self, name: str, survey: str | None = None) -> str:
        """Trace where a field's value comes from, step by step within the form: a
        calculate to the fields it reads, a pulldata() or search() to the dataset
        column and the field it is matched on, down to questions someone answered.
        """
        self._check_reload()
        if self.is_empty:
            return self._empty_message()
        labels = self._filtered_labels(survey)
        if survey and not labels:
            return self._no_survey_match_msg(survey)

        results = []
        for label in labels:
            column = self._column(label, name)
            entry = self._vardicts.get(label, {}).get(column) or {}
            question = (entry.get("survey") or {}).get("original_variable_name") or name
            q = self._q_index.get(label, {}).get(question)
            if q is None:
                continue
            shown = self._shown(label, column) if entry else question
            lines = [f"[{label}] Where {shown} comes from:"]
            self._lineage(label, q, 1, set(), lines)
            results.append("\n".join(lines))

        if not results:
            suggestions = self._suggest_similar(name, survey)
            msg = f"No match for '{name}'."
            if suggestions:
                msg += f"\n  Did you mean: {', '.join(suggestions[:5])}"
            return msg
        return "\n\n".join(results)

    def _lineage(
        self, label: str, q: dict, depth: int, seen: set[str], lines: list[str]
    ) -> None:
        """Append one step of ``q``'s lineage, then the steps of what it reads."""
        pad = "  " * depth
        name = q["variable_name"]
        if name in seen:
            lines.append(f"{pad}{name} (traced above)")
            return
        seen.add(name)
        q_index = self._q_index.get(label, {})
        qtype = q["type"]
        source = q["data_source"]
        reads: list[str] = []
        if (
            qtype in ("calculate", "calculate_here", "repeat_count")
            and q["calculation"]
        ):
            # Every field the formula reads, a pulldata()'s match included; the
            # dataset's own rows come from outside the form.
            lines.append(f"{pad}{name} ({qtype}) = {q['calculation']}")
            if source:
                lines.append(f"{pad}  <- {_source_line(source)}")
            reads = _REF.findall(q["calculation"])
        else:
            # A question someone answered; a search() select's choices are a
            # dataset's rows, filtered by the fields its match reads.
            text = (q["question_text"] or "").strip()[:80]
            withheld = q.get("choices_withheld") or next(
                (
                    e["survey"]["choices_withheld"]
                    for e in self._vardicts.get(label, {}).values()
                    if (e.get("survey") or {}).get("original_variable_name") == name
                    and e["survey"].get("choices_withheld")
                ),
                None,
            )
            if withheld:
                note = f"  [list withheld: {', '.join(withheld)}]"
            elif q["choice_list"]:
                note = f"  [choices: {q['choice_list']}]"
            else:
                note = ""
            lines.append(
                f"{pad}{name} ({qtype})" + (f": {text}" if text else "") + note
            )
            if source:
                lines.append(f"{pad}  <- choices from {_source_line(source)}")
                reads = [
                    r for m in source["match"].values() if m for r in _REF.findall(m)
                ]
        repeats = {
            q2["repeat_group_name"]
            for q2 in self._questions.get(label, [])
            if q2.get("repeat_group_name")
        }
        for ref in dict.fromkeys(reads):
            if ref in q_index:
                self._lineage(label, q_index[ref], depth + 1, seen, lines)
            elif ref in repeats:
                lines.append(f"{pad}  {ref} (repeat group; its instances)")
            else:
                lines.append(f"{pad}  {ref} (not a question in this form)")

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
            for rg_name, info in sorted(
                tree.items(), key=lambda x: (x[1]["depth"], x[0])
            ):
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
# Shell front end (the skill's search_survey.py)
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"node_modules", "__pycache__"}


def discover(root: Path) -> dict[str, dict]:
    """Return a DATASETS-shaped dict for every ``*_variable_dictionary.json`` under
    ``root``, each with the ``<key>_questions.json`` beside it. Hidden folders
    (``.venv``, ``.git``) and ``node_modules`` are not descended into; two
    dictionaries with one key raise, since either could be the stale one.
    """
    found: dict[str, dict] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
        )
        for name in sorted(names):
            if not name.endswith("_variable_dictionary.json"):
                continue
            path = Path(folder) / name
            label = name.removesuffix("_variable_dictionary.json")
            if label in found:
                raise SystemExit(
                    f"two dictionaries for survey {label!r}: "
                    f"{found[label]['output_json']} and {path}; pass --dir to the "
                    "current outputs"
                )
            found[label] = {
                "output_json": path,
                "questions_json": path.with_name(f"{label}_questions.json"),
            }
    return found


def main(argv: list[str] | None = None, store: SurveyStore | None = None) -> None:
    """Answer one lookup from the shell, as the MCP tools would."""
    parser = argparse.ArgumentParser(
        description="Look up SurveyCTO survey metadata from the extractor's outputs.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path("."),
        help="folder searched for *_variable_dictionary.json (default: here)",
    )
    parser.add_argument("--survey", help="survey key, partial match")
    sub = parser.add_subparsers(dest="command", required=True)
    var = sub.add_parser("var", help="one variable in full, or several in brief")
    var.add_argument("names", nargs="+")
    var.add_argument("--context", type=int, default=0)
    search = sub.add_parser("search", help="questions by words (TF-IDF ranked)")
    search.add_argument("words", nargs="+")
    search.add_argument("--max", type=int, default=20)
    choices = sub.add_parser("choices", help="a choice list and who uses it")
    choices.add_argument("list_name")
    gates = sub.add_parser("gates", help="why a question is asked")
    gates.add_argument("name")
    hood = sub.add_parser("neighborhood", help="what feeds, gates or follows it")
    hood.add_argument("name")
    hood.add_argument("--depth", type=int, default=1)
    group = sub.add_parser("group", help="a group's questions in order")
    group.add_argument("group")
    lineage = sub.add_parser("lineage", help="where a field's value comes from")
    lineage.add_argument("name")
    sub.add_parser("repeats", help="repeat groups: nesting, counts, suffixes")
    sub.add_parser("info", help="overview of the loaded surveys")
    args = parser.parse_args(argv)

    if store is None:
        datasets = discover(args.dir)
        if not datasets:
            parser.error(f"no *_variable_dictionary.json under {args.dir.resolve()}")
        store = SurveyStore(datasets)
    survey = args.survey
    if args.command == "var" and len(args.names) == 1:
        out = store.lookup_var(args.names[0], context=args.context, survey=survey)
    elif args.command == "var":
        out = store.lookup_vars_batch(args.names, survey=survey)
    elif args.command == "search":
        out = store.search(" ".join(args.words), max_results=args.max, survey=survey)
    elif args.command == "choices":
        out = store.get_choice_list(args.list_name, survey=survey)
    elif args.command == "gates":
        out = store.get_gate_chain(args.name, survey=survey)
    elif args.command == "neighborhood":
        out = store.get_neighborhood(args.name, depth=args.depth, survey=survey)
    elif args.command == "group":
        out = store.get_group(args.group, survey=survey)
    elif args.command == "lineage":
        out = store.trace_lineage(args.name, survey=survey)
    elif args.command == "repeats":
        out = store.get_repeat_structure(survey=survey)
    else:
        out = store.get_info()
    # Question text is often not ASCII; a Windows console defaults to cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    print(out)
    # A miss exits 1, so a shell check (`... || echo missing`) can tell.
    first = out.split("\n", 1)[0]
    if first.startswith("No ") or "not found" in first or " refused " in first:
        sys.exit(1)


if __name__ == "__main__":
    main()
