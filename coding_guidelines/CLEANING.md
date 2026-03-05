# Data Cleaning Guidelines

Language-independent principles for cleaning survey data. For Stata-specific syntax and
implementation, see [STATA.md](STATA.md).

## Table of Contents
- [Module Organization](#module-organization)
- [Assertions](#assertions)
- [Two-Phase Workflow](#two-phase-workflow)
- [Survey Instrument Verification](#survey-instrument-verification)
- [Zero-by-Logic](#zero-by-logic)
- [Short-Survey Dual Versioning](#short-survey-dual-versioning)
- [Structural vs. Response Missing](#structural-vs-response-missing)
- [Sentinel Code Mapping](#sentinel-code-mapping)
- [Missing Value Tracking](#missing-value-tracking)
- [Aggregate Creation with Missing Handling](#aggregate-creation-with-missing-handling)
- [Cross-Variable Consistency Checks](#cross-variable-consistency-checks)
- [Tolerance Specification Guidelines](#tolerance-specification-guidelines)
- [Skip-Logic Violation Resolution](#skip-logic-violation-resolution-gate-vs-downstream)
- [Other-Specify Classification](#other-specify-classification)
- [QC Reporting Requirements](#qc-reporting-requirements)
- [Variable Labels as Data Lineage](#variable-labels-as-data-lineage)
- [Select-Multiple Decomposition](#select-multiple-decomposition)
- [Reshape Safety](#reshape-safety)
- [Deterministic Joins](#deterministic-joins)
- [Final Merge Cardinality Checks](#final-merge-cardinality-checks)
- [One-Off Data Corrections](#one-off-data-corrections)
- [Input Data Schema Assumptions](#input-data-schema-assumptions)

---

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

---

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

---

## Two-Phase Workflow

### Principle
Separate validation into two phases to ensure transformations rest on verified raw data.

**Phase A --- Validate raw data BEFORE you touch it**
1. Check raw invariants that should hold straight from the instrument:
   - Positive values where expected (e.g., loan sizes).
   - Categorical values within allowed domain (e.g., loan type in {1, 2, 3}).
   - Gateway logic: if household reports N items, the first N detail records must be present.
   - Structural skip: downstream variables missing where skip applies.
2. Verify the do-file's skip logic against the survey instrument (see [Survey Instrument Verification](#survey-instrument-verification)).

**Phase B --- Validate AFTER/WHILE transforming**
1. Perform transformations (recode special missings, zero-by-logic, generate totals).
2. Add tolerance/proportion assertions with rationale comments.
3. If an assertion fails, inspect contradictions, refine logic or code, repeat.

---

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

---

## Zero-by-Logic

Whenever the value of a downstream variable can be inferred from a skip pattern or gateway, fill in the logically implied value rather than leaving it missing.

**When to apply**: The gateway definitively determines the downstream value. For example:
- "No animals owned" implies `has_<animal_type> = 0` for all animal types.
- "No loans received" implies `loan_size = 0`, `loan_balance = 0`.
- "Did not purchase any inputs" implies all purchase indicators = 0.

**When NOT to apply**: The gateway says "no" but the downstream question is about something the gateway doesn't determine. In that case, the value is genuinely missing.

**Multi-level zero-by-logic**: When skip patterns are nested (e.g., "bought any inputs?" -> "bought fertilizer?" -> "bought brand X?"), apply zero-by-logic at each level:
1. First, set all downstream variables to 0 if the top-level gateway is off.
2. Then, set item-specific downstream variables to 0 if the item-level gateway is off but the top-level gateway is on.

This ensures every constructed outcome has the same observation count as its gateway variable (excluding missing gateways).

---

## Short-Survey Dual Versioning

When a survey has long and short forms that collect different levels of detail, create **two versions** of every aggregate variable:

1. **Main version** (`var`): Contains the full-detail value for long-survey respondents. Set to structural missing (e.g., `.s` or `.m`) for short-survey respondents who were not asked the detail questions.
2. **Short-survey version** (`var_sh`): Contains the imputed or partial value available for short-survey respondents (e.g., from gateway questions only). Set to missing for long-survey respondents.

### Why Two Versions

A single variable that mixes long- and short-survey values conflates different measurement precision levels. Keeping them separate lets downstream analysis choose: use only long-survey (more precise, smaller N) or combine (larger N, noisier).

### Rules

1. **Always guard conditional fills with multiple conditions.** When filling zeros for short-survey HH, require both the short-survey flag AND the relevant gateway:
   ```
   set to 0 only if: (short_survey == 1) AND (hh_gate == 0)
   ```
   This prevents overwriting genuine missing values from form-version gaps.

   **Additionally, always include a missing-check condition** when zeroing short-survey variables. If an upstream module has already set a value (e.g., a binary indicator derived from gateway data), a blanket zero-fill overwrites it:
   ```
   WRONG:  set to 0 if (short_survey == 1)           -- overwrites legitimate values
   RIGHT:  set to 0 if (short_survey == 1) AND missing(var)  -- only fills genuinely missing
   ```

2. **Label both versions** to distinguish their provenance: `"Total X (long survey only)"` vs. `"Total X (short survey, imputed from gateway)"`.

3. **Never silently combine** the two versions in downstream modules. If a final merge or analysis step needs a single variable, create it explicitly with a documented rule (e.g., "use long-survey value when available, fall back to short-survey").

---

## Structural vs. Response Missing

Distinguish between two fundamentally different reasons a variable can be missing:

- **Structural missing**: The question was *not asked* because of skip logic. The respondent was never meant to answer. Example: "in-kind loan size" is structurally missing when the loan is cash-only.
- **Response missing**: The question *was asked* but the respondent could not or would not answer. Represented by sentinel codes like don't-know (-99) or refused (-88).

### Design Principles

1. **Create explicit flags** to record each reason separately (e.g., `mi_dk_var`, `mi_rf_var`, `mi_struct_var`).
2. **Assert structural missing implies missing content**: If the skip condition applies, the downstream variable must be missing.
3. **Assert non-structural paths supply data**: If the question was asked, the variable should be non-missing (subject to sentinel-code tolerances).
4. **Never conflate the two**: A structurally missing variable should not have the same missing code as a "don't know" response. Use extended missing values or separate flags.

---

## Sentinel Code Mapping

Survey instruments encode special responses as negative integers. These must be systematically recoded to proper missing values before any analysis.

### Standard Codes

| Code | Meaning | Target |
|------|---------|--------|
| -99 | Don't know | Missing (dk) |
| -88 | Refused | Missing (rf) |
| -77 | Not applicable | Missing (na) |
| -66 | Other (specify) | Keep or reclassify (see [Other-Specify Classification](#other-specify-classification)) |
| -98 | Don't remember | Missing (dk) |

### Workflow

1. **Document before recoding**: Before recoding any sentinel codes, summarize or tabulate the variable to record which codes are present and their frequency.
2. **Assert coverage**: All negative values should be accounted for by known sentinel codes. If `var < 0`, it should equal one of the known codes --- otherwise there is an unknown code.
3. **Assert tolerance**: The share of sentinel codes should be below a documented threshold (see [Tolerance Specification Guidelines](#tolerance-specification-guidelines)).
4. **Recode systematically**: Map all applicable sentinel codes in a single pass per variable or variable group. Do not recode only -99 when -88 and -77 are also present in the instrument.
5. **Recode before arithmetic that transforms values**: Integer sentinels must be recoded to missing *before* any arithmetic that changes their magnitude (especially division/reciprocal). `1/(-55)` produces `-0.018` — a real-looking negative value that a later recode on the derived variable will never match. Multiplication and addition are less dangerous (the result is still large/negative, so recode or non-negative assertions can catch it), but division and modular arithmetic can produce values in the plausible range. When sentinel recoding and arithmetic happen in different modules, ensure the recode runs first or add a non-negative assertion on the derived variable to catch leaks.
6. **Handle -66 separately**: Other-specify codes require classification before recoding (see [Other-Specify Classification](#other-specify-classification)).

**Mixed sentinel formats and type conversion**: Some data collection platforms partially convert integer sentinels to Stata extended missing (`.d`, `.r`) during field data collection — some variables may have -99/-88 while others already have `.d`/`.r`. Always check for both integer sentinels and pre-existing extended missing. Survey exports may also deliver the same variable as string in one batch and numeric in another. Before recoding, ensure all target variables are numeric. Do not use error-suppressing wrappers (e.g., `capture destring`) that silently swallow failures — if conversion fails, the script should stop so the cause can be investigated.

### Consistency Across Modules
Recode according to what the instrument defines for each question type. Different question types may use different sentinel codes (e.g., -98 "don't remember" for brand recall vs. -99 "don't know" for amounts). Check the instrument to determine which codes are valid for each variable, and recode exactly those. This prevents both missing a valid code and recoding a code that doesn't apply.

### Completeness Within a Module
Every numeric variable loaded by a module that accepts sentinel codes per the survey instrument must have an explicit recode. It is not sufficient to recode the "main" variables (e.g., quantities) while leaving related variables (e.g., monetary amounts, conversion factors) unrecoded in the same module. When reviewing or writing a module:
1. **List all numeric variables** loaded from raw data (the `use ... using` statement).
2. **Check each one** against the survey instrument for its constraint or choice list.
3. **Add recodes** for any variable whose instrument definition allows sentinel codes but has no recode in the do-file.
4. **Add a comment** for variables confirmed to have no sentinel codes in their instrument definition (e.g., "choice list yesno: valid 0/1, no sentinels").

Common patterns where recodes are missed: payment/income variables recoded in one section but not another; balance/amount variables after a reshape; conversion factors that feed into division (see step 5 above).

### Survey Constraint Bugs
SurveyCTO constraints may be incomplete — e.g., `.<=7` instead of `(.>=0 and .<=7) or .=-99 or .=-88`. A missing lower bound allows any negative number (including sentinel codes and typos) to pass validation during data collection. When you encounter unexpected negative values that are not standard sentinel codes, check the instrument constraint. If the constraint lacks a lower bound or explicit sentinel allowances, the value is likely an enumerator typo that passed the defective constraint. Recode it to the most plausible extended missing code.

---

## Missing Value Tracking

### Principle
Create dummy variables to record the specific reason a variable is missing. This preserves information that would otherwise be lost when recoding sentinel codes to a generic missing value.

### Flag Design
For each variable with meaningful missing patterns, create:
- `is_mi_var`: Overall missing indicator (1 if missing for any reason).
- `mi_dk_var`: Don't know (originally -99).
- `mi_rf_var`: Refused (originally -88).
- `mi_na_var`: Not applicable (originally -77).

Create these flags **before** recoding sentinel codes to missing, since the sentinel code values are needed to populate the flags.

### When to Create Flags
- Create flags when the tolerance assertion for a sentinel code used a threshold **>5%**. Below 5%, the sentinel code is rare enough that separate flags add variable clutter without analytical value.
- Also create flags when the analysis plan explicitly distinguishes between "don't know" and other forms of missing, regardless of share.
- Skip flag creation for the majority of variables where sentinel codes are well under 5%.

### Scope: Full Sample, Unconditional
Even when the tolerance assertion was computed on a subpopulation (e.g., "share of dk among respondents who reported having loans"), the missing-reason flags must be computed on the **full sample** (unconditionally). This ensures downstream analysis can filter by missing reason without needing to reconstruct the subpopulation logic.

---

## Aggregate Creation with Missing Handling

When combining multiple variables into a single aggregate (e.g., summing sub-scores, computing totals), missing-value propagation must be handled deliberately.

### Rule: Strict Propagation Only

If *any* component is missing, the aggregate must be missing. A partial sum is misleading --- it understates the true value and introduces systematic bias toward zero.

- **Implementation (few components, <=5)**: Use direct addition (`a + b + c`) which naturally propagates missing.
- **Implementation (many components)**: Use a row-total function to compute the sum, then check whether any component is missing and set the aggregate to missing if so.
- **Example**: Kessler-6 score = sum of 6 recoded items. If any item is don't-know/refused, the total is missing.
- **Example**: FCS score = weighted sum of 9 food groups. If any group is missing (after sentinel code handling), the total is missing. If a respondent truly didn't consume a food group, the instrument records 0 --- a missing value means the question was not answered, not that consumption was zero.

Do **not** use `egen rowtotal(...), missing`. Its semantics (missing only when *all* components are missing) conflict with strict propagation and are easily misinterpreted.

### What About "All Components Missing"?

Strict propagation handles this naturally: if all components are missing, the aggregate is missing (since *any* component is missing). No special handling is needed.

### When a Component "Should Be Zero"

If a component is missing but you believe the true value is zero (e.g., a food group not consumed), that is a **zero-by-logic** decision, not a missing-propagation decision. Apply zero-by-logic *before* aggregation to fill the zero explicitly. Then the aggregate will include the zero and strict propagation works correctly.

### Document the Choice
Every aggregate must have a comment explaining which missing-propagation approach is used and which flag drives the missing (e.g., "missing if any Kessler item is dk/rf").

### Post-Aggregation Validation

After collapsing detail records into household-level totals, validate that the aggregation preserved expected properties:

1. **Row count check**: Assert the collapsed dataset has the expected number of observations (e.g., one per household).
2. **Component sum check**: For aggregates built from sub-components, spot-check that the total equals the sum of parts for a sample of observations.
3. **Range check**: Assert the aggregate falls within a plausible domain (e.g., annual income > 0 for households that reported any income source).

Without these checks, silent upstream changes (e.g., a recoding that shifts a few items between categories) can propagate undetected through aggregation.

---

## Cross-Variable Consistency Checks

### Principle
When one variable logically constrains another (e.g., a gateway determines whether a follow-up was asked, or a count determines how many detail records exist), validate the relationship explicitly.

### Common Patterns

**Gateway-to-frequency validation**: If a binary occurrence variable says "no" (e.g., "did not experience food insecurity"), the associated frequency variable must be 0. Assert this and fill where needed.

**Count-to-detail harmonization**: When a headline count variable (e.g., "number of loans received") disagrees with the number of populated detail records, decide which is authoritative:
- If detail records are more numerous, the count was likely under-reported --- harmonize upward.
- If the count is larger, some detail records were skipped --- keep the count but mark the missing details.
- Assert and document the number of cases requiring harmonization.

**Computed-vs-reported validation**: When the survey platform computes a variable (e.g., a calculated total), compare it against your independently computed version. Assert they agree within a tolerance (e.g., differ by <0.1 in fewer than 1% of cases).

**Cross-section consistency**: When the same household appears in multiple modules, key identifiers and structural variables (e.g., treatment arm, strata) must match. Merge and assert consistency.

### Ordinal Categorization

When constructing ordinal categories from multiple conditions (e.g., food insecurity severity), two approaches exist:

**Preferred: Mutually exclusive conditions.** Define each category with conditions that cannot overlap by construction. Then assert: (a) every observation is assigned exactly one category (exhaustiveness), and (b) no observation meets conditions for two categories (mutual exclusivity). This is self-documenting and order-independent.

**Acceptable when required by methodology: Sequential overwrite ("severity-wins").** Some standard indices (e.g., HFIAS) define categories with intentionally overlapping conditions, where the most severe category wins. In this case, assign categories in increasing severity order so the last `replace` wins. When using this pattern:
1. **Document that order matters** --- a comment explaining why sequential overwrite is used (cite the methodology).
2. **Assert coverage** --- after categorization, assert that the share of uncategorized observations is below the documented missing tolerance.
3. **Do not generalize** --- use this pattern only when the external methodology requires it, not as a default approach.

---

## Tolerance Specification Guidelines

### Principle
When raw data contains violations of expected patterns, quantify the deviation, print a warning, and assert the share is below a documented tolerance. This makes data quality visible without halting the pipeline for minor issues.

### Default Tolerance: 1%
Use 1% as the default tolerance unless there is a documented reason to deviate. This means: violations must affect fewer than 1% of observations in the relevant denominator.

### When to Use Higher Tolerances

| Tolerance | When | Example |
|-----------|------|---------|
| 1% | Default for skip-logic violations, sentinel code shares, cross-variable inconsistencies | "<=1% of gate-off observations have populated follow-ups" |
| 1.5% | Don't-know shares on well-understood questions | "<=1.5% don't know on loan interest rate" |
| 2% | Don't-know shares on harder-to-answer questions | "<=2% don't know on business sales" |
| 5--10% | Structural data quality issues from known instrument problems | "<=10% missing frequency when occurrence is present" (known form logic gap) |
| >10% | Only with explicit documentation of a known instrument defect or design choice | "<=15% of in-kind loans have 'other specify' item type" |
| >25% | Extremely rare; indicates the variable may not be usable | "<=50% don't know on total installment payment" --- flag for potential exclusion from analysis |

### Conditional / Sub-Question Variables
Variables asked only under a skip-logic gate (e.g., "which shop?" when seller == Shop) will have high sentinel-code shares if you divide by the full sample. Always compute shares using the **eligible denominator**:

- Main/gateway variable: denominator is observations that should answer the main question.
- Conditional sub-question: denominator is observations that satisfy the parent gate.

By default, keep formal share assertions on gateway variables and at least log warnings for sub-questions. If a sub-question has a strong instrument expectation, assert using its gate-eligible denominator.

### Requirements
1. **Always assert with shares, not absolute counts.** An assertion like `assert bad_count < 20` is fragile --- it breaks when sample size changes. Instead, compute `bad_count / denominator` and assert the share is below the tolerance. Print a warning with both the count and the share.
2. **Always count against the relevant denominator**, not total observations.
3. **Comment the tolerance** with a brief rationale: why this threshold, and what instrument behavior or data pattern justifies it.
4. **Print a warning** when violations are found, even if within tolerance, so they appear in the log for review.
5. **Update tolerances** when new data arrives. A tolerance set at 1% for pilot data may need adjustment for the full sample.

---

## Skip-Logic Violation Resolution (Gate vs. Downstream)

When a gate says "off" but downstream follow-up data exists (or vice versa), resolve the conflict using the **follow-up fill rate rule**:

1. **Count violations** against the **relevant denominator** (gate-off obs, not total obs).
2. **Assert tolerance**: Violations must be <=1% of the relevant denominator.
3. **Determine fix direction** using the 75% rule:
   - Count how many of the expected follow-up questions (respecting nested skip patterns) are filled with valid-domain values.
   - If **>=75% of follow-ups are filled**: **Flip the gate on** (the enumerator likely answered questions then changed the gate afterward).
   - If **<75% of follow-ups are filled**: **Enforce the current gate** (zero/missing out the sparse downstream data; the enumerator likely started, realized the mistake, and corrected the gate).
4. **Document the decision** in a comment explaining what was done and why.

### Missing Propagation
When information is genuinely unknown (e.g., "Other" selected but specify-text is blank, or "don't know" codes), the constructed outcome must be **missing**, not defaulted to a value. Blank text fields are the string equivalent of missing numeric values --- they mean "we don't know."

---

## Other-Specify Classification

When a categorical survey question includes an "Other (specify)" option with a free-text field, use keyword matching to classify the text into the same categories as the coded responses.

### Principles

1. **Blank text = missing.** If the respondent selected "Other" but left the text blank, the constructed outcome must be **missing** --- we cannot determine the category.
2. **Unmatched text = missing.** If the text doesn't match any known keyword pattern, the constructed outcome is missing. Do not default to any category.
3. **Ambiguous text = missing.** Keywords like "mixed", "combination", "temporary" indicate we cannot cleanly classify. Set to missing.
4. **Responses describing absence or inapplicability map to the substantively correct category**, not missing. For example, if asked about roofing material and the response is "has no house" or "tent", the respondent clearly does not have improved roofing --- set to 0 (unimproved), not missing. The key distinction: we *do* know the answer; it's just not a standard material.

### Implementation Pattern

1. Lowercase and trim the text.
2. Define keyword regex patterns by category, building from domain knowledge of the survey instrument. Include standard responses AND absence/inapplicability responses in the substantively correct category.
3. Generate match flags for each category.
4. Apply priority ordering (see below) to resolve conflicts.
5. Map to constructed outcome: only clear keyword matches get a value; everything else stays missing.

### Priority Ordering

When a text matches multiple keyword lists (e.g., "iron and mud"), use this priority:
1. **Ambiguous** overrides everything --- if the text signals mixed/unclear, set missing.
2. **Improved** wins over unimproved --- if both match but not ambiguous, the improved material is likely the primary one (e.g., "cement over mud" = cement roof).
3. **Unimproved** --- only if no improved or ambiguous match.
4. **Unknown** --- no match at all, or blank text.

### Absence/Inapplicability Responses

Respondents sometimes answer "Other" with text that doesn't describe a material but describes a situation: "has no house", "lives with relatives", "tent", "under construction". These are **not missing** --- they carry information. Map them to the substantively correct category:
- For improved-vs-unimproved indicators, these are clearly **unimproved** (or "no proper housing").
- Add keywords for these patterns to the appropriate regex.
- When new data arrives, review the QC table for new absence patterns and update the regex.

### QC Table Requirement

Always produce a QC table showing each unique Other-specify text and its mapping. This table must appear in the log so it can be reviewed. It shows each unique text (trimmed), the assigned mapping category, and the count of observations with that text, sorted by frequency (descending) for easy review.

---

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
For modules with Other-specify classification or complex categorical mappings, produce a QC table (see [Other-Specify Classification > QC Table Requirement](#qc-table-requirement)). Surface these tables to the user for review.

### Contract Tables
When creating new binary indicators from multi-select or categorical variables, produce a frequency table (contract) showing the distribution. This confirms the indicator was constructed correctly and provides a baseline for future data comparison.

---

## Variable Labels as Data Lineage

Use variable labels to document where each variable comes from and how it was constructed. This is especially important for:

- **Imputed variables**: Label must say what was imputed and from where (e.g., `"Asset value (imputed via arm×stratum median where short survey)"`).
- **Hybrid variables**: When a variable draws from multiple sources, the label should say so (e.g., `"HH revenue: survey Q5 (food) + median price × Q3_qty (non-food)"`).
- **Variant suffixes**: When `_p1`, `_p2`, `_sh`, `_imp` suffixes distinguish versions, the label must explain the distinction — the suffix alone is not self-documenting.

### Rules

1. **Never leave a generated variable unlabeled.** Every `gen` or `egen` that creates an output variable must be followed by a `label var` statement.
2. **Include the module reference** in labels for cross-module variables: `"Net income (see 20_net_income.do)"`.
3. **Mark imputation explicitly**: Use words like "imputed", "median-filled", or "scaled" in the label text.

---

## Select-Multiple Decomposition

Survey instruments with "select all that apply" questions often store responses as a delimited string (e.g., `"2 4 5"` meaning options 2, 4, and 5 were selected). These should be decomposed into binary indicator variables for analysis on an as-needed basis.

### Principle
For each choice value in the select-multiple question, create a binary indicator (0/1) that is 1 if that choice was selected and 0 otherwise. Set to missing if the original variable is missing (respondent wasn't asked or didn't answer).

### Workflow
1. **Check the instrument** for the full set of valid choice values (including "other" codes like -66).
2. **Create one binary indicator per choice value**, named consistently (e.g., `varname_choicevalue_suffix`).
3. **Apply zero-by-logic** from gateway variables: if the gateway says the question was not asked, all indicators should be 0.
4. **Label each indicator** with a human-readable description of what the choice means.

### "None of the Above" Option Inversion

Select-multiple questions often include a "None" option (typically coded as 0) alongside substantive options (1, 2, 3, ...). The binary indicator for option 0 means "none selected" — but the useful analytical variable is usually `has_any` (= 1 if any option was selected).

To invert, use an equality test, **not subtraction**:
```
WRONG:  has_any = none_option - 1     --> produces -1 / 0 (invalid encoding)
RIGHT:  has_any = (none_option == 0)  --> produces  0 / 1 (correct 0/1 indicator)
```

The subtraction bug is easy to miss because downstream code may treat any nonzero value as "true," masking the -1.

---

## Reshape Safety

Reshape operations (wide-to-long or long-to-wide) are a common source of silent data corruption. Follow these rules:

### Before Reshape

1. **Verify uniqueness of i() variables**: Run `isid` on the variables in `i()` before reshaping. If `i()` contains non-key variables (e.g., flags that happen to be constant within ID), the reshape works by coincidence — if upstream data changes, it will silently produce wrong results.
2. **Only use true keys in i()**: The `i()` clause should contain only variables that structurally define a unique observation (e.g., `hhid`), not convenience variables that happen to be constant.

### After Reshape

1. **Verify the new key structure**: After reshape long, run `isid` on the new composite key (e.g., `hhid item_id`). After reshape wide, run `isid` on the original key (e.g., `hhid`).
2. **Assert expected dimension values**: After reshape long, assert that the `j()` variable contains exactly the expected values (e.g., `assert inlist(crop_order, 1, 2, 3, 4, 5)`).
3. **Assert expected row count**: If you know the expected expansion factor, assert the row count (e.g., `assert _N == N_households * N_items`).

---

## Deterministic Joins

### Principle
Many-to-many joins are non-deterministic — the row matching depends on sort order and can produce different results on different runs or machines. Never use them.

### When Many-to-Many Relationships Exist
When two datasets share a key that is not unique on either side, make the relationship explicit:

1. **Cross-join** the datasets on the shared key, producing all pairwise combinations.
2. **Collapse** or **reshape** to the desired unit of observation.
3. **Verify** the resulting dataset has the expected ID structure.

### Why This Matters
A non-deterministic join can silently change results when data is re-sorted, re-exported, or run on a different machine. The output may look reasonable but differ from run to run. Deterministic alternatives (cross-join + collapse, or reshape to create a proper key) produce identical results regardless of input order.

Use `joinby` (explicit cross-join) + `collapse` instead of `merge m:m`.

---

## Final Merge Cardinality Checks

When combining multiple cleaned modules into a single analysis dataset (e.g., in a final merge script), validate expected sample sizes at every step.

### Pattern

1. **Start from a known frame**: Load the baseline/sampling-frame dataset and assert its expected row count.
2. **Assert after every merge**: After each `merge 1:1`, count matched observations and assert they equal the expected module sample size.
3. **Document expected counts**: Comment each assertion with the source of the expected number (e.g., "N = survey completions from fieldwork tracking sheet").

### Why This Matters

Without cardinality checks, a module that silently drops observations (e.g., due to a changed filter or failed reshape) will propagate undetected into the final dataset. The final merge is the last opportunity to catch sample-size discrepancies before analysis.

---

## One-Off Data Corrections

### Principle
When fixing individual data errors (e.g., a misrecorded value for a specific household), prefer structural/value signatures over hard-coded IDs.

### Why
Hard-coded household IDs break when IDs are tokenized (de-identified), renamed, or resequenced. Structural signatures — combinations of observable variable values that uniquely identify the error — remain valid across ID transformations.

### Pattern
1. **Identify the error** using a combination of variable values that is unique to the affected observation(s) (e.g., an unusual label + quantity + amount combination).
2. **Assert the match count** — the signature must match exactly the expected number of observations (usually 1). If the data changes and the signature matches 0 or more than expected, the assert fails loudly.
3. **Apply the correction** conditional on the signature.
4. **Document the rationale** in a comment: what the error is, why the correction is appropriate, and what instrument evidence supports it.

### Example (Pseudocode)
```
Count observations where item_label == "strange_value" AND qty == 999
Assert count == 1
Replace qty = correct_value where item_label == "strange_value" AND qty == 999
```

### Manual Override Registries

When a module requires multiple manual value overrides (e.g., 5+ unit prices set from field-officer feedback), do not scatter them inline through the script. Instead:

1. **Centralize overrides** in a clearly labeled block at the top of the module, or in an external reference file (CSV or Excel) that the module loads.
2. **Document each override** with: the variable affected, the old value, the new value, the source of the correction, and a brief rationale.
3. **Assert count**: After applying overrides, assert the number of affected observations matches expectations.

Scattered manual overrides are difficult to audit, easy to miss during review, and impossible to reproduce from the script alone.

---

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
