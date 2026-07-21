# SurveyCTO Extractor

A Python toolkit for IPA-style cleaning projects that turns a SurveyCTO `.xlsx`
instrument — plus, once it arrives, the collected `.dta` — into structured
documentation, a synthetic export CSV for HFC dry-runs, a variable dictionary,
a relationship graph, and summary-stats do-files. Built to be used alongside
an AI coding agent like Claude Code: the outputs double as a knowledge surface
your agent can query while you clean.

---

## Quick start with the sample

```bash
git clone https://github.com/PovertyAction/surveycto-extractor.git
cd surveycto-extractor
uv sync                              # create the env + install (uses the committed uv.lock)

cp sample/config.example.toml config.toml   # the pre-filled demo config

uv run surveycto-extract --survey household_survey
uv run surveycto-vardict --survey household_survey --xlsx
```

For a **real project** (not the bundled demo), run `uv run surveycto-init` to
drop a blank `config.toml` template into the current directory, then fill in the
`[surveys.KEY]` / `[datasets.KEY]` tables. `config.toml` is per-project and
gitignored; the tool discovers it in the directory you run from.

Outputs land in `sample/output/`. The sample uses the
[IPA High Frequency Checks](https://github.com/PovertyAction/high-frequency-checks)
training dataset — a public training resource; every person name appearing in
`sample/household_survey.dta` is fictional.

---

## How this fits the survey lifecycle

A survey has two phases — and this toolkit covers both:

**Pre-collection — instrument day.** You have a SurveyCTO `.xlsx` form but no
submissions yet. Run the toolkit against the form alone to:

- Extract structured documentation, group hierarchy, per-section JSON slices.
- Generate a synthetic SurveyCTO-shaped export CSV with N synthetic respondents.
- **Run your full cleaning + HFC pipeline against the synthetic CSV** to catch
  type mismatches, missing-column errors, and skip-logic bugs before the first
  respondent is interviewed.

**Post-collection — data day.** Real `.dta` has landed. Add it to the config
and run the toolkit to:

- Generate a variable dictionary mapping every Stata variable to its source
  question, skip logic, choices, observed range, sentinel counts, missing rates.
- Generate a relationship graph (calculation dependencies, gating chains,
  repeat siblings, shared choice domains).
- Generate a summary-stats do-file grouped by skip condition.
- Query the dictionary from your AI agent during cleaning via the
  `survey-expert` skill or the MCP server.

The two phases share one config file. You can fill in the instrument-side
settings on day 1 of the project and add the data-side settings once
collection starts.

---

## Pre-collection: instrument day

### 1. Configure your project

Run `uv run surveycto-init` to write a `config.toml`, then fill in a
`[surveys.KEY]` table — the instrument-side settings. Only `input_file`,
`output_dir`, `sections_dir`, and `name` are required at this stage. Full
reference under
[Configuration](#configuration).

### 2. Extract documentation and structure

```bash
uv run surveycto-extract --survey my_survey
```

The default `--phases all` produces every instrument-side output in one run:

| Output | What it's for |
|---|---|
| `<survey>_questions.json` | Machine-readable question metadata with Stata-translated skip logic. Primary input to everything downstream. |
| `<survey>_structure.txt` | Human-readable group hierarchy of the form. |
| `sections/<section>.json` | Per-section slices of the question JSON. |
| `<survey>_synthetic.csv` | Synthetic export CSV (see below). |

Run individual phases with `--phases csv|json|sections|synthetic` (or any
combination).

### 3. Generate a synthetic export CSV

```bash
uv run surveycto-extract --survey my_survey --phases synthetic --rows 20 --seed 42
```

Produces `<survey>_synthetic.csv` — a wide-export CSV indistinguishable in
shape from what SurveyCTO will produce once real submissions arrive. Same
metadata columns (`KEY`, `SubmissionDate`, `starttime`, `duration`, `caseid`,
audit URLs), same skip-induced blanks, same `pulldata`-resolved joins, same
`search()`-driven choice expansion, same `calculate` / `calculate_here`
values.

```bash
# Force the consent cascade so HFC dry-runs exercise the gated sections
uv run surveycto-extract --survey my_survey --phases synthetic --rows 20 --seed 42 \
    --force-value c_consent=1 --force-value hh_consent=1
```

Skip logic is **ironclad**: relevance is enforced by one deterministic
evaluator that neither the RNG nor any supplied answer can override. For
coherent, path-faithful rows — e.g. "interesting cases" for an HFC dry-run —
`--answers-file` applies a JSON answer sheet **through the real gates** (unlike
`--force-value`, which bypasses them):

```bash
uv run surveycto-extract --survey my_survey --phases synthetic --rows 1 \
    --answers-file interesting_case_01.json
```

Determinism: same `--seed K` produces byte-identical output across reruns
(on the same day; the date sampler clamps to today).

If your form uses `pulldata()`, the referenced CSVs must be on disk in one of
the configured `pulldata_search_dirs` — without them, gated cascades won't
populate and `search()` can't resolve. The synth raises `MissingPullDataError`
with the search-dir list when it can't find them.

See [`docs/synthetic-generator.md`](docs/synthetic-generator.md) for the full
flag reference, the `search()` mechanics, type-concordance benchmarks against
real exports, and known limitations.

### 4. Dry-run your cleaning + HFC pipeline

Point your existing Stata cleaning script at `<survey>_synthetic.csv`. The
output shape matches the real SurveyCTO wide export, so anything that works
against the synth will work against real data. Use this step to:

- Catch missing-column errors before fieldwork starts.
- Verify Stata type inference matches your project conventions.
- Exercise `tostring` / `destring` paths and confirm HFC range checks fire.
- Iterate on skip-logic translations without waiting for real submissions.

---

## Post-collection: data day

Add a `[datasets.KEY]` table to `config.toml` once your `.dta` is available. Each
entry points at the dataset, the bridge `*_questions.json` from instrument
day, and the variable-dictionary output paths.

### 1. Generate the variable dictionary

```bash
uv run surveycto-vardict --survey my_survey --xlsx
```

Maps every Stata variable to its source question, skip logic, choice list,
sentinel counts, and form position — in **form order**, not dataset column
order. Per-variable fields include:

- `stata_type` — native Stata type (`double`, `float`, `int16`, `str244`, ...)
  from `pyreadstat` metadata, not pandas dtypes.
- `missing_rate` and `is_all_missing` — from Parquet row-group metadata, no
  data scan needed.
- `data_min` / `data_max` — observed range for integer, decimal, calculate,
  and date / time variables.
- **Sentinel counts** — raw integer sentinels (`-99`, `-88`), string
  sentinels, Stata extended missing values (`.d`, `.r`), type mismatches,
  risky calculates.
- `constraint`, `stata_constraint`, `choice_filter`, `references` —
  data-structure fields surfaced for downstream consumers.

Exports to JSON (machine-readable) and XLSX (for sharing with field teams or
PIs). Also writes `<survey>_data_ord.dta` — your dataset with columns
reordered to match form structure — and a Parquet sidecar for fast columnar
access by downstream scripts.

The export summary lists all-missing and sparse (≥95% missing) variables so
you can spot data-quality issues immediately.

#### Optional: overlay the compiled-form XML contract

By default the column → question mapping uses heuristic name matching, which is
the right default when all you have is the dataset plus `questions.json`. On
complex instruments (nested repeats, `select_multiple` inside repeats,
`pulldata()` preloads, dotted group nodes) fuzzy matching both leaves real
columns unmatched and can occasionally mismatch.

If you **saved the deployed form's compiled XForm XML** (Design → Download under
your SurveyCTO server's form definition), point at it and the extractor uses it
as the authoritative, deterministic spine:

```toml
# in config.toml, under the [datasets.KEY] table:
xml_path = "data/forms/my_survey.xml"
```

Then re-run Phase 4 (or `uv run surveycto-enrich --survey my_survey` to
overlay without rebuilding). Each variable gains a `contract` block recording
exactly **where it comes from** — `node_path`, repeat coordinates,
`select_multiple` choice code, and `data_source` (parsed `search()` / `pulldata()`
provenance). Columns fuzzy matching missed are resolved with no false positives,
and select-from-file choice labels are pulled from the form's attached CSVs.

This is **optional and additive**: omit `xml_path` and Phase 4 behaves exactly as
before. It's worth saving the XForm — it is the one input that makes the mapping
deterministic. See `surveycto-enrich`.

### 2. Use the variable graph

The variable-dictionary step also writes `<survey>_variable_graph.json` — a
directed graph capturing how variables relate (requires `networkx`, which
`uv sync` installs by default):

- `calculates_from` — A's calculation references B.
- `gated_by` — A's relevance references B.
- `group_gated_by` — A's parent group's relevance references B.
- `constrained_by` — A's constraint references B.
- `repeat_sibling` — A and B are inside the same repeat group.
- `shares_choices` — A and B draw from the same choice list.

Use it interactively via the skill's `--neighborhood VAR` flag or the MCP
server's `get_variable_neighborhood` tool. Typical questions answered in one
hop:

- "What depends on `crpsale_qty`?"
- "Why might `income_1` be missing for some observations?"
- "What variables share the `yesnodk` choice list?"

### 3. Generate the summary-stats do-file

```bash
uv run surveycto-summary-stats --survey my_survey
```

Produces a Stata do-file with `tabstat` calls for every numeric variable,
grouped by skip condition. Groups with zero observations in the dataset are
automatically detected and commented out so Stata doesn't throw `r(2000)` at
runtime. The do-file exports tabstat output to Excel for sharing with field
teams or PIs.

Customise the preamble's `${input_data}` path and the `sumstats_dir` global
via the `output_do` and `sumstats_dir_stata` keys in your `DATASETS` entry.

### 4. Query the dictionary from your AI agent

This is where the project's AI-native design pays off — see the next section.

---

## AI-native design

The variable dictionary, expression evaluator, and relationship graph are
designed so a Claude Code (or similar) session can find the right metadata in
one tool call rather than asking you to dig through a spreadsheet. Two
surfaces sit on top of the same JSON outputs.

### Survey-expert skill

The `skill/` directory contains a Claude Code skill that gives your agent
direct lookup access. It is the primary way to query survey metadata during
cleaning — faster and more reliable than asking the agent to reason from a
spreadsheet or a fragment of the form.

The skill answers questions like:

- "What is the skip condition for `hh_size`?" → `--var`
- "What are the valid choices for `asset_type`?" → `--choice-list`
- "Which variables capture crop sales?" → `--search` (TF-IDF ranked,
  natural-language queries work)
- "Why is `income_1` missing for some observations?" → `--gate-chain`
- "What's the observed range of `hh_age`?" → shown automatically under
  `data_range`
- "What depends on `crpsale_qty`?" → `--neighborhood`

Setup:

1. Copy `skill/SKILL.md` → `.claude/skills/survey-expert/SKILL.md`.
2. Copy `skill/search_survey.py` → `.claude/skills/survey-expert/search_survey.py`.
3. In `search_survey.py`, update `DOCS` to point at your
   `survey_documentation/` directory.
4. In `SKILL.md`, fill in the `[TODO: ...]` placeholders (project name,
   survey names, variable counts, file paths).

The skill auto-discovers all surveys under `DOCS` by scanning for
`*_variable_dictionary.json` files.

### MCP server (optional, higher-performance)

The `mcp_server/` directory contains an optional MCP server that keeps variable
dictionaries in memory for instant lookups — the high-performance alternative
for sessions with heavy query volume (10–50+ lookups per cleaning module).

| | `skill/search_survey.py` | `surveycto_extractor.mcp_server` |
|---|---|---|
| Loads JSON | Every call | Once at startup |
| Dependencies | None (stdlib) | `mcp[cli]` |
| Setup | Copy to `.claude/skills/` | Add to `.mcp.json` |
| Best for | Occasional lookups | Cleaning sessions (10–50+ lookups) |
| Batch queries | Not supported | `lookup_variables` tool |
| Gate chain | `--gate-chain` flag | `get_gate_chain` tool |
| Neighborhood | `--neighborhood` flag | `get_variable_neighborhood` tool |
| Data range | Shown in output | Shown in lookup tools |
| Multi-survey filter | `--survey KEY` flag | `survey` parameter on every tool |

```bash
uv sync --extra mcp
```

Add to your project's `.mcp.json` (a ready-made `.mcp.json.example` in the
repo root works out of the box for the sample: `cp .mcp.json.example .mcp.json`):

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "uv",
      "args": ["run", "--extra", "mcp", "python", "-m", "surveycto_extractor.mcp_server.survey_server"]
    }
  }
}
```

> **Note:** this directory was previously named `mcp/`. If your `.mcp.json`
> still points at `mcp/survey_server.py`, update the path — a stale path shows
> up as "failed to connect" in `/mcp`.

The server discovers `config.toml` (or a legacy `config.py`) in the current
working directory (override with `SURVEYCTO_CONFIG`). It degrades gracefully — if config or JSON
files are missing, tools return setup instructions rather than crashing. See
`mcp_server/README.md` for full details.

### Coding guidelines for agents

The `coding_guidelines/` directory contains project-agnostic standards for
Stata cleaning pipelines, written to be read by AI coding agents. **Reference
them from your `AGENTS.md`** so every agent session has them in context
automatically:

```markdown
## Coding Standards

