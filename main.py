"""
Main orchestrator for SurveyCTO Survey Documentation System
"""
import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import config
from parsers.survey_parser import SurveyParser
from extractors.csv_extractor import CSVExtractor
from extractors.json_extractor import JSONExtractor
from generators.diagram_generator import DiagramGenerator
from generators.section_splitter import SectionSplitter
from generators.seed_generator import generate_seed_dofile


class SurveyDocumentationSystem:
    """Main orchestrator for survey documentation generation"""

    def __init__(self, survey_key: str):
        """
        Initialize system for specific survey

        Args:
            survey_key: Survey identifier (key from config.SURVEYS)
        """
        if survey_key not in config.SURVEYS:
            raise ValueError(f"Unknown survey: {survey_key}. Must be one of: {', '.join(config.SURVEYS.keys())}")

        self.survey_key = survey_key
        self.config = config.SURVEYS[survey_key]
        self.survey_name = self.config["name"]

        # Ensure output directories exist
        self.config["output_dir"].mkdir(parents=True, exist_ok=True)
        self.config["sections_dir"].mkdir(parents=True, exist_ok=True)

    def run_all_phases(self):
        """Execute all three phases of documentation generation"""
        print(f"\n{'=' * 80}")
        print(f"SURVEYCTO DOCUMENTATION SYSTEM")
        print(f"Survey: {self.survey_name}")
        print(f"{'=' * 80}")

        # Load survey
        print(f"\nLoading survey from: {self.config['input_file']}")
        external_csv = self.config.get("external_choices_csv")
        if external_csv:
            print(f"Loading external choices from: {external_csv}")
        parser = SurveyParser(self.config["input_file"], external_choices_csv=external_csv)
        survey_df, choices_df = parser.load()

        # Print survey info
        info = parser.get_survey_info()
        print(f"[OK] Loaded survey: {info['total_rows']} rows, {info['groups']} groups, {info['questions']} questions")

        choice_lists = parser.get_choice_lists()
        print(f"[OK] Loaded choices: {len(choice_lists)} choice lists")

        # Phase 1: CSV Extraction
        csv_extractor = CSVExtractor(survey_df, choices_df, self.config["output_dir"])
        survey_csv, choices_csv = csv_extractor.extract_all(
            config.SURVEY_COLUMNS,
            config.CHOICES_COLUMNS,
            f"{self.survey_key}_survey.csv",
            f"{self.survey_key}_choices.csv"
        )

        # Phase 2: JSON Extraction and Diagram
        json_extractor = JSONExtractor(survey_df, choices_df, self.config["output_dir"])
        questions = json_extractor.extract_all_questions()
        json_path = json_extractor.save_json(questions, f"{self.survey_key}_questions.json")

        # Generate structure diagram
        print("=== Phase 2: Structure Diagram ===")
        diagram_generator = DiagramGenerator(survey_df, self.config["output_dir"])
        diagram_path = diagram_generator.save_diagram(f"{self.survey_key}_structure.txt")
        print()

        # Phase 3: Section Splitting
        max_depth = self.config.get("max_section_depth")
        section_splitter = SectionSplitter(questions, self.config["sections_dir"], max_depth=max_depth)
        section_paths = section_splitter.split_and_save(prefix="section")

        # Final summary
        print(f"{'=' * 80}")
        print(f"DOCUMENTATION COMPLETE")
        print(f"{'=' * 80}")
        print(f"\nOutput directory: {self.config['output_dir']}")
        print(f"Sections directory: {self.config['sections_dir']}")
        print(f"\nGenerated files:")
        print(f"  - {survey_csv.name}")
        print(f"  - {choices_csv.name}")
        print(f"  - {json_path.name} ({len(questions)} questions)")
        print(f"  - {diagram_path.name}")
        print(f"  - {len(section_paths)} section files in sections/")
        print()


def run_seed_phase(survey_key: str):
    """Phase 2c: generate seed .do file from questions.json (no dataset needed)."""
    survey_cfg = config.SURVEYS[survey_key]
    dataset_cfg = config.DATASETS.get(survey_key)
    if dataset_cfg is None:
        print(f"[SKIP] No DATASETS entry for '{survey_key}' -- cannot locate questions_json for seed")
        return

    json_key = 'questions_json' if 'questions_json' in dataset_cfg else 'json'
    questions_json = Path(dataset_cfg[json_key])
    if not questions_json.exists():
        print(f"[SKIP] questions.json not found at {questions_json} -- run phase 2 first")
        return

    output_do = survey_cfg["output_dir"] / f"{survey_key}_create_seed.do"
    repeat_defaults = survey_cfg.get("repeat_defaults", {})
    data_path = Path(dataset_cfg["data"]) if "data" in dataset_cfg else None

    print(f"\n=== Phase 2c: Seed Dataset Generator ({survey_key}) ===")
    generate_seed_dofile(
        questions_json_path=questions_json,
        output_do_path=output_do,
        survey_name=survey_cfg["name"],
        repeat_defaults=repeat_defaults,
        data_path=data_path,
    )
    print()


def main():
    """Main entry point with CLI"""
    survey_keys = list(config.SURVEYS.keys())
    valid_phases = ["csv", "json", "seed", "sections", "all"]

    parser = argparse.ArgumentParser(
        description="Generate comprehensive documentation for SurveyCTO surveys"
    )
    parser.add_argument(
        "--survey",
        choices=survey_keys + ["all"],
        required=True,
        help=f"Survey to process ({', '.join(survey_keys)}, or all)"
    )
    parser.add_argument(
        "--phases",
        nargs="+",
        default=["all"],
        metavar="PHASE",
        help=f"Phases to run: {', '.join(valid_phases)} (default: all). "
             f"'seed' requires questions.json from phase 'json'."
    )

    args = parser.parse_args()

    if args.survey == "all":
        surveys = survey_keys
    else:
        surveys = [args.survey]

    phases = set(args.phases)
    run_all = "all" in phases

    for survey_key in surveys:
        try:
            # Seed-only: no need to load the survey XLSX
            if not run_all and phases == {"seed"}:
                run_seed_phase(survey_key)
                continue

            system = SurveyDocumentationSystem(survey_key)
            system.run_all_phases()

            # Seed phase runs after JSON extraction
            if run_all or "seed" in phases:
                run_seed_phase(survey_key)

        except Exception as e:
            print(f"\nERROR processing {survey_key} survey: {str(e)}")
            import traceback
            traceback.print_exc()
            continue


if __name__ == "__main__":
    main()
