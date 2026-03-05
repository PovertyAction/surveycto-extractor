# SurveyCTO Extractor — Setup & Usage

This guide walks you through copying the surveycto_extractor engine into a new project
and configuring it to generate survey documentation, variable dictionaries, seed datasets,
and summary statistics do-files.

---

## Prerequisites

- Python 3.8+
- A SurveyCTO XLSX instrument file (required for Phase 1–3)
- A Stata `.dta` dataset (required for Phase 4–5)
- Optional: a separate external choices CSV (if choices are not embedded in the XLSX)
- `pyreadstat` — enables `_ord.dta` generation (form-ordered dataset) in Phase 4 and repeat-count inference in Phase 2c

---

## 1. File Layout

Copy the contents of this `surveycto_extractor_takeout/` directory into your project.
Recommended location (adjust to your project):

```
my_project/
└── surveycto_extractor/
    ├── coding_guidelines/              ← copy as-is; reference from your AGENTS.md
    │   ├── CLEANING.md
    │   ├── STATA.md
    │   └── SURVEYCTO_RELEVANCE_TRANSLATION.md
    ├── config.py                       ← copy from config.template.py, fill in
    ├── main.py
    ├── load_survey_metadata.py
    ├── create_variable_dictionaries.py
    ├── create_summary_stats_dofile.py
    ├── check_missing_cond_vars.py
    ├── check_string_conditions.py
    ├── scan_skip_logic_funcs.py
    ├── test_index_strip.py
    ├── requirements.txt
    ├── parsers/
    ├── extractors/
    ├── generators/
    └── transformers/
```

Then copy the skill:

```
my_project/
└── .claude/
    └── skills/
        └── survey-expert/
            ├── SKILL.md           ← copy from skill/SKILL.md, fill in TODOs
            └── search_survey.py   ← copy from skill/search_survey.py, update DOCS path
```

---

## 2. Install Dependencies

```bash
pip install -r surveycto_extractor/requirements.txt
# or with uv:
uv pip install -r surveycto_extractor/requirements.txt
```

---

## 3. Configure `config.py`

Copy `config.template.py` to `config.py` (in the same directory) and fill it in.

### `SURVEYS` dict — one entry per SurveyCTO instrument

Required for Phase 1–3 (extraction, JSON docs, section splitting) and Phase 2c (seed).

```python
SURVEYS = {
    "my_survey": {
        "input_file":           Path("path/to/My_Survey.xlsx"),
        "external_choices_csv": None,   # or Path("...") if choices are in separate CSV
        "output_dir":           Path("path/to/survey_documentation/my_survey"),
        "sections_dir":         Path("path/to/survey_documentation/my_survey/sections"),
        "name":                 "My Survey Full Name",
        "max_section_depth":    3,
        "repeat_defaults":      {},     # {repeat_group_name: n_iterations} overrides
    },
}
```

`repeat_defaults` controls how many iterations the seed generator emits for each repeat
group when it cannot determine the count from the form itself (e.g. `repeat_count=${hh_size}`).
If the `.dta` dataset is available, the generator scans it for the maximum observed
iteration and uses that instead — `repeat_defaults` wins over everything else.

### `DATASETS` dict — one entry per survey dataset

Required for Phase 4–5 (variable dictionary + summary stats do-file).
Key must match the corresponding key in `SURVEYS`.

```python
DATASETS = {
    "my_survey": {
        "data":           Path("path/to/my_survey_data.dta"),
        "questions_json": Path("path/to/survey_documentation/my_survey/my_survey_questions.json"),
        "output_json":    Path("path/to/survey_documentation/my_survey/my_survey_variable_dictionary.json"),
        "output_xlsx":    Path("path/to/survey_documentation/my_survey/my_survey_variable_dictionary.xlsx"),
        # Optional: override the output do-file location
        # "output_do":    Path("scripts/cleaning/my_survey_summary_stats.do"),
        # Optional: override the Stata global for sumstats_dir
        # "sumstats_dir_stata": "${project_root}/path/to/survey_documentation/my_survey",
    },
}
```

### Notes on `EXCLUDED_TYPES`

- Keep `calculate` in the extracted questions (useful for skip logic resolution and seed generation)
- If your project's `calculate` fields pollute the output, add `"calculate"` to the list
- Check your instrument to decide; the default template excludes `"calculate"`

---

## 4. Phase 1–3: Run Extraction

Generates the survey structure diagram, questions JSON, and section files.
Every extracted question gets a `question_order` field (form-position integer) used
downstream by Phase 2c and Phase 4.

