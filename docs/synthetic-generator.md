# Synthetic export CSV generator

A synthetic-respondent generator that walks a SurveyCTO `.xlsx` instrument and
emits a wide-export CSV indistinguishable in shape from what SurveyCTO produces
once real submissions arrive. Designed for HFC and cleaning-pipeline dry-runs
**before** real data is collected — you can run your full Stata cleaning code
against the synthetic CSV and verify it parses, types correctly, evaluates skip
logic, and exercises every check before the first respondent is interviewed.

## TL;DR

```bash
# Generate questions.json first (required input)
python main.py --survey my_survey --phases json

# Then synth (default 5 rows)
python main.py --survey my_survey --phases synthetic --rows 20 --seed 42
```

Outputs: `<output_dir>/<survey>_synthetic.csv` and (when applicable) a
sibling `<survey>_synthetic.strip.log` listing expressions the evaluator
couldn't interpret.

## What it does

Per respondent the walker:

1. Iterates `questions.json` in order, maintaining a partial row dict.
2. Resolves repeat counts dynamically (the count variable's value is only known
   after upstream questions have been answered).
3. Composes effective relevance as `(all group_relevances ∧ own relevance)` and
   leaves the cell blank when false.
4. For `calculate` / `calculate_here` questions, evaluates the calculation via the
   shared expression evaluator. `calculate_here` with `duration()` advances a
   per-respondent counter so successive snapshots increase monotonically — useful
   for HFC checks that subtract module-start from module-end timestamps.
5. For other types, samples a type-correct value via `generators/sampling.py`,
   respecting any extractable constraint bounds and any narrowed choice list.
6. `select_multiple` is unrolled to the parent `"v1 v2"` cell **and** per-choice
   indicator columns — the exact wide-export shape SurveyCTO produces.
7. Inside a repeat group, variables are wide-suffixed (`var_1, var_2, …`) and
   per-iteration `index()` is exposed so constraint expressions that branch on
   iteration work correctly. **Nested repeats** are modelled fully: a field in
   `[outer, inner]` is expanded over both levels (`var_1_1, var_1_2, …,
   var_2_1, …`), each inner repeat resolves its own count per outer iteration
   (`inner_count_1, inner_count_2, …`), and the column header is the rectangular
   `max(outer) × max(inner)` grid SurveyCTO pads to. This mirrors the suffix
   logic the variable dictionary uses to reconstruct nested rosters from real
   data (`create_variable_dictionaries._build_repeat_tree`).

## CLI flags

