"""
Main orchestrator for SurveyCTO Survey Documentation System
"""
import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# config.py is per-project and gitignored; import it gracefully so this module
# imports without a config present (e.g. tests inject a stub). main() exits
# with a clear message if it's genuinely missing at run time.
try:
    import config
except ModuleNotFoundError:
    config = None
from parsers.survey_parser import SurveyParser
from extractors.csv_extractor import CSVExtractor
from extractors.json_extractor import JSONExtractor
from generators.diagram_generator import DiagramGenerator
from generators.section_splitter import SectionSplitter
from generators.synthetic_data import ScriptedProvider, generate_synthetic_csv
from transformers.logic_converter import clear_strip_log


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

    def _load_survey(self):
        """Load the SurveyCTO instrument XLSX and return parsed data."""
        print(f"\n{'=' * 80}")
        print(f"SURVEYCTO DOCUMENTATION SYSTEM")
        print(f"Survey: {self.survey_name}")
        print(f"{'=' * 80}")

        print(f"\nLoading survey from: {self.config['input_file']}")
        external_csv = self.config.get("external_choices_csv")
        if external_csv:
            print(f"Loading external choices from: {external_csv}")
        parser = SurveyParser(self.config["input_file"], external_choices_csv=external_csv)
        survey_df, choices_df = parser.load()

        info = parser.get_survey_info()
        print(f"[OK] Loaded survey: {info['total_rows']} rows, {info['groups']} groups, {info['questions']} questions")

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
            f"{self.survey_key}_choices.csv"
        )
        return survey_csv, choices_csv

    def run_json_phase(self, survey_df, choices_df):
        """Phase 2: Extract questions JSON and generate structure diagram."""
        json_extractor = JSONExtractor(survey_df, choices_df, self.config["output_dir"])
        questions = json_extractor.extract_all_questions()
        json_path = json_extractor.save_json(questions, f"{self.survey_key}_questions.json")

        print("=== Phase 2: Structure Diagram ===")
        diagram_generator = DiagramGenerator(survey_df, self.config["output_dir"])
        diagram_path = diagram_generator.save_diagram(f"{self.survey_key}_structure.txt")
        print()

        return questions, json_path, diagram_path

    def run_sections_phase(self, questions):
        """Phase 3: Split questions JSON into per-section files."""
        max_depth = self.config.get("max_section_depth")
        section_splitter = SectionSplitter(questions, self.config["sections_dir"], max_depth=max_depth)
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
                json_path = self.config["output_dir"] / f"{self.survey_key}_questions.json"
                if not json_path.exists():
                    print(f"[SKIP] sections phase requires questions.json -- run --phases json first")
                else:
                    with open(json_path, "r", encoding="utf-8") as fh:
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


def _parse_force_values(specs):
    """Parse ``--force-value`` arguments into a dict.

    Accepts a list of strings (from ``action='append'``) or a single string
    (legacy). Within one spec, comma-separated ``VAR=VAL`` pairs are
    accepted only when every comma-segment contains ``=`` — so values that
    embed commas can be passed unambiguously by repeating the flag (which
    avoids the parse conflict).
    """
    out = {}
    if specs is None:
        return out
    if isinstance(specs, str):
        specs = [specs]
    for spec in specs:
        if not spec:
            continue
        parts = [p for p in spec.split(",") if p.strip()]
        # Only treat commas as entry separators when every segment is a
        # well-formed VAR=VAL — otherwise the comma probably belongs to a
        # value, and we treat the whole spec as a single VAR=VAL.
        if len(parts) > 1 and all("=" in p for p in parts):
            entries = parts
        else:
            entries = [spec]
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                raise ValueError(
                    f"--force-value entry {entry!r} must be in VAR=VAL form"
                )
            k, v = entry.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _load_answer_files(paths):
    """Load one or more ``--answers-file`` JSON sheets for the scripted provider.

    Each file: ``{"answers": {var-or-suffixed-key: value, ...},
    "directives": {"repeat_counts": {repeat_name: N, ...}}}``. Multiple files
    merge in order (later wins). Returns ``(answers, repeat_counts)``. Scripted
    answers are inputs to gating -- an answer for a question the deterministic
    evaluator gates out is ignored, never forced (that is the separate
    ``--force-value`` path)."""
    answers = {}
    repeat_counts = {}
    if not paths:
        return answers, repeat_counts
    for p in paths:
        path = Path(p)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"--answers-file {p}: expected a JSON object")
        a = data.get("answers") or {}
        if not isinstance(a, dict):
            raise ValueError(f"--answers-file {p}: 'answers' must be an object")
        answers.update({str(k): v for k, v in a.items()})
        directives = data.get("directives") or {}
        rc = (directives.get("repeat_counts") if isinstance(directives, dict) else {}) or {}
        if isinstance(rc, dict):
            repeat_counts.update({str(k): int(v) for k, v in rc.items()})
    return answers, repeat_counts


