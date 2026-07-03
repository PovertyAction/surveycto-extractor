"""
Extract JSON with complete question metadata
Phase 2: JSON Master File Generation
"""
import json
import re
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional
from parsers.group_tracker import GroupStack
from parsers.type_parser import parse_question_type
from transformers.logic_converter import LogicConverter
import config


_REF_RX = re.compile(r'[$][{]([^}]+)[}]')


def _parse_refs(*expressions: Optional[str]) -> Optional[List[str]]:
    """Return the deduped list of ${var} tokens across the given expressions."""
    seen = []
    seen_set = set()
    for expr in expressions:
        if not expr:
            continue
        for m in _REF_RX.findall(expr):
            name = m.strip()
            if name and name not in seen_set:
                seen_set.add(name)
                seen.append(name)
    return seen if seen else None


def _is_static_choice_filter(expr: str) -> bool:
    """A choice_filter is static when it has no `${var}` reference.

    Dynamic filters depend on runtime form values and can't be resolved at
    dictionary build time; static filters reference only choices-sheet
    columns and can be evaluated row-by-row against the choices list.
    """
    if not expr:
        return False
    return _REF_RX.search(expr) is None


def _eval_static_choice_filter(
    expr: str, choice_row: Dict[str, str]
) -> Optional[bool]:
    """Evaluate a static choice_filter expression against one choice row.

    Returns True/False if the row passes/fails, or None if the expression
    contains a construct the evaluator doesn't recognise (caller falls
    back to keeping the row in that case so we never drop data we can't
    confidently exclude).

    Supports:
      <col> = <value>          equality (SurveyCTO uses `=`, not `==`)
      <col> != <value>         inequality
      <col> < <value>          numeric comparisons
      <col> <=/>=/> <value>
      selected('<value>', <col>)        col-as-list contains value
      not(<sub>)
      <sub> and <sub>          logical and
      <sub> or <sub>           logical or
      (<sub>)                  grouping

    `<value>` may be:
      - a quoted string literal: '1', "Yes", '-66'
      - a bare integer / negative integer literal
      - the bare token `''` (empty string)
    """
    src = expr.strip()
    try:
        result, rest = _eval_filter_or(src, choice_row)
        if rest.strip():
            return None
        return result
    except _UnhandledFilter:
        return None


class _UnhandledFilter(Exception):
    """Raised when the filter evaluator hits a construct it doesn't recognise."""


def _eval_filter_or(s: str, row: Dict[str, str]) -> tuple:
    left, s = _eval_filter_and(s, row)
    while True:
        s2 = s.lstrip()
        m = re.match(r'\bor\b', s2)
        if not m:
            return left, s
        s = s2[m.end():]
        right, s = _eval_filter_and(s, row)
        left = left or right


def _eval_filter_and(s: str, row: Dict[str, str]) -> tuple:
    left, s = _eval_filter_unary(s, row)
    while True:
        s2 = s.lstrip()
        m = re.match(r'\band\b', s2)
        if not m:
            return left, s
        s = s2[m.end():]
        right, s = _eval_filter_unary(s, row)
        left = left and right


