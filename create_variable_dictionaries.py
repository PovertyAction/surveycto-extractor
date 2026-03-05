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
import json
import re
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pyreadstat
    _PYREADSTAT_AVAILABLE = True
except ImportError:
    _PYREADSTAT_AVAILABLE = False

# Pull DATASETS from this project's config.py (same directory as this script)
sys.path.insert(0, str(Path(__file__).parent))
from config import DATASETS


def load_data(dataset_name: str):
    """Load Stata dataset and JSON metadata."""
    cfg = DATASETS[dataset_name]
    # Support both 'json' key (old style) and 'questions_json' key (template style)
    json_key = 'questions_json' if 'questions_json' in cfg else 'json'

    print(f"Loading {dataset_name} data...")
    if _PYREADSTAT_AVAILABLE:
        df, meta = pyreadstat.read_dta(str(cfg['data']))
    else:
        df = pd.read_stata(cfg['data'])
        meta = None
    print(f"  Loaded {len(df)} observations, {len(df.columns)} variables")

    with open(cfg[json_key], 'r', encoding='utf-8') as f:
        questions = json.load(f)
    print(f"  Loaded {len(questions)} questions from JSON")

    return df, questions, cfg, meta


def find_question_for_variable(var_name: str, questions: List[Dict]) -> Optional[Dict]:
    """
    Find the source question for a given variable.

    Handles:
    - Direct match
    - Repeat group iterations (var_1, var_2, etc.)
    - Select_multiple choices (var_1, var_2, var_97, etc.)
    - Double-suffix (var_1_2 = repeat 1, choice 2)
    """
    # First try exact match
    for q in questions:
        if q['variable_name'] == var_name:
            return q

    # Try matching base name (remove numeric suffix with underscore: var_1, var_2)
    match = re.match(r'(.+?)_(\d+)$', var_name)
    if match:
        base_name = match.group(1)
        for q in questions:
            if q['variable_name'] == base_name:
                return q

    # Try matching base name (digit appended directly without underscore: var_r1, var_r2)
    # Handles SurveyCTO repeat variables whose names end in a non-digit character,
    # e.g. form variable "f_hr_fn_r" → Stata columns "f_hr_fn_r1", "f_hr_fn_r2"
    match_direct = re.match(r'^(.+?)(\d+)$', var_name)
    if match_direct:
        base_name = match_direct.group(1)
        for q in questions:
            if q['variable_name'] == base_name:
                return q

    # Try even longer base names (for nested structures)
    parts = var_name.split('_')
    for i in range(len(parts) - 1, 0, -1):
        potential_base = '_'.join(parts[:i])
        for q in questions:
            if q['variable_name'] == potential_base:
                return q

    return None


def get_choice_label(question: Dict, choice_code: str) -> Optional[str]:
    """Get the label for a specific choice code."""
    if not question.get('choices'):
        return None
    for choice in question['choices']:
        if str(choice.get('value', '')) == str(choice_code):
            return choice.get('label')
    return None


def adjust_variable_refs(logic_str: str, repeat_group: str, iteration: int, questions: List[Dict]) -> str:
    """Adjust variable references in skip logic for repeat iteration."""
    if not logic_str:
        return logic_str

    var_refs = re.findall(r'\$\{([^}]+)\}', logic_str)
    result = logic_str
    for var_ref in var_refs:
        question = find_question_for_variable(var_ref, questions)
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


def adjust_skip_logic_for_repeats(metadata: Dict, var_name: str, questions: List[Dict]) -> Dict:
    """Generate both template and iteration-specific skip logic for repeat variables."""
    if not metadata['is_repeat']:
        return metadata

    iteration = metadata['repeat_iteration']
    group_path = metadata['group_path']

    metadata['skip_logic_template'] = metadata['stata_skip_logic']
    metadata['group_relevances_template'] = metadata['group_relevances']

    if metadata['stata_skip_logic']:
        adjusted_logic = adjust_variable_refs(
            metadata['stata_skip_logic'], group_path, iteration, questions
        )
        adjusted_logic = replace_index_function(adjusted_logic, iteration)
        metadata['skip_logic_iteration_specific'] = adjusted_logic

    if metadata['group_relevances']:
        adjusted_relevances = []
        for relevance in metadata['group_relevances']:
            adjusted = adjust_variable_refs(relevance, group_path, iteration, questions)
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


def determine_variable_source(var_name: str, questions: List[Dict]) -> Dict:
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

    question = find_question_for_variable(var_name, questions)
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
        result = adjust_skip_logic_for_repeats(result, var_name, questions)

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


def create_variable_dictionary(df: pd.DataFrame, questions: List[Dict], dataset_name: str) -> pd.DataFrame:
    """Create comprehensive variable dictionary."""
    print(f"\nCreating {dataset_name} variable dictionary...")
    records = []

    for i, var_name in enumerate(df.columns, 1):
        if i % 100 == 0:
            print(f"  Processing variable {i}/{len(df.columns)}...")

        var_type = str(df[var_name].dtype)
        non_null_count = int(df[var_name].notna().sum())
        metadata = determine_variable_source(var_name, questions)

        record = {
            'variable_name': var_name,
            'variable_order': i,
            'stata_type': var_type,
            'non_null_count': non_null_count,
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


def save_ord_dta(var_dict: pd.DataFrame, df: pd.DataFrame, cfg: Dict, meta=None) -> Optional[Path]:
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
    df_ord = df[ordered_cols].copy()

    # Coerce object columns that actually contain numerics (pyreadstat read artifact
    # for sparse repeat-group columns -- stored as object with int/float values).
    for col in df_ord.select_dtypes(include='object').columns:
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


def export_dictionary(var_dict: pd.DataFrame, df: pd.DataFrame, cfg: Dict, dataset_name: str, meta=None):
    """Export variable dictionary to JSON."""
    print(f"\n{dataset_name.title()} Variable Dictionary Summary:")
    print(f"  Total variables: {len(var_dict)}")
    print(f"  Matched to questions: {var_dict['original_variable_name'].notna().sum()}")
    print(f"  Unmatched: {var_dict['original_variable_name'].isna().sum()}")
    print(f"  From repeat groups: {var_dict['is_repeat'].sum()}")
    print(f"  Select_multiple choices: {var_dict['is_select_multiple'].sum()}")
    print(f"  Synthetic (_count) variables: {var_dict['is_synthetic'].sum()}")
    print(f"  Repeat variables with iteration-specific skip logic: {var_dict['skip_logic_iteration_specific'].notna().sum()}")

    if var_dict['question_type'].notna().any():
        print("\nQuestion types:")
        for qtype, count in var_dict['question_type'].value_counts().items():
            print(f"    {qtype}: {count}")

    # Save form-ordered dataset
    save_ord_dta(var_dict, df, cfg, meta)

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
        df, questions, cfg, meta = load_data(dataset_name)
        var_dict = create_variable_dictionary(df, questions, dataset_name)
        export_dictionary(var_dict, df, cfg, dataset_name, meta)

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