def run_synthetic_phase(
    survey_key: str, n_rows: int = 5, seed: int = 0,
    force_values=None, provider=None, strict: bool = True,
    repeat_count_overrides=None,
):
    """Generate a SurveyCTO-shaped synthetic export CSV from questions.json."""
    survey_cfg = config.SURVEYS[survey_key]
    dataset_cfg = config.DATASETS.get(survey_key)
    if dataset_cfg is None:
        print(f"[SKIP] No DATASETS entry for '{survey_key}' -- cannot locate questions_json for synthetic")
        return

    json_key = "questions_json" if "questions_json" in dataset_cfg else "json"
    questions_json = Path(dataset_cfg[json_key])
    if not questions_json.exists():
        print(f"[SKIP] questions.json not found at {questions_json} -- run phase 2 first")
        return

    output_csv = survey_cfg["output_dir"] / f"{survey_key}_synthetic.csv"
    # Search for pulldata CSVs in the configured dirs (default: dir of input_file)
    search_dirs = survey_cfg.get("pulldata_search_dirs")
    if not search_dirs:
        search_dirs = [Path(survey_cfg["input_file"]).parent]
    else:
        search_dirs = [Path(d) for d in search_dirs]

    geo_bbox = survey_cfg.get("geo_bbox")

    # Read settings sheet from the original XLSForm so the synth can use
    # the real form_id and version when building auto-generated metadata
    # (formdef_version, text_audit URL, audio_audit URL). XLSForm
    # settings sheets are conventionally single-row (form-wide settings,
    # not per-question), so we only read row 0. A malformed multi-row
    # settings sheet would silently pick the first row.
    #
    # Route through SurveyParser._normalize_string_cells so settings cells
    # share the same NaN-coerce + whitespace-strip contract as survey and
    # choices cells. Single normalization point at the parse boundary.
    form_settings = {}
    try:
        import pandas as _pd
        s_df = _pd.read_excel(
            survey_cfg["input_file"],
            sheet_name="settings",
            dtype=str,
            keep_default_na=False,
        )
        s_df = SurveyParser._normalize_string_cells(s_df)
        if len(s_df) > 0:
            form_settings = {k: (v or None) for k, v in s_df.iloc[0].to_dict().items()}
    except Exception:
        form_settings = {}

    print(f"\n=== Phase 2d: Synthetic Data Generator ({survey_key}) ===")
    generate_synthetic_csv(
        questions_json_path=questions_json,
        output_csv_path=output_csv,
        pulldata_search_dirs=search_dirs,
        survey_name=survey_cfg["name"],
        n_rows=n_rows,
        seed=seed,
        force_values=force_values,
        geo_bbox=geo_bbox,
        form_settings=form_settings,
        provider=provider,
        strict=strict,
        repeat_count_overrides=repeat_count_overrides,
    )
    print()