def _eval_filter_unary(s: str, row: Dict[str, str]) -> tuple:
    s = s.lstrip()
    # ``not(...)`` and ``not (...)`` (with a space) are both legal in
    # SurveyCTO. Match either form by checking the keyword first and
    # then skipping any whitespace between ``not`` and the opening paren.
    m = re.match(r"not\s*\(", s)
    if m:
        inner, rest = _eval_filter_or(s[m.end():], row)
        rest = rest.lstrip()
        if not rest.startswith(')'):
            raise _UnhandledFilter("unbalanced not(")
        return (not inner), rest[1:]
    if s.startswith('('):
        inner, rest = _eval_filter_or(s[1:], row)
        rest = rest.lstrip()
        if not rest.startswith(')'):
            raise _UnhandledFilter("unbalanced (")
        return inner, rest[1:]
    # selected('value', col)
    m = re.match(r"selected\s*\(\s*'([^']*)'\s*,\s*([A-Za-z_]\w*)\s*\)", s)
    if not m:
        m = re.match(r'selected\s*\(\s*"([^"]*)"\s*,\s*([A-Za-z_]\w*)\s*\)', s)
    if m:
        needle, col = m.group(1).strip(), m.group(2).strip()
        cell = str(row.get(col, '') or '').strip()
        return (needle in cell.split()), s[m.end():]
    # <col> <op> <value>
    m = re.match(
        r"([A-Za-z_]\w*)\s*(!=|<=|>=|=|<|>)\s*"
        r"(?:'([^']*)'|\"([^\"]*)\"|(-?\d+(?:\.\d+)?)|(''|\"\"))",
        s
    )
    if m:
        col = m.group(1).strip()
        op  = m.group(2)
        if m.group(3) is not None:
            value = m.group(3)
            value_is_string = True
        elif m.group(4) is not None:
            value = m.group(4)
            value_is_string = True
        elif m.group(5) is not None:
            value = m.group(5)
            value_is_string = False
        else:
            value = ''
            value_is_string = True
        cell = str(row.get(col, '') or '').strip()
        # Numeric comparison if both sides parse as numeric.
        try:
            cell_num = float(cell)
            val_num  = float(value)
            cmp_numeric = True
        except (TypeError, ValueError):
            cmp_numeric = False
        if op == '=' and value == '' and value_is_string:
            # filter='' means the cell is empty
            return (cell == ''), s[m.end():]
        if op == '!=' and value == '' and value_is_string:
            return (cell != ''), s[m.end():]
        if cmp_numeric:
            a, b = cell_num, val_num
        else:
            a, b = cell, value
        if op == '=':
            return (a == b), s[m.end():]
        if op == '!=':
            return (a != b), s[m.end():]
        if not cmp_numeric:
            raise _UnhandledFilter(f"non-numeric ordering: {col} {op} {value}")
        if op == '<':  return (a <  b), s[m.end():]
        if op == '<=': return (a <= b), s[m.end():]
        if op == '>':  return (a >  b), s[m.end():]
        if op == '>=': return (a >= b), s[m.end():]
    raise _UnhandledFilter(f"unrecognised token at: {s[:40]!r}")


def _apply_static_choice_filter(
    expr: Optional[str], choices: Optional[List[Dict]]
) -> Optional[List[Dict]]:
    """Return choices narrowed by a static `choice_filter`, or None if the
    filter is dynamic / unparseable / not applicable.
    """
    if not expr or not choices or not _is_static_choice_filter(expr):
        return None
    narrowed: List[Dict] = []
    for row in choices:
        if not isinstance(row, dict):
            narrowed.append(row)
            continue
        ok = _eval_static_choice_filter(expr, row)
        if ok is None:
            # Unparseable — bail out, keep the full list rather than risk
            # dropping rows we shouldn't.
            return None
        if ok:
            narrowed.append(row)
    return narrowed


