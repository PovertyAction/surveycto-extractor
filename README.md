# SurveyCTO Extractor

A Python toolkit that turns a SurveyCTO `.xlsx` instrument into a suite of Stata-ready
outputs: structured survey documentation, a variable dictionary, a schema seed dataset,
and summary-stats do-files. Designed to be copied into any IPA-style cleaning project
and used alongside an AI coding agent (Claude Code or similar).

---

## Quick start with the sample

```bash
git clone https://github.com/PovertyAction/surveycto-extractor.git
cd surveycto-extractor
pip install -r requirements.txt

cp sample/config.example.py config.py

python main.py --survey household_survey
python create_variable_dictionaries.py --survey household_survey --xlsx
```

Outputs land in `sample/output/`. The sample uses the
[IPA High Frequency Checks](https://github.com/PovertyAction/high-frequency-checks)
training dataset.

---

## What you get

| Output | How to generate | What it's for |
|---|---|---|
| `*_questions.json` | `main.py` | Machine-readable question metadata with Stata skip logic — primary input to everything else |
| `*_structure.txt` | `main.py` | Human-readable group hierarchy of the form |
| `sections/*.json` | `main.py` | Per-section slices of the question JSON |
| `*_create_seed.do` | `main.py --phases seed` | Stata do-file that builds a 1-row schema dataset — run this before your real data arrives to test your cleaning pipeline |
| `*_variable_dictionary.json` | `create_variable_dictionaries.py` | Maps every Stata variable to its source question, skip logic, choice list, and sentinel counts — in form order, not dataset column order |
| `*_variable_dictionary.xlsx` | `create_variable_dictionaries.py --xlsx` | Same dictionary as a spreadsheet for sharing with field teams or PIs |
| `*_data_ord.dta` | `create_variable_dictionaries.py` | Column-reordered dataset following form structure |
| `*.parquet` | `create_variable_dictionaries.py` | Parquet sidecar written next to the `.dta` — used internally for fast columnar access; also available for downstream analysis |
| `*_summary_stats.do` | `create_summary_stats_dofile.py` | Stata do-file with `tabstat` calls grouped by skip condition + Excel export |

---

## Setting up for your project

### 1. Copy the files

Copy this repo's contents into your project:

```
my_project/
└── surveycto_extractor/
    ├── coding_guidelines/       ← reference these from your AGENTS.md (see below)
    ├── skill/                   ← copy to .claude/skills/survey-expert/ (see below)
    ├── config.py                ← copy from config.template.py, fill in
    ├── main.py
    ├── create_variable_dictionaries.py
    ├── create_summary_stats_dofile.py
    ├── load_survey_metadata.py      ← required by create_summary_stats_dofile.py
    └── ...
```

### 2. Install dependencies

```bash
pip install -r surveycto_extractor/requirements.txt
```

### 3. Configure `config.py`

Copy `config.template.py` to `config.py` and fill in your paths.

The config has two dicts that reflect a meaningful split in the pipeline:

**`SURVEYS`** — instrument side, required for phases 1–3 and seed. Only needs the `.xlsx` form — can be configured on day 1, before any data is collected.

```python
SURVEYS = {
    "my_survey": {
        "input_file":           Path("path/to/My_Survey.xlsx"),
        "external_choices_csv": None,
        "output_dir":           Path("path/to/survey_documentation/my_survey"),
        "sections_dir":         Path("path/to/survey_documentation/my_survey/sections"),
        "name":                 "My Survey Full Name",
        "max_section_depth":    3,
        "repeat_defaults":      {},   # {repeat_group_name: n} to override auto-detection
    },
}
```

**`DATASETS`** — data side, required for variable dictionary and summary stats. Needs a collected `.dta` and the `questions_json` bridge file produced by Phase 2. Each key must match a key in `SURVEYS`.

```python
DATASETS = {
    "my_survey": {
        "data":           Path("path/to/my_survey_data.dta"),
        "questions_json": Path("path/to/survey_documentation/my_survey/my_survey_questions.json"),  # Phase 2 output
        "output_json":    Path("path/to/survey_documentation/my_survey/my_survey_variable_dictionary.json"),
        "output_xlsx":    Path("path/to/survey_documentation/my_survey/my_survey_variable_dictionary.xlsx"),
        # "output_do":          Path("scripts/cleaning/my_survey_summary_stats.do"),
        # "sumstats_dir_stata": "${project_root}/path/to/survey_documentation/my_survey",
    },
}
```

The `questions_json` path is the bridge between the two sides: it points to the `*_questions.json` file produced by `main.py --phases json`. Phase 4 will fail with an actionable error if this file doesn't exist yet.

---

## Running the pipeline

### Phases 1–3: Extract survey structure

```bash
python main.py --survey my_survey                        # all phases (default)
python main.py --survey my_survey --phases csv           # CSV only
python main.py --survey my_survey --phases json          # questions JSON + diagram only
python main.py --survey my_survey --phases sections      # section split only (reads questions JSON from disk)
python main.py --survey my_survey --phases json seed     # JSON then seed
python main.py --survey all                              # all surveys in config
```

Phases 1–3 only require the SurveyCTO `.xlsx` instrument — they can run before any data is collected. Valid phase names: `csv`, `json`, `sections`, `seed`, `all`.

Produces `*_questions.json`, `*_structure.txt`, and `sections/*.json`.

### Seed dataset

```bash
python main.py --survey my_survey --phases seed
```

Generates `*_create_seed.do` — a Stata do-file that builds a 1-row dataset covering
every variable in the form, including conditionally-triggered ones. Run it in Stata
**before your real data arrives** to test that your cleaning do-files run end-to-end
against the complete variable schema. Repeat groups are expanded to N iterations
(auto-detected from the `.dta` if available, or set via `repeat_defaults` in config).

### Variable dictionary

```bash
python create_variable_dictionaries.py --survey my_survey --xlsx
```

Maps every Stata variable to its source question, Stata skip logic, choice list, and
form position. Per-variable fields include:

- **`stata_type`** — Stata native type (`double`, `float`, `int16`, `int8`, `string`, etc.) from pyreadstat metadata, not pandas dtypes
- **`missing_rate`** — fraction of observations that are null, from parquet row-group metadata (no data scan)
- **`is_all_missing`** — `true` when the variable has zero non-null observations
- **`data_min` / `data_max`** — observed range for integer, decimal, calculate, and date/time variables (from parquet metadata)
- **sentinel counts** — detects raw integer sentinels (`-99`, `-88`, etc.), string sentinels, extended missing values (`.d`, `.r`), type mismatches, and risky calculate fields

The export summary prints a diagnostic of all-missing and sparse (≥95% missing) variables.

Exports to both JSON (machine-readable) and XLSX (for sharing).
Also saves `*_data_ord.dta` — a copy of your dataset with columns reordered to match
form structure rather than submission order. A parquet sidecar is written next to the
`.dta` for fast columnar access by downstream scripts.

### Summary stats do-file

```bash
python create_summary_stats_dofile.py --survey my_survey
```

Generates a Stata do-file with `tabstat` calls for all numeric variables, grouped by
skip condition. Groups with zero observations in the dataset are automatically detected
and commented out to prevent Stata `r(2000)` errors at runtime. Merges with survey
metadata and exports to Excel. Check the preamble — it uses `${input_data}` and a
`sumstats_dir` global; customize via `output_do` and `sumstats_dir_stata` in your
DATASETS config entry.

---

## Survey expert skill (Claude Code)

The `skill/` directory contains a Claude Code skill that gives your AI agent direct
lookup access to the variable dictionary. **This is the primary way to query survey
metadata during cleaning** — faster and more reliable than asking Claude to reason
from a spreadsheet or description.

The skill answers questions like:
- "What is the skip condition for `hh_size`?" (`--var`)
- "What are the valid choices for `asset_type`?" (`--choice-list`)
- "Which variables belong to the household roster repeat group?" (`--search`)
- "Why is `income_1` missing for some observations?" (`--gate-chain`)
- "What's the observed range of `hh_age`?" (shown automatically via `data_range`)

### Setup

1. Copy `skill/SKILL.md` → `.claude/skills/survey-expert/SKILL.md`
2. Copy `skill/search_survey.py` → `.claude/skills/survey-expert/search_survey.py`
3. In `search_survey.py`, update `DOCS` to point to your `survey_documentation/` directory
4. In `SKILL.md`, fill in the `[TODO: ...]` placeholders (project name, survey names, variable counts, file paths)

The skill auto-discovers all surveys under `DOCS` by scanning for `*_variable_dictionary.json` files.

---

## MCP server add-on (optional)

The `mcp/` directory contains an optional MCP server that keeps variable dictionaries
in memory for instant lookups. This is the high-performance alternative to the skill
for sessions with heavy query volume (10–50+ variable lookups per cleaning module).

| | `skill/search_survey.py` | `mcp/survey_server.py` |
|---|---|---|
| Loads JSON | Every call | Once at startup |
| Dependencies | None (stdlib) | `mcp[cli]` |
| Setup | Copy to `.claude/skills/` | Add to `.mcp.json` |
| Best for | Occasional lookups | Cleaning sessions (10-50+ lookups) |
| Batch queries | Not supported | `lookup_variables` tool |
| Gate chain | `--gate-chain` flag | `get_gate_chain` tool |
| Data range | Shown in output | Shown in lookup tools |
| Multi-survey filter | `--survey KEY` flag | `survey` parameter on every tool |

### Setup

```bash
pip install "mcp[cli]"
```

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "python",
      "args": ["path/to/surveycto_extractor/mcp/survey_server.py"]
    }
  }
}
```

The server finds `config.py` in its parent directory automatically (override with
`SURVEY_CONFIG` env var). It degrades gracefully — if config or JSON files are missing,
tools return setup instructions instead of crashing.

See `mcp/README.md` for full details.

---

## Coding guidelines for agents

The `coding_guidelines/` directory contains project-agnostic standards for Stata cleaning
pipelines, written to be read by AI coding agents. **Reference them from your `AGENTS.md`**
so every agent session has them in context automatically:

```markdown
## Coding Standards

