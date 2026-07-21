"""config.template.py — SurveyCTO Extractor Configuration Template.
================================================================
This is the blank template that `surveycto-init` writes into your project as
config.py. config.py is per-project and gitignored; the tool discovers it in the
directory you run it from. Fill in SURVEYS and DATASETS for your project.

    SURVEYS  → Phase 1–3 (extraction, JSON docs, section splitting)
    DATASETS → Phase 4   (variable dictionary generation)
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Base paths — adjust to your project layout
# ---------------------------------------------------------------------------
# config.py lives in your project's working directory (where you run the tool),
# so PROJECT_ROOT is simply this file's directory. Point the dirs below at your
# instrument / output / data folders (absolute paths are fine too).
PROJECT_ROOT = Path(__file__).parent

INSTRUMENTS_DIR = PROJECT_ROOT / "path" / "to" / "instruments"  # TODO
OUTPUT_DIR = PROJECT_ROOT / "path" / "to" / "survey_documentation"  # TODO
DATA_DIR = PROJECT_ROOT / "path" / "to" / "data"  # TODO


# ---------------------------------------------------------------------------
# SURVEYS — Instrument-side configuration (one entry per SurveyCTO form)
# Used by: surveycto-extract (Phases 1-3: extraction, JSON docs, sections, synthetic)
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
# Used by: surveycto-vardict, surveycto-summary-stats
#
# These entries describe the actual collected data (.dta) paired with its
# instrument metadata.  Phase 4 (variable dictionaries) and Phase 5 (summary
# stats) require both a Stata dataset and a questions.json file, so they can
# only run after data collection and after Phase 2 (json) has been run.
#
# BRIDGE: "questions_json" points to the _questions.json file produced by
# Phase 2 for the corresponding SURVEYS entry.  This file bridges instrument
# metadata (question labels, skip logic, choice lists) into the data pipeline.
# Run `uv run surveycto-extract --survey my_survey --phases json` to generate it.
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
    #
    #     # --- Optional: compiled-XForm contract overlay (deterministic mapping) ---
    #     # If you saved the deployed form's compiled XForm XML, point xml_path at
    #     # it to enable surveycto-enrich.  The XML is the authoritative
    #     # column->node map, so it resolves columns fuzzy matching misses (nested
    #     # repeats, select_multiple-in-repeat, pulldata preloads) with no false
    #     # positives, and resolves select-from-file choice labels from the form's
    #     # attached CSVs.  Purely additive: omit xml_path and Phase 4 behaves
    #     # exactly as before (fuzzy matching only).  See surveycto-enrich.
    #     # "xml_path":             DATA_DIR / "forms" / "my_survey.xml",
    #     # "xml_attachments_dirs": [DATA_DIR / "forms"],   # extra dirs for search() CSVs (xml_path.parent is always searched)
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
    "calculation",
]

CHOICES_COLUMNS = ["list_name", "value", "label"]

# ---------------------------------------------------------------------------
# Types to exclude from question extraction
# Remove "calculate" from the list if you want calculated fields in the output.
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
    "phonenumber",
]

# System variable prefixes to exclude from extraction
SYSTEM_PREFIXES = ["instanceID", "instanceName", "KEY", "SET-OF-"]


# --- Optional: sentinel / special-missing codes -----------------------------
# Numeric codes standing in for non-response reasons. This is a PROJECT
# CONVENTION, not a standard -- override it to match your survey's codes.
# Format: {code: (human_label, stata_extended_missing)}. Omit to use the IPA
# default (-99=Don't know/.d, -88=Refused/.r, -77=N/A/.n, -66=Other/.o,
# -55=Not in list/.m). Consumed by surveycto-vardict (choice-code
# labels + data scan) and load_survey_metadata.py (Stata mvdecode recode).
# SENTINEL_MEANINGS = {
#     -99: ("Don't know", ".d"),
#     -88: ("Refused to answer", ".r"),
# }
# Extra codes to scan/count as sentinels but never recode (default: [-98]).
# SENTINEL_SCAN_ONLY = [-98]