class JSONExtractor:
    """Extract complete question metadata to JSON"""

    def __init__(self, survey_df: pd.DataFrame, choices_df: pd.DataFrame, output_dir: Path):
        """
        Initialize extractor

        Args:
            survey_df: Survey sheet DataFrame
            choices_df: Choices sheet DataFrame
            output_dir: Output directory for JSON files
        """
        self.survey_df = survey_df
        self.choices_df = choices_df
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.group_stack = GroupStack()
        self.logic_converter = LogicConverter()

        # Build choice lookup dictionary for quick access
        self._build_choice_lookup()
        # Build variable → type and variable → choice codes lookups
        self._build_question_type_lookup()

    def _build_choice_lookup(self) -> None:
        """
        Build dictionary mapping choice list names to their choices.

        Each choice row preserves every non-empty column from the choices
        worksheet beyond `list_name` / `value` / `label`. Extra columns are
        the filter dimensions (e.g. `type`, `region`) referenced by
        `choice_filter` expressions on the survey sheet — keeping them lets
        the variable dictionary show the structural partition of the choice
        universe for filtered selects.
        """
        self.choice_lookup = {}

        if 'list_name' not in self.choices_df.columns:
            return

        # Columns that are universally meaningful and rendered as core fields.
        core_cols = {'list_name', 'value', 'label'}
        # Translation columns (label:Spanish, text_label:Swahili, hint:French,
        # media:image:Hindi, etc.) are GUI / language metadata not data
        # structure. Drop any column whose name has at least one `:` —
        # SurveyCTO uses `colon`-prefixed suffixes for every translated/
        # localized variant. The bare data-structure columns (`list_name`,
        # `value`, `label`, `filter`, custom filter columns) never contain `:`.
        def _is_translation(col: str) -> bool:
            return ':' in col

        # Any column that isn't core, translation, or fully empty is a filter
        # column we want to surface. Skip `Unnamed: N` (pandas's auto-name for
        # blank header cells in the XLSX) — those are not real columns.
        def _is_unnamed(col: str) -> bool:
            return col.startswith('Unnamed:')

        filter_cols = [
            c for c in self.choices_df.columns
            if c not in core_cols
            and not _is_translation(c)
            and not _is_unnamed(c)
            and self.choices_df[c].replace('', pd.NA).notna().any()
        ]

        for list_name in self.choices_df['list_name'].unique():
            if pd.isna(list_name) or list_name == '':
                continue

            list_choices = self.choices_df[self.choices_df['list_name'] == list_name]
            choices = []

            for _, row in list_choices.iterrows():
                # row.get() on pandas Series returns NaN (not the default)
                # when the column exists but the cell is empty — coerce to str
                raw_val = row.get('value', '')
                raw_lbl = row.get('label', '')
                choice = {
                    'value': '' if pd.isna(raw_val) else str(raw_val),
                    'label': '' if pd.isna(raw_lbl) else str(raw_lbl),
                }
                # Preserve filter-column values when non-empty
                for col in filter_cols:
                    v = row.get(col, '')
                    if pd.isna(v) or v == '':
                        continue
                    choice[col] = str(v)
                choices.append(choice)

            self.choice_lookup[list_name] = choices

        # Stash the filter-column set on the instance so downstream code
        # (Tier 4.2 vardict rendering) can identify which non-core fields
        # are filter dimensions vs. unrelated columns.
        self._choice_filter_columns = filter_cols

    def _build_question_type_lookup(self) -> None:
        """
        Pre-pass over the survey sheet to build two dicts used by the
        logic converter:

          self.question_types  — {variable_name: base_type_string}
              e.g. {"food_source": "select_one", "lstock_own_6m": "select_multiple"}

          self.var_choice_codes — {variable_name: ["1", "2", "-66", ...]}
              Required for count-selected() expansion.
        """
        self.question_types: Dict[str, str] = {}
        self.var_choice_codes: Dict[str, list] = {}

        for _, row in self.survey_df.iterrows():
            variable_name = row.get("name", "")
            if not variable_name:
                continue
            raw_type = row.get("type", "")
            base_type, choice_list = parse_question_type(raw_type)
            if base_type:
                self.question_types[variable_name] = base_type
            if choice_list and choice_list in self.choice_lookup:
                codes = [
                    str(c["value"])
                    for c in self.choice_lookup[choice_list]
                    if str(c.get("value", "")).strip()
                ]
                self.var_choice_codes[variable_name] = codes

    def _is_excluded(self, row: pd.Series) -> bool:
        """Check if row should be excluded from question extraction"""
        row_type = row.get("type", "")

        # Exclude structural types
        if row_type in config.EXCLUDED_TYPES:
            return True

        # Exclude system variables
        variable_name = row.get("name", "")
        if variable_name and any(variable_name.startswith(prefix) for prefix in config.SYSTEM_PREFIXES):
            return True

        return False

    def _extract_question(self, row: pd.Series) -> Optional[Dict]:
        """
        Extract single question with complete metadata

        Args:
            row: Survey DataFrame row

        Returns:
            Question dictionary or None if excluded
        """
        # Check if should be excluded
        if self._is_excluded(row):
            return None

        # Parse type
        raw_type = row.get("type", "")
        base_type, choice_list = parse_question_type(raw_type)

        # Get basic fields
        variable_name = row.get("name", "")
        if not variable_name:
            return None

        question_text = row.get("label", "")
        # Fallback for translation-only forms (#36). Multilingual SurveyCTO
        # forms often define question text ONLY under `label:<lang>` columns
        # (e.g. `label:english`, `label:shona`) with no bare `label` column, so
        # `row.get("label")` is empty and question_text would be blank for most
        # questions. Fall back to a translation label, preferring English, then
        # the first available language. Purely additive: forms carrying a bare
        # `label` are unaffected.
        def _blank(v):
            return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""
        if _blank(question_text):
            label_cols = [c for c in row.index if str(c).lower().startswith("label:")]
            ordered = [c for c in label_cols if str(c).lower() in ("label:english", "label:en")] + \
                      [c for c in label_cols if str(c).lower() not in ("label:english", "label:en")]
            for c in ordered:
                v = row.get(c, "")
                if not _blank(v):
                    question_text = v
                    break
        if isinstance(question_text, float) and pd.isna(question_text):
            question_text = ""
        constraint = row.get("constraint", None) or None
        relevance = row.get("relevance", None) or None
        required = row.get("required", "") == "yes"
        calculation = row.get("calculation", None) or None
        # choice_filter is a data-structure column: it constrains which choice
        # codes can land in the dataset for this variable. Captured for
        # downstream consumers (vardict choice-list rendering uses it to
        # surface the filter dimensions from the choices sheet).
        choice_filter = row.get("choice_filter", None) or None
        # appearance is a presentation column but carries the SurveyCTO
        # ``search('CSV', 'matches', col, val, ...)`` directive that
        # dynamically populates a select's choice list from a media-bundle
        # CSV. The synthetic generator uses this to expand placeholder
        # choice lists (e.g. peer rosters, enumerator lists) at run time
        # against the actual pulldata table.
        appearance = row.get("appearance", None) or None

        # For calculate type, use calculation expression as label fallback
        if base_type == "calculate" and not question_text and calculation:
            question_text = calculation

        # Get inherited group information
        group_path = self.group_stack.get_current_path()
        group_relevances = self.group_stack.get_inherited_relevance()

        # Convert to Stata logic
        stata_skip_logic = self.logic_converter.convert_with_inheritance(
            relevance,
            group_relevances,
            question_types=self.question_types,
            choice_codes=self.var_choice_codes,
            varname=variable_name,
        )

        # Convert constraint to Stata via the constraint-aware wrapper.
        # `.` in the SurveyCTO source becomes the variable name in Stata.
        stata_constraint = self.logic_converter.convert_constraint_to_stata(
            constraint,
            variable_name,
            question_types=self.question_types,
            choice_codes=self.var_choice_codes,
        )

        # Get actual choice values if this is a select question. When the
        # variable has a *static* `choice_filter` (no `${var}` references),
        # narrow the rendered choices to only the rows the filter accepts —
        # the data universe for this question is deterministic. For
        # *dynamic* filters we default to the full list since we can't
        # resolve runtime variable values at extraction time.
        choices = None
        choice_filter_static_applied = False
        if choice_list and choice_list in self.choice_lookup:
            base_choices = self.choice_lookup[choice_list]
            narrowed = _apply_static_choice_filter(choice_filter, base_choices)
            if narrowed is not None:
                choices = narrowed
                choice_filter_static_applied = True
            else:
                choices = base_choices

        # Check if question is disabled (relevance=0 means permanently skipped,
        # either on the question itself or inherited from a parent group)
        disabled = (relevance == "0") or self.group_stack.is_disabled()

        # references: deduped ${var} tokens parsed from this question's
        # data-structure expression columns. First-hop dependency view that
        # also lets the skill render references for form-name lookups.
        references = _parse_refs(relevance, constraint, calculation, choice_filter)

        # Build question object
        question = {
            "type": base_type,
            "choice_list": choice_list,
            "choices": choices,
            "variable_name": variable_name,
            "question_text": question_text,
            "disabled": disabled,
            "constraint": constraint,
            "stata_constraint": stata_constraint,
            "relevance": relevance,
            "group_relevances": group_relevances,
            "required": required,
            "calculation": calculation,
            "choice_filter": choice_filter,
            "appearance": appearance,
            "references": references,
            "group_path": group_path,
            "stata_skip_logic": stata_skip_logic,
        }
        # Note when the choices list was narrowed by a static filter, so
        # downstream consumers can distinguish "full list" from "filter-
        # restricted list" without re-parsing the filter expression.
        if choice_filter_static_applied:
            question["choice_filter_applied"] = "static"
        elif choice_filter:
            question["choice_filter_applied"] = "dynamic"

        return question

    def extract_all_questions(self) -> List[Dict]:
        """
        Extract all questions with metadata

        Returns:
            List of question dictionaries
        """
        questions = []
        self.group_stack = GroupStack()  # Reset stack
        q_order = 0  # form-position counter

        for idx, row in self.survey_df.iterrows():
            row_type = row.get("type", "")

            # Handle group boundaries
            if row_type == "begin group":
                # The parser normalizes NaN/whitespace to "" upstream, so
                # ``row.get(..., default)`` never falls back to the default —
                # gate on the empty string explicitly and log when the
                # diagnostic placeholder fires so malformed forms are visible.
                group_name = row.get("name", "") or f"unnamed_group_{idx}"
                if group_name.startswith("unnamed_group_"):
                    print(f"  [WARN] begin group at row {idx} has blank `name` cell - using placeholder '{group_name}'")
                group_label = row.get("label", "")
                group_relevance = row.get("relevance", None) or None
                self.group_stack.push(group_name, group_label, group_relevance)
                continue

            elif row_type == "end group":
                # Some XLSForms leave 'name' blank on end group rows. Parser
                # normalizes NaN -> "" upstream, so blank cells now arrive as
                # the empty string. Pop without a name to skip the mismatch
                # error that GroupStack.pop() would otherwise raise.
                group_name = row.get("name", "")
                if not group_name:
                    self.group_stack.pop("")
                else:
                    self.group_stack.pop(group_name)
                continue

            elif row_type == "begin repeat":
                repeat_name = row.get("name", "") or f"unnamed_repeat_{idx}"
                if repeat_name.startswith("unnamed_repeat_"):
                    print(f"  [WARN] begin repeat at row {idx} has blank `name` cell - using placeholder '{repeat_name}'")
                repeat_label = row.get("label", "")
                repeat_relevance = row.get("relevance", None) or None
                repeat_count_expr = row.get("repeat_count", None) or None

                # Emit synthetic _count variable BEFORE pushing (it lives at parent level)
                parent_path = self.group_stack.get_current_path()
                parent_relevances = self.group_stack.get_inherited_relevance()
                parent_disabled = self.group_stack.is_disabled()

                stata_skip_logic = self.logic_converter.convert_with_inheritance(
                    repeat_relevance,
                    parent_relevances,
                    question_types=self.question_types,
                    choice_codes=self.var_choice_codes,
                    varname=f"{repeat_name}_count",
                )

                count_question = {
                    "type": "repeat_count",
                    "choice_list": None,
                    "choices": None,
                    "variable_name": f"{repeat_name}_count",
                    "question_text": f"Number of iterations in '{repeat_label or repeat_name}' repeat group",
                    "disabled": parent_disabled or (repeat_relevance == "0"),
                    "constraint": None,
                    "relevance": repeat_relevance,
                    "group_relevances": parent_relevances,
                    "required": False,
                    "calculation": repeat_count_expr,
                    "group_path": parent_path,
                    "stata_skip_logic": stata_skip_logic,
                    "repeat_group_name": repeat_name,
                    "question_order": q_order,
                }
                q_order += 1
                questions.append(count_question)

                self.group_stack.push(repeat_name, repeat_label, repeat_relevance)
                continue

            elif row_type == "end repeat":
                repeat_name = row.get("name", "")
                self.group_stack.pop(repeat_name)
                continue

            # Extract question
            question = self._extract_question(row)
            if question:
                question["question_order"] = q_order
                q_order += 1
                questions.append(question)

        # Validate group stack
        if not self.group_stack.validate_closed():
            errors = self.group_stack.get_errors()
            print("WARNING: Group nesting errors detected:")
            for error in errors:
                print(f"  - {error}")

        return questions

    def save_json(self, questions: List[Dict], output_name: str) -> Path:
        """
        Save questions to JSON file

        Args:
            questions: List of question dictionaries
            output_name: Output filename

        Returns:
            Path to created JSON file
        """
        output_path = self.output_dir / output_name

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(questions, f, indent=2, ensure_ascii=False)

        print(f"[OK] Created {output_name}: {len(questions)} questions")
        return output_path

    def extract_and_save(self, output_name: str) -> Path:
        """
        Extract all questions and save to JSON

        Args:
            output_name: Output filename

        Returns:
            Path to created JSON file
        """
        print(f"\n=== Phase 2: JSON Extraction ===")
        questions = self.extract_all_questions()
        output_path = self.save_json(questions, output_name)
        print()
        return output_path
