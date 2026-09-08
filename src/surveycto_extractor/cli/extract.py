"""Main orchestrator for SurveyCTO Survey Documentation System."""

import argparse
import json
import sys

from surveycto_extractor.config_loader import load_config
from surveycto_extractor.extractors.csv_extractor import CSVExtractor
from surveycto_extractor.extractors.json_extractor import JSONExtractor
from surveycto_extractor.generators.diagram_generator import DiagramGenerator
from surveycto_extractor.generators.section_splitter import SectionSplitter
from surveycto_extractor.parsers.survey_parser import SurveyParser
from surveycto_extractor.transformers.logic_converter import clear_strip_log

# config.py is per-project and gitignored; discovered from the working directory
# (see config_loader). None when absent -- tests inject a stub, and main() exits
# with a clear message if it is genuinely missing at run time.
config = load_config()


class SurveyDocumentationSystem:
    """Main orchestrator for survey documentation generation."""

    def __init__(self, survey_key: str):
        """Initialize system for specific survey.

        Args:
            survey_key: Survey identifier (key from config.SURVEYS)

        """
        if survey_key not in config.SURVEYS:
            raise ValueError(
                f"Unknown survey: {survey_key}. Must be one of: {', '.join(config.SURVEYS.keys())}"
            )

        self.survey_key = survey_key
        self.config = config.SURVEYS[survey_key]
        self.survey_name = self.config["name"]

        # Ensure output directories exist
        self.config["output_dir"].mkdir(parents=True, exist_ok=True)
        self.config["sections_dir"].mkdir(parents=True, exist_ok=True)

    def _load_survey(self):
        """Load the SurveyCTO instrument XLSX and return parsed data."""
        print(f"\n{'=' * 80}")
        print("SURVEYCTO DOCUMENTATION SYSTEM")
        print(f"Survey: {self.survey_name}")
        print(f"{'=' * 80}")

        print(f"\nLoading survey from: {self.config['input_file']}")
        external_csv = self.config.get("external_choices_csv")
        if external_csv:
            print(f"Loading external choices from: {external_csv}")
        parser = SurveyParser(
            self.config["input_file"], external_choices_csv=external_csv
        )
        survey_df, choices_df = parser.load()

        info = parser.get_survey_info()
        print(
            f"[OK] Loaded survey: {info['total_rows']} rows, {info['groups']} groups, {info['questions']} questions"
        )

        choice_lists = parser.get_choice_lists()
        print(f"[OK] Loaded choices: {len(choice_lists)} choice lists")

        return survey_df, choices_df

    def run_csv_phase(self, survey_df, choices_df):
        """Phase 1: Extract survey and choices CSVs."""
        csv_extractor = CSVExtractor(survey_df, choices_df, self.config["output_dir"])
        survey_csv, choices_csv = csv_extractor.extract_all(
            config.SURVEY_COLUMNS,
            config.CHOICES_COLUMNS,
            f"{self.survey_key}_survey.csv",
            f"{self.survey_key}_choices.csv",
        )
        return survey_csv, choices_csv

    def run_json_phase(self, survey_df, choices_df):
        """Phase 2: Extract questions JSON and generate structure diagram."""
        json_extractor = JSONExtractor(survey_df, choices_df, self.config["output_dir"])
        questions = json_extractor.extract_all_questions()
        json_path = json_extractor.save_json(
            questions, f"{self.survey_key}_questions.json"
        )

        print("=== Phase 2: Structure Diagram ===")
        diagram_generator = DiagramGenerator(survey_df, self.config["output_dir"])
        diagram_path = diagram_generator.save_diagram(
            f"{self.survey_key}_structure.txt"
        )
        print()

        return questions, json_path, diagram_path

    def run_sections_phase(self, questions):
        """Phase 3: Split questions JSON into per-section files."""
        max_depth = self.config.get("max_section_depth")
        section_splitter = SectionSplitter(
            questions, self.config["sections_dir"], max_depth=max_depth
        )
        return section_splitter.split_and_save(prefix="section")

    def run_phases(self, phases):
        """Execute only the requested phases."""
        survey_df, choices_df = self._load_survey()
        questions = None

        if "csv" in phases:
            self.run_csv_phase(survey_df, choices_df)

        if "json" in phases:
            questions, _, _ = self.run_json_phase(survey_df, choices_df)

        if "sections" in phases:
            if questions is None:
                # sections depends on questions JSON — load from disk if available
                json_path = (
                    self.config["output_dir"] / f"{self.survey_key}_questions.json"
                )
                if not json_path.exists():
                    print(
                        "[SKIP] sections phase requires questions.json -- run --phases json first"
                    )
                else:
                    with open(json_path, encoding="utf-8") as fh:
                        questions = json.load(fh)
            if questions is not None:
                self.run_sections_phase(questions)

    def run_all_phases(self):
        """Execute all three phases of documentation generation."""
        survey_df, choices_df = self._load_survey()
        survey_csv, choices_csv = self.run_csv_phase(survey_df, choices_df)
        questions, json_path, diagram_path = self.run_json_phase(survey_df, choices_df)
        section_paths = self.run_sections_phase(questions)

        print(f"{'=' * 80}")
        print("DOCUMENTATION COMPLETE")
        print(f"{'=' * 80}")
        print(f"\nOutput directory: {self.config['output_dir']}")
        print(f"Sections directory: {self.config['sections_dir']}")
        print("\nGenerated files:")
        print(f"  - {survey_csv.name}")
        print(f"  - {choices_csv.name}")
        print(f"  - {json_path.name} ({len(questions)} questions)")
        print(f"  - {diagram_path.name}")
        print(f"  - {len(section_paths)} section files in sections/")
        print()


def main():
    """Run the extract CLI entry point."""
    if config is None:
        print(
            "ERROR: config.toml not found in the current directory. Run "
            "`surveycto-init` to create one (or copy sample/config.example.toml "
            "to config.toml for the bundled sample), then fill in SURVEYS/DATASETS."
        )
        sys.exit(1)
    survey_keys = list(config.SURVEYS.keys())
    valid_phases = ["csv", "json", "sections", "all"]

    parser = argparse.ArgumentParser(
        description="Generate comprehensive documentation for SurveyCTO surveys"
    )
    parser.add_argument(
        "--survey",
        choices=survey_keys + ["all"],
        required=True,
        help=f"Survey to process ({', '.join(survey_keys)}, or all)",
    )
    parser.add_argument(
        "--phases",
        nargs="+",
        default=["all"],
        choices=valid_phases,
        help=f"Phases to run: {', '.join(valid_phases)} (default: all). "
        f"'sections' requires questions.json from phase 'json'.",
    )
    args = parser.parse_args()

    surveys = survey_keys if args.survey == "all" else [args.survey]

    phases = set(args.phases)
    run_all = "all" in phases

    errors = []
    for survey_key in surveys:
        try:
            # Clear logic converter strip log between surveys to avoid
            # stale entries bleeding across instruments
            clear_strip_log()

            system = SurveyDocumentationSystem(survey_key)
            if run_all:
                system.run_all_phases()
            else:
                system.run_phases(phases)

        except Exception as e:
            print(f"\nERROR processing {survey_key} survey: {str(e)}")
            import traceback

            traceback.print_exc()
            errors.append(survey_key)
            continue

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
