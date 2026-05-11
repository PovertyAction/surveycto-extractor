"""
Sample config for the bundled household survey demo.

To run the sample:
  1. Copy this file to config.py in the repo root (same folder as main.py):
         cp sample/config.example.py config.py
  2. From the repo root, run:
         python main.py --survey household_survey
         python create_variable_dictionaries.py --survey household_survey --xlsx
"""

from pathlib import Path

# When copied to config.py at the repo root, Path(__file__).parent is the repo root.
# Sample files live in sample/ relative to the repo root.
SAMPLE_DIR = Path(__file__).parent / "sample"

# Instrument side — only needs the .xlsx; can run before data collection (Phases 1-3, seed)
SURVEYS = {
    "household_survey": {
        "input_file":           SAMPLE_DIR / "Household_Survey.xlsx",
        "external_choices_csv": None,
        "output_dir":           SAMPLE_DIR / "output",
        "sections_dir":         SAMPLE_DIR / "output" / "sections",
        "name":                 "Household Survey (IPA HFC sample)",
        "max_section_depth":    3,
        "repeat_defaults":      {},
        # Optional per-survey settings used by --phases synthetic:
        #   "pulldata_search_dirs": [DATA_DIR / "form_attachments"],
        #       Directories searched for pulldata CSVs (defaults to the
        #       directory containing input_file).
        #   "geo_bbox": (lat_min, lat_max, lon_min, lon_max),
        #       Bounding box for synthetic geopoint sampling (defaults to
        #       the global -90..90 / -180..180).
    },
}

# Data side — needs .dta + questions_json bridge; run after data collection (Phase 4+)
# questions_json points to the *_questions.json file produced by: main.py --phases json
DATASETS = {
    "household_survey": {
        "data":           SAMPLE_DIR / "household_survey.dta",
        "questions_json": SAMPLE_DIR / "output" / "household_survey_questions.json",
        "output_json":    SAMPLE_DIR / "output" / "household_survey_variable_dictionary.json",
        "output_xlsx":    SAMPLE_DIR / "output" / "household_survey_variable_dictionary.xlsx",
    },
}

SURVEY_COLUMNS  = ["type", "name", "label", "constraint", "relevance", "required", "calculation"]
CHOICES_COLUMNS = ["list_name", "value", "label"]

EXCLUDED_TYPES = [
    "begin group", "end group", "begin repeat", "end repeat",
    "note", "start", "end", "deviceid", "username", "simserial", "phonenumber"
]

SYSTEM_PREFIXES = ["instanceID", "instanceName", "KEY", "SET-OF-"]