| Flag | Default | What it does |
|---|---|---|
| `--survey {key,all}` | required | Survey to process. Must match a key in `config.SURVEYS`. |
| `--phases synthetic` | included in default `all` | Run the synthetic generator. (`json` must have been run already.) `--phases all` (the CLI default) runs `csv`, `json`, `sections`, **and** `synthetic`; explicit `--phases synthetic` runs only the generator. |
| `--rows N` | 5 | Number of synthetic respondents. |
| `--seed K` | 0 | RNG seed. Same `--seed` produces **byte-identical** CSV output across runs on the same day. |
| `--force-value VAR=VAL` | — | Force one or more variables to specific values, **bypassing relevance**. May be repeated, or comma-separate multiple pairs in one flag. Example: `--force-value c_consent=1,hh_consent=1`. |
| `--answers-file PATH` | — | Apply a JSON answer sheet whose values fill matching questions — but **only through open gates** (unlike `--force-value`, a scripted answer never bypasses relevance). Invalid values fall back to a sampled value. May be repeated (sheets merge). See [Scripted answers](#--answers-file-scripted-coherent-answers). |
| `--legacy-fail-open-relevance` | off (strict) | Restore the pre-ironclad behaviour where a relevance expression the evaluator can't interpret **shows** the question (fail open) instead of hiding it. See [Ironclad gating](#ironclad-gating). |

## Reproducibility

For a given `--seed K`, the output is byte-identical across reruns on
the same day (the date sampler is clamped to today, so reruns next year
would draw from a longer past-range — within a day, output is stable).
The seed is fed via `random.Random` string seeds, which use SHA-512
hashing under the hood, so determinism does **not** depend on
`PYTHONHASHSEED` and holds across processes and across Python 3.2+
versions. Each respondent gets a deterministic per-respondent seed
derived from the master seed; within that, two further RNGs split
run-context generation (timestamps, IDs) from question sampling, so
adding or removing a metadata field doesn't shift the byte-output of
question-level samples.

Verify with `md5sum`:

```bash
python main.py --survey my_survey --phases synthetic --rows 20 --seed 42
md5sum <output_dir>/my_survey_synthetic.csv
# Re-run — md5 must match
```

## Pulldata: obligatory when referenced

If your form has no `pulldata()` calls, this section doesn't apply — the
generator runs fine with zero CSVs configured.

If your form references `pulldata('foo', ...)` (in calculation,
relevance, constraint, or choice_filter) **or** a select uses a
`search('foo', ...)` appearance to pull its choices from a CSV, the
generator **must** find `foo.csv` in one of the configured
`pulldata_search_dirs`. Both are mandatory: missing CSVs raise
`MissingPullDataError` with the list of search directories. There's no
override flag — the simulation is essentially useless without the preload
(gated cascades won't populate, `search()` choice expansion can't resolve,
and pulldata-derived columns stay blank). A caller-ID / case-managed form
is the clearest case: the screening gates read `pulldata('cases', 'wave', …)`
etc., so **without `cases.csv` the survey body never opens**.

### Running on a specific case context (`--case-prefix` / `--case-ids-file`)

When the form is keyed on `${caseid}` (a `pulldata('cases', …, 'id', ${caseid})`
lookup), the generator draws caseids from that table. Restrict the pool to run
the simulation on a chosen case-management context:

```bash
# bench-test cases only -- their preload (wave, total_phones) drives the gates
python main.py --survey s --phases synthetic --case-prefix BT
# or an explicit id list (one per line) -- e.g. the exact cases a tester used
python main.py --survey s --phases synthetic --case-ids-file bt_ids.txt
```

Equivalently, an answer sheet may carry `"directives": {"case_pool": {"prefix": "BT"}}`
(or `{"ids": [...]}`) so the bench-test skill sets the case profile per scenario.
A filter that matches no caseid is a hard error (you asked for a context that
isn't in the table).

Configure search dirs per-survey in `config.py`:

```python
SURVEYS = {
    "my_survey": {
        "input_file": Path("path/to/My_Survey.xlsx"),
        # ...
        "pulldata_search_dirs": [
            Path("path/to/forms/My Survey-media"),  # the SurveyCTO form-media bundle
            Path("path/to/forms"),                  # fallback
        ],
    },
}
```

CSV filenames must match the bare name from the `pulldata()` call: a form
referencing `pulldata('cases', ...)` looks for `cases.csv` (not
`my_survey_cases.csv`).

## Dynamic choice lists: `search()` appearance

SurveyCTO's `appearance="search('csv', 'matches', col, ${var}[, col, val]*)"`
directive populates a select's choice list at runtime from a media-bundle
CSV. The static XLSForm choice list typically has just a placeholder row
plus a few sentinel rows (e.g. `0`, `-88`).

The generator parses the directive, loads the named CSV, and replaces the
placeholder with one entry per CSV row. The CSV's column **named** the same
as the static choice's `value` field becomes the actual value column;
similarly for `label`. Sentinel rows are preserved.

- **Column header** includes every possible CSV row's indicator column
  (no matches filter at header time) — matches the real export shape.
- **Per-respondent sampling** applies the matches-clause filter against
  the current row state so picks come from the rows valid for that
  respondent.

Affected question count (representative): ~40 in a typical IPA multi-module
evaluation form, ~3 in a smaller survey, 0 if the form has no `search()`
appearances.

## `caseid` sampling from pulldata

If the form does `pulldata('TBL', 'col', 'id', ${caseid})` anywhere (the
`${case_id}` underscore variant is also recognised), the generator
detects this and draws `caseid` values from `TBL.df['id']` — one per
synthetic respondent, without replacement when the pool is large enough,
deterministically off the master seed. This makes downstream pulldata
lookups (wave, surveyor, preloaded phone numbers, etc.) resolve to real
values instead of leaving those columns blank.

**Pool exhaustion**: when `--rows N` is larger than the caseid pool,
caseids are cycled (`pool[i % len(pool)]`). Multiple synthetic
respondents will share the same caseid, and the lookups against that
caseid return identical preloaded values. HFC checks that flag duplicate
caseids will fire on synthetic data when this happens — keep `--rows`
within the pulldata pool size if your HFC needs unique caseids.

**Multiple caseid-keyed tables**: when the form references more than one
`(table, key_col)` pair, the generator uses only the first pair seen in
form-order and warns to stderr about the rest. Merging pools across
tables would produce caseids unresolvable in some of them.

Falls back to a synthetic `case-NNNN` form when no caseid-keyed pulldata
reference exists.

## `--force-value`: bypass-relevance forcing

Many real surveys gate their substantive content behind a consent
cascade. With random sampling, only ~12.5% of respondents pass a
3-step yes/no gate, so the synthetic CSV would have most rows mostly
blank — not useful for HFC dry-runs.

`--force-value` fixes the listed variables to specific values **and**
bypasses their own relevance check, so the cascade populates regardless
of upstream sampling. Pre-seeded into the row dict at walk start so
forward references (a question early in the form whose relevance
references a forced variable that's defined later) resolve correctly.

```bash
# Force the whole consent cascade
python main.py --survey my_survey --phases synthetic --rows 20 \
    --force-value c_consent_qs_ans=1,c_consent_understand=1,c_consent=1

# Or with repeated flags (use this when values contain commas)
python main.py --survey my_survey --phases synthetic \
    --force-value c_consent=1 \
    --force-value hh_consent=1
```

## `--answers-file`: scripted, coherent answers

`--answers-file` is the **ironclad** counterpart to `--force-value`. Where
`--force-value` *bypasses* a gate to guarantee a cell populates,
`--answers-file` supplies answers that are applied **only when the deterministic
evaluator opens the gate** — so a scripted respondent is subjected to exactly
the skip logic a real person would hit. To reach a gated section you satisfy its
gate by answering the upstream questions, not by forcing past it.

This is what the bench-test skill uses to fill coherent "interesting cases": the
skill authors a whole-respondent answer sheet consistent with a persona, and the
engine fills the rest stochastically while guaranteeing the paths are real.

Sheet shape:

```json
{
  "answers": {
    "c_consent_qs_ans": "1",
    "hh_age": 34,
    "symptoms": "2 5",
    "member_age_2": 12
  },
  "directives": { "repeat_counts": { "members": 3 } }
}
```

- **`answers`** — map of variable name (or a suffixed key like `member_age_2`
  for repeat iteration 2) to value. Resolution per cell: `answers[suffixed_key]`
  > `answers[base_var]` > stochastic fallback. `select_multiple` values are the
  space-joined choice string (`"2 5"`).
- **Validation** — each value is checked against the question's choices /
  numeric bounds / text length. An **invalid** value is *not* written; the cell
  falls back to a sampled value (source `scripted_invalid_fallback`), so the CSV
  is never illegal.
- **`directives.repeat_counts`** — pin a roster size. The repeat's own relevance
  still gates it, so a pinned count on a gated-out repeat yields 0 iterations.
- **Gated-out answers are ignored** — an answer for a question the evaluator
  gates out never populates the cell (it is recorded, not forced).

```bash
python main.py --survey my_survey --phases synthetic --rows 1 \
    --seed 7 --answers-file interesting_case_01.json
```

## Ironclad gating

Relevance is evaluated by one deterministic authority (the walker's
`_is_relevant`), and neither the RNG nor a scripted answer can override it. Two
fidelity guarantees beyond the historical behaviour:

- **Strict fail-closed (default).** If a relevance expression cannot be
  evaluated (parse error / unsupported function), the question is **hidden** and
  the failure is recorded in the strip-log — rather than silently *shown*, which
  fabricated a cell real SurveyCTO would have blanked. Pass
  `--legacy-fail-open-relevance` to restore the old show-on-error behaviour.
- **Forward references resolve.** A gate that references a variable defined later
  in the form used to see a blank (single forward pass) and wrongly gate the
  cell out. The walker now re-evaluates gates to a fixpoint, so a gate reading a
  later answer resolves correctly. Sampled values are memoized, so this never
  perturbs the RNG stream; when a form has no forward references the output is
  byte-identical to a single pass.

## Type behaviour

Synth emits values as their natural evaluator output — string-typed
expressions stay string, numeric-typed stay numeric. Stata's
`import delimited` infers column types from content. For columns where
the project's import pipeline applies its own `tostring` / `destring`,
the conversion happens **on the project side**, not in the synth.

Type-concordance benchmarks against real SurveyCTO wide-export CSVs
(synth-vs-real-CSV, populated-both-columns). These compare against
internal IPA production exports that aren't in this repository; the
numbers are reproducible if you have access to the underlying real
`WIDE.csv` (and the same form's `*_questions.json`).

| Survey shape | Concordance |
|---|---:|
| Adult endline (~2.9k columns) | 99.9% |
| Household endline (~21k columns) | 98.5% |

Remaining mismatches are dominated by:

1. **Project-side `tostring` conventions** that affect the post-import
   `.dta` but not the wide CSV.
2. **Form-side oddities** where the XLSForm declares `type=text` but
   real respondents typed digits (synth honours the form's declaration;
   `import delimited` infers numeric from the digit content in the real
   CSV). The `numbers_decimal` / `numbers` / `numbers_phone` appearance
   hints are honoured — those text fields emit digit-only content to
   match form-designer intent (preserves leading zeros in phone numbers).

## Missing-value semantics: evaluator NaN vs Stata `.`

The expression evaluator and the Stata cleaning pipeline represent "missing"
differently, and they do **not** behave the same way in comparisons. This
matters when you reason about how one relevance expression evaluates in the
synth versus how the same logic evaluates after the SurveyCTO data is imported
into Stata.

- **In the evaluator** (`transformers/expression_evaluator.py`), a blank /
  unanswered numeric field is **NaN** in numeric context — `_to_number` maps
  `""` to `float('nan')`, matching XPath `number('')`. A comparison with NaN is
  **False in both directions** (with a blank `age`, both `${age} < 18` and
  `${age} > 18` are False), NaN arithmetic yields NaN, and a NaN result
  serialises to a **blank cell** (`_to_string`). This faithfully models
  SurveyCTO, where a skipped field neither satisfies a gate nor contributes a
  `0` to a sum. (Earlier versions coerced blank to `0`, which leaked through
  `< N` comparisons and `+` arithmetic — see issue #28.)
- **The one remaining, deliberate divergence** is division by zero: the
  evaluator returns NaN where XPath 1.0 returns ±Infinity (so `1 div 0 > 5` is
  False here, True in XPath). NaN is the safer downstream default; this is the
  only intentional numeric divergence left.
- **In Stata**, an unanswered numeric field is system missing `.`, which Stata
  treats as **positive infinity**: `. > 18` is **True**, `. < 18` is False. This
  is the exact trap documented in
  [`coding_guidelines/SURVEYCTO_RELEVANCE_TRANSLATION.md` §4](../coding_guidelines/SURVEYCTO_RELEVANCE_TRANSLATION.md#4-the-stata-missing-value-trap-for-numeric-comparisons),
  which is why `logic_converter.py` appends `& !missing(var)` to every `>`,
  `>=`, `<`, `<=` it translates.

Two consequences worth keeping in mind:

1. **The synth is structurally correct, not semantically Stata-equivalent.** A
   relevance gate may keep a row blank in the synthetic CSV (SurveyCTO/evaluator
   semantics) while the *translated Stata condition* would have included that row
   (or vice-versa) if the missing guard were absent. The synth mirrors how the
   form behaved at collection time; it is not a model of the downstream Stata
   `if` logic. Use it to verify dataset *shape* and that cleaning code *parses*
   and *types* correctly — not to predict Stata truth values for missing data.
2. **The converter does not translate missing-value handling for you.** Clauses
   it cannot translate are stripped and recorded in the strip log (see below),
   never silently reinterpreted. Likewise, when an expression hands the evaluator
   something malformed (for example a `regex()` pattern that fails to compile),
   the evaluator now **raises** rather than silently returning a value;
   `safe_evaluate()` then applies the caller's documented fallback (relevance
   defaults True, calculation defaults empty) and logs it, so a bad pattern shows
   up as a strip-log entry instead of silently marking every row "no match".

## Auto-metadata fields

The generator emits all SurveyCTO auto-generated export columns the way
real exports do:

| Column | Synth output |
|---|---|
| `KEY` | `uuid:<v4 UUID>` |
| `SubmissionDate` / `starttime` / `endtime` / `CompletionDate` | `MMM DD, YYYY HH:MM:SS AM/PM` |
| `formdef_version` | Real `settings.version` from the XLSForm settings sheet (falls back to a random YYYYMMDD if not set) |
| `deviceid` / `subscriberid` / `simid` / `devicephonenum` / `username` / `duration` / `caseid` | Synthesised from per-respondent run-context |
| `text_audit` (form type `text audit`) | `https://<host>/api/v2/forms/<form_id>/submissions/<KEY>/attachments/TA_<uuid>.csv` |
| `audio_audit*` (form type `audio audit`) | `https://<host>/api/v2/forms/<form_id>/submissions/<KEY>/attachments/AA_<uuid>_AFTER_<seconds>S.m4a` |
| `speed_violations_list` (form type `speed violations list`) | empty (matches real default when no violations occurred) |

URL placeholders:
- `<KEY>` is the full submission key in the form `uuid:<UUID>` (matches
  the `KEY` column value).
- `<uuid>` in the filename is the **bare** UUID (the `uuid:` prefix
  stripped).
- `<form_id>` comes from the XLSForm settings sheet's `form_id` field.
- `<host>` is currently a non-configurable placeholder
  (`synthetic.surveycto.com`). HFC code that parses real audit-URL hosts
  should treat this as opaque.

## `geo_bbox` for plausible geopoints

Geopoint fields are sampled within a bounding box. Default is the global
`(-90, 90, -180, 180)` which lands many synthetic submissions in the
ocean — fine for "geopoint exists" checks but breaks "GPS within enumeration
zone" HFC checks. Set a country bbox per survey in `config.py`:

```python
SURVEYS = {
    "my_uganda_survey": {
        # ...
        "geo_bbox": (-1.5, 4.2, 29.5, 35.0),  # (lat_min, lat_max, lon_min, lon_max)
    },
}
```

## Strip log

Expressions the evaluator can't interpret are recorded in
`<survey>_synthetic.strip.log` and skipped rather than failing the run.
Typical entries are SurveyCTO functions that require runtime state
(`phone-call-log()`, `collect-is-phone-app()`) or one-off form-side
syntax errors. The log is regenerated each run; absence of the file
means the run was clean.

## What it doesn't do

- **No 2-pass forward-reference resolution.** A question early in the
  form whose relevance references a variable not yet computed will
  evaluate against an empty value. The targeted workaround is
  `--force-value`, which is pre-seeded so the forced variable is visible
  to upstream relevance checks. A full 2-pass walker is on the wishlist.
- **No realistic-distribution choice sampling.** When the choice list is
  large (e.g. a 2,000-entry peer roster after `search()` expansion),
  picks are uniform over `1..len(choices)` — the median synthetic
  respondent picks ~1,000 peers, far more than a real respondent. Type
  concordance is fine; realism for downstream HFC outlier checks isn't.
- **No project-side type coercion.** If your Stata import script
  applies `tostring`/`destring` to specific columns, run the same
  conversion on the synthetic CSV when you import it. The synth produces
  the wide CSV; type drift between wide CSV and `.dta` is the import's
  responsibility.
- **Schema is not stable across different seeds.** The output column
  count depends on the maximum repeat-group iteration sampled in that
  run: a run that happens to sample `f_hr_rpt_count = 5` produces
  `*_1`..`*_5` columns for each variable in the repeat; a run that tops
  out at 2 produces only `*_1` and `*_2`. Same `--seed K` yields the
  same schema on the same day, but cleaning code that pins a fixed
  column list will break across seeds. Workaround: use
  `--force-value <count_var>=<N>` on each repeat-count driver to lock
  the schema, or pass a large `--rows N` so the rare high-count
  iterations are reliably sampled.

## Concordance verification (optional)

If you have a real SurveyCTO wide-export CSV (typically named
`<form>_WIDE.csv`), you can compare synth output against it column-by-column
to find shape divergences. See the comparison snippets in
`active/synthetic-csv-generator/pipelines/` notes for the analysis
patterns we use.

## Files

| File | What it does |
|---|---|
| `main.py:run_synthetic_phase` | Phase entry point, reads settings sheet for `form_id` / `version`, dispatches to `generate_synthetic_csv` |
| `generators/synthetic_data.py` | Walker, run-context builder, metadata-value emitter, search() expander, audit URL formatter, CSV writer |
| `generators/sampling.py` | Type-aware value sampling, numeric/text bound extraction, conditional select_multiple constraint parsing |
| `extractors/pulldata_loader.py` | Pulldata CSV discovery, indexed lookup, key normalisation |
| `transformers/expression_evaluator.py` | XLSForm-expression AST evaluator (most of `expressions.md` minus runtime-only functions) |

## Design history

Key design decisions and pipeline notes are captured in the commit
messages on this branch:

- `a2ac4d3` — pulldata `search()` choice expansion, `calculate_here` /
  `once(duration())` handling, audit-URL formatting, `caseid` pool
  sampling, force-value pre-seeding.
- `3d54f53` — making pulldata obligatory.
- `c86339f` / `7d0c3f4` / `351cb13` / `7108692` — earlier review-round
  fixes (select_multiple parent state, sm-in-repeat column emission,
  conditional exclusive-choice constraints).
