"""
Create Variable Dictionaries
=============================
Maps Stata dataset variables to SurveyCTO question metadata.

Handles:
- Repeat groups (multiple iterations)
- Select_multiple questions (binary choice variables)
- Double-suffix patterns (select_multiple within repeats)

Usage:
    python create_variable_dictionaries.py [--survey KEY] [--validate] [--xlsx]

    --survey KEY   Process only the named survey (key from config.DATASETS).
                   Default: process all surveys in config.DATASETS.
    --validate     Run select_multiple validation after creation.
    --validate-only  Only run validation on existing dictionaries.
    --xlsx         Export XLSX variable dictionary alongside JSON.

Config:
    Edit config.py to add survey entries to the DATASETS dict.
"""

import pandas as pd
import numpy as np
import json
import re
import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pyreadstat
    _PYREADSTAT_AVAILABLE = True
except ImportError:
    _PYREADSTAT_AVAILABLE = False

try:
    import pyarrow  # noqa: F401 — presence check; pandas uses it via engine="pyarrow"
    import pyarrow.parquet as _pq
    _PYARROW_AVAILABLE = True
except ImportError:
    _pq = None
    _PYARROW_AVAILABLE = False

try:
    import networkx as nx
    _NETWORKX_AVAILABLE = True
except ImportError:
    nx = None
    _NETWORKX_AVAILABLE = False

# Pull DATASETS from this project's config.py (same directory as this script)
sys.path.insert(0, str(Path(__file__).parent))
from config import DATASETS


def _write_parquet_sidecar(df: pd.DataFrame, dta_path: Path) -> Optional[Path]:
    """Write a parquet sidecar next to the .dta.  Returns the path, or None."""
    if not _PYARROW_AVAILABLE:
        return None
    pq_path = dta_path.with_suffix('.parquet')
    t0 = time.perf_counter()
    df.to_parquet(pq_path, engine='pyarrow')
    elapsed = time.perf_counter() - t0
    size_mb = pq_path.stat().st_size / (1024 * 1024)
    print(f"  Wrote parquet sidecar: {pq_path.name} "
          f"({size_mb:.1f} MB, {elapsed:.1f}s)")
    return pq_path


def _read_parquet_column_stats(pq_path: Optional[Path]) -> Dict[str, Dict]:
    """Read per-column statistics from parquet row-group metadata.

    For numeric/date columns collects min/max.  For ALL columns collects
    null_count and num_values (non-null count), derived from row-group stats.
    Reads only file metadata — no data scan.

    Returns {col_name: {
        "min": value, "max": value,      # numeric/date only
        "null_count": int,               # total nulls across all row groups
        "num_values": int,               # total non-null values
        "missing_rate": float,           # null_count / total_rows
        "is_all_missing": bool,          # True when num_values == 0
    }}
    """
    if not _PYARROW_AVAILABLE or not pq_path or not Path(pq_path).exists():
        return {}

    t0 = time.perf_counter()
    pf = _pq.ParquetFile(str(pq_path))
    file_meta = pf.metadata
    schema = pf.schema_arrow

    n_fields = len(schema)
    col_names = [schema.field(i).name for i in range(n_fields)]
    col_types = [schema.field(i).type for i in range(n_fields)]

    # Identify numeric/temporal columns for min/max collection;
    # also identify null-typed columns (all-missing by definition — pyarrow
    # stores fully-null columns as 'null' type and emits no row-group stats).
    import pyarrow.types as _pat
    _numeric_or_temporal = {
        i for i, t in enumerate(col_types)
        if (_pat.is_integer(t) or _pat.is_floating(t) or _pat.is_decimal(t)
                or _pat.is_date(t) or _pat.is_timestamp(t) or _pat.is_time(t))
    }
    _null_typed = {i for i, t in enumerate(col_types) if _pat.is_null(t)}

    col_min: Dict[int, object] = {}
    col_max: Dict[int, object] = {}
    col_null: Dict[int, int] = {}
    col_vals: Dict[int, int] = {}

    total_rows = file_meta.num_rows

    for rg_idx in range(file_meta.num_row_groups):
        rg = file_meta.row_group(rg_idx)
        for col_idx in range(rg.num_columns):
            col = rg.column(col_idx)
            stats = col.statistics
            if stats is None:
                continue

            # Null/value counts — available for all column types
            if stats.null_count is not None:
                col_null[col_idx] = col_null.get(col_idx, 0) + stats.null_count
            if stats.num_values is not None:
                col_vals[col_idx] = col_vals.get(col_idx, 0) + stats.num_values

            # Min/max — numeric/temporal only
            if col_idx in _numeric_or_temporal and stats.has_min_max:
                lo, hi = stats.min, stats.max
                if col_idx not in col_min or lo < col_min[col_idx]:
                    col_min[col_idx] = lo
                if col_idx not in col_max or hi > col_max[col_idx]:
                    col_max[col_idx] = hi

    result: Dict[str, Dict] = {}
    for col_idx, name in enumerate(col_names):
        entry: Dict = {}

        # Min/max
        if col_idx in col_min:
            mn = col_min[col_idx]
            mx = col_max[col_idx]
            if mn is not None and mx is not None:
                mn = mn.item() if hasattr(mn, 'item') else mn
                mx = mx.item() if hasattr(mx, 'item') else mx
                if hasattr(mn, 'isoformat'):
                    mn = mn.isoformat()
                    mx = mx.isoformat()
                entry['min'] = mn
                entry['max'] = mx

        # Missing stats — from row-group statistics or from null schema type
        if col_idx in _null_typed:
            # null-typed columns have no row-group statistics; they are all-missing
            entry['null_count'] = total_rows
            entry['num_values'] = 0
            entry['missing_rate'] = 1.0
            entry['is_all_missing'] = True
        else:
            null_count = col_null.get(col_idx)
            num_values = col_vals.get(col_idx)
            if null_count is not None or num_values is not None:
                null_count = null_count or 0
                num_values = num_values or 0
                entry['null_count'] = null_count
                entry['num_values'] = num_values
                entry['missing_rate'] = round(null_count / total_rows, 6) if total_rows > 0 else 0.0
                entry['is_all_missing'] = (num_values == 0)

        if entry:
            result[name] = entry

    elapsed = time.perf_counter() - t0
    n_minmax = sum(1 for v in result.values() if 'min' in v)
    print(f"  Parquet column stats: {len(result)} columns, {n_minmax} with min/max ({elapsed:.1f}s)")
    return result


# Question types where data_min/data_max is informative (not select_one/
# select_multiple whose range is just choice codes or 0/1 binary indicators)
_MINMAX_QUESTION_TYPES = {'integer', 'decimal', 'calculate', 'calculate_here',
                          'date', 'datetime', 'time'}