Stata cleaning modules must follow the guidelines in:
- `surveycto_extractor/coding_guidelines/CLEANING.md` — language-independent cleaning principles
- `surveycto_extractor/coding_guidelines/STATA.md` — Stata-specific patterns and guardrails
- `surveycto_extractor/coding_guidelines/SURVEYCTO_RELEVANCE_TRANSLATION.md` — SurveyCTO → Stata skip logic translation rules
```

These files need no modification — they are fully project-agnostic.

---

## Common issues

**`search_survey.py` finds no surveys**
Check that `DOCS` points to the directory containing `*_variable_dictionary.json` files.
Run phase 4 first if the dictionary doesn't exist yet.

**Seed emits only 1 iteration for a variable-driven repeat group**
The generator scans the `.dta` for max iterations but needs the file to exist.
Override in config: `"repeat_defaults": {"my_repeat_group": 5}`.

**Phase 5 do-file has wrong `${input_data}` path**
Add `"output_do"` and `"sumstats_dir_stata"` keys to your DATASETS entry.

**`selected()` expressions not translated in skip logic**
Re-run phase 1–3 first — the logic converter needs question type information from
the JSON extractor to distinguish `select_one` from `select_multiple`.

**`calculate` fields missing from `questions.json`**
Remove `"calculate"` from `EXCLUDED_TYPES` in `config.py`. Calculate fields are
needed for skip logic resolution and seed generation.

**`FileNotFoundError: Bridge file not found`**
Phase 4 (`create_variable_dictionaries.py`) requires the `*_questions.json` file
produced by Phase 2. Run `python main.py --survey my_survey --phases json` first,
then re-run the variable dictionary script.
