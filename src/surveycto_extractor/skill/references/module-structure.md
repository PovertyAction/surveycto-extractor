# Module structure, assertions and QC

Read this when starting a new cleaning module or reviewing one as a whole: how a module is laid out, what it asserts, and what it reports. Principles come first, then the Stata code.

## Contents

- [Module Organization](#module-organization)
- [Assertions](#assertions)
- [Two-Phase Workflow](#two-phase-workflow)
- [Survey Instrument Verification](#survey-instrument-verification)
- [QC Reporting Requirements](#qc-reporting-requirements)
- [Variable Labels as Data Lineage](#variable-labels-as-data-lineage)
- [Input Data Schema Assumptions](#input-data-schema-assumptions)
- [In Stata](#in-stata)
  - [Stata `assert` and `isid`](#stata-assert-and-isid)
  - [Verifying `.dta` Output Parity](#verifying-dta-output-parity)
- [Testing Guidelines](#testing-guidelines)

## Module Organization

### One Module, One Domain

Each cleaning module should answer one business question or handle one instrument section. When a module grows beyond ~300 lines or covers multiple unrelated domains, split it.

**Split by domain, not by operation.** For example, split a monolithic livestock module into `05a_lstock_basic_long` (reshape raw data), `05b_unit_prices` (compute median prices), `05c_animals_owned` (derive ownership counts), etc. — each focused on one analytical concept. Don't split by "first half" and "second half" of operations on the same construct.

### Naming Conventions for Split Modules

When splitting a module, use a letter suffix under the same numeric prefix: `05a_`, `05b_`, `05c_`. This preserves execution order while making the relationship clear. Include the domain in the filename (e.g., `05d_lstock_flows`, not `05d_part4`).

### Module Header Documentation

Every module must begin with a comment block documenting:

- **INPUTS**: All files loaded (with `${global}` paths), one per line.
- **OUTPUTS**: All files saved.
- **DEPENDENCIES**: Other modules whose output this module consumes.

Update headers whenever input files change. The header is the primary documentation for data lineage.

### Form-Version Coverage

When survey instruments were added or removed mid-fieldwork (e.g., a module added after the first N enumerators), document:

1. Which households have coverage (a flag or indicator variable).
2. How the module handles households without coverage (imputation, scaling, or structural missing).
3. What matched-household aggregates are used for imputation (prefer treatment-arm medians over global medians to preserve experimental structure).

### Multi-Source Variables

When the same construct can be measured from multiple survey sections (e.g., fruit income from both the crops module and the fruit-trees module), document:

1. All possible data sources and the hierarchy for deduplication.
2. The logic for households that report in only one source.
3. The prevalence of each case (how many HH fall into each branch).

## Assertions

### Philosophy

Use assertions to codify what must hold in the raw data for transformations to be sensible. If an assertion fails, the script should stop --- that is a feature, not a bug.

### Core Patterns

**Gateway zeroing**: Values must be 0 when gateway = No and non-zero only when logically allowed.

**Structural missing**: Assert variable is missing exactly where skip applies; conversely assert presence where asked.

**Uniqueness / ID integrity**: Verify ID uniqueness early and after reshapes/merges.

**Range / domain**: Categorical in allowed set; proportions between 0 and 1; naturally positive variables.

**Mutual exclusivity**: Ensure branches or flags don't overlap if they shouldn't.

**Non-negative assertions as sentinel guards**: After computing any variable that represents a non-negative quantity (income, assets, sales revenue, hours worked, counts, land area), add `assert var >= 0 if !missing(var)`. This catches unrecoded sentinel codes (e.g., -99, -88) that survived into derived variables. Place these assertions immediately after the variable is created or transformed, before it flows into downstream aggregation. **Cross-module caveat**: If sentinel recoding happens in a different module than where the variable is first created, place the assertion after the recode, not at the point of creation — otherwise the assertion fires on raw sentinel values that are about to be cleaned.

## Two-Phase Workflow

### Principle

Separate validation into two phases to ensure transformations rest on verified raw data.

#### Phase A --- Validate raw data BEFORE you touch it

1. Check raw invariants that should hold straight from the instrument:
   - Positive values where expected (e.g., loan sizes).
   - Categorical values within allowed domain (e.g., loan type in {1, 2, 3}).
   - Gateway logic: if household reports N items, the first N detail records must be present.
   - Structural skip: downstream variables missing where skip applies.
2. Verify the do-file's skip logic against the survey instrument (see [Survey Instrument Verification](#survey-instrument-verification)).

#### Phase B --- Validate AFTER/WHILE transforming

1. Perform transformations (recode special missings, zero-by-logic, generate totals).
2. Add tolerance/proportion assertions with rationale comments.
3. If an assertion fails, inspect contradictions, refine logic or code, repeat.

## Survey Instrument Verification

Before trusting or modifying a cleaning module's skip-logic implementation, verify it against the actual survey instrument. Do-files often encode survey logic implicitly through `if` conditions, gateway variables, and scoping filters --- but these can drift from the instrument through copy-paste errors, misunderstood variable names, or incomplete documentation.

### What to verify

1. **Gateway cascade**: Trace the full chain of relevance conditions from the survey instrument and confirm the do-file implements the same chain. For example, if the survey gates a section on `random_module < 0.5`, then gates questions on `received_support == 1`, then gates amount questions on `support_type_cash == 1`, the do-file must enforce all three levels.

2. **Not-asked vs. zero**: When a question is skipped by survey logic, the value in Stata is missing (`.`), not zero. If the do-file replaces these structural missings with zero, verify this is a deliberate zero-by-logic decision (the gateway definitively implies zero) rather than an error.

3. **Valid sentinel codes per question and variable suffix mapping**: Survey constraints define which sentinel codes are valid for each question. If the do-file recodes a sentinel code not in the constraint, flag it as defensive or investigate whether the data actually contains that value. For repeat groups, confirm that variable suffixes match the expected iteration-to-item mapping — in SurveyCTO, repeat iteration numbers may differ from choice codes.

4. **Scope conditions**: Verify that aggregation operations (e.g., `rowtotal`, `egen`) are correctly scoped to the eligible population (e.g., `if support_mod == 1`). Missing scope conditions produce false zeros for out-of-scope observations.

### How to verify

- Use your project's survey expert skill or read the section JSON files in your project's survey documentation directory to look up relevance conditions, choice lists, and constraints.
- For questions not covered by documentation, read the original XLSForm directly.
- Some surveys use `pulldata()` from external CSVs for choice lists — check companion CSV files.
- For ambiguous cases, inspect the raw `.dta` file to see what values actually appear.

### When to verify

- When reviewing a refactored module (any PR that changes skip-logic-related code).
- When a parity test shows unexpected changes in observation counts (could indicate a scoping error).
- When a do-file uses complex nested conditions that are hard to trace without the instrument.

## QC Reporting Requirements

### Module-End Audit

Every cleaning module must end with a summary of the output dataset:

- Full variable descriptions (names, types, labels).
- Summary statistics for all variables (mean, min, max, count, missing count).

This output appears in the log and serves as a regression test: if a code change alters the dataset structure or value distributions, it will be visible in the log diff.

### In-Module Diagnostics

Throughout each module, include targeted summaries at key checkpoints:

- After loading data: confirm row count and key variable types.
- After sentinel code recoding: summarize affected variables to confirm recodes applied correctly.
- After aggregate creation: summarize the aggregate and its components.
- After zero-by-logic fills: summarize to confirm expected value distributions.

### What to Review in Logs

When reviewing a module's log output, check for:

- **Warnings**: Any "WARNING:" messages printed by the module (skip-logic violations, tolerance checks).
- **Replace counts**: Stata's "X real changes made" messages --- unexpected counts indicate a problem.
- **Assertion failures**: If the script completed, all assertions passed, but review the log to understand what was checked.
- **Summary statistics**: Look for unexpected min/max values, high missing counts, or implausible means.
- **TODOs and developer comments**: Notes left for future work.

### QC Tables

For modules with Other-specify classification or complex categorical mappings, produce a QC table (see [Other-Specify Classification > QC Table Requirement](other-specify.md#qc-table-requirement)). Surface these tables to the user for review.

### Contract Tables

When creating new binary indicators from multi-select or categorical variables, produce a frequency table (contract) showing the distribution. This confirms the indicator was constructed correctly and provides a baseline for future data comparison.

## Variable Labels as Data Lineage

Use variable labels to document where each variable comes from and how it was constructed. This is especially important for:

- **Imputed variables**: Label must say what was imputed and from where (e.g., `"Asset value (imputed via arm×stratum median where short survey)"`).
- **Hybrid variables**: When a variable draws from multiple sources, the label should say so (e.g., `"HH revenue: survey Q5 (food) + median price × Q3_qty (non-food)"`).
- **Variant suffixes**: When `_p1`, `_p2`, `_sh`, `_imp` suffixes distinguish versions, the label must explain the distinction — the suffix alone is not self-documenting.

### Rules

1. **Never leave a generated variable unlabeled.** Every `gen` or `egen` that creates an output variable must be followed by a `label var` statement.
2. **Include the module reference** in labels for cross-module variables: `"Net income (see 20_net_income.do)"`.
3. **Mark imputation explicitly**: Use words like "imputed", "median-filled", or "scaled" in the label text.

## Input Data Schema Assumptions

### Principle: Verify Then Assume Immutability

When writing a cleaning module, first research the input data to determine what variables exist, their types, and their value domains. Once verified, **code as if the schema is immutable** --- do not use defensive wrappers (e.g., error-suppressing captures, "drop if exists" patterns) to handle schema changes silently.

### Rationale

If the input schema changes (e.g., a variable is renamed, removed, or changes type), the cleaning module **should break loudly** so the change is noticed and the code is updated deliberately. Silent defensive code masks upstream changes that could affect data quality.

### In Practice

- **Drops**: If a variable was verified to exist in the input data, drop it directly without error suppression. If the upstream schema changes and removes it, the script will fail, surfacing the change.
- **Type conversions**: If a variable was verified to be string, convert it directly. Do not wrap in error-suppressing constructs.
- **Variable existence**: Do not use "capture confirm variable" to check if a variable exists before using it. If the variable should exist per the instrument, use it directly and let the script fail if it doesn't.
- **Variable selection — no star wildcards**: All `*` wildcards in variable selection and processing statements (`use`, `keep`, `drop`, `recode`, `rename`, `foreach`, `ds`) must be replaced with explicit single-character (`?`) or two-character (`??`) patterns, or with explicit variable lists via locals. The `*` wildcard silently catches unintended variables when new variables are added to a dataset. The `?` wildcard is acceptable because it matches exactly one character (intentional, predictable width).

  **Composite suffix wildcards**: Variables with multiple suffix dimensions may require `*` if the suffix structure is too complex for `?` patterns. In these cases, retain the `*` with a comment documenting exactly which variables it matches and why `?` patterns are insufficient.

The exception is variables whose presence depends on *data content* rather than schema (e.g., a reshape may produce different stubs depending on how many repeat groups have data). In those cases, conditional checks are appropriate.

## In Stata

### Stata `assert` and `isid`

```stata
* Gateway zeroing
assert loan_owned_size == 0 if loan_owned != 1 & !mi(loan_owned)

* Structural missing
assert mi(loansize_12m_2) if loan_cashinkind_12m_2 == 2 & loan_count_12m >= 2

* Uniqueness
isid hhid
isid hhid loan_index  // after reshape

* Range / domain
assert inlist(loan_cashinkind_12m_1, 1, 2, 3) if !mi(loan_cashinkind_12m_1)
assert loansize_12m_1 > 0 if !mi(loansize_12m_1)
```

### Verifying `.dta` Output Parity

To confirm a refactor didn't change a `.dta` output, compare old vs new using `datasignature` (after sorting on the key) and optionally `cf _all`. Note: `cf` does not support `id()`, so ensure both datasets are identically sorted (or save a sorted tempfile) before running `cf`.

**Caveat: `rowtotal` masks intermediate changes.** `rowtotal` (without `, missing`) treats individual missings as 0. If a refactor changes a component from 0 to missing (or vice versa), the row total is identical — output parity checks will not detect the change. Always pair output comparison with line-by-line code review when reviewing refactored modules that use `rowtotal`.

## Testing Guidelines

### Validation Approach

There is no automated test suite. Validation is done via:

- Stata `assert` / `isid` checks embedded in each module (the pipeline halts on failure).
- Log review: after running, scan per-module logs for `r(#)` errors, unexpected replace counts, and WARNING messages.
- Baseline comparison: optionally maintain a known-good snapshot of key output variables and compare against it after refactoring.

### Test Isolation

When writing tests that include shared program files, start the test do-file with `program drop _all` (after `clear`) to prevent `r(110)` program redefinition errors when running multiple tests in sequence:

```stata
clear
program drop _all
set more off
```

### Global Checking

Do NOT use `capture confirm global name` — this is invalid Stata syntax. Use string comparison:

```stata
* Correct:
if "$analysis_data" == "" {
    global analysis_data "${project_root}/data/for_analysis"
}

* Wrong (invalid syntax):
capture confirm global analysis_data
if _rc { ... }
```

### Data State Management

Use `preserve` and `restore` when tests load auxiliary datasets (like threshold files) to ensure the original dataset is available for subsequent steps:

```stata
preserve
use "`thresh_file'", clear
* ... work with threshold data ...
local some_value = p99
restore
* Original data is back
```

### Log Management

Do NOT use `capture log close` in individual test files when running under a test runner, as it interferes with the runner's logging. The test runner manages logs centrally.

### Collinearity Checks

When checking if a variable was dropped due to collinearity, inspect `_se[var]` (it will be 0 or missing) rather than relying on `colnames e(b)`, as Stata often retains omitted variables in the matrix with zero coefficients:

```stata
* Correct:
local x2_se = _se[x2]
if `x2_se' == 0 {
    di "x2 was dropped due to collinearity"
}

* Less reliable:
local colnames : colnames e(b)
local x2_dropped = !strpos("`colnames'", "x2")  // May incorrectly report present
```