Stata cleaning modules must follow the guidelines in:
- `surveycto_extractor/coding_guidelines/CLEANING.md` — language-independent cleaning principles
- `surveycto_extractor/coding_guidelines/STATA.md` — Stata-specific patterns and guardrails
- `surveycto_extractor/coding_guidelines/SURVEYCTO_RELEVANCE_TRANSLATION.md` — SurveyCTO → Stata skip logic translation rules
- `surveycto_extractor/coding_guidelines/surveycto_refs/xlsform.md` and `expressions.md` — in-house primers distilled from the SurveyCTO documentation, used by the converter as a technical reference
```

These files need no modification — they are fully project-agnostic. The
`surveycto_refs/` subdirectory holds derivative summaries we maintain by hand
against <https://docs.surveycto.com>. See
`coding_guidelines/surveycto_refs/README.md` for the source pages and refresh
procedure.

---

## Configuration

`config.toml` has two main tables that map onto the two lifecycle phases (a
legacy `config.py` with `SURVEYS`/`DATASETS` dicts is still honoured as a
fallback). Path values are **resolved relative to the config file's directory**.

**`[surveys.KEY]`** — instrument side, required for the pre-collection phases
(`csv`, `json`, `sections`, `synthetic`). Only needs the `.xlsx` form, so you
can configure it on day 1.

```toml
[surveys.my_survey]
input_file        = "path/to/My_Survey.xlsx"
output_dir        = "survey_documentation/my_survey"
sections_dir      = "survey_documentation/my_survey/sections"
name              = "My Survey Full Name"
max_section_depth = 3
# external_choices_csv = "path/to/choices.csv"
# Optional, used by --phases synthetic:
# pulldata_search_dirs = ["path/to/forms-media"]
# geo_bbox             = [lat_min, lat_max, lon_min, lon_max]
```

**`[datasets.KEY]`** — data side, required for the post-collection scripts.
Needs the collected `.dta` plus the `questions_json` bridge file from Phase 2 of
instrument day. Each `[datasets.KEY]` shares its `KEY` with a `[surveys.KEY]`.

```toml
[datasets.my_survey]
data           = "path/to/my_survey_data.dta"
questions_json = "survey_documentation/my_survey/my_survey_questions.json"
output_json    = "survey_documentation/my_survey/my_survey_variable_dictionary.json"
output_xlsx    = "survey_documentation/my_survey/my_survey_variable_dictionary.xlsx"
# output_do          = "scripts/cleaning/my_survey_summary_stats.do"
# sumstats_dir_stata = "${project_root}/path/to/survey_documentation/my_survey"
```

The `questions_json` path is the bridge between the two sides — Phase 4 will
fail with an actionable error if it doesn't exist yet. (`SURVEY_COLUMNS`,
`CHOICES_COLUMNS`, `EXCLUDED_TYPES`, `SYSTEM_PREFIXES` default to the IPA
convention; override them under a `[columns]` table only if you need to.)

### Sentinel / special-missing codes

Sentinel counts and the Stata `mvdecode` recode assume a **default set of
special-missing codes** — the IPA convention this toolkit has been used with:

| Code | Meaning | Stata missing |
|---|---|---|
| `-99` | Don't know | `.d` |
| `-88` | Refused to answer | `.r` |
| `-77` | Not applicable | `.n` |
| `-66` | Other (specify) | `.o` |
| `-55` | Not in list | `.m` |
| `-98` | *(scanned/counted, never recoded)* | — |

This is a **project convention, not a fixed standard**. If your study uses a
different set — or if any of these are legitimate response values in your data
— override the table wholesale under `[sentinels]` in `config.toml` (an empty
`[sentinels.meanings]` disables recoding):

```toml
[sentinels]
scan_only = [-98]                # counted as sentinels but never recoded

