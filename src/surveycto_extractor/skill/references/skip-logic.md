# Skip logic: zeroing, violations, short surveys

Read this before setting a gated question to zero, resolving answers that break the form's skip logic, or using a relevance condition as a Stata `if`. Principles come first, then the Stata code.

## Contents

- [Zero-by-Logic](#zero-by-logic)
- [Skip-Logic Violation Resolution (Gate vs. Downstream)](#skip-logic-violation-resolution-gate-vs-downstream)
- [Short-Survey Dual Versioning](#short-survey-dual-versioning)
- [In Stata](#in-stata)
  - [Zero-by-Logic](#zero-by-logic)
  - [Skip-Logic Violation Resolution (Gate vs. Downstream)](#skip-logic-violation-resolution-gate-vs-downstream)
  - [Short-Survey Dual Versioning](#short-survey-dual-versioning)
- [SurveyCTO Skip Logic in Stata](#surveycto-skip-logic-in-stata)

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

## Short-Survey Dual Versioning

When a survey has long and short forms that collect different levels of detail, create **two versions** of every aggregate variable:

1. **Main version** (`var`): Contains the full-detail value for long-survey respondents. Set to structural missing (e.g., `.s` or `.m`) for short-survey respondents who were not asked the detail questions.
2. **Short-survey version** (`var_sh`): Contains the imputed or partial value available for short-survey respondents (e.g., from gateway questions only). Set to missing for long-survey respondents.

### Why Two Versions

A single variable that mixes long- and short-survey values conflates different measurement precision levels. Keeping them separate lets downstream analysis choose: use only long-survey (more precise, smaller N) or combine (larger N, noisier).

### Rules

1. **Always guard conditional fills with multiple conditions.** When filling zeros for short-survey HH, require both the short-survey flag AND the relevant gateway:

   ```text
   set to 0 only if: (short_survey == 1) AND (hh_gate == 0)
   ```

   This prevents overwriting genuine missing values from form-version gaps.

   **Additionally, always include a missing-check condition** when zeroing short-survey variables. If an upstream module has already set a value (e.g., a binary indicator derived from gateway data), a blanket zero-fill overwrites it:

   ```text
   WRONG:  set to 0 if (short_survey == 1)           -- overwrites legitimate values
   RIGHT:  set to 0 if (short_survey == 1) AND missing(var)  -- only fills genuinely missing
   ```

2. **Label both versions** to distinguish their provenance: `"Total X (long survey only)"` vs. `"Total X (short survey, imputed from gateway)"`.

3. **Never silently combine** the two versions in downstream modules. If a final merge or analysis step needs a single variable, create it explicitly with a documented rule (e.g., "use long-survey value when available, fall back to short-survey").

## In Stata

### Zero-by-Logic

> For the principle (when to apply, multi-level patterns), see [Zero-by-Logic](#zero-by-logic).

**Simple case** — single gateway, single downstream variable:

```stata
* No animals owned --> set has_<animal_type> to zero for all animal types
replace has_cattle = 0 if owns_any_animals == 0
```

**Gateway loop** — when a numbered set of downstream variables depends on per-item and top-level gateways:

```stata
* Load gateway variables alongside downstream vars
use hhid asset_amount_? asset_amount_?? ///
    asset_owned_? asset_owned_?? ///
    asset_own_list_0 ///
    using "${working_data}/survey_data.dta", clear

* Recode sentinel codes BEFORE zeroing (so extended missings are preserved)
recode `asset_vars' (-88 = .r) (-99 = .d)

* Set zeroes from gateway logic — per-item and top-level
forvalues i = 1/62 {
    capture confirm variable asset_amount_`i'
    if _rc == 0 {
        replace asset_amount_`i' = 0 if asset_owned_`i' == 0
        replace asset_amount_`i' = 0 if asset_own_list_0 == 1
    }
}
```

This replaces the anti-pattern of blanket `replace x = 0 if missing(x)` at the end of a calculation, which destroys the distinction between "doesn't own" (should be 0) and "refused/don't know" (should stay `.r`/`.d`). Always set zeroes from the survey logic, not from the absence of data.

### Skip-Logic Violation Resolution (Gate vs. Downstream)

> For the principle (75% rule, tolerance, gate-flip logic), see [Skip-Logic Violation Resolution](#skip-logic-violation-resolution-gate-vs-downstream).

Stata reference implementation:

```stata
* Count violations against relevant denominator
count if house_improved == 0 & !mi(house_improved)
local denom = r(N)
count if house_improved == 0 & !mi(house_improved) & !mi(house_improved_amt)
local bad = r(N)
local share = cond(`denom' > 0, `bad' / `denom', 0)
if `bad' > 0 {
    di as error "WARNING: house_improved_amt populated when house_improved==0 " ///
        "(N=`bad', share=" %5.3f `share' ")."
}
assert `share' <= 0.01  // <=1% skip-logic violations

* Decision: flip gate where amount is valid (>=100 post-sentinel); enforce gate otherwise
replace house_improved = 1 if house_improved == 0 & !mi(house_improved_amt) ///
    & house_improved_amt >= 100
replace house_improved_amt = 0 if house_improved == 0 & !mi(house_improved)
```

### Short-Survey Dual Versioning

Create parallel variables for short- and long-survey households:

```stata
* Calculate aggregate for all observations
egen total_value = rowtotal(comp1 comp2 comp3)
replace total_value = . if any_missing > 0

* Create short-survey version (copy value where short survey applies)
clonevar total_value_sh = total_value if shortsurvey_module == 1
label var total_value_sh "Total value (short survey, gateway-only)"

* Set main version to structural missing for short-survey obs
replace total_value = .s if shortsurvey_module == 1
label var total_value "Total value (long survey, full detail)"
```

> For the principle, see [Short-Survey Dual Versioning](#short-survey-dual-versioning).

## SurveyCTO Skip Logic in Stata

> For the complete function-by-function translation reference (every SurveyCTO relevance function,
> `selected()` semantics, empty-string handling, clause-stripping rules), see
> [SURVEYCTO_RELEVANCE_TRANSLATION.md](SURVEYCTO_RELEVANCE_TRANSLATION.md).
> This section covers the Stata-side **runtime pitfalls** that arise when translated conditions
> are evaluated against the loaded dataset.

When SurveyCTO relevance conditions are converted to Stata `if` clauses, several patterns cause
runtime errors. The following pitfalls apply whenever skip logic conditions are used in `tabstat`,
`replace if`, or any other command that evaluates the condition against the loaded dataset.

### `tabstat if` with String Variables → r(109)

**Problem:** A SurveyCTO skip condition may reference a variable that is string-typed in Stata
(e.g., `resi_r101 != 0`). Stata raises `r(109)` (type mismatch) when comparing a string variable
to a numeric literal.

**Root cause:** SurveyCTO stores some `select_one` responses as strings in Stata exports (especially
short `calculate` fields or label-stored responses). The skip logic was generated using numeric
comparisons against these fields.

**Fix:** Before using skip conditions as Stata `if` clauses, identify which variables in the
condition are string-typed in the dataset and strip those clauses. A clause is safe to strip if
the variable is structurally empty (all missing strings) — the stripped broader condition is still
correct because all respondents who answered the gate question have system-missing or numeric values.

### `tabstat if` with Absent Variables → r(111)

**Problem:** SurveyCTO repeat-group `calculate` fields create numbered variables (`emp_act_loc_1`,
`emp_act_loc_2`, ...) but NOT the bare name. Any skip condition containing `emp_act_loc == 1`
causes `r(111)` ("variable not found" or "ambiguous abbreviation").

**Root cause:** The skip logic for variables inside a repeat group references the base name;
only the indexed names exist in the exported dataset.

**Fix:** Strip any clause whose comparison variable is absent from the dataset's column list.
This broadens the `tabstat` condition but is safe: respondents who did not qualify under the
stripped clause have structurally missing values.

**Extension:** The check also covers variables used as *arguments* inside `inlist()` and
`rowtotal()`, not just the left side of comparison operators.

### `tabstat if` with Always-False Conditions → r(2000)

**Problem:** SurveyCTO marks disabled questions with `relevance = 0` (always false). If the
skip logic converts to `(consent==1) & (0)`, the effective condition is always false —
tabstat finds zero observations and raises `r(2000)` ("too few observations").

**Fix (two-part):**

1. Exclude disabled variables from the numeric universe entirely (filter `disabled == False`).
2. As a safety net, strip any clause matching `(0)` literally from the condition string.

### `selected()` — Resolved via Question-Type Dict

**Problem:** SurveyCTO `selected(var, 'N')` translates differently depending on question type:

- `select_one`: should become `var == N`
- `select_multiple`: should become `var_N == 1`

**Fix:** `json_extractor.py` passes a `{var_name: question_type}` dict to `LogicConverter` at
extraction time. The converter emits:

- `select_one` → `var == N`
- `select_multiple` → `var_N == 1`  (e.g., `food_source_3 == 1`)
- Dynamic second argument (another variable) → stripped (untranslatable)

For manual Stata coding without the pipeline, stripping is still a safe fallback: respondents
who did not qualify under the stripped clause have structurally missing values.

### `tabstat if` with Single-Quoted Literals → macro syntax error

**Problem:** SurveyCTO conditions like `ag_practices_know != '-55'` use single quotes around
numeric literals. In Stata, single quotes delimit macros; bare `'-55'` is invalid syntax and
causes a macro expansion error.

**Fix (Step 3b in `logic_converter.py`):**

- For `select_multiple` var: `var != '-55'` → `var__55 != 1`
  (negative code: `-` → `_`, yielding double underscore prefix)
- For other vars: strip the single quotes → `var != -55`

### Validation Scripts

After generating a summary stats do-file, run from the extractor directory to catch remaining
translation problems before running in Stata:

- `check_missing_cond_vars.py` — groups with IF condition referencing a non-existent variable
  (catches `r(111)` risk)
- `check_string_conditions.py` — groups with IF condition referencing a truly-string variable
  (uses `metadataonly=True` for accurate Stata storage types; filters destring-convertible vars;
  catches `r(109)` risk)
