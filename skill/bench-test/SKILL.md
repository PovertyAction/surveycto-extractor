---
name: bench-test
description: Populate an HFC-ready synthetic dataset by bench-testing a SurveyCTO instrument -- propose (or accept) enumerator-style scenarios, fill coherent "interesting cases" through the ironclad synthetic engine, and report whether each scenario reached its intended path.
version: 0.1.0
tags:
  - survey
  - synthetic
  - bench-test
  - skip-logic
  - hfc
  - surveycto
user_invocable: true
---

<!--
SETUP INSTRUCTIONS:
1. Copy this folder to .claude/skills/bench-test/ in your project.
2. Copy bench_scenarios.py alongside this file.
3. Requires the surveycto-extractor toolkit (main.py + a filled config.py with the
   survey's SURVEYS/DATASETS entry) and, ideally, the survey-expert MCP server for
   scenario derivation. Delete this comment block once customised.
-->

# Bench-test: coherent synthetic data for HFC dry-runs

Your job is to populate an **HFC-ready synthetic dataset** for a survey, spending
LLM effort only where it pays: making a handful of **"interesting cases"**
internally coherent (a real-looking respondent, a plausible outlier, a case that
should trip a specific check), while the bulk stays cheap stochastic. Every row
is filled through the toolkit's **ironclad** synthetic engine, so the skip-logic
paths are always faithful -- a scripted answer is an *input* to gating, never an
override.

Invoke: `/bench-test <survey_key>` (a key in `config.SURVEYS`). With no key, list
`config.SURVEYS` and ask which survey.

## The engine contract you build on

The toolkit's `main.py --phases synthetic` walks the form deterministically and
fills values. Two flags matter here (see `docs/synthetic-generator.md`):

- `--answers-file PATH` -- apply a JSON answer sheet **through open gates only**
  (unlike `--force-value`, which bypasses relevance). Sheet shape:
  `{"answers": {var-or-suffixed-key: value}, "directives": {"repeat_counts": {name: N}}}`.
  Invalid values fall back to a sampled value; a scripted answer for a gated-out
  question is ignored (recorded, not forced).
- `--coverage-trace [PATH]` -- write `<csv>.coverage.json`: per row, which cells
  were asked vs gated (with the failing gate expression + resolved operands) and
  where each answer came from. This is how you verify a scenario reached its path.

`bench_scenarios.py` (next to this file) does the deterministic glue:
`validate` (check must-hit answers), `expand` (materialise per-variation sheets +
a seed manifest), `report` (join traces to expectations).

## Workflow

### 1. Read the survey
Prefer the **survey-expert MCP tools** (no raw-file reading): `get_survey_info`
to orient, `get_repeat_structure` for roster/count axes, `search_questions` +
`get_choice_list` for key variables and their domains (incl. sentinels
-99/-88/-666), and `get_gate_chain` / `get_variable_neighborhood` to learn which
answers open which sections (so a scenario's answers are internally consistent).

### 2. Gather scenarios (two sources -- use either or both)

**(a) User-supplied (primary).** If the user hands you a set of
prompts/situations -- inline, or a `<survey>_cases.json` they wrote ("eligible
rural household that consents, 3 kids in school"; "declines consent"; "max
roster") -- **wire each situation into a concrete path**: use the gate chains to
resolve it to the `must_hit` answers that make the intended sections reachable,
and set `expect.reach` / `expect.block_at` to the sections the situation should
hit or skip. One situation becomes one scenario (fill subagents run per
scenario/variation in step 4).

**(b) Auto-derived (fallback / augmentation).** If the user gives no scenarios
(or wants more coverage), propose the number they asked for in step 3 (default
~6-8), spanning what an HFC needs to exercise:
- **realistic-normal** coherent households (baseline distributions, real correlations),
- **plausible edge/outlier** respondents (boundary ages, large rosters, rare-but-valid combos),
- **should-trip-a-check** cases (internally inconsistent, straightlining, speeding, duplicate-like).

Each scenario: `{id, title, persona, must_hit:{var:value}, expect:{reach:[...], block_at:[...]}, n_rows}`.
Write them to `<output_dir>/<survey>_cases.json` (the source of truth + a
reproducibility artifact the user can edit).

### 3. Confirm scope with the user (ask -- do not assume)
BEFORE deriving or generating, ask with `AskUserQuestion`:
1. **How many scenarios / interesting cases, and how much coverage** -- e.g.
   "a few (3-4, just the key paths)" / "moderate (6-8, main branches + common edges)" /
   "thorough (10+, exhaustive branch + sentinel coverage)". Use the answer to size how many
   scenarios you derive in step 2b (or which of a user-supplied set to run).
2. **Variations per scenario** (rows each) and how many stochastic **bulk** rows to add.

Then present the concrete scenarios (`AskUserQuestion` multiSelect: which to run), write the
accepted set to `<survey>_cases.json`, and run
`python bench_scenarios.py validate --cases <cases.json> --questions <questions.json>`,
fixing any FAIL before generating.

### 4. Generate (coherent cases + stochastic bulk -> one dataset)
`python bench_scenarios.py expand --cases <cases.json> --variations N --seed S --out <run_dir>`
writes one answer sheet per variation + `run_manifest.json` (deterministic seeds).

For each scenario you may **enrich** its sheets in-character: add coherent values
(consistent ages/relationships, plausible amounts) for substantive fields the
`must_hit` didn't pin -- but never for metadata/calculate (leave those to the
engine). For scale, run one fill subagent per scenario (or per variation): each
edits its sheet, then runs the engine. Fill each variation:

```bash
python main.py --survey <key> --phases synthetic --rows 1 \
    --seed <run.seed> --answers-file <run.answers_file> \
    --coverage-trace <run_dir>/<scenario>_v<NN>.coverage.json
```

**Repair rejected answers (do this after every fill).** Run
`python bench_scenarios.py rejections --trace <run_dir>/<scenario>_v<NN>.coverage.json`.
It lists any scripted answer the engine *refused* (value out of the question's choices/
constraint -- frequently a **dynamic choice_filter** that narrows the list per row, so a value
legal in the abstract is illegal for this respondent) and any un-evaluable gate. For each
rejection, fix the answer sheet -- pick a value valid under the filter, or set the filter's
input variables so the intended value becomes valid -- and re-run that variation. Repeat until
the trace is clean, or **accept-and-note** a residual: a value the form genuinely disallows for
that respondent is itself a finding, not something to force. (The engine never wrote the
illegal value; it fell back to a sampled one, so the CSV is always legal.)

Add the stochastic **bulk** (no answers file) as one more run, then concatenate
all per-run CSVs into the final `<survey>_synthetic.csv` (headers align because
the column contract is stable; pin roster sizes via `directives.repeat_counts`
if a scenario needs a fixed schema).

### 5. Report
`python bench_scenarios.py report --manifest <run_dir>/run_manifest.json --trace-dir <run_dir> --out <run_dir>/<survey>_bench_report.md`
produces per-scenario **PASS/FAIL** verdicts (a FAIL pulls the failing gate +
resolved operands from the trace, e.g. "expected the youth module but
`${age} >= 15` false (age=12)") plus a coverage CSV. Summarise the report for the
user, flag any REJECTED must-hit answers, and point them at the final CSV.

## Guardrails
- **Synthetic only.** Read form *metadata* (`questions.json`, `*_variable_graph.json`)
  and the human-authored cases file **only**. Never read real respondent data
  (`data/`, `*_WIDE.csv`, `*.dta`, `*.parquet`). Personas are fictional -- never
  transcribe a real respondent.
- **Reproducibility.** The stochastic tail is seeded (byte-reproducible on the
  same day). Your in-character enrichment is not -- so keep every generated
  answer sheet + the seed manifest; those plus the engine reproduce the run.
- **Ironclad, always.** Do not reach for `--force-value` to "make" a section
  appear; satisfy its gate by answering upstream. If a scenario can't reach its
  target, that's a finding (report it), not something to force past.