[sentinels.meanings]             # code -> [label, Stata extended-missing]
"-99" = ["Don't know", ".d"]
"-88" = ["Refused", ".r"]
```

The single source of truth is `core/sentinels.py`, read by both
`surveycto-vardict` and the Stata-metadata loader (`generators/load_survey_metadata.py`)
so the two can never disagree. If `core/sentinels.py` is missing (e.g. a partial
vendor copy of the toolkit), the pipeline falls back to these same defaults with
a warning rather than failing.

---

## Using this toolkit in a project

Install it as a package — from a clone of this repo, or (once published) as a
dependency of your own project:

```bash
uv sync                       # from a clone of this repo
# or, in another project:  uv add surveycto-extractor
```

Then, from your project directory:

```bash
uv run surveycto-init                        # writes a config.toml template into ./
# edit config.toml → fill in [surveys.KEY] / [datasets.KEY]
uv run surveycto-extract --survey my_survey  # instrument day (csv/json/sections/synthetic)
uv run surveycto-vardict --survey my_survey  # post-collection vardict + graph
```

`config.toml` is per-project and gitignored; the tool discovers it in the
directory you run from (override with `SURVEYCTO_CONFIG`). Source
layout:

```text
surveycto-extractor/
├── pyproject.toml  uv.lock  .python-version
├── src/surveycto_extractor/
│   ├── cli/            ← console entry points: extract, vardict, summary_stats, enrich, init
│   ├── core/           ← shared support (sentinels table)
│   ├── parsers/        ← survey + compiled-XForm (xml_contract) parsers
│   ├── extractors/  transformers/   ← CSV/JSON extraction, logic conversion
│   ├── generators/     ← outputs incl. Stata metadata (load_survey_metadata)
│   ├── mcp_server/     ← optional MCP add-on (uv sync --extra mcp)
│   └── templates/      ← the config.toml template surveycto-init writes
├── skill/              ← copy to .claude/skills/ (stdlib-only search + bench-test)
├── coding_guidelines/  ← reference from your AGENTS.md
└── sample/             ← bundled demo (household_survey)
```

---

## Common issues

**`MissingPullDataError` from the synth phase**
Your form references `pulldata('foo', ...)` but `foo.csv` isn't in any of the
configured `pulldata_search_dirs`. See the
[`docs/synthetic-generator.md`](docs/synthetic-generator.md) pulldata section
— the synth requires the referenced CSVs to be on disk.

**`search_survey.py` finds no surveys**
Check that `DOCS` in `search_survey.py` points to the directory containing
your `*_variable_dictionary.json` files. Run
`surveycto-vardict` first if the dictionary doesn't exist yet.

**`FileNotFoundError: Bridge file not found`**
`surveycto-vardict` needs the `*_questions.json` file produced
by `surveycto-extract --phases json`. Run instrument-day first.

**Summary-stats do-file has wrong `${input_data}` path**
Add `"output_do"` and `"sumstats_dir_stata"` keys to your `DATASETS` entry.

**`selected()` expressions not translated in skip logic**
Re-run instrument-day first — the logic converter needs question-type
information from the JSON extractor to distinguish `select_one` from
`select_multiple`.

**`calculate` fields missing from `questions.json`**
Add a `[columns]` table to `config.toml` with an `excluded_types` list that omits `"calculate"`. Calculate fields
are needed for skip-logic resolution and synthetic generation.
