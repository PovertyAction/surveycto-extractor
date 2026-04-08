"""
Load and parse SurveyCTO XLSX survey forms
"""
import pandas as pd
from pathlib import Path
from typing import Dict, Tuple


class SurveyParser:
    """Parse SurveyCTO XLSX survey forms"""

    def __init__(self, file_path: Path, external_choices_csv: Path = None):
        """
        Initialize parser

        Args:
            file_path: Path to XLSX file
            external_choices_csv: Optional path to external choices CSV (wide format)
        """
        self.file_path = file_path
        self.external_choices_csv = external_choices_csv
        self.survey_df = None
        self.choices_df = None
        self.settings_df = None

    def load(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Load survey and choices sheets from XLSX

        Returns:
            Tuple of (survey_df, choices_df)
        """
        try:
            # Load survey sheet
            self.survey_df = pd.read_excel(
                self.file_path,
                sheet_name="survey",
                dtype=str,
                keep_default_na=False
            )

            # Load choices sheet
            self.choices_df = pd.read_excel(
                self.file_path,
                sheet_name="choices",
                dtype=str,
                keep_default_na=False
            )

            # Load and merge external choices if provided
            if self.external_choices_csv and self.external_choices_csv.exists():
                external_choices = self._load_external_choices()
                self.choices_df = self._merge_choices(self.choices_df, external_choices)

            # Try to load settings sheet (optional)
            try:
                self.settings_df = pd.read_excel(
                    self.file_path,
                    sheet_name="settings",
                    dtype=str,
                    keep_default_na=False
                )
            except ValueError:
                self.settings_df = None

            return self.survey_df, self.choices_df

        except FileNotFoundError:
            raise FileNotFoundError(f"Survey file not found: {self.file_path}")
        except Exception as e:
            raise Exception(f"Error loading survey file: {str(e)}")

    def get_survey_info(self) -> Dict[str, any]:
        """
        Get basic survey information

        Returns:
            Dictionary with survey metadata
        """
        if self.survey_df is None:
            raise ValueError("Survey not loaded. Call load() first.")

        info = {
            "total_rows": len(self.survey_df),
            "columns": list(self.survey_df.columns),
            "groups": 0,
            "repeat_groups": 0,
            "questions": 0
        }

        # Count different types
        if "type" in self.survey_df.columns:
            type_counts = self.survey_df["type"].value_counts()
            info["groups"] = type_counts.get("begin group", 0)
            info["repeat_groups"] = type_counts.get("begin repeat", 0)

            # Questions are rows that aren't structural
            structural_types = ["begin group", "end group", "begin repeat", "end repeat"]
            info["questions"] = len(
                self.survey_df[~self.survey_df["type"].isin(structural_types)]
            )

        return info

    def get_choice_lists(self) -> Dict[str, int]:
        """
        Get summary of choice lists

        Returns:
            Dictionary mapping list_name to number of choices
        """
        if self.choices_df is None:
            raise ValueError("Choices not loaded. Call load() first.")

        if "list_name" in self.choices_df.columns:
            return self.choices_df.groupby("list_name").size().to_dict()
        return {}

    def _load_external_choices(self) -> pd.DataFrame:
        """
        Load external choices CSV in wide format and convert to long format

        Expected format: columns like choice_list_value, choice_list_label
        or choice_list_key, choice_list_label

        Returns:
            DataFrame with columns [list_name, value, label]
        """
        try:
            csv_df = pd.read_csv(self.external_choices_csv, dtype=str,
                                    encoding="utf-8-sig")

            # Find all value/key columns
            value_cols = [c for c in csv_df.columns if c.endswith('_value') or c.endswith('_key') or c.endswith('_id')]

            all_choices = []

            for value_col in value_cols:
                # Determine list name from column (remove the MATCHED suffix only;
                # order matters — strip the longest matching suffix first to avoid
                # 'household_id_value' → 'household' instead of 'household_id')
                list_name = value_col
                for suffix in ('_value', '_key', '_id'):
                    if value_col.endswith(suffix):
                        list_name = value_col[:-len(suffix)]
                        break

                # Find corresponding label column
                label_col = f"{list_name}_label"
                if label_col not in csv_df.columns:
                    continue

                # Extract non-null rows for this choice list
                subset = csv_df[[value_col, label_col]].dropna(subset=[value_col])

                if len(subset) == 0:
                    continue

                # Convert to long format
                for _, row in subset.iterrows():
                    all_choices.append({
                        'list_name': list_name,
                        'value': str(row[value_col]).strip(),
                        'label': str(row[label_col]).strip()
                    })

            if not all_choices:
                return pd.DataFrame(columns=['list_name', 'value', 'label'])

            return pd.DataFrame(all_choices)

        except Exception as e:
            print(f"Warning: Error loading external choices CSV: {str(e)}")
            return pd.DataFrame(columns=['list_name', 'value', 'label'])

    def _merge_choices(self, xlsx_choices: pd.DataFrame, external_choices: pd.DataFrame) -> pd.DataFrame:
        """
        Merge choices from XLSX and external CSV, with XLSX taking precedence

        Args:
            xlsx_choices: Choices from XLSX sheet
            external_choices: Choices from external CSV

        Returns:
            Merged choices DataFrame
        """
        if external_choices.empty:
            return xlsx_choices

        # Get list names from XLSX
        xlsx_lists = set(xlsx_choices['list_name'].str.strip().unique()) if 'list_name' in xlsx_choices.columns else set()

        # Filter external choices to only those NOT in XLSX
        new_choices = external_choices[~external_choices['list_name'].isin(xlsx_lists)]

        if new_choices.empty:
            return xlsx_choices

        # Concatenate
        merged = pd.concat([xlsx_choices, new_choices], ignore_index=True)

        return merged