_EXT_MISSING_LETTERS = set('abcdefghijklmnopqrstuvwxyz')


def _scan_and_clean_extended_missings(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, Dict[str, Dict[str, int]]]:
    """Scan for extended missing letter tags and clean them in one pass.

    pyreadstat with user_missing=True returns extended missings (.d, .r, etc.)
    as single-letter strings mixed into object columns.  This function:
      1. Detects which object columns contain letter tags (vectorized isin)
      2. Counts them per variable (for the sentinel scan)
      3. Replaces them with NaN and coerces back to numeric

    Returns (cleaned_df, {var_name: {"d": 5, "r": 2, ...}}).
    """
    t0 = time.perf_counter()
    result: Dict[str, Dict[str, int]] = {}

    obj_cols = [c for c in df.columns if df[c].dtype == object]
    if not obj_cols:
        print(f"  Extended missing scan+clean: 0 object cols, skipped ({time.perf_counter() - t0:.1f}s)")
        return df, result

    for col in obj_cols:
        # Vectorized check: isin is C-level, much faster than .apply(lambda)
        mask = df[col].isin(_EXT_MISSING_LETTERS)
        n_ext = mask.sum()
        if n_ext > 0:
            # Count by letter
            counts = df[col][mask].value_counts()
            result[col] = {str(k): int(v) for k, v in counts.items()}
            # Replace in-place and coerce back to numeric
            df.loc[mask, col] = np.nan
            try:
                df[col] = pd.to_numeric(df[col])
            except (ValueError, TypeError):
                pass  # genuine string column — leave as object

    elapsed = time.perf_counter() - t0
    n_vars = len(result)
    n_total = sum(sum(v.values()) for v in result.values())
    print(f"  Extended missing scan+clean: {n_vars} vars with {n_total:,} "
          f"ext missing values, {len(obj_cols)} object cols checked ({elapsed:.1f}s)")
    return df, result


def load_data(dataset_name: str):
    """Load Stata dataset and JSON metadata.

    Reads with user_missing=True to capture extended missing counts (.d, .r,
    etc.), then cleans the DataFrame back to standard NaN representation.
    When pyarrow is available a parquet sidecar is written from the cleaned
    data for fast columnar access downstream.
    """
    cfg = DATASETS[dataset_name]
    # Support both 'json' key (old style) and 'questions_json' key (template style)
    json_key = 'questions_json' if 'questions_json' in cfg else 'json'

    # Validate the bridge file exists before doing expensive .dta load
    questions_path = Path(cfg[json_key])
    if not questions_path.exists():
        raise FileNotFoundError(
            f"Bridge file not found: {questions_path}\n"
            f"  This file is produced by Phase 2 (JSON extraction) for the '{dataset_name}' survey.\n"
            f"  Run: python main.py --survey {dataset_name} --phases json"
        )

    dta_path = Path(cfg['data'])

    print(f"Loading {dataset_name} data...")
    t0 = time.perf_counter()
    ext_missing_counts = {}
    if _PYREADSTAT_AVAILABLE:
        df, meta = pyreadstat.read_dta(str(dta_path), user_missing=True)
        elapsed = time.perf_counter() - t0
        print(f"  Loaded {len(df)} obs x {len(df.columns)} vars "
              f"from .dta ({elapsed:.1f}s)")

        # Scan + clean extended missings in one fused pass (vectorized isin)
        df, ext_missing_counts = _scan_and_clean_extended_missings(df)
    else:
        df = pd.read_stata(cfg['data'])
        meta = None
        elapsed = time.perf_counter() - t0
        print(f"  Loaded {len(df)} obs x {len(df.columns)} vars "
              f"from .dta ({elapsed:.1f}s)")

    # Write parquet sidecar for fast columnar access downstream
    pq_path = _write_parquet_sidecar(df, dta_path)

    # Read per-column stats from parquet row-group metadata (no data scan)
    minmax = _read_parquet_column_stats(pq_path)

    with open(cfg[json_key], 'r', encoding='utf-8') as f:
        questions = json.load(f)
    print(f"  Loaded {len(questions)} questions from JSON")

    return df, questions, cfg, meta, pq_path, ext_missing_counts, minmax


SENTINEL_CODES_LIST = [-99, -88, -98, -77, -66, -55]
SENTINEL_STRINGS_SET = {"-99", "-88", "-98", "-77", "-66", "-55"}