def main():
    """Main entry point with CLI"""
    if config is None:
        print("ERROR: config.py not found. Copy config.template.py to config.py "
              "(or sample/config.example.py for the bundled sample) and fill in "
              "SURVEYS/DATASETS.")
        sys.exit(1)
    survey_keys = list(config.SURVEYS.keys())
    valid_phases = ["csv", "json", "sections", "synthetic", "all"]

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
        choices=valid_phases,
        help=f"Phases to run: {', '.join(valid_phases)} (default: all). "
             f"'synthetic' requires questions.json from phase 'json'."
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=None,
        help="Number of rows for --phases synthetic. Default 5."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for reproducible synthetic CSV generation. Default 0. "
             "Same --seed produces byte-identical CSV output."
    )
    parser.add_argument(
        "--force-value",
        action="append",
        default=None,
        metavar="VAR=VAL[,VAR=VAL...]",
        help="For --phases synthetic: force one or more variables to specific "
             "values, overriding random sampling. Bypasses relevance so gated "
             "cascades populate even when their parent gates would otherwise "
             "evaluate false (useful for HFC dry-runs of consent-gated "
             "sections). May be repeated; multiple VAR=VAL pairs in one flag "
             "can be comma-separated (use repeated flags if values contain "
             "commas). Example: --force-value c_consent=1,hh_consent=1"
    )
    parser.add_argument(
        "--answers-file",
        action="append",
        default=None,
        metavar="PATH",
        help="For --phases synthetic: a JSON answer sheet whose values fill the "
             "matching questions -- but ONLY through open gates (unlike "
             "--force-value, a scripted answer never bypasses relevance). "
             "Shape: {\"answers\": {var-or-suffixed-key: value}, \"directives\": "
             "{\"repeat_counts\": {repeat_name: N}}}. Invalid values fall back to "
             "a sampled value. May be repeated; sheets merge in order. Used by "
             "the bench-test skill to fill coherent 'interesting cases'."
    )
    parser.add_argument(
        "--legacy-fail-open-relevance",
        action="store_true",
        help="For --phases synthetic: restore the pre-ironclad behaviour where a "
             "relevance expression the evaluator cannot interpret shows the "
             "question (fail open) instead of hiding it. Default is strict "
             "(fail closed + recorded)."
    )

    args = parser.parse_args()

    if args.survey == "all":
        surveys = survey_keys
    else:
        surveys = [args.survey]

    phases = set(args.phases)
    run_all = "all" in phases

    synthetic_rows = args.rows if args.rows is not None else 5
    force_values = _parse_force_values(args.force_value)
    scripted_answers, repeat_count_overrides = _load_answer_files(args.answers_file)
    synthetic_provider = ScriptedProvider(scripted_answers) if scripted_answers else None
    synthetic_strict = not args.legacy_fail_open_relevance

    errors = []
    for survey_key in surveys:
        try:
            # Clear logic converter strip log between surveys to avoid
            # stale entries bleeding across instruments
            clear_strip_log()

            # synthetic-only: no need to load the survey XLSX
            if not run_all and phases == {"synthetic"}:
                run_synthetic_phase(
                    survey_key, n_rows=synthetic_rows, seed=args.seed,
                    force_values=force_values, provider=synthetic_provider,
                    strict=synthetic_strict,
                    repeat_count_overrides=repeat_count_overrides,
                )
                continue

            system = SurveyDocumentationSystem(survey_key)
            if run_all:
                system.run_all_phases()
            else:
                system.run_phases(phases)

            # Synthetic CSV phase runs after JSON extraction
            if run_all or "synthetic" in phases:
                run_synthetic_phase(
                    survey_key, n_rows=synthetic_rows, seed=args.seed,
                    force_values=force_values, provider=synthetic_provider,
                    strict=synthetic_strict,
                    repeat_count_overrides=repeat_count_overrides,
                )

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
