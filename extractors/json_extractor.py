"""
Extract JSON with complete question metadata
Phase 2: JSON Master File Generation
"""
import json
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional
from parsers.group_tracker import GroupStack
from parsers.type_parser import parse_question_type
from transformers.logic_converter import LogicConverter
import config


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
        """Build dictionary mapping choice list names to their choices"""
        self.choice_lookup = {}

        if 'list_name' not in self.choices_df.columns:
            return

        for list_name in self.choices_df['list_name'].unique():
            if pd.isna(list_name) or list_name == '':
                continue

            list_choices = self.choices_df[self.choices_df['list_name'] == list_name]
            choices = []

            for _, row in list_choices.iterrows():
                choice = {
                    'value': row.get('value', ''),
                    'label': row.get('label', '')
                }
                choices.append(choice)

            self.choice_lookup[list_name] = choices

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
        constraint = row.get("constraint", None) or None
        relevance = row.get("relevance", None) or None
        required = row.get("required", "") == "yes"
        calculation = row.get("calculation", None) or None

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

        # Get actual choice values if this is a select question
        choices = None
        if choice_list and choice_list in self.choice_lookup:
            choices = self.choice_lookup[choice_list]

        # Check if question is disabled (relevance=0 means permanently skipped,
        # either on the question itself or inherited from a parent group)
        disabled = (relevance == "0") or self.group_stack.is_disabled()

        # Build question object
        question = {
            "type": base_type,
            "choice_list": choice_list,
            "choices": choices,
            "variable_name": variable_name,
            "question_text": question_text,
            "disabled": disabled,
            "constraint": constraint,
            "relevance": relevance,
            "group_relevances": group_relevances,
            "required": required,
            "calculation": calculation,
            "group_path": group_path,
            "stata_skip_logic": stata_skip_logic
        }

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
                group_name = row.get("name", f"unnamed_group_{idx}")
                group_label = row.get("label", "")
                group_relevance = row.get("relevance", None) or None
                self.group_stack.push(group_name, group_label, group_relevance)
                continue

            elif row_type == "end group":
                group_name = row.get("name", "")
                self.group_stack.pop(group_name)
                continue

            elif row_type == "begin repeat":
                repeat_name = row.get("name", f"unnamed_repeat_{idx}")
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
