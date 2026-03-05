"""
Extract CSV files from SurveyCTO XLSX surveys
Phase 1: CSV Extraction
"""
import pandas as pd
from pathlib import Path
from typing import List


class CSVExtractor:
    """Extract filtered CSV files from survey and choices sheets"""

    def __init__(self, survey_df: pd.DataFrame, choices_df: pd.DataFrame, output_dir: Path):
        """
        Initialize extractor

        Args:
            survey_df: Survey sheet DataFrame
            choices_df: Choices sheet DataFrame
            output_dir: Output directory for CSV files
        """
        self.survey_df = survey_df
        self.choices_df = choices_df
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def extract_survey_csv(self, columns: List[str], output_name: str) -> Path:
        """
        Extract survey CSV with specified columns

        Args:
            columns: List of column names to extract
            output_name: Output filename (e.g., "girls_survey.csv")

        Returns:
            Path to created CSV file
        """
        # Filter to available columns
        available_columns = [col for col in columns if col in self.survey_df.columns]
        missing_columns = [col for col in columns if col not in self.survey_df.columns]

        if missing_columns:
            print(f"Warning: Missing columns in survey: {', '.join(missing_columns)}")

        # Extract and save
        df_filtered = self.survey_df[available_columns].copy()

        # Replace empty strings with proper NaN for better CSV handling
        df_filtered = df_filtered.replace("", pd.NA)

        output_path = self.output_dir / output_name
        df_filtered.to_csv(output_path, index=False, na_rep="")

        print(f"[OK] Created {output_name}: {len(df_filtered)} rows, {len(available_columns)} columns")
        return output_path

    def extract_choices_csv(self, columns: List[str], output_name: str) -> Path:
        """
        Extract choices CSV with specified columns

        Args:
            columns: List of column names to extract
            output_name: Output filename (e.g., "girls_choices.csv")

        Returns:
            Path to created CSV file
        """
        # Filter to available columns
        available_columns = [col for col in columns if col in self.choices_df.columns]
        missing_columns = [col for col in columns if col not in self.choices_df.columns]

        if missing_columns:
            print(f"Warning: Missing columns in choices: {', '.join(missing_columns)}")

        # Extract and save
        df_filtered = self.choices_df[available_columns].copy()

        # Replace empty strings with proper NaN
        df_filtered = df_filtered.replace("", pd.NA)

        output_path = self.output_dir / output_name
        df_filtered.to_csv(output_path, index=False, na_rep="")

        # Get summary of choice lists
        if "list_name" in available_columns:
            list_counts = df_filtered.groupby("list_name").size()
            print(f"[OK] Created {output_name}: {len(df_filtered)} choices across {len(list_counts)} lists")
        else:
            print(f"[OK] Created {output_name}: {len(df_filtered)} rows")

        return output_path

    def extract_all(self, survey_columns: List[str], choices_columns: List[str],
                   survey_name: str, choices_name: str) -> tuple:
        """
        Extract both survey and choices CSVs

        Args:
            survey_columns: Columns to extract from survey
            choices_columns: Columns to extract from choices
            survey_name: Survey CSV filename
            choices_name: Choices CSV filename

        Returns:
            Tuple of (survey_path, choices_path)
        """
        print(f"\n=== Phase 1: CSV Extraction ===")
        survey_path = self.extract_survey_csv(survey_columns, survey_name)
        choices_path = self.extract_choices_csv(choices_columns, choices_name)
        print()
        return survey_path, choices_path