```bash
cd surveycto_extractor/

# Process one survey
python main.py --survey my_survey

# Process all surveys in config.SURVEYS
python main.py --survey all

# Run only specific phases
python main.py --survey my_survey --phases csv json
python main.py --survey my_survey --phases json seed
python main.py --survey my_survey --phases seed      # seed-only (skips XLSX load)
```

Valid `--phases` values: `csv`, `json`, `seed`, `sections`, `all` (default).

> **Note**: The `--survey` choices are auto-derived from `config.SURVEYS` — you never need
> to edit `main.py`. Add or remove surveys in `config.py` and the CLI updates automatically.

**Outputs** (in `output_dir` from config):
- `my_survey_structure.txt` — hierarchical overview
- `my_survey_questions.json` — all questions with skip logic (Stata-translated) and `question_order`
- `sections/*.json` — one file per survey section

---

## 5. Phase 2c: Seed Dataset Generator

Generates a Stata `.do` file that creates a 1-row schema seed dataset.
Uses only `questions.json` — no `.dta` needed (though if the dataset exists it will be
scanned to auto-detect max repeat-group iterations).

```bash
# Run as part of the main pipeline (after JSON extraction)
python main.py --survey my_survey --phases json seed

# Run standalone (requires questions.json from Phase 1-3)
python main.py --survey my_survey --phases seed
```

**Output**: `my_survey_create_seed.do` in `output_dir`

The generated do-file:
- Declares explicit Stata storage types for every variable (`long`, `double`, `str32`, etc.)
- Expands repeat groups to N iterations (from `repeat_defaults`, data scan, or form literal)
- Expands `select_multiple` questions into binary choice columns
- Uses `calculate` expressions as variable labels when no label is defined

---

## 6. Phase 4: Variable Dictionary

Maps every Stata dataset variable to its SurveyCTO source question.
Variables are sorted in **form order** (using `question_order` from Phase 1–3), not
Stata column order. Within repeat groups, variables are ordered iteration-first:
`var_1, var_2, ..., var_N` for each form question in sequence.

```bash
# Process one survey (requires Phase 1-3 output)
python create_variable_dictionaries.py --survey my_survey

# Process all surveys in config.DATASETS
python create_variable_dictionaries.py

# Also run select_multiple validation
python create_variable_dictionaries.py --survey my_survey --validate

# Also export XLSX
python create_variable_dictionaries.py --survey my_survey --xlsx
```

**Outputs** (in `output_dir` from config):
- `my_survey_variable_dictionary.json` — complete variable metadata in form order
- `my_survey_data_ord.dta` — column-reordered dataset (form order, Stata release 118); value labels and variable labels preserved

---

## 7. Phase 5: Summary Stats Do-file

Generates a Stata do-file with `tabstat` calls for all numeric variables grouped
by skip condition, plus merge with survey metadata and Excel export.

```bash
python create_summary_stats_dofile.py --survey my_survey
```

**Output**: `<survey>_summary_stats.do` (location depends on `output_do` in config or default next to data file)

**Important**: Check the generated preamble. It uses `${input_data}` and a `sumstats_dir` global.
Make sure these match your project's Stata global structure.
To customize, add `sumstats_dir_stata` to the DATASETS entry in config.py.

---

## 8. Diagnostics

Run these after Phase 1–3 to catch common issues:

```bash
# Check for variables referenced in skip conditions that don't exist in the dataset
python check_missing_cond_vars.py

# Find any remaining SurveyCTO string syntax in the Stata skip logic output
python check_string_conditions.py

# Scan survey for SurveyCTO functions like pulldata(), once(), jr:choice-name
python scan_skip_logic_funcs.py

# Test index() stripping logic with synthetic examples
python test_index_strip.py
```

These scripts require `SURVEYS` to be populated in `config.py` and Phase 1–3 to have run.

---

## 9. Skill Setup

1. Copy `skill/SKILL.md` to `.claude/skills/survey-expert/SKILL.md`
2. Copy `skill/search_survey.py` to `.claude/skills/survey-expert/search_survey.py`
3. Open `search_survey.py` and update the `DOCS` path (near the top) to your project's
   survey_documentation directory. The script will auto-discover all surveys underneath.
4. Open `SKILL.md` and fill in the `[TODO: ...]` placeholders:
   - Project name
   - Survey names and variable counts (from Phase 4 output summary)
   - File paths to instruments and datasets
   - Any project-specific critical reminders (e.g., stratification requirements)

---

## 10. Verification

After setup, verify each phase:

