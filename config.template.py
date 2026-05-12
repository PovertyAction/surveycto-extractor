"""
config.template.py — SurveyCTO Extractor Configuration Template
================================================================
Copy this file to your project's surveycto_extractor/ directory and rename it
to config.py.  Fill in SURVEYS and DATASETS for your project.

    SURVEYS  → Phase 1–3 (extraction, JSON docs, section splitting)
    DATASETS → Phase 4   (variable dictionary generation)
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Base paths — adjust to your project layout
# ---------------------------------------------------------------------------
# If your surveycto_extractor/ folder is N levels below the project root,
# use Path(__file__).parent + N * ".parent".
#
# Examples:
#   root/surveycto_extractor/config.py           → .parent.parent  (1 level up)
#   root/scripts/misc/surveycto_extractor/config.py → 4× .parent   (see g2r pattern)
PROJECT_ROOT = Path(__file__).parent.parent        # TODO: adjust levels

INSTRUMENTS_DIR  = PROJECT_ROOT / "path" / "to" / "instruments"   # TODO
OUTPUT_DIR       = PROJECT_ROOT / "path" / "to" / "survey_documentation"  # TODO
DATA_DIR         = PROJECT_ROOT / "path" / "to" / "data"          # TODO


# ---------------------------------------------------------------------------
# SURVEYS — Instrument-side configuration (one entry per SurveyCTO form)
# Used by: main.py, check_missing_cond_vars.py, check_string_conditions.py
#
# These entries describe the SurveyCTO instrument (.xlsx form definition).
# Phases 1-3 only need the instrument — they can run before any data is
# collected.  The short key (e.g. "my_survey") is the canonical identifier
# that links each SURVEYS entry to its corresponding DATASETS entry.
# ---------------------------------------------------------------------------
SURVEYS = {
    # TODO: Replace "my_survey" with a short key (no spaces).
    # "my_survey": {
    #     "input_file":           INSTRUMENTS_DIR / "my_survey_instrument.xlsx",
    #     "external_choices_csv": None,  # or Path("path/to/choices.csv") if choices are in a separate sheet
    #     "output_dir":           OUTPUT_DIR / "my_survey",
    #     "sections_dir":         OUTPUT_DIR / "my_survey" / "sections",
    #     "name":                 "My Survey Full Name",
    #     "max_section_depth":    3,     # limit section nesting depth
    # },
}


# ---------------------------------------------------------------------------
# DATASETS — Data-side configuration (one entry per collected dataset)
# Used by: create_variable_dictionaries.py, create_summary_stats_dofile.py
#
# These entries describe the actual collected data (.dta) paired with its
# instrument metadata.  Phase 4 (variable dictionaries) and Phase 5 (summary
# stats) require both a Stata dataset and a questions.json file, so they can
# only run after data collection and after Phase 2 (json) has been run.
#
# BRIDGE: "questions_json" points to the _questions.json file produced by
# Phase 2 for the corresponding SURVEYS entry.  This file bridges instrument
# metadata (question labels, skip logic, choice lists) into the data pipeline.
# Run `python main.py --survey my_survey --phases json` to generate it.
#
# CONSTRAINT: Each key here must match a key in SURVEYS above.
# ---------------------------------------------------------------------------
DATASETS = {
    # TODO: Add one entry per survey dataset.
    # "my_survey": {
    #     "data":        DATA_DIR / "my_survey_data.dta",          # Stata dataset
    #     "questions_json": OUTPUT_DIR / "my_survey" / "my_survey_questions.json",   # Phase 2 output (bridge)
    #     "output_json": OUTPUT_DIR / "my_survey" / "my_survey_variable_dictionary.json",
    #     "output_xlsx": OUTPUT_DIR / "my_survey" / "my_survey_variable_dictionary.xlsx",
    #     # "skip_ord_dta": True,  # set True to skip _ord.dta (e.g. underscore-prefix vars or very wide datasets)
    # },
    #
    # Phase 4 also generates *_variable_graph.json alongside the variable
    # dictionary (requires networkx).  The graph file path is derived
    # automatically from output_json — no config key needed.
}


# ---------------------------------------------------------------------------
# Column mappings — usually identical across projects; adjust only if needed
# ---------------------------------------------------------------------------
SURVEY_COLUMNS = [
    "type",
    "name",
    "label",
    "constraint",
    "relevance",
    "required",
    "calculation"
]

CHOICES_COLUMNS = [
    "list_name",
    "value",
    "label"
]

# ---------------------------------------------------------------------------
# Types to exclude from question extraction
# Remove "calculate" from the list if you want calculated fields in the output
# (g2r keeps them; brac_try's original config excluded them).
# ---------------------------------------------------------------------------
EXCLUDED_TYPES = [
    "begin group",
    "end group",
    "begin repeat",
    "end repeat",
    "note",
    "start",
    "end",
    "deviceid",
    "username",
    "simserial",
    "phonenumber"
]

# System variable prefixes to exclude from extraction
SYSTEM_PREFIXES = [
    "instanceID",
    "instanceName",
    "KEY",
    "SET-OF-"
]
