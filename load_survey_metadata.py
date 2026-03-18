import json
import re

import pandas as pd


def load_survey_metadata(json_path: str) -> pd.DataFrame:
    """
    Load survey variable metadata from the ltfu_hh variable dictionary JSON.

    Returns a DataFrame with one row per variable, indexed by variable_name.
    Minimum columns guaranteed: variable_order, variable_name, type, label, relevance.
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    variables = data["variables"]

    rows = []
    for var_name, var_data in variables.items():
        survey = var_data.get("survey", {}) or {}
        stata = var_data.get("stata", {}) or {}
        rows.append({
            "variable_name":   var_name,
            "variable_order":  var_data.get("variable_order"),
            "non_null_count":  var_data.get("non_null_count"),
            "type":            survey.get("type"),
            "label":           survey.get("question_text"),
            "relevance":       survey.get("skip_logic_iteration_specific") or survey.get("stata_skip_logic"),
            # extra fields kept for completeness
            "stata_type":      stata.get("type"),
            "original_variable_name": survey.get("original_variable_name"),
            "disabled":        survey.get("disabled"),
            "choice_list":     survey.get("choice_list"),
            "group_path":      survey.get("group_path"),
            "group_relevances": survey.get("group_relevances"),
        })

    df = pd.DataFrame(rows)
    df = df.set_index("variable_name")
    df = df.sort_values("variable_order")

    print("Shape:", df.shape)
    print("\nColumns:", df.columns.tolist())
    print("\nHead(10):")
    print(df.head(10).to_string())

    return df


NUMERIC_TYPES = {
    "select_one", "select_multiple", "integer", "decimal",
    "calculate", "calculate_here", "repeat_count", "date",
}


def get_numeric_universe(survey_metadata: pd.DataFrame) -> pd.DataFrame:
    """
    Filter survey_metadata to variable types that produce numeric Stata values.

    Keeps rows whose 'type' is one of: select_one, select_multiple, integer,
    decimal, calculate, calculate_here, repeat_count, date — AND that have at
    least one non-null observation in the Stata dataset.

    Returns a subset DataFrame with the same columns/index as the input.
    """
    mask = (
        survey_metadata["type"].isin(NUMERIC_TYPES)
        & (survey_metadata["non_null_count"] > 0)
        & (~survey_metadata["disabled"].astype(bool))
    )
    subset = survey_metadata[mask].copy()

    counts = subset["type"].value_counts()
    print("Count by type:")
    print(counts.to_string())
    print(f"\nTotal: {len(subset)}")

    return subset


_UNTRANSLATED_FUNCS = re.compile(
    r'\bselected\s*\('          # dynamic selected() not resolved to == N
    r'|count-selected\s*\('     # count-selected() — no Stata equivalent
    r'|\bindex\s*\(',            # index() — should be gone after skip_logic_iteration_specific, safety net
    re.IGNORECASE,
)

_STATA_KEYWORDS = frozenset({
    "if", "in", "not", "and", "or", "inlist", "inrange", "missing",
    "mi", "abs", "max", "min", "mod", "int", "floor", "ceil", "round",
})

# Stata built-in functions that appear as word tokens inside clauses but are
# not variable names.  Used by _clause_has_bad_var to avoid false positives.
_STATA_FUNCTIONS = frozenset({
    "rowtotal", "inlist", "inrange", "missing", "cond", "regexm",
    "substr", "strpos", "real", "int", "abs", "max", "min", "mod",
    "floor", "ceil", "round", "string", "length", "strlen",
    "log", "exp", "sqrt", "sign", "sum", "count",
})


def _clause_has_bad_var(
    clause: str,
    valid_cols: frozenset,
    string_cols: frozenset = frozenset(),
) -> bool:
    """
    Return True if `clause` references a variable that would cause a Stata error:
      - r(111) ambiguous abbreviation: variable absent from the dataset
      - r(109) type mismatch: variable is string-typed (won't be destringed)

    Two passes:
    1. Variables directly before comparison operators (var OP value) — original approach.
    2. All other identifiers in the clause, e.g. inside inlist(), rowtotal(), etc.
       These are checked only for r(111) (absent variable), since string variables
       that appear only as function arguments (not in comparisons) are less likely
       to cause r(109) but can still cause r(111) if absent from the dataset.

    The combined set is checked against valid_cols and string_cols.
    """
    _SKIP = _STATA_KEYWORDS | _STATA_FUNCTIONS

    # Pass 1: variables before comparison operators
    comparison_vars = set(re.findall(r'\b([a-zA-Z_]\w*)\s*(?:==|!=|>=|<=|>|<)', clause))

    # Pass 2: all identifiers (catches function arguments like inlist(var, ..))
    all_idents = set(re.findall(r'\b([a-zA-Z_]\w*)\b', clause)) - _SKIP

    for tok in comparison_vars - _SKIP:
        if tok not in valid_cols:
            return True   # absent from dataset → r(111)
        if tok in string_cols:
            return True   # string-typed after destring → r(109)

    for tok in all_idents - comparison_vars:
        # Only check for absence (r(111)); string check already done above
        if tok not in valid_cols:
            return True   # absent from dataset → r(111)

    return False


def _split_top_level_and(cond: str) -> list[str]:
    """Split a condition string on top-level & operators (not inside parentheses)."""
    clauses: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in cond:
        if ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == '&' and depth == 0:
            clauses.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        clauses.append(''.join(current).strip())
    return clauses


def _strip_untranslatable_clauses(
    cond: str,
    valid_cols: frozenset = frozenset(),
    string_cols: frozenset = frozenset(),
) -> str:
    """
    Remove & clauses that contain:
      - Untranslatable SurveyCTO functions (selected(), count-selected(), index())
      - Comparisons against variable names absent from the dataset (e.g. repeat-group
        base names whose iteration suffix was never substituted by the extractor)

    Preserves the remaining valid clauses.  Returns "" if all clauses are stripped.

    Examples:
        "(consent==1) & (selected(x, y))"            -> "(consent==1)"
        "(consent==1) & (emp_act_loc==1)"             -> "(consent==1)"
        "(consent==1) & (a!=0) & (selected(x, y))"   -> "(consent==1) & (a!=0)"
        "(selected(x, y))"                            -> ""
    """
    clauses = _split_top_level_and(cond)

    good = []
    for c in clauses:
        if not c:
            continue
        if _UNTRANSLATED_FUNCS.search(c):
            continue
        if valid_cols and _clause_has_bad_var(c, valid_cols, string_cols):
            continue
        # Strip always-false disabled clauses like (0)
        if re.fullmatch(r'\(\s*0\s*\)', c):
            continue
        good.append(c)
    return " & ".join(good)


def build_relevance_groups(
    numeric_universe: pd.DataFrame,
    valid_cols: frozenset = frozenset(),
    string_cols: frozenset = frozenset(),
) -> dict:
    """
    Group variables in numeric_universe by their stata_skip_logic condition.

    Variables with NaN or empty relevance are unconditional and keyed as "".
    Clauses within a condition that contain untranslatable SurveyCTO functions
    (dynamic selected(), count-selected(), leftover index()) or comparisons against
    variable names absent from the dataset (e.g. repeat-group base names whose
    iteration suffix was never substituted by the extractor) are stripped so
    remaining valid Stata clauses are preserved.  If stripping removes all clauses
    the variable becomes unconditional.

    Args:
        numeric_universe: DataFrame with 'relevance' column
        valid_cols: frozenset of column names actually present in the Stata dataset.
                    When provided, clauses referencing absent variables are also stripped.

    Returns:
        dict mapping stata_skip_logic string -> space-separated variable names
    """
    # Normalise: treat NaN and whitespace-only as unconditional
    relevance = numeric_universe["relevance"].fillna("").str.strip()

    stripped_count = 0
    stripped_examples: list[tuple[str, str, str]] = []   # (var, original, cleaned) — up to 3

    groups: dict[str, list] = {}
    for var_name, cond in relevance.items():
        needs_strip = cond and (
            _UNTRANSLATED_FUNCS.search(cond)
            or (valid_cols and any(
                _clause_has_bad_var(c, valid_cols, string_cols)
                for c in _split_top_level_and(cond) if c
            ))
        )
        if needs_strip:
            cleaned = _strip_untranslatable_clauses(cond, valid_cols, string_cols)
            stripped_count += 1
            if len(stripped_examples) < 3:
                stripped_examples.append((var_name, cond, cleaned))
            cond = cleaned
        groups.setdefault(cond, []).append(var_name)

    # Convert lists to space-separated strings
    result = {cond: " ".join(names) for cond, names in groups.items()}

    # Diagnostics
    n_unconditional = len(groups.get("", []))
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    print(f"Total unique relevance groups: {len(result)}")
    print(f"Unconditional variables (empty relevance): {n_unconditional}")
    if stripped_count:
        print(f"Conditions with untranslatable clauses stripped: {stripped_count} vars (showing up to 3 examples):")
        for var, orig, cleaned in stripped_examples:
            print(f"  {var}:")
            print(f"    before : {orig[:110]}")
            print(f"    after  : {cleaned[:110] if cleaned else '(unconditional)'}")

    print("\nTop 5 largest groups:")
    for cond, names in sorted_groups[:5]:
        label = '""  (unconditional)' if cond == "" else f'"{cond}"'
        print(f"  {len(names):5d} vars  {label}")
        print(f"         first 3: {', '.join(names[:3])}")

    return result


_STATA_LOCAL_LIMIT = 67_000  # conservative; Stata hard limit is 67,784 chars


def _chunk_varlist(var_names: list[str], limit: int = _STATA_LOCAL_LIMIT) -> list[list[str]]:
    """Split a variable list into chunks that each fit within Stata's local macro limit."""
    chunks, current, current_len = [], [], 0
    for v in var_names:
        token_len = len(v) + 1  # +1 for the separating space
        if current and current_len + token_len > limit:
            chunks.append(current)
            current, current_len = [v], token_len
        else:
            current.append(v)
            current_len += token_len
    if current:
        chunks.append(current)
    return chunks


def write_destring_block(
    destring_universe: pd.DataFrame,
    mvdecode_universe: pd.DataFrame = None,
) -> str:
    """
    Generate Stata code to:
      1. Destring variables in destring_universe that are still stored as strings
      2. Recode sentinels across mvdecode_universe (defaults to destring_universe)

    Wrapped in a skip_cleaning toggle: when skip_cleaning==1 the cleaned dataset
    is loaded from a tempfile saved on the previous run, bypassing all destring
    and mvdecode work.  Set skip_cleaning=0 (default) for a fresh run.

    Both passes are wrapped in quietly {} to suppress "missing values generated"
    output spam.

    Sentinel mapping (from ltfu_hh_questions.json choice lists):
      -99 = Don't know  (234 choice lists) -> .d
      -88 = Refused     ( 64 choice lists) -> .r
      -77 = N/A         (  2 choice lists) -> .n
      -66 = Other/spec  (132 choice lists) -> .o
      -55 = Not in list ( 55 choice lists) -> .m
    """
    if mvdecode_universe is None:
        mvdecode_universe = destring_universe

    ds_names  = destring_universe.index.tolist()
    ds_chunks = _chunk_varlist(ds_names)
    mv_names  = mvdecode_universe.index.tolist()
    mv_chunks = _chunk_varlist(mv_names)

    lines = [
        "* ---------------------------------------------------------------",
        "* Destring + sentinel recode",
        f"* Pass 1 (destring) : {len(ds_names):,} convertible string vars",
        f"* Pass 2 (mvdecode) : {len(mv_names):,} numeric universe vars",
        "* skip_cleaning=1 reloads saved cleaned data, skipping both passes.",
        "* ---------------------------------------------------------------",
        "",
        "local skip_cleaning = 0",
        "",
        "if `skip_cleaning' == 1 {",
        '    use "${sumstats_dir}/temp_cleaned.dta", clear',
        "}",
        "else {",
        "",
    ]

    # chunk locals (indented inside else block)
    lines.append("    * -- chunk locals --")
    for i, chunk in enumerate(ds_chunks, 1):
        lines.append(f"    local ds_{i} {' '.join(chunk)}")
    lines.append("")
    for i, chunk in enumerate(mv_chunks, 1):
        lines.append(f"    local mv_{i} {' '.join(chunk)}")
    lines.append("")

    # Pass 1: destring inside quietly
    lines += [
        "    * Pass 1: destring convertible string variables",
        "    quietly {",
    ]
    for i in range(1, len(ds_chunks) + 1):
        lines.append(f"        ds `ds_{i}', has(type string)")
        lines.append(f"        if `\"`r(varlist)'\"' != \"\" destring `r(varlist)', replace")
    lines += ["    }", ""]

    # Pass 2: mvdecode inside quietly
    lines += [
        "    * Pass 2: recode sentinels (-99=.d  -88=.r  -77=.n  -66=.o  -55=.m)",
        "    quietly {",
    ]
    for i in range(1, len(mv_chunks) + 1):
        lines.append(f"        ds `mv_{i}', has(type numeric)")
        lines.append(
            f"        if `\"`r(varlist)'\"' != \"\" "
            f"mvdecode `r(varlist)', mv(-99=.d \\ -88=.r \\ -77=.n \\ -66=.o \\ -55=.m)"
        )
    lines += ["    }", ""]

    # save cleaned snapshot for skip_cleaning re-use
    lines += [
        '    save "${sumstats_dir}/temp_cleaned.dta", replace',
        "}",
    ]

    code = "\n".join(lines)
    print(code)
    return code


def save_survey_metadata_dta(survey_metadata: pd.DataFrame, output_path: str) -> None:
    """
    Save survey_metadata DataFrame as a Stata .dta file.

    Handles:
    - Resets index so variable_name becomes a regular column
    - Renames 'relevance' -> 'stata_skip_logic' to match write_merge_and_export
    - Drops group_relevances (complex object, not needed downstream)
    - Converts pandas StringDtype -> object (to_stata compatibility)
    - Converts bool -> int8 (Stata has no bool type)
    - variable_order and non_null_count confirmed as int64

    Prints shape, column list with dtypes, and confirms variable_name is a column.
    """
    df = survey_metadata.reset_index()  # variable_name becomes a regular column

    # Rename relevance -> stata_skip_logic (matches write_merge_and_export order cmd)
    df = df.rename(columns={"relevance": "stata_skip_logic"})

    # Drop complex column not needed for merge/export
    df = df.drop(columns=["group_relevances"], errors="ignore")

    # Convert pandas 3.x StringDtype -> object (to_stata requires object for strings)
    for col in df.columns:
        if hasattr(df[col].dtype, "name") and df[col].dtype.name == "string":
            df[col] = df[col].astype(object)

    # Convert bool -> int8 (Stata has no bool)
    for col in df.select_dtypes(include="bool").columns:
        df[col] = df[col].astype("int8")

    # Diagnostics
    print(f"Shape: {df.shape}")
    print(f"variable_name is a column (not index): {'variable_name' in df.columns}")
    print("\nColumn dtypes:")
    for col, dtype in df.dtypes.items():
        max_len = ""
        if df[col].dtype == object:
            mx = df[col].dropna().astype(str).str.len().max()
            max_len = f"  (max_len={mx})"
        print(f"  {col:30s}  {str(dtype):10s}{max_len}")

    df.to_stata(output_path, write_index=False, version=118)
    print(f"\nSaved -> {output_path}")


def write_merge_and_export() -> str:
    """
    Generate Stata code to merge sumstats_raw.dta with survey_metadata.dta,
    sort and order columns, save as sumstats_final.dta, and export to Excel.

    Returns the generated Stata code as a string and prints it.
    """
    lines = [
        "* ---------------------------------------------------------------",
        "* Merge stats with survey metadata, finalise, export",
        "* ---------------------------------------------------------------",
        "",
        "* Open stats output",
        'use "${analysis_data}/sumstats_raw.dta", clear',
        "",
        "* Merge 1:1 on variable_name with survey metadata",
        "* keep(match): variables that failed tabstat won't appear in",
        "* sumstats_raw so they are silently excluded here.",
        'merge 1:1 variable_name using "${analysis_data}/survey_metadata.dta", keep(match) nogenerate',
        "",
        "* Sort by position in the survey instrument",
        "sort variable_order",
        "",
        "* Column order for readability",
        "order variable_order variable_name label type N Mean SD Min p25 p50 p75 Max stata_skip_logic",
        "",
        "* Save final dataset",
        'save "${analysis_data}/sumstats_final.dta", replace',
        "",
        "* Export to Excel",
        'export excel using "${analysis_data}/sumstats_final.xlsx", firstrow(variables) replace',
    ]

    code = "\n".join(lines)
    print(code)
    return code


_STATS_CMD  = "stat(n mean sd min p25 p50 p75 max) columns(stat)"
_STAT_KEEP  = "variable_name N Mean SD Min p25 p50 p75 Max"


def write_stats_blocks(relevance_groups: dict) -> str:
    """
    Generate Stata code that collects tabstat summary statistics for every
    relevance group and saves sumstats_raw.dta.

    Strategy
    --------
    Stata/SE matsize max is 11,000 rows.  Accumulating all 15k+ variables in
    one matrix would exceed this.  Instead we accumulate per-group (max group
    is ~3,352 rows — well within the limit), save each group to a tempfile via
    preserve/drop */svmat, then append all tempfiles at the end.

    Within a multi-chunk group the transposed r(StatTotal) matrices are joined
    with the  \\ (vertical-concatenation) operator:
        matrix _grp = _grp \\ r(StatTotal)'
    so variable rows stack up before the group is flushed.

    Args:
        relevance_groups: output of build_relevance_groups — key is the
            stata_skip_logic condition string ("" = unconditional), value is
            space-separated variable names.

    Returns:
        Generated Stata code as a string.  Also prints diagnostics and the
        first 2 complete group blocks as a preview.
    """
    # --- duplicate-variable check -------------------------------------------
    all_vars: list[str] = []
    for var_str in relevance_groups.values():
        all_vars.extend(var_str.split())
    seen: set[str] = set()
    duplicates: list[str] = []
    for v in all_vars:
        if v in seen:
            duplicates.append(v)
        else:
            seen.add(v)
    if duplicates:
        uniq_dups = sorted(set(duplicates))
        print(f"WARNING: {len(uniq_dups)} variable(s) appear in multiple "
              f"relevance groups: {uniq_dups[:10]}"
              + (" ..." if len(uniq_dups) > 10 else ""))

    # --- sort: unconditional first, then by descending group size ------------
    sorted_groups = sorted(
        relevance_groups.items(),
        key=lambda x: (x[0] != "", -len(x[1].split())),
    )
    n_groups = len(sorted_groups)

    # pre-compute chunks so we can report total tabstat calls up front
    group_data: list[tuple[str, list[list[str]]]] = []
    total_tabstat = 0
    for cond, var_str in sorted_groups:
        chunks = _chunk_varlist(var_str.split())
        total_tabstat += len(chunks)
        group_data.append((cond, chunks))

    # --- code generation -----------------------------------------------------
    lines: list[str] = [
        "* ---------------------------------------------------------------",
        "* Summary statistics: tabstat collection",
        f"* {n_groups} relevance groups, {total_tabstat} tabstat calls total",
        "* Uses postfile to collect rows — no preserve/restore cycles.",
        "* ---------------------------------------------------------------",
        "",
        "tempname pf",
        "tempfile sumstats_temp",
        "postfile `pf' str244 variable_name double N double Mean double SD "
        "double Min double p25 double p50 double p75 double Max "
        "using `sumstats_temp', replace",
        "",
    ]

    for gi, (cond, chunks) in enumerate(group_data):
        n_vars  = sum(len(c) for c in chunks)
        n_chnks = len(chunks)
        cond_label = "(unconditional)" if cond == "" else f"if {cond}"

        lines.append(f"* --- Group {gi}: {cond_label} | {n_vars} vars, {n_chnks} chunk(s) ---")

        # chunk locals
        for ci, chunk in enumerate(chunks, 1):
            lines.append(f"local g{gi}_c{ci} {' '.join(chunk)}")

        # tabstat calls + matrix build (unchanged)
        for ci, chunk in enumerate(chunks, 1):
            if_clause = f" if {cond}" if cond else ""
            lines.append(
                f"tabstat `g{gi}_c{ci}'{if_clause}, save {_STATS_CMD}"
            )
            if ci == 1:
                lines.append("matrix _grp = r(StatTotal)'")
            else:
                lines.append("matrix _grp = _grp \\ r(StatTotal)'")

        # post each row from the group matrix — no preserve/restore needed
        lines += [
            "local _rn : rownames _grp",
            "local _nr = rowsof(_grp)",
            "forvalues _i = 1/`_nr' {",
            "    local _vname : word `_i' of `_rn'",
            "    post `pf' (\"`_vname'\") "
            "(_grp[`_i',1]) (_grp[`_i',2]) (_grp[`_i',3]) (_grp[`_i',4]) "
            "(_grp[`_i',5]) (_grp[`_i',6]) (_grp[`_i',7]) (_grp[`_i',8])",
            "}",
            "matrix drop _grp",
            "",
        ]

    # finalise postfile and save
    lines += [
        "* --- Close postfile and save ---",
        "postclose `pf'",
        "use `sumstats_temp', clear",
        'save "${analysis_data}/sumstats_raw.dta", replace',
    ]

    code = "\n".join(lines)

    # --- diagnostics + preview -----------------------------------------------
    print(f"Total relevance groups : {n_groups}")
    print(f"Total tabstat calls    : {total_tabstat}")

    code_lines = code.split("\n")
    group_markers = [
        i for i, l in enumerate(code_lines) if l.startswith("* --- Group ")
    ]

    print("\n=== Preview: first 2 complete group blocks ===")
    for gi in range(min(2, len(group_markers))):
        start = group_markers[gi]
        end   = group_markers[gi + 1] if gi + 1 < len(group_markers) else len(code_lines)
        block_lines = code_lines[start:end]
        # truncate giant varlist lines for readability
        preview = []
        for bl in block_lines:
            if bl.startswith("local g") and len(bl) > 80:
                toks = bl.split()
                n_v  = len(toks) - 2
                preview.append(
                    f"local {toks[1]}  [{n_v} vars: {' '.join(toks[2:5])} ...]"
                )
            else:
                preview.append(bl)
        print("\n".join(preview))
        print()

    return code


_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def filter_truly_numeric(
    numeric_universe: pd.DataFrame,
    dta_path: str,
    parquet_path: str = None,
) -> tuple[list[str], list[str]]:
    """
    Scan the actual Stata dataset to determine which variables in
    numeric_universe that are string-typed in the dataset can actually be
    destringed (every non-missing value looks numeric).

    Variables already stored as a numeric dtype (float64, int64, etc.) are
    not loaded and are returned implicitly as safe — they go straight to tabstat.

    Args:
        numeric_universe: output of get_numeric_universe, indexed by variable_name.
        dta_path:         path to the .dta file (e.g. ugs_ltfu_hh_checked.dta).
        parquet_path:     optional path to a parquet sidecar.  When provided,
                          string columns are read from parquet (columnar, fast)
                          instead of re-reading the .dta.

    Returns:
        convertible     — str-typed vars whose non-null values are all numeric-looking.
                          Safe to destring; can stay in the universe.
        non_convertible — str-typed vars with at least one non-numeric text value.
                          Should be dropped from the universe before tabstat.

    Prints:
        - Count of purely-numeric vars (skipped), convertible, non_convertible
        - For each non_convertible: variable name + up to 5 example bad values
    """
    # -----------------------------------------------------------------------
    # 1. Identify string-typed variables in the universe
    # -----------------------------------------------------------------------
    str_mask = (
        numeric_universe["stata_type"].str.startswith("str", na=False)
        | numeric_universe["stata_type"].isin(["object"])
    )
    str_vars  = numeric_universe[str_mask].index.tolist()
    num_vars  = numeric_universe[~str_mask].index.tolist()

    print(f"Numeric universe        : {len(numeric_universe):,} vars")
    print(f"  Already numeric dtype : {len(num_vars):,}  (not scanned)")
    print(f"  String dtype          : {len(str_vars):,}  (will scan dataset)")

    if not str_vars:
        print("No string vars to scan — all vars are numeric, nothing to filter.")
        return [], []

    # -----------------------------------------------------------------------
    # 2. Load only the string-typed columns from the dataset
    # -----------------------------------------------------------------------
    import time
    t0 = time.perf_counter()
    if parquet_path:
        print(f"\nLoading {len(str_vars):,} string columns from parquet ...")
        df = pd.read_parquet(parquet_path, columns=str_vars)
    else:
        print(f"\nLoading {len(str_vars):,} string columns from .dta ...")
        df = pd.read_stata(dta_path, columns=str_vars, convert_categoricals=False)
    elapsed = time.perf_counter() - t0
    print(f"  Loaded: {df.shape[0]:,} obs x {df.shape[1]:,} cols ({elapsed:.1f}s)")

    # -----------------------------------------------------------------------
    # 3. For each string column, check whether every non-null value is numeric
    # -----------------------------------------------------------------------
    convertible:     list[str] = []
    non_convertible: list[str] = []
    bad_examples:    dict[str, list[str]] = {}

    for var in str_vars:
        col = df[var].dropna().astype(str)
        col = col[col.str.strip() != ""]   # treat blank string as missing too

        bad = col[~col.str.strip().map(lambda v: bool(_NUMERIC_RE.match(v.strip())))]

        if bad.empty:
            convertible.append(var)
        else:
            non_convertible.append(var)
            bad_examples[var] = bad.unique()[:5].tolist()

    # -----------------------------------------------------------------------
    # 4. Print diagnostics
    # -----------------------------------------------------------------------
    print(f"\n=== filter_truly_numeric results ===")
    print(f"  Convertible (destring-safe) : {len(convertible):,}")
    print(f"  Non-convertible (drop)      : {len(non_convertible):,}")

    if non_convertible:
        print(f"\n  Non-convertible variables (name + example values):")
        for var in non_convertible:
            examples = ", ".join(repr(v) for v in bad_examples[var])
            # Guard against Windows cp1252 terminal encoding issues
            safe = examples.encode("ascii", errors="replace").decode("ascii")
            print(f"    {var:40s}  {safe}")

    return convertible, non_convertible


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python load_survey_metadata.py <variable_dictionary.json> <dataset.dta>")
        sys.exit(1)
    json_path = sys.argv[1]
    dta_path = sys.argv[2]
    df = load_survey_metadata(json_path)
    numeric_df = get_numeric_universe(df)
    convertible, non_convertible = filter_truly_numeric(numeric_df, dta_path)