| Phase | Command | Expected output |
|-------|---------|-----------------|
| 1–3   | `python main.py --survey <key>` | `<key>_structure.txt`, `<key>_questions.json` (with `question_order`), `sections/*.json` |
| 2c    | `python main.py --survey <key> --phases seed` | `<key>_create_seed.do` |
| 4     | `python create_variable_dictionaries.py --survey <key>` | `<key>_variable_dictionary.json` (form-ordered), `<key>_data_ord.dta` (if pyreadstat) |
| 5     | `python create_summary_stats_dofile.py --survey <key>` | `<key>_summary_stats.do` |
| diag  | `python check_missing_cond_vars.py` | Prints count of missing condition vars (may be > 0; review) |
| skill | `python .claude/skills/survey-expert/search_survey.py --var <any_var>` | Question metadata for the variable |

---

## Common Issues

### `KeyError: 'json'` in create_variable_dictionaries.py
The DATASETS entry uses `'questions_json'` (template style) but old code used `'json'`.
The takeout version supports both — make sure your config uses `'questions_json'`.

### Phase 5 do-file has wrong `${input_data}` path
Add `"output_do"` and `"sumstats_dir_stata"` keys to your DATASETS entry in config.py
to override the defaults.

### search_survey.py finds no surveys
Check that `DOCS` at the top of the script points to the directory containing your
`*_variable_dictionary.json` files. Or set `SURVEY_FILES` manually (see Option B in the script).

### `selected()` expressions not translated
The 20-step `logic_converter.py` needs the `question_types` dict to distinguish
`select_one` from `select_multiple`. This is populated by `json_extractor.py` during
Phase 1–3. If you see untranslated `selected()` calls, re-run Phase 1–3 first.

### `calculate` fields missing from questions.json
Check `EXCLUDED_TYPES` in config.py. If `"calculate"` is in the list, remove it.
Calculate fields are included by default so they appear in the seed do-file and can
be referenced in skip logic resolution.

### Seed do-file emits only 1 iteration for a variable-driven repeat group
The generator scans the `.dta` for max iterations but needs the file to exist first.
Add an explicit override in config to force a specific count:
```python
"repeat_defaults": {"my_repeat_group": 5},
```

---

## 11. Coding Standards for Agents

The `coding_guidelines/` directory contains project-agnostic standards for Stata cleaning
pipelines. These are designed to be read by AI agents working on your cleaning code.

**Add a reference in your project's `AGENTS.md`** (or equivalent agent instructions file)
so agents automatically have these standards in context:

```markdown
## Coding Standards

Stata cleaning modules must follow the guidelines in:
- `surveycto_extractor/coding_guidelines/CLEANING.md` — language-independent cleaning principles
- `surveycto_extractor/coding_guidelines/STATA.md` — Stata-specific patterns and guardrails
- `surveycto_extractor/coding_guidelines/SURVEYCTO_RELEVANCE_TRANSLATION.md` — SurveyCTO → Stata translation rules
```

These files require no modification — they are fully project-agnostic as written.

---

## 12. Known Limitations & Future Work

### `constraint_parsed` field in `questions.json`
Currently every downstream script that cares about integer constraints re-parses the raw
`constraint` string independently (regex for lower/upper bounds, DK/Refuse allowance, etc.).
The extractor should parse this once in `json_extractor.py` and store a structured object:
```json
"constraint_parsed": {"lower": 0, "upper": 120, "allow_dk": true, "allow_refuse": true}
```
This would eliminate duplicated fragile regex across `create_integer_constraints_report.py`,
`create_summary_stats_dofile.py`, and any future audit scripts.

### Validation mode
A `--validate` flag on `main.py` that reports after Phase 1–3:
- `type="integer"` questions with no `constraint`
- Questions with `required="yes"` but no `relevance` (always required — intentional?)
- Orphaned `end group` / `end repeat` markers

Useful for catching instrument issues early without running a separate audit script.

### Multilingual label support
SurveyCTO forms can define `label::English`, `label::Swahili`, etc. columns alongside
the default `label` column. A config knob in `SURVEYS`:
```python
"label_column": "label::English",  # default: "label"
```
would let the extractor surface non-English primary labels without any code changes.

### Integer constraint audit script
`create_integer_constraints_report.py` exists in the source project but is not yet
project-agnostic. Generalizing it to read from `config.SURVEYS`/`config.DATASETS`
and drop into any project would make it a first-class template tool alongside
`create_summary_stats_dofile.py`.

### HTML stripping in question text
Question labels occasionally contain HTML tags from SurveyCTO rich-text formatting.
Currently stripped ad-hoc in individual downstream scripts. A shared `strip_html(text)`
utility (one regex, one place) would centralize this.