def batch_sentinel_scan(
    df: pd.DataFrame,
    var_dict_df: pd.DataFrame,
    ext_missing_counts: Optional[Dict[str, Dict[str, int]]] = None,
) -> pd.DataFrame:
    """Vectorized sentinel scan across all columns at once.

    Detects four sentinel states:
      1. Raw integer: -99, -88 etc. as numeric values
      2. String sentinel: "-99", "-88" as text in string columns
      3. Extended missing: .d, .r etc. (from HFC recoding) — requires
         ext_missing_counts from a user_missing=True read
      4. Type mismatch: form says integer/decimal but Stata has string
      5. Calculate risk: calculate field with unexplained negative values

    Returns var_dict_df with sentinel columns added.
    """
    t0 = time.perf_counter()
    if ext_missing_counts is None:
        ext_missing_counts = {}

    # Initialize sentinel columns
    var_dict_df['sentinel_raw_int'] = 0
    var_dict_df['sentinel_raw_detail'] = None
    var_dict_df['sentinel_string'] = 0
    var_dict_df['sentinel_string_detail'] = None
    var_dict_df['sentinel_ext_missing'] = 0
    var_dict_df['sentinel_ext_missing_detail'] = None
    var_dict_df['sentinel_type_mismatch'] = False
    var_dict_df['sentinel_calculate_risk'] = False

    # Build index: variable_name -> row position in var_dict_df
    var_to_idx = {v: i for i, v in enumerate(var_dict_df['variable_name'])}

    # --- State 1: vectorized numeric sentinel scan ---
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and c in var_to_idx]
    if numeric_cols:
        sentinel_counts = df[numeric_cols].isin(SENTINEL_CODES_LIST).sum()
        for col, cnt in sentinel_counts[sentinel_counts > 0].items():
            idx = var_to_idx[col]
            var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_raw_int')] = int(cnt)
            detail = df[col][df[col].isin(SENTINEL_CODES_LIST)].value_counts()
            var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_raw_detail')] = json.dumps(
                {str(int(k)): int(v) for k, v in detail.items()}
            )
    t1 = time.perf_counter()

    # --- State 3: vectorized string sentinel scan ---
    string_cols = [c for c in df.columns
                   if (pd.api.types.is_string_dtype(df[c]) or pd.api.types.is_object_dtype(df[c]))
                   and c in var_to_idx]
    if string_cols:
        # Process in chunks to limit memory (20k string cols * 10k rows is large)
        chunk_size = 500
        for start in range(0, len(string_cols), chunk_size):
            chunk_cols = string_cols[start:start + chunk_size]
            chunk_df = df[chunk_cols].fillna('').astype(str)
            sentinel_mask = chunk_df.isin(SENTINEL_STRINGS_SET)
            counts = sentinel_mask.sum()
            for col, cnt in counts[counts > 0].items():
                idx = var_to_idx[col]
                var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_string')] = int(cnt)
                vals = chunk_df[col][sentinel_mask[col]].value_counts()
                var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_string_detail')] = json.dumps(
                    {str(k): int(v) for k, v in vals.items()}
                )
    t2 = time.perf_counter()

    # --- State 3: extended missings (.d, .r, etc. from HFC recoding) ---
    if ext_missing_counts:
        for col, counts in ext_missing_counts.items():
            if col in var_to_idx:
                idx = var_to_idx[col]
                total = sum(counts.values())
                var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_ext_missing')] = total
                var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_ext_missing_detail')] = json.dumps(counts)
    t2b = time.perf_counter()

    # --- Type mismatch: form says numeric but Stata has string ---
    for idx, row in var_dict_df.iterrows():
        qtype = row.get('question_type')
        stype = row.get('stata_type', '')
        if qtype in ('integer', 'decimal') and ('str' in str(stype) or 'object' in str(stype)):
            var_dict_df.at[idx, 'sentinel_type_mismatch'] = True

    # --- State 4: calculate fields with unexplained negatives ---
    calc_vars = var_dict_df[var_dict_df['question_type'] == 'calculate']['variable_name'].tolist()
    calc_numeric = [c for c in calc_vars if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
    if calc_numeric:
        for col in calc_numeric:
            neg_count = int((df[col] < 0).sum())
            if neg_count > 0:
                idx = var_to_idx[col]
                var_dict_df.iat[idx, var_dict_df.columns.get_loc('sentinel_calculate_risk')] = True
    t3 = time.perf_counter()

    # Summary
    n_raw = int((var_dict_df['sentinel_raw_int'] > 0).sum())
    n_str = int((var_dict_df['sentinel_string'] > 0).sum())
    n_ext = int((var_dict_df['sentinel_ext_missing'] > 0).sum())
    n_mismatch = int(var_dict_df['sentinel_type_mismatch'].sum())
    n_calc = int(var_dict_df['sentinel_calculate_risk'].sum())
    print(f"  Sentinel scan ({t3-t0:.1f}s): "
          f"{n_raw} raw int, {n_str} string, {n_ext} ext missing, "
          f"{n_mismatch} type mismatch, {n_calc} calculate risk")
    print(f"    Timing: numeric {t1-t0:.1f}s, string {t2-t1:.1f}s, "
          f"ext {t2b-t2:.1f}s, calc {t3-t2b:.1f}s")

    return var_dict_df


def _build_question_index(questions: List[Dict]) -> Dict[str, Dict]:
    """Build a {variable_name: question} dict for O(1) lookups."""
    return {q['variable_name']: q for q in questions if 'variable_name' in q}


def find_question_for_variable(var_name: str, questions: List[Dict],
                               _index: Dict[str, Dict] = None) -> Optional[Dict]:
    """
    Find the source question for a given variable.

    Handles:
    - Direct match
    - Repeat group iterations (var_1, var_2, etc.)
    - Select_multiple choices (var_1, var_2, var_97, etc.)
    - Double-suffix (var_1_2 = repeat 1, choice 2)

    When _index is provided (a {variable_name: question} dict), all lookups
    are O(1) instead of O(N) linear scans.  Build it once with
    _build_question_index() and pass to all calls.
    """
    if _index is None:
        _index = _build_question_index(questions)

    # First try exact match
    if var_name in _index:
        return _index[var_name]

    # Try matching base name (remove numeric suffix with underscore: var_1, var_2)
    match = re.match(r'(.+?)_(\d+)$', var_name)
    if match:
        base_name = match.group(1)
        if base_name in _index:
            return _index[base_name]

    # Try matching base name (digit appended directly without underscore: var_r1, var_r2)
    # Handles SurveyCTO repeat variables whose names end in a non-digit character,
    # e.g. form variable "f_hr_fn_r" -> Stata columns "f_hr_fn_r1", "f_hr_fn_r2"
    match_direct = re.match(r'^(.+?)(\d+)$', var_name)
    if match_direct:
        base_name = match_direct.group(1)
        if base_name in _index:
            return _index[base_name]

    # Try even longer base names (for nested structures)
    parts = var_name.split('_')
    for i in range(len(parts) - 1, 0, -1):
        potential_base = '_'.join(parts[:i])
        if potential_base in _index:
            return _index[potential_base]

    return None


def get_choice_label(question: Dict, choice_code: str) -> Optional[str]:
    """Get the label for a specific choice code."""
    if not question.get('choices'):
        return None
    for choice in question['choices']:
        if str(choice.get('value', '')) == str(choice_code):
            return choice.get('label')
    return None


def adjust_variable_refs(logic_str: str, repeat_group: str, iteration: int,
                         questions: List[Dict], _index: Dict[str, Dict] = None) -> str:
    """Adjust variable references in skip logic for repeat iteration."""
    if not logic_str:
        return logic_str

    var_refs = re.findall(r'\$\{([^}]+)\}', logic_str)
    result = logic_str
    for var_ref in var_refs:
        question = find_question_for_variable(var_ref, questions, _index=_index)
        if question:
            var_group_path = '/'.join(question.get('group_path', []))
            if repeat_group in var_group_path:
                result = result.replace(f'${{{var_ref}}}', f'{var_ref}_{iteration}')
            else:
                result = result.replace(f'${{{var_ref}}}', var_ref)
        else:
            result = result.replace(f'${{{var_ref}}}', var_ref)
    return result


def replace_index_function(logic_str: str, iteration: int) -> str:
    """Replace index() function calls with actual iteration number."""
    if not logic_str:
        return logic_str
    return re.sub(r'index\(\)', str(iteration), logic_str)


def adjust_skip_logic_for_repeats(metadata: Dict, var_name: str, questions: List[Dict],
                                  _index: Dict[str, Dict] = None) -> Dict:
    """Generate both template and iteration-specific skip logic for repeat variables."""
    if not metadata['is_repeat']:
        return metadata

    iteration = metadata['repeat_iteration']
    group_path = metadata['group_path']

    metadata['skip_logic_template'] = metadata['stata_skip_logic']
    metadata['group_relevances_template'] = metadata['group_relevances']

    if metadata['stata_skip_logic']:
        adjusted_logic = adjust_variable_refs(
            metadata['stata_skip_logic'], group_path, iteration, questions, _index=_index
        )
        adjusted_logic = replace_index_function(adjusted_logic, iteration)
        metadata['skip_logic_iteration_specific'] = adjusted_logic

    if metadata['group_relevances']:
        adjusted_relevances = []
        for relevance in metadata['group_relevances']:
            adjusted = adjust_variable_refs(relevance, group_path, iteration, questions, _index=_index)
            adjusted = replace_index_function(adjusted, iteration)
            adjusted = adjusted.replace('${', '').replace('}', '').replace('=', '==')
            adjusted_relevances.append(adjusted)
        metadata['group_relevances_iteration_specific'] = adjusted_relevances

    return metadata


def get_special_code_meaning(choice_code: str) -> Optional[str]:
    """Get meaning for special missing value codes."""
    special_codes = {
        '-99': 'Refused to answer',
        '-98': "Don't know",
        '-97': 'Other (specify)'
    }
    return special_codes.get(str(choice_code))


def determine_variable_source(var_name: str, questions: List[Dict],
                              _index: Dict[str, Dict] = None) -> Dict:
    """Determine the source and metadata for a variable."""
    result = {
        'variable_name': var_name,
        'source_question': None,
        'original_variable_name': None,
        'question_text': None,
        'question_type': None,
        'choice_list': None,
        'choices': None,
        'stata_skip_logic': None,
        'group_relevances': None,
        'is_repeat': False,
        'repeat_iteration': None,
        'is_select_multiple': False,
        'choice_code': None,
        'choice_label': None,
        'group_path': None,
        'skip_logic_template': None,
        'skip_logic_iteration_specific': None,
        'group_relevances_template': None,
        'group_relevances_iteration_specific': None,
        'special_code_meaning': None,
        'disabled': False,
        'form_order': None,
        'choice_index': None,
    }

    question = find_question_for_variable(var_name, questions, _index=_index)
    if not question:
        return result

    result['source_question'] = question
    result['original_variable_name'] = question['variable_name']
    result['question_text'] = question.get('question_text', '')
    result['question_type'] = question.get('type', '')
    result['choice_list'] = question.get('choice_list', '')
    result['choices'] = question.get('choices', None)
    result['stata_skip_logic'] = question.get('stata_skip_logic', '')
    result['group_relevances'] = question.get('group_relevances', [])
    result['group_path'] = '/'.join(question.get('group_path', []))
    result['disabled'] = question.get('disabled', False)
    result['form_order'] = question.get('question_order')

    base_name = question['variable_name']
    if var_name != base_name:
        double_match = re.match(rf'{re.escape(base_name)}_(\d+)_(\d+)$', var_name)
        if double_match:
            choice_num = double_match.group(1)
            repeat_num = double_match.group(2)
            result['is_repeat'] = True
            result['repeat_iteration'] = int(repeat_num)
            result['is_select_multiple'] = True
            result['choice_code'] = choice_num
            result['choice_label'] = get_choice_label(question, choice_num)
            result['special_code_meaning'] = get_special_code_meaning(choice_num)
            # choice_index: position of this choice in the choice list (0-based)
            choice_values = [str(c.get('value', '')) for c in (question.get('choices') or [])]
            result['choice_index'] = choice_values.index(choice_num) if choice_num in choice_values else None
        else:
            match = re.match(rf'{re.escape(base_name)}_(\d+)$', var_name)
            # Fallback: digit appended directly without underscore (e.g. base "f_hr_fn_r" → "f_hr_fn_r1")
            match_direct = re.match(rf'{re.escape(base_name)}(\d+)$', var_name) if not match else None
            active_match = match or match_direct
            if active_match:
                suffix_num = active_match.group(1)
                if question['type'] == 'select_multiple':
                    choice_values = [str(c.get('value', '')) for c in (question.get('choices') or [])]
                    if str(suffix_num) in choice_values:
                        result['is_select_multiple'] = True
                        result['choice_code'] = suffix_num
                        result['choice_label'] = get_choice_label(question, suffix_num)
                        result['special_code_meaning'] = get_special_code_meaning(suffix_num)
                        result['choice_index'] = choice_values.index(suffix_num) if suffix_num in choice_values else None
                    else:
                        result['is_repeat'] = True
                        result['repeat_iteration'] = int(suffix_num)
                else:
                    result['is_repeat'] = True
                    result['repeat_iteration'] = int(suffix_num)

    if result['is_repeat']:
        result = adjust_skip_logic_for_repeats(result, var_name, questions, _index=_index)

    return result


def enrich_count_variables(var_dict_df: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """Enrich _count variables with repeat_metadata."""
    print("\nEnriching _count variables with repeat metadata...")
    repeat_count_mask = var_dict_df['question_type'] == 'repeat_count'
    n_matched = repeat_count_mask.sum()
    print(f"  Found {n_matched} repeat_count variables matched from JSON")

    for idx in var_dict_df[repeat_count_mask].index:
        var_name = var_dict_df.loc[idx, 'variable_name']
        base_name = var_name[:-6]  # Strip '_count'
        related = [col for col in df.columns
                   if col.startswith(base_name + '_')
                   and col != var_name
                   and re.search(r'_(\d+)$', col)]
        group_path = var_dict_df.loc[idx, 'group_path']
        repeat_group_name = group_path.split('/')[-1] if pd.notna(group_path) and group_path else base_name
        var_dict_df.loc[idx, 'is_synthetic'] = True
        var_dict_df.loc[idx, 'repeat_metadata'] = json.dumps({
            'repeat_group_base': base_name,
            'repeat_group_name': repeat_group_name,
            'num_related_variables': len(related)
        })
        if related:
            print(f"    {var_name}: {len(related)} related variables in group '{repeat_group_name}'")

    return var_dict_df


def create_variable_dictionary(
    df: pd.DataFrame,
    questions: List[Dict],
    dataset_name: str,
    ext_missing_counts: Optional[Dict[str, Dict[str, int]]] = None,
    minmax: Optional[Dict[str, Dict]] = None,
    meta=None,
) -> pd.DataFrame:
    """Create comprehensive variable dictionary."""
    print(f"\nCreating {dataset_name} variable dictionary...")
    records = []
    if minmax is None:
        minmax = {}

    # Build question index once — O(1) lookups instead of O(N) linear scans
    q_index = _build_question_index(questions)

    # Use readstat variable types when available (preserves Stata types like
    # "double", "str244", "long") — falls back to pandas dtype strings when meta
    # is absent (pd.read_stata path).
    readstat_types = meta.readstat_variable_types if meta else None

    all_non_null = df.notna().sum()

    for i, var_name in enumerate(df.columns, 1):
        if i % 100 == 0:
            print(f"  Processing variable {i}/{len(df.columns)}...")

        var_type = (
            readstat_types[var_name]
            if readstat_types and var_name in readstat_types
            else str(df[var_name].dtype)
        )
        non_null_count = int(all_non_null[var_name])
        metadata = determine_variable_source(var_name, questions, _index=q_index)

        # Sentinel fields added in batch after dictionary is built (batch_sentinel_scan)

        # Stats from parquet metadata (no data scan)
        mm = minmax.get(var_name)

        record = {
            'variable_name': var_name,
            'variable_order': i,
            'stata_type': var_type,
            'non_null_count': non_null_count,
            'missing_rate': mm.get('missing_rate') if mm else None,
            'is_all_missing': mm.get('is_all_missing', False) if mm else False,
            'data_min': mm.get('min') if mm else None,
            'data_max': mm.get('max') if mm else None,
            'original_variable_name': metadata['original_variable_name'],
            'question_text': metadata['question_text'],
            'question_type': metadata['question_type'],
            'choice_list': metadata['choice_list'],
            'choices': json.dumps(metadata['choices']) if metadata['choices'] else None,
            'stata_skip_logic': metadata['stata_skip_logic'],
            'group_relevances': json.dumps(metadata['group_relevances']) if metadata['group_relevances'] else None,
            'group_path': metadata['group_path'],
            'is_repeat': metadata['is_repeat'],
            'repeat_iteration': metadata['repeat_iteration'],
            'is_select_multiple': metadata['is_select_multiple'],
            'choice_code': metadata['choice_code'],
            'choice_label': metadata['choice_label'],
            'skip_logic_template': metadata['skip_logic_template'],
            'skip_logic_iteration_specific': metadata['skip_logic_iteration_specific'],
            'group_relevances_template': json.dumps(metadata['group_relevances_template']) if metadata['group_relevances_template'] else None,
            'group_relevances_iteration_specific': json.dumps(metadata['group_relevances_iteration_specific']) if metadata['group_relevances_iteration_specific'] else None,
            'special_code_meaning': metadata['special_code_meaning'],
            'disabled': metadata['disabled'],
            'is_synthetic': False,
            'repeat_metadata': None,
            'form_order': metadata['form_order'],
            'choice_index': metadata['choice_index'],
        }
        records.append(record)

    print(f"  Processed {len(records)} variables")
    var_dict_df = pd.DataFrame(records)
    var_dict_df = enrich_count_variables(var_dict_df, df)

    # Batch sentinel scan (vectorized across all columns)
    var_dict_df = batch_sentinel_scan(df, var_dict_df, ext_missing_counts)

    # Sort by form position.
    #
    # For non-repeat variables: (form_order, 0, 0, choice_index)
    # For repeat variables:     (repeat_group_anchor, iteration, form_order, choice_index)
    #
    # Using the repeat group anchor (form_order of the *_count variable) as the primary key
    # groups all iterations of a repeat together AND within each iteration orders questions
    # in form sequence — e.g. crafft1_1, crafft2_1, ..., crafft6_1, crafft1_2, crafft2_2, ...
    # rather than crafft1_1, crafft1_2, ..., crafft2_1, crafft2_2, ...
    #
    # Variables with no form_order (unmatched system vars) go last.
    has_form_order = var_dict_df['form_order'].notna()
    max_order = var_dict_df['form_order'].max() if has_form_order.any() else 0

    # Build lookup: repeat_group_name -> form_order of its _count variable
    repeat_anchor = {}
    count_rows = var_dict_df[var_dict_df['question_type'] == 'repeat_count']
    for _, row in count_rows.iterrows():
        if pd.notna(row['form_order']):
            group_name = row['variable_name'][:-6]  # strip _count
            repeat_anchor[group_name] = row['form_order']

    def _repeat_group_anchor(group_path_str):
        """Return form_order of the innermost repeat group in group_path, or None."""
        if not group_path_str or not isinstance(group_path_str, str):
            return None
        for part in reversed(group_path_str.split('/')):
            if part in repeat_anchor:
                return repeat_anchor[part]
        return None

    # Build sort keys
    sort_group = []
    sort_iter  = []
    sort_within = []
    sort_choice = []

    for _, row in var_dict_df.iterrows():
        is_rep = bool(row['is_repeat']) and pd.notna(row['repeat_iteration'])
        fo = row['form_order'] if pd.notna(row['form_order']) else max_order + 1

        if is_rep:
            anchor = _repeat_group_anchor(row['group_path'])
            sort_group.append(anchor if anchor is not None else fo)
            sort_iter.append(float(row['repeat_iteration']))
            sort_within.append(fo)
        else:
            sort_group.append(fo)
            sort_iter.append(0.0)
            sort_within.append(0.0)

        sort_choice.append(float(row['choice_index']) if pd.notna(row['choice_index']) else 0.0)

    var_dict_df['_sort_group']  = sort_group
    var_dict_df['_sort_iter']   = sort_iter
    var_dict_df['_sort_within'] = sort_within
    var_dict_df['_sort_choice'] = sort_choice

    var_dict_df = var_dict_df.sort_values(
        ['_sort_group', '_sort_iter', '_sort_within', '_sort_choice'],
        kind='stable'
    ).drop(columns=['_sort_group', '_sort_iter', '_sort_within', '_sort_choice'])

    # Renumber variable_order to reflect sorted position
    var_dict_df['variable_order'] = range(1, len(var_dict_df) + 1)
    var_dict_df = var_dict_df.reset_index(drop=True)

    return var_dict_df


def save_ord_dta(var_dict: pd.DataFrame, df: pd.DataFrame, cfg: Dict, meta=None,
                 parquet_path: Optional[Path] = None) -> Optional[Path]:
    """Save a column-reordered *_ord.dta alongside the original dataset."""
    if not _PYREADSTAT_AVAILABLE:
        print("  [SKIP] pyreadstat not available -- skipping _ord.dta generation")
        return None

    if cfg.get('skip_ord_dta'):
        print("  [SKIP] skip_ord_dta=True in config -- skipping _ord.dta generation")
        return None

    data_path = Path(cfg.get('data', ''))
    if not data_path.name:
        print("  [SKIP] no 'data' key in config -- skipping _ord.dta generation")
        return None
    ord_path = data_path.parent / (data_path.stem + '_ord.dta')

    ordered_cols = [v for v in var_dict['variable_name'] if v in df.columns and v[:1].isalpha()]

    # Read selected columns from parquet (columnar, skips unneeded cols) or
    # fall back to subsetting the in-memory DataFrame.
    if parquet_path and Path(parquet_path).exists():
        df_ord = pd.read_parquet(str(parquet_path), columns=ordered_cols)
    else:
        df_ord = df[ordered_cols].copy()

    # Coerce object columns that actually contain numerics (pyreadstat read artifact
    # for sparse repeat-group columns -- stored as object with int/float values).
    for col in df_ord.select_dtypes(include=['object', pd.StringDtype()]).columns:
        try:
            df_ord[col] = pd.to_numeric(df_ord[col])
        except (ValueError, TypeError):
            pass  # leave as string column

    # Collect pyreadstat metadata if available
    # column_labels: list of strings in column order (pyreadstat write_dta format)
    col_labels_list = None
    val_labels = {}
    if meta is not None:
        if hasattr(meta, 'column_names_to_labels') and meta.column_names_to_labels:
            name_to_label = meta.column_names_to_labels
            col_labels_list = [str(name_to_label.get(c, '') or '') for c in ordered_cols]
        if hasattr(meta, 'variable_value_labels') and meta.variable_value_labels:
            val_labels = {
                k: v for k, v in meta.variable_value_labels.items()
                if k in ordered_cols
            }

    pyreadstat.write_dta(
        df_ord, str(ord_path),
        column_labels=col_labels_list,
        variable_value_labels=val_labels if val_labels else None,
        version=14,  # release 118 -- matches original files; readable by Stata SE/MP/IC 14+
    )
    print(f"  [OK] Saved form-ordered dataset: {ord_path.name}")
    return ord_path


def export_dictionary(var_dict: pd.DataFrame, df: pd.DataFrame, cfg: Dict, dataset_name: str, meta=None,
                      parquet_path: Optional[Path] = None):
    """Export variable dictionary to JSON."""
    print(f"\n{dataset_name.title()} Variable Dictionary Summary:")
    print(f"  Total variables: {len(var_dict)}")
    print(f"  Matched to questions: {var_dict['original_variable_name'].notna().sum()}")
    print(f"  Unmatched: {var_dict['original_variable_name'].isna().sum()}")
    print(f"  From repeat groups: {var_dict['is_repeat'].sum()}")
    print(f"  Select_multiple choices: {var_dict['is_select_multiple'].sum()}")
    print(f"  Synthetic (_count) variables: {var_dict['is_synthetic'].sum()}")
    print(f"  Repeat variables with iteration-specific skip logic: {var_dict['skip_logic_iteration_specific'].notna().sum()}")

    # Missing-rate diagnostics from parquet metadata
    if 'missing_rate' in var_dict.columns and var_dict['missing_rate'].notna().any():
        all_missing = var_dict[var_dict['is_all_missing'] == True]
        sparse = var_dict[
            var_dict['missing_rate'].notna() & (var_dict['missing_rate'] >= 0.95)
            & ~var_dict['is_all_missing'].fillna(False)
        ]
        print(f"  All-missing variables (0 non-null): {len(all_missing)}")
        if 0 < len(all_missing) <= 20:
            for vn in all_missing['variable_name']:
                print(f"    {vn}")
        print(f"  Sparse variables (>=95% missing, not all-missing): {len(sparse)}")

    if var_dict['question_type'].notna().any():
        print("\nQuestion types:")
        for qtype, count in var_dict['question_type'].value_counts().items():
            print(f"    {qtype}: {count}")

    # Save form-ordered dataset
    save_ord_dta(var_dict, df, cfg, meta, parquet_path=parquet_path)

    print(f"\nExporting to JSON: {cfg['output_json']}")
    Path(cfg['output_json']).parent.mkdir(parents=True, exist_ok=True)

    variables_dict = {}
    for _, row in var_dict.iterrows():
        var_name = row['variable_name']
        all_choices = json.loads(row['choices']) if pd.notna(row['choices']) else None
        group_relevances = json.loads(row['group_relevances']) if pd.notna(row['group_relevances']) else None
        group_relevances_template = json.loads(row['group_relevances_template']) if pd.notna(row['group_relevances_template']) else None
        group_relevances_iteration_specific = json.loads(row['group_relevances_iteration_specific']) if pd.notna(row['group_relevances_iteration_specific']) else None
        repeat_metadata = json.loads(row['repeat_metadata']) if pd.notna(row['repeat_metadata']) else None

        if row['is_select_multiple'] and all_choices and pd.notna(row['choice_code']):
            choice_code = str(row['choice_code'])
            specific_choice = None
            for choice in all_choices:
                if str(choice.get('value', '')) == choice_code:
                    specific_choice = choice
                    break
            choices = specific_choice
            if pd.notna(row['special_code_meaning']) and isinstance(choices, dict):
                choices['special_meaning'] = row['special_code_meaning']
        else:
            choices = all_choices

        var_metadata = {
            'variable_order': int(row['variable_order']),
            'non_null_count': int(row['non_null_count']),
            'stata': {'type': row['stata_type']},
            'survey': {
                'original_variable_name': row['original_variable_name'] if pd.notna(row['original_variable_name']) else None,
                'question_text': row['question_text'] if pd.notna(row['question_text']) else None,
                'type': row['question_type'] if pd.notna(row['question_type']) else None,
                'disabled': bool(row['disabled']) if pd.notna(row['disabled']) else False,
                'choice_list': row['choice_list'] if pd.notna(row['choice_list']) else None,
                'choices': choices,
                'stata_skip_logic': row['stata_skip_logic'] if pd.notna(row['stata_skip_logic']) else None,
                'group_relevances': group_relevances,
                'group_path': row['group_path'] if pd.notna(row['group_path']) else None
            }
        }

        if row['is_repeat'] and pd.notna(row['repeat_iteration']):
            var_metadata['repeat_iteration'] = int(row['repeat_iteration'])
        if pd.notna(row['skip_logic_template']):
            var_metadata['survey']['skip_logic_template'] = row['skip_logic_template']
        if pd.notna(row['skip_logic_iteration_specific']):
            var_metadata['survey']['skip_logic_iteration_specific'] = row['skip_logic_iteration_specific']
        if group_relevances_template:
            var_metadata['survey']['group_relevances_template'] = group_relevances_template
        if group_relevances_iteration_specific:
            var_metadata['survey']['group_relevances_iteration_specific'] = group_relevances_iteration_specific
        if row['is_synthetic']:
            var_metadata['is_synthetic'] = True
            if repeat_metadata:
                var_metadata['repeat_metadata'] = repeat_metadata

        # Sentinel counts — only include keys with non-zero values
        sentinels = {}
        if int(row.get('sentinel_raw_int', 0)) > 0:
            sentinels['raw_int'] = int(row['sentinel_raw_int'])
            if pd.notna(row.get('sentinel_raw_detail')):
                sentinels['raw_int_detail'] = json.loads(row['sentinel_raw_detail'])
        if int(row.get('sentinel_string', 0)) > 0:
            sentinels['string'] = int(row['sentinel_string'])
            if pd.notna(row.get('sentinel_string_detail')):
                sentinels['string_detail'] = json.loads(row['sentinel_string_detail'])
        if int(row.get('sentinel_ext_missing', 0)) > 0:
            sentinels['ext_missing'] = int(row['sentinel_ext_missing'])
            if pd.notna(row.get('sentinel_ext_missing_detail')):
                sentinels['ext_missing_detail'] = json.loads(row['sentinel_ext_missing_detail'])
        if row.get('sentinel_type_mismatch', False):
            sentinels['type_mismatch'] = True
        if row.get('sentinel_calculate_risk', False):
            sentinels['calculate_risk'] = True
        if sentinels:
            var_metadata['sentinels'] = sentinels

        # Missing rate from parquet metadata (all columns)
        missing_rate = row.get('missing_rate')
        if missing_rate is not None and not (isinstance(missing_rate, float) and np.isnan(missing_rate)):
            var_metadata['missing_rate'] = round(float(missing_rate), 4)
        if row.get('is_all_missing'):
            var_metadata['is_all_missing'] = True

        # Data range from parquet metadata — only for types where range is
        # informative (not select_one/select_multiple whose range is just
        # choice codes or 0/1 binary indicators)
        qtype = row.get('question_type')
        d_min = row.get('data_min')
        d_max = row.get('data_max')
        if (d_min is not None and d_max is not None
                and qtype in _MINMAX_QUESTION_TYPES):
            try:
                # Guard against NaN (float columns with all-NaN produce NaN stats)
                if not (isinstance(d_min, float) and np.isnan(d_min)):
                    var_metadata['data_min'] = d_min
                    var_metadata['data_max'] = d_max
            except (TypeError, ValueError):
                var_metadata['data_min'] = d_min
                var_metadata['data_max'] = d_max

        variables_dict[var_name] = var_metadata

    output_structure = {
        'dataset': {
            'name': dataset_name,
            'n_observations': len(df),
            'n_variables': len(var_dict)
        },
        'summary': {
            'total_variables': len(var_dict),
            'matched_to_questions': int(var_dict['original_variable_name'].notna().sum()),
            'unmatched': int(var_dict['original_variable_name'].isna().sum()),
            'from_repeat_groups': int(var_dict['is_repeat'].sum()),
            'select_multiple_choices': int(var_dict['is_select_multiple'].sum())
        },
        'variables': variables_dict
    }

    with open(cfg['output_json'], 'w', encoding='utf-8') as f:
        json.dump(output_structure, f, indent=2, ensure_ascii=False)


def validate_select_multiple(dict_path, dataset_name: str):
    """Validate select_multiple variable mappings."""
    print(f"\n{'='*70}")
    print(f"Validating {dataset_name.title()} Select Multiple Variables")
    print(f"{'='*70}")

    with open(dict_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    variables = data['variables']
    cfg = DATASETS[dataset_name]
    json_key = 'questions_json' if 'questions_json' in cfg else 'json'
    with open(cfg[json_key], 'r', encoding='utf-8') as f:
        questions = json.load(f)

    questions_dict = {q['variable_name']: q for q in questions}
    sm_vars = {
        var_name: var_info
        for var_name, var_info in variables.items()
        if (var_info.get('survey', {}).get('type') == 'select_multiple' and
            isinstance(var_info.get('survey', {}).get('choices'), dict))
    }

    print(f"\nFound {len(sm_vars)} select_multiple variables")
    if len(sm_vars) == 0:
        print("No select_multiple variables to validate.")
        return True

    mismatches = []
    valid_count = 0

    for var_name, var_info in sm_vars.items():
        survey_info = var_info['survey']
        choice_dict = survey_info.get('choices')
        original_var_name = survey_info.get('original_variable_name')

        if not choice_dict or not isinstance(choice_dict, dict):
            mismatches.append({'variable': var_name, 'issue': 'No choice dict found', 'original_variable': original_var_name})
            continue

        original_question = questions_dict.get(original_var_name)
        if not original_question:
            mismatches.append({'variable': var_name, 'issue': 'Original question not found', 'original_variable': original_var_name})
            continue

        original_choices = original_question.get('choices', [])
        original_choice_values = [str(c.get('value', '')) for c in original_choices]
        choice_code = str(choice_dict.get('value', ''))

        if choice_code not in original_choice_values:
            mismatches.append({
                'variable': var_name,
                'choice_code': choice_code,
                'issue': f'Choice code "{choice_code}" not found in original question',
                'available_values': original_choice_values,
                'original_variable': original_var_name
            })
        else:
            valid_count += 1

    print(f"\nValidation Results:")
    print(f"  Valid mappings: {valid_count}")
    print(f"  Mismatches: {len(mismatches)}")

    if mismatches:
        print(f"\n{'='*70}")
        print("MISMATCHES FOUND:")
        for i, mismatch in enumerate(mismatches[:10], 1):
            print(f"\n{i}. Variable: {mismatch['variable']}")
            print(f"   Original: {mismatch['original_variable']}")
            print(f"   Issue: {mismatch['issue']}")
            if 'available_values' in mismatch:
                print(f"   Available values: {', '.join(mismatch['available_values'])}")
        if len(mismatches) > 10:
            print(f"\n... and {len(mismatches) - 10} more mismatches")
        return False
    else:
        print(f"\n{'='*70}")
        print("All select_multiple variables correctly mapped!")
        return True


# ---------------------------------------------------------------------------
# Variable relationship graph
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"[$][{]([^}]+)[}]")


def build_variable_graph(questions: List[Dict], vardict_json: dict,
                         output_path: Path) -> Optional[Path]:
    """Build a variable relationship graph and write it as JSON.

    Nodes are form-level variables (one per question). Edges encode semantic
    relationships: calculation dependencies, gating, constraints, repeat
    siblings, and shared choice lists. Returns the output path, or None if
    networkx is not available.
    """
    if not _NETWORKX_AVAILABLE:
        print("  [SKIP] networkx not installed -- skipping variable graph")
        return None

    G = nx.MultiDiGraph()
    variables = vardict_json.get("variables", {})

    # -- Build reverse map: form variable name -> list of Stata column names --
    stata_vars_map: Dict[str, list] = {}
    for stata_name, entry in variables.items():
        if not isinstance(entry, dict):
            continue
        form_name = entry.get("survey", {}).get("original_variable_name") or stata_name
        stata_vars_map.setdefault(form_name, []).append(stata_name)

    # -- Build question index --
    q_index = {q["variable_name"]: q for q in questions if "variable_name" in q}

    # -- Helper: compute repeat depth and innermost repeat group from group_path --
    # Repeat groups are identified by having variables with repeat_iteration in vardict.
    # Collect known repeat group names.
    repeat_group_names: set = set()
    for entry in variables.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("repeat_iteration") is not None:
            gp = entry.get("survey", {}).get("group_path") or ""
            parts = [p.strip() for p in gp.replace("/", " ").split() if p.strip()]
            if parts:
                repeat_group_names.add(parts[-1])
        if entry.get("survey", {}).get("type") == "repeat_count":
            base = entry.get("repeat_metadata", {}).get("repeat_group_base", "")
            if base:
                repeat_group_names.add(base)

    def _repeat_info(group_path):
        """Return (repeat_depth, innermost_repeat_group) from a group_path list."""
        if not group_path:
            return 0, None
        parts = group_path if isinstance(group_path, list) else [
            p.strip() for p in group_path.replace("/", " ").split() if p.strip()
        ]
        depth = sum(1 for p in parts if p in repeat_group_names)
        innermost = None
        for p in reversed(parts):
            if p in repeat_group_names:
                innermost = p
                break
        return depth, innermost

    # -- Add nodes --
    for q in questions:
        vn = q.get("variable_name")
        if not vn:
            continue
        gp = q.get("group_path", [])
        gp_str = "/".join(gp) if isinstance(gp, list) else (gp or "")
        depth, rg = _repeat_info(gp)
        G.add_node(vn,
                    type=q.get("type", ""),
                    group_path=gp_str,
                    repeat_depth=depth,
                    repeat_group=rg,
                    stata_vars=stata_vars_map.get(vn, [vn]))

    # -- Add directed edges from ${ref} parsing --
    for q in questions:
        vn = q.get("variable_name", "")
        if not vn:
            continue

        # Calculation: input -> calculate var
        for ref in _REF_RE.findall(q.get("calculation", "") or ""):
            if ref in q_index:
                G.add_edge(ref, vn, type="calculates_from")

        # Relevance: gate var -> gated var
        for ref in _REF_RE.findall(q.get("relevance", "") or ""):
            if ref in q_index:
                G.add_edge(ref, vn, type="gated_by")

        # Group relevances: gate var -> group member
        for gr in (q.get("group_relevances") or []):
            if not isinstance(gr, str):
                continue
            for ref in _REF_RE.findall(gr):
                if ref in q_index:
                    G.add_edge(ref, vn, type="group_gated_by")

        # Constraint: constraining var -> constrained var
        for ref in _REF_RE.findall(q.get("constraint", "") or ""):
            if ref in q_index:
                G.add_edge(ref, vn, type="constrained_by")

    # -- Repeat sibling edges (bidirectional) --
    # Group questions by their innermost repeat group
    repeat_members: Dict[str, list] = {}
    for q in questions:
        vn = q.get("variable_name", "")
        gp = q.get("group_path", [])
        _, rg = _repeat_info(gp)
        if rg and vn:
            repeat_members.setdefault(rg, []).append(vn)

    for members in repeat_members.values():
        if len(members) < 2:
            continue
        # Full mesh — repeat groups are small, and star topology would hide
        # siblings from non-hub nodes at depth=1
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                G.add_edge(left, right, type="repeat_sibling")
                G.add_edge(right, left, type="repeat_sibling")

    # -- Shared choice list edges (bidirectional) --
    choice_members: Dict[str, list] = {}
    for q in questions:
        cl = q.get("choice_list")
        vn = q.get("variable_name", "")
        if cl and vn:
            choice_members.setdefault(cl, []).append(vn)

    for members in choice_members.values():
        if len(members) < 2:
            continue
        hub = members[0]
        for member in members[1:]:
            G.add_edge(hub, member, type="shares_choices")
            G.add_edge(member, hub, type="shares_choices")

    # -- Write --
    data = nx.node_link_data(G)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    # Summary
    edge_types = {}
    for _, _, d in G.edges(data=True):
        t = d.get("type", "unknown")
        edge_types[t] = edge_types.get(t, 0) + 1
    summary = ", ".join(f"{t}: {c}" for t, c in sorted(edge_types.items()))
    print(f"  Variable graph: {G.number_of_nodes()} nodes, "
          f"{G.number_of_edges()} edges ({summary})")
    print(f"  Written to: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description='Create variable dictionaries from config.DATASETS'
    )
    parser.add_argument(
        '--survey', metavar='KEY',
        help='Process only the named survey (key from config.DATASETS). Default: all.'
    )
    parser.add_argument('--validate', action='store_true',
                        help='Run validation after creation')
    parser.add_argument('--validate-only', action='store_true',
                        help='Only run validation on existing dictionaries')
    parser.add_argument('--xlsx', action='store_true',
                        help='Export XLSX variable dictionary alongside JSON')
    args = parser.parse_args()

    if not DATASETS:
        print("ERROR: config.DATASETS is empty. Add survey entries to config.py.")
        sys.exit(1)

    if args.validate_only:
        datasets_to_process = []
        validate = True
    elif args.survey:
        if args.survey not in DATASETS:
            print(f"ERROR: '{args.survey}' not found in config.DATASETS. "
                  f"Available keys: {', '.join(DATASETS.keys())}")
            sys.exit(1)
        datasets_to_process = [args.survey]
        validate = args.validate
    else:
        datasets_to_process = list(DATASETS.keys())
        validate = args.validate

    print("=" * 70)
    print("Create Variable Dictionaries")
    print("=" * 70)

    for dataset_name in datasets_to_process:
        print(f"\n{'='*70}")
        print(f"Processing {dataset_name.upper()}")
        print(f"{'='*70}")
        df, questions, cfg, meta, pq_path, ext_missing_counts, minmax = load_data(dataset_name)
        var_dict = create_variable_dictionary(df, questions, dataset_name, ext_missing_counts, minmax, meta=meta)
        export_dictionary(var_dict, df, cfg, dataset_name, meta, parquet_path=pq_path)

        # Build variable relationship graph (reuse in-memory var_dict)
        vardict_path = Path(cfg['output_json'])
        graph_path = vardict_path.with_name(
            vardict_path.stem.replace('_variable_dictionary', '_variable_graph') + '.json'
        )
        build_variable_graph(questions, var_dict, graph_path)

        if args.xlsx:
            from generators.xlsx_exporter import XLSXExporter
            xlsx_path = Path(str(cfg['output_json']).replace('.json', '.xlsx'))
            print(f"\nExporting XLSX: {xlsx_path}")
            XLSXExporter().export(cfg['output_json'], xlsx_path)

    if validate:
        print(f"\n{'='*70}")
        print("VALIDATION")
        print(f"{'='*70}")
        all_valid = True
        for dataset_name in (datasets_to_process or list(DATASETS.keys())):
            dict_path = DATASETS[dataset_name]['output_json']
            if Path(dict_path).exists():
                valid = validate_select_multiple(dict_path, dataset_name)
                all_valid = all_valid and valid

        print(f"\n{'='*70}")
        if all_valid:
            print("All validations passed!")
        else:
            print("WARNING: Some validations failed - review mismatches above")
        print(f"{'='*70}")

    print("\n" + "=" * 70)
    print("COMPLETE!")
    print("=" * 70)


if __name__ == "__main__":
    main()
