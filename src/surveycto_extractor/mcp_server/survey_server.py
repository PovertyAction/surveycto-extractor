"""The survey-expert MCP server: ``surveycto_extractor.survey.SurveyStore`` over stdio.

``surveycto-mcp`` serves the project whose config it discovers (``config.toml`` or
``config.py`` in the working directory, or ``SURVEYCTO_CONFIG``); every tool is a
thin call into the store, so the skill's ``search_survey.py`` answers the same.
``surveycto-skill`` registers it in a project's ``.mcp.json``.

A missing or broken config starts the server empty, and the tools say what to
run: an always-on server must not crash a session.
"""

import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from surveycto_extractor.survey import SurveyStore


def _load_user_config():
    """Return the project config, or None when it is absent or broken."""
    # SURVEY_CONFIG is a legacy alias for SURVEYCTO_CONFIG.
    if "SURVEYCTO_CONFIG" not in os.environ and os.environ.get("SURVEY_CONFIG"):
        os.environ["SURVEYCTO_CONFIG"] = os.environ["SURVEY_CONFIG"]
    try:
        from surveycto_extractor.config_loader import config_path, load_config

        cfg = load_config()
        if cfg is None:
            print(
                f"[survey-expert] no config found at {config_path()}. Server starts "
                f"with no data. Set SURVEYCTO_CONFIG, launch from your project "
                f"directory, or run surveycto-vardict first.",
                file=sys.stderr,
            )
        return cfg
    except Exception as exc:  # broken config -> start empty rather than crash
        print(f"[survey-expert] failed to load config: {exc}", file=sys.stderr)
        return None


server = MCPServer(
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
        "Every column carries a pii.status: for a flagged one (contains_pii, "
        "may_contain_pii) the tools show counts, never values, and so must you. "
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
        cfg = _load_user_config()
        datasets = getattr(cfg, "DATASETS", {}) if cfg is not None else {}
        if datasets:
            print(
                f"[survey-expert] Config loaded ({len(datasets)} dataset(s))",
                file=sys.stderr,
            )
        elif cfg is not None:
            print(
                "[survey-expert] Config loaded but DATASETS is empty.",
                file=sys.stderr,
            )
        # flow.json sits in [project] output_dir (surveycto-flow); a legacy
        # config.py has no PROJECT.
        out_dir = (getattr(cfg, "PROJECT", None) or {}).get("output_dir")
        _store = SurveyStore(datasets, Path(out_dir) / "flow.json" if out_dir else None)
    return _store


@server.tool()
def lookup_variable(name: str, context: int = 0, survey: str = "") -> str:
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
    return _get_store().lookup_var(name, context=context, survey=survey or None)


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
def search_questions(query: str, max_results: int = 20, survey: str = "") -> str:
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
    return _get_store().search(query, max_results=max_results, survey=survey or None)


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
    return _get_store().get_choice_list(list_name, survey=survey or None)


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
    return _get_store().get_gate_chain(name, survey=survey or None)


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
def get_variable_neighborhood(name: str, depth: int = 1, survey: str = "") -> str:
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
      uv run surveycto-vardict --survey <KEY>

    Use the survey parameter to filter to a specific survey (partial match).
    """
    return _get_store().get_neighborhood(name, depth=depth, survey=survey or None)


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


@server.tool()
def get_group(group: str, survey: str = "") -> str:
    """List a group's questions in form order, with their type and condition.

    Shows the group's own relevance, the enclosing groups' conditions (outermost
    first) and each subgroup's condition once, where its questions start. Use it to
    read a section of the form as the enumerator met it.

    Use the survey parameter to filter to a specific survey (partial match).
    """
    return _get_store().get_group(group, survey=survey or None)


@server.tool()
def trace_lineage(name: str, survey: str = "") -> str:
    """Trace where a field's value comes from, step by step within the form.

    A calculate leads to the fields its formula reads; a pulldata() or search() to
    the dataset column it is read from and the field it is matched on; the chain
    ends at the questions someone answered. Accepts a Stata column name.

    Use the survey parameter to filter to a specific survey (partial match).
    """
    return _get_store().trace_lineage(name, survey=survey or None)


@server.tool()
def get_project() -> str:
    """Say what the project is about and how its surveys connect.

    Returns the user's description, the entities they named (each with its key and
    the surveys carrying it), the datasets linking the surveys (who publishes and
    who reads each) and the candidate keys not yet named. Call it first in a
    session; when the project is not described yet, it says how to set it up.
    """
    return _get_store().get_project()


def main() -> None:
    """Entry point for ``surveycto-mcp``: load the project and serve over stdio."""
    _get_store()
    server.run()


if __name__ == "__main__":
    main()
