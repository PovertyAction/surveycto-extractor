# Stata Coding Guidelines

This document consolidates Stata-specific guidance for survey data cleaning pipelines. For language-independent cleaning principles, see [CLEANING.md](CLEANING.md). For SurveyCTO relevance translation rules, see [SURVEYCTO_RELEVANCE_TRANSLATION.md](SURVEYCTO_RELEVANCE_TRANSLATION.md).

## Table of Contents

- [Batch Mode and Error Detection](#batch-mode-and-error-detection)
- [Environment and Initialization](#environment-and-initialization)
- [Coding Style](#coding-style)
- [Guardrails](#guardrails)
- [Testing Guidelines](#testing-guidelines)
- [Assertions (Stata Syntax)](#assertions-stata-syntax)
- [Common Patterns (Stata Syntax)](#common-patterns-stata-syntax)
- [SurveyCTO Skip Logic in Stata](#surveycto-skip-logic-in-stata)
- [Advanced Debugging](#advanced-debugging)

---

## Batch Mode and Error Detection

**IMPORTANT: Stata batch mode (`-e` or `-b` flags) always exits with code 0**, even when errors occur. You cannot rely on the exit code to detect failures.

### Detecting Errors

You must check the log file for error patterns like `r(601);`. If using a build tool, configure it to scan log output for `r(###);` patterns after each run:

```bash
# Example: awk-based error detection after a Stata batch run
stata-se -e -q do path/to/00_run.do module.do && \
    tail -n 40 00_run.log | awk '{print} /^[[:space:]]*r\([0-9]+\);/{err=1} END{exit (err?1:0)}'
```

### Manual Verification

When running Stata directly, always inspect the log after running:

```bash
stata-se -e -q do path/to/00_run.do module.do
tail -n 40 run.log  # Look for r(###); error codes
```

Common error codes:

- `r(601)` - file not found
- `r(198)` - invalid syntax or option
- `r(111)` - variable not found
- `r(110)` - already defined (e.g., program redefinition)

---

## Environment and Initialization

### Reproducibility

Use a consistent initialization pattern across all do-files to ensure reproducibility across machines:

- Set a fixed random seed at the top of any module that uses random operations.
- Use `version` to lock Stata's command syntax to a specific release.
- Pin package versions (e.g., via a requirements file) rather than installing from SSC on the fly.

### `maxvar` Compatibility

Stata/SE supports up to 32,767 variables. If your pipeline uses `set maxvar`, do not exceed the SE limit, or the script will fail on SE installations.

### Package Dependencies

Track package dependencies explicitly (e.g., in a requirements file). Install packages to a local `ado/` directory checked into the repo rather than relying on users' global SSC installations. This ensures every collaborator uses the same package version.

---

## Coding Style

### Formatting

- Indent Stata code with four spaces; align continuation lines beneath the originating command.
- Use `*` for section headers and `//` for inline comments.
- Keep globals in lowercase snake_case near the top of do-files.
- Default to ASCII unless working in files that already require Unicode.
- Remember Stata varnames are capped at **32 characters**; plan suffixes accordingly.

### Line Continuation

Use Stata's line continuation operator `///` at the physical end of a line:

- Always place `///` as the very last characters on the line (avoid trailing spaces).
- Do not use backslashes (`\`) — they are not Stata syntax.
- Indent continuation lines for readability.
- If you want an inline comment on a continued line, put the comment *before* the `///`.

```stata
keep hhid has_loans_received has_loans_given ///
    total_loans_received total_loans_repaid ///
    total_loans_still_owed total_net_loan_value

local loan_wide_stubs ///
    loan_cashinkind_12m_ loansize_12m_ /// size stubs
    loan_inkind_size_12m_ loanrepaid_12m_
```

### Dynamic Locals

Use the single-nest expansion pattern rather than double-backtick forms:

```stata
local scope_key `scope_key_`scope''
```

### Wildcards in Var Lists

**Prefer `?` / `??` over `*`**. The `*` wildcard matches any suffix, which can silently pull in unrelated variables. Use `?` (single character) and `??` (two characters) to match only numeric suffixes:

```stata
* Avoid: * can pull in string variables or unrelated stubs
use hhid asset_amount* asset_owned* using "${working_data}/data.dta", clear

* Prefer: explicit digit-width wildcards
use hhid asset_amount_? asset_amount_?? ///
    asset_owned_? asset_owned_?? ///
    using "${working_data}/data.dta", clear
```

This is more verbose (you need `?` for 1-digit, `??` for 2-digit, `???` for 3-digit suffixes) but prevents accidental inclusion of variables with non-numeric or longer suffixes. The same principle applies in `keep`, `drop`, `rename`, and local macro definitions — anywhere a varlist accepts wildcards.

When you store wildcards in locals and need to iterate/confirm variables, expand them explicitly after the dataset is loaded:

```stata
local all_outcomes "var_? var_?? var_amount_? var_amount_??"
unab all_outcomes : `all_outcomes'
foreach v of local all_outcomes {
    confirm variable `v'
}
```

### No Silent `capture`

Do not use `capture` to silently swallow errors. Either:

1. **Use `capture` + check `_rc` explicitly** — e.g., `capture confirm file ...` / `if _rc { ... }` for conditional logic.
2. **Don't use `capture` at all** — if the command already handles both cases (e.g., `label define ... , replace` works whether or not the label exists).

```stata
* Wrong: capture with no _rc check — silently swallows errors
capture label define _ml_yesno 0 "No" 1 "Yes", replace

* Right: the ,replace option handles both "exists" and "doesn't exist"
label define _ml_yesno 0 "No" 1 "Yes", replace

* Right: capture + explicit _rc check for conditional logic
capture confirm file "`some_path'"
if _rc {
    di as text "SKIP: file not found."
    exit 0
}
```

### Safe Destring

When variables may arrive as string or numeric depending on the survey export, do **not** use `capture destring` (which silently swallows errors). Instead, filter to strings first and convert only those:

```stata
ds var1 var2 var3, has(type string)
qui destring `r(varlist)', replace
confirm numeric variable var1 var2 var3
```

### Saving Data Files

Write directly to the destination path instead of using intermediate local variables:

```stata
* Do this:
qui compress
save "${analysis_data}/05_loans.dta", replace

* Avoid this:
local output_filepath "${analysis_data}/05_loans.dta"
save `output_filepath', replace
```

### Tempvar Persistence

`tempvar` in a do-file (not inside a `program`) creates variables that persist until the do-file finishes. If `save` is called before the do-file ends, the tempvar (named `__000000`, `__000001`, etc.) is written to the `.dta` file. This pollutes the output dataset with unintended variables.

Prefer `tempvar` helper variables to avoid name collisions, and always `drop` them after use (before any `save`):

```stata
* Do this:
tempvar mi_any
egen `mi_any' = rowmiss(`components')
egen score = rowtotal(`components')
replace score = . if `mi_any' > 0
drop `mi_any'

* Avoid this (tempvar is left in memory and may be saved to .dta):
tempvar mi_any
egen `mi_any' = rowmiss(`components')
egen score = rowtotal(`components')
replace score = . if `mi_any' > 0
* missing drop `mi_any'
```

### Magic Number Avoidance

Define scaling constants and repeated numeric literals as named locals at the top of the module:

```stata
* Define scaling factors at module top
local weeks_per_month  4.3    // 7-day recall → monthly
local months_per_year 12

* Use named locals in calculations
gen exp_foodcons_1m  = food_spend_7d * `weeks_per_month'
gen exp_foodcons_12m = exp_foodcons_1m * `months_per_year'
```

This makes the intent clear, centralizes update points, and prevents inconsistent uses of the same constant across a module.

### Standard File Ending for Cleaning Scripts

At the end of each cleaning `.do` file, after the `save` command:

```stata
*for logs:
describe, fullnames
su
```

---

## Guardrails

Non-negotiable rules to prevent silent-failure bugs in the pipeline.

### Command Abbreviation

**Never abbreviate dangerous commands** — always spell out fully:
`save`, `merge`, `append`, `sort`, `drop`, `keep`, `rename`, `replace`, `assert`

**Safe to abbreviate**: `gen`, `reg`, `tab`, `sum`, `qui`, `di`, `cap`, `bys`

### No Interactive Commands in Pipeline Modules

Never use `pause`, `set trace on`, `browse`, or `edit` in committed pipeline modules. These block automated batch runs and require manual intervention. If you need to inspect state during development, use `display` or logging, then remove the interactive command before committing.

### Never `clear all` in Pipeline Modules

`clear all` deletes programs loaded by any shared program file, causing downstream modules to fail with "command not recognized" errors. Use `clear` or `use ..., clear` instead.

### Merge Safety

- **Never `merge ..., force`** — it silently coerces types and overwrites data. Fix the underlying type mismatch instead.
- **No `merge m:m`** — it is non-deterministic. Use `joinby` + `collapse` or reshape to get a proper key structure. See [CLEANING.md > Deterministic Joins](CLEANING.md#deterministic-joins).
- **Assert after every merge** — check `_merge` values explicitly before dropping:

```stata
merge 1:1 hhid using "${analysis_data}/baseline_vars.dta"
assert inlist(_merge, 1, 3)  // expect master-only or matched
drop _merge
```

- **Always clean `_merge` before saving** — a stray `_merge` variable in a saved `.dta` file will cause the next module's merge to fail with `_merge already defined`. Even when using `nogen`, verify no leftover `_merge` from a prior merge remains in the dataset before `save`.
- **Use `keepus()` when merging auxiliary data** — load only the variables you need to prevent name collisions with variables already in memory. Define the needed variables in a local for traceability:

```stata
* Good: explicit variable list, traceable
local hh_vars hhmembers_count adult_equivalents children_0_14 adults_15plus
merge 1:1 hhid using "${working_data}/household_characteristics.dta", ///
    gen(_merge) keepus(`hh_vars')
assert _merge == 3
drop _merge
```

When you genuinely need all variables from the using dataset, omitting `keepus()` is fine — but that should be the exception, not the default.

### Final Merge Cardinality

In the final merge script, assert expected sample sizes at every step:

```stata
* Start from known frame
use "${input_data}/sampling_frame.dta", clear
assert _N == <expected_frame_size>  // document source of this number

* Merge each module output, assert expected matches
merge 1:1 hhid using "${analysis_data}/04_consumption.dta", gen(_m_cons) assert(1 3)
count if _m_cons == 3
assert r(N) == <expected_survey_completions>

drop _m_*
```

> For the principle, see [CLEANING.md > Final Merge Cardinality Checks](CLEANING.md#final-merge-cardinality-checks).

### `isid` Before Saving

Verify the expected unique identifier before writing any dataset:

```stata
isid hhid
qui compress
save "${analysis_data}/05_loans.dta", replace
```

### One-Off Data Corrections

Avoid hard-coded ID patch rules. Prefer structural/value signatures (e.g., label + quantity + amount) with explicit `count`/`assert` guards on expected match counts. This keeps corrections valid after ID tokenization and fails loudly if data drift changes match cardinality. See [CLEANING.md > One-Off Data Corrections](CLEANING.md#one-off-data-corrections).

### Path Separators

Always use forward slashes (`/`) in Stata path strings. On Linux, Stata treats backslashes (`\`) as literal filename characters, creating files like `for_analysis\file.dta` instead of navigating directories. Windows Stata handles both, so `/` works everywhere.

### Macro Name Length

Stata macro names are limited to **32 characters**. Registry-style names like `deid_required_<long_dataset_id>` will fail with `invalid name` (`r(198)`). Plan naming conventions with headroom for suffixes.

---

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

---

## Assertions (Stata Syntax)

> For the conceptual framework (philosophy, core patterns, two-phase workflow, tolerances), see [CLEANING.md > Assertions](CLEANING.md#assertions) and [CLEANING.md > Two-Phase Workflow](CLEANING.md#two-phase-workflow).

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

### Documenting Special Code Proportions

```stata
count if loan_amount == -99
local dk = r(N)
count
assert `dk'/r(N) <= 0.02  // <=2% don't know allowed; adjust if instrument changes
```

### Documenting Expected Relationships With Tolerances

> For the principle of counting against relevant denominators and choosing thresholds, see [CLEANING.md > Tolerance Specification Guidelines](CLEANING.md#tolerance-specification-guidelines).

```stata
* Example: sales_1m should be missing when most-days==1; allow small tolerance
count if biz_mostdaysofweek == 1 & !mi(biz_sales_1m)
local bad = r(N)
count if biz_mostdaysofweek == 1
local denom = r(N)
local share = cond(`denom' > 0, `bad' / `denom', 0)
if `bad' > 0 {
    di as error "WARNING: biz_sales_1m present when biz_mostdaysofweek==1 (N=`bad', share=`share')"
}
assert `share' <= 0.02  // allow <=2% inconsistency; adjust if instrument changes
```

### Structural vs. Response Missing (Stata)

> For the principle (structural vs. response missing, flag design), see [CLEANING.md > Structural vs. Response Missing](CLEANING.md#structural-vs-response-missing).

```stata
* Create explicit flags
gen mi_struct_interest = (interest_percent_1 == 0) & mi(interest_rate_12m_1)

* Assert structural missing implies missing content
assert mi(interest_rate_12m_1) if mi_struct_interest

* Assert non-structural paths supply data
assert !mi(interest_rate_12m_1) if interest_percent_1 == 1
```

---

## Common Patterns (Stata Syntax)

### Zero-by-Logic

> For the principle (when to apply, multi-level patterns), see [CLEANING.md > Zero-by-Logic](CLEANING.md#zero-by-logic).

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

> For the principle (75% rule, tolerance, gate-flip logic), see [CLEANING.md > Skip-Logic Violation Resolution](CLEANING.md#skip-logic-violation-resolution-gate-vs-downstream).

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

### Other-Specify Classification

> For the principle (blank=missing, priority ordering, absence responses, QC table), see [CLEANING.md > Other-Specify Classification](CLEANING.md#other-specify-classification).

Stata reference implementation:

```stata
* 1. Lowercase and trim the text
gen strL _osp_lc = lower(strtrim(some_var_osp))
replace _osp_lc = "" if mi(_osp_lc)

* 2. Define keyword regex patterns by category
local re_improved "(iron|metal|steel|zinc|tin|sheet|corrugated|cement|concrete|brick|stone|slate)"
local re_unimproved "(thatch|grass|straw|mud|clay|dung|leaf|tarpaulin|polythene|plastic|tent|no house|construction|building house|lives at)"
local re_ambig "(mixed|combination|temporary|fabric|other)"

* 3. Generate match flags
gen byte _osp_hit_improved   = (gate_var == -66) & regexm(_osp_lc, "`re_improved'")
gen byte _osp_hit_unimproved = (gate_var == -66) & regexm(_osp_lc, "`re_unimproved'")
gen byte _osp_hit_ambig      = (gate_var == -66) & regexm(_osp_lc, "`re_ambig'")

* 4. Build mapping with priority ordering (most specific wins)
gen str20 osp_map = ""
replace osp_map = "unknown"     if gate_var == -66
replace osp_map = "ambiguous"   if _osp_hit_ambig
replace osp_map = "improved"    if _osp_hit_improved & !_osp_hit_ambig
replace osp_map = "unimproved"  if _osp_hit_unimproved & !_osp_hit_improved & !_osp_hit_ambig

* 5. Apply to constructed outcome
replace outcome = . if gate_var == -66
replace outcome = 1 if gate_var == -66 & osp_map == "improved"
replace outcome = 0 if gate_var == -66 & osp_map == "unimproved"
```

QC table:

```stata
di as text "QC: Other-specify keyword mapping"
preserve
    keep if gate_var == -66
    gen str244 osp_trim = strtrim(some_var_osp)
    replace osp_trim = "[blank]" if osp_trim == "" | mi(osp_trim)
    contract osp_trim osp_map, freq(N)
    gsort -N
    list osp_trim osp_map N, noobs abbreviate(48)
restore
```

### Using `egen rowtotal()`

> For the principle (strict propagation only), see [CLEANING.md > Aggregate Creation with Missing Handling](CLEANING.md#aggregate-creation-with-missing-handling).

**Do NOT use `rowtotal(...), missing`.** Its semantics (missing only when *all* components are missing, treats individual missings as zero) conflict with strict propagation and are easily misinterpreted. All aggregates must use strict propagation: missing if *any* component is missing.

For few summands (<=5), just sum directly:

```stata
gen sumvar = summand1 + summand2 + summand3
```

For many summands, use `rowtotal` (without `, missing`) + an any-missing check:

```stata
tempvar mi_any
egen `mi_any' = rowmiss(var1 var2 var3 var4 var5 var6 var7 var8 var9)
egen sumvar = rowtotal(var1 var2 var3 var4 var5 var6 var7 var8 var9)
replace sumvar = . if `mi_any' > 0
drop `mi_any'
```

### `rowmiss()` Danger with Structurally Sparse Data

`rowmiss()` counts system missing (`.`) across variables but **cannot distinguish structural missing from data-quality missing (`.d`/`.r`)**. When components are 80-98% structurally missing due to skip logic, `rowmiss() > 0` triggers for nearly everyone, making the aggregate missing for thousands of observations that should be zero.

**Use flag-based `.d`/`.r` tracking instead:**

```stata
* Wrong: rowmiss treats structural missing and .d/.r the same
egen mi_count = rowmiss(comp_1 comp_2 comp_3 comp_4)
egen total = rowtotal(comp_1 comp_2 comp_3 comp_4)
replace total = . if mi_count > 0   // most observations become missing!

* Right: flag-based approach — set structural zeros explicitly, flag only .d/.r
gen byte dkr_flag = 0
forvalues i = 1/4 {
    gen comp_`i' = .
    replace comp_`i' = raw_value_`i' if gateway_`i' == 1   // owns this type
    replace comp_`i' = 0 if gateway_`i' == 0                // doesn't own
    replace comp_`i' = 0 if gateway_`i' == .                // structural missing = doesn't own
    replace dkr_flag = 1 if inlist(raw_value_`i', .d, .r) & gateway_`i' == 1
}
egen total = rowtotal(comp_1 comp_2 comp_3 comp_4)
replace total = . if dkr_flag == 1
drop dkr_flag
```

**Rule of thumb:** Only use `rowmiss()` when ALL components are expected to be non-missing for every observation. If any component has skip-logic gateways, use the flag-based pattern above.

**Critical:** The DKR flag must check **source** (raw) variables, not derived variables. If the flag checks a variable that was produced by arithmetic (e.g., `qty * price`), the extended missing will already have been destroyed (see [Arithmetic Destroys Extended Missing](#arithmetic-destroys-extended-missing)).

### Missing Value Tracking

> For the principle (flag design, when to create flags), see [CLEANING.md > Missing Value Tracking](CLEANING.md#missing-value-tracking).

```stata
gen is_mi_var = mi(var)
gen mi_dk_var = (var == -99)   // don't know
gen mi_rf_var = (var == -88)   // refused
gen mi_na_var = (var == -77)   // not applicable
```

Use `.x` when initializing variables empty (to distinguish from other extended missings):

```stata
gen newvar = .x
```

### System Missing vs Extended Missing

Once sentinel codes are recoded to extended missings (`.r`, `.d`, `.n`, `.o`), the distinction between system missing (`.`) and extended missing matters:

| Expression | What it catches | Use when |
|------------|----------------|----------|
| `missing(x)` or `mi(x)` | All missing types: `.`, `.a`–`.z` | You need to identify any observation without usable data |
| `x == .` | System missing only (`.`) | You specifically need system missing, excluding extended missings that carry meaning (refused, don't know, etc.) |
| `!missing(x)` or `!mi(x)` | All non-missing values | You need only observations with available data |

**Common trap**: After recoding `-88 = .r` and `-99 = .d`, a line like `replace x = 0 if x == .` will skip refused/don't-know observations (they are `.r`/`.d`, not `.`). If you intended to zero out all missing values, use `missing(x)`. If you intended to preserve extended missings and only zero out system missing, `== .` is correct — but document the intent.

### Arithmetic Destroys Extended Missing

**Any arithmetic operation on an extended missing produces system missing (`.`), not the original extended missing code.** This is fundamental Stata behavior:

```stata
. display .d * 4.3    // result: .     (system missing, NOT .d)
. display .r + 100    // result: .     (system missing, NOT .r)
. display .d / 7      // result: .     (system missing, NOT .d)
```

Direct assignment preserves extended missing; arithmetic does not:

```stata
gen y = x           // if x == .d, then y == .d  (preserved)
gen z = x * 4.3     // if x == .d, then z == .   (destroyed)
```

**Consequence for DKR flag construction:** When building a flag to track don't-know/refused through aggregations, always check the **source** variables before any arithmetic, not the derived result. If you check after multiplication, the `.d`/`.r` has already been converted to `.` and `inlist(result, .d, .r)` will never fire.

```stata
* WRONG — checks the product, where .d has already been destroyed:
gen time = days * hours
gen byte dkr_flag = inlist(time, .d, .r)   // always 0!

* RIGHT — checks source variables before multiplication:
gen byte dkr_flag = 0
replace dkr_flag = 1 if inlist(days, .d, .r)
replace dkr_flag = 1 if inlist(hours, .d, .r)
gen time = days * hours
```

This applies to all arithmetic: multiplication (`*`), addition (`+`), division (`/`), subtraction (`-`), and any expression. The same pattern applies when a source variable is used in a `replace ... = source * scalar` — the scalar multiplication destroys the extended missing before it reaches the target variable.

**Also watch for `recode (. = 0)` after arithmetic:** If a source variable was `.d` and arithmetic converted it to `.`, a subsequent `recode var (. = 0)` will silently treat the DKR as zero. The DKR flag must be created before both the arithmetic and the recode.

### Raw Data May Have Mixed Sentinel Formats

Some data collection platforms partially convert integer sentinel codes (-99, -88) to Stata extended missing (`.d`, `.r`) during field data collection. However, this conversion is often incomplete. The result is that **within the same dataset, some variables have integer sentinels and others have extended missing codes**.

This means:

- `payment_1m` might already have `.d`/`.r` (platform-converted) while `work_days_7d` still has -99/-88 (missed by the platform)
- A recode line like `recode var (-99 = .d) (-88 = .r)` is harmless on variables that were already converted (no -99/-88 to match)
- But relying on `if var < 0` to detect all sentinels will miss variables where the platform already converted them to extended missing

**Always check for both formats** when auditing a variable for sentinels:

```stata
* Check for integer sentinels
tab var if var < 0

* Check for extended missing already present
count if var == .d
count if var == .r
```

In Python, extended missing codes appear as `NaN` by default. Use `pyreadstat` with `user_missing=True` to detect them:

```python
df, meta = pyreadstat.read_dta('file.dta', usecols=['var'], user_missing=True)
# Extended missings show as string tags: 'd', 'r', etc.
ext_miss = df['var'][df['var'].apply(lambda x: isinstance(x, str))]
```

### Select-Multiple String Parsing

> For the principle (when to decompose, workflow), see [CLEANING.md > Select-Multiple Decomposition](CLEANING.md#select-multiple-decomposition).

When a select-multiple variable stores responses as a space-delimited string (e.g., `"2 4 5"`), parse it into binary indicators by padding the string with spaces and searching for each choice value:

```stata
* Create binary indicator for choice value `i' in select-multiple variable
gen byte varname_`i' = 0 if !mi(select_multiple_var)
replace varname_`i' = 1 if strpos(" " + select_multiple_var + " ", " `i' ") > 0
replace varname_`i' = . if mi(select_multiple_var)
```

The space padding (`" " + var + " "`) prevents partial matches (e.g., searching for `" 2 "` won't match `"12"`).

**"None of the above" inversion** (see [CLEANING.md > "None of the Above" Option Inversion](CLEANING.md#none-of-the-above-option-inversion)):

```stata
* Option 0 = "None" in select-multiple → invert to has_any indicator
gen byte has_any = (select_none_0 == 0)   // produces 0/1
* WRONG: gen has_any = select_none_0 - 1  // produces -1/0
```

### Frame-Based Reshape Isolation

When a reshape is needed for intermediate cleaning (e.g., wide-to-long for per-item validation, then back to wide), use Stata frames to isolate the reshape from the main dataset. This avoids losing variables that don't participate in the reshape:

```stata
frame copy default my_frame
frame change my_frame
reshape long stub_, i(hhid) j(item_num)
* ... validate, clean, compute per-item ...
reshape wide result_vars, i(hhid) j(item_num)
frame change default
frlink 1:1 hhid, frame(my_frame)
frget new_vars, from(my_frame)
frame drop my_frame
```

### ID Verification

After opening a dataset or changing the data structure (e.g., reshape), confirm uniqueness of ID:

```stata
use "${data}/mydata.dta", clear
isid hhid

* After reshape
reshape long loan_, i(hhid) j(loan_index)
isid hhid loan_index
```

### Verifying `.dta` Output Parity

To confirm a refactor didn't change a `.dta` output, compare old vs new using `datasignature` (after sorting on the key) and optionally `cf _all`. Note: `cf` does not support `id()`, so ensure both datasets are identically sorted (or save a sorted tempfile) before running `cf`.

**Caveat: `rowtotal` masks intermediate changes.** `rowtotal` (without `, missing`) treats individual missings as 0. If a refactor changes a component from 0 to missing (or vice versa), the row total is identical — output parity checks will not detect the change. Always pair output comparison with line-by-line code review when reviewing refactored modules that use `rowtotal`.

### String/Tokenized ID Handling

When household IDs are tokenized strings (e.g., after de-identification), panel commands like `xtset` fail because they require numeric IDs. Create a temporary numeric group variable:

```stata
* Panel commands with string ID
egen long id_panel = group(hhid)
xtset id_panel time_var
* ... panel operations ...
drop id_panel
```

Related rules:

- **Use `missing()` not `== .`** to test for missing values. `missing()` works for both string and numeric types; `== .` fails silently on strings.
- **Update hardcoded ID lists** when migrating from numeric to tokenized IDs. Search for `inlist(hhid, ...)` patterns and replace numeric values with their tokenized equivalents.

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

> For the principle, see [CLEANING.md > Short-Survey Dual Versioning](CLEANING.md#short-survey-dual-versioning).

### Preserve+Tempfile for Split-and-Recombine

When processing must diverge by subgroup (e.g., different strata that each require a different merge partner), use preserve+tempfile:

```stata
preserve
    keep if stratum == 1
    merge m:1 stratum_id using "${input_data}/stratum1_data.dta", assert(2 3) keep(3) nogen
    tempfile stratum1_merged
    save `stratum1_merged'
restore, not

keep if stratum == 2
merge m:1 stratum_id using "${input_data}/stratum2_data.dta", assert(2 3) keep(3) nogen

append using `stratum1_merged'
isid hhid
```

### Stratum-Specific Calculations

When computing reference values (medians, means) for imputation, stratify by the variables that define experimental or sampling strata to preserve study structure:

```stata
preserve
    keep if unit_price > 0 & !mi(unit_price)
    collapse (median) price_p50 = unit_price, by(stratum arm)
    tempfile ref_prices
    save `ref_prices'
restore

merge m:1 stratum arm using `ref_prices', nogen
replace imputed_value = quantity * price_p50 if mi(reported_value)
label var imputed_value "Value (imputed via arm×stratum median where missing)"
```

### All-or-Missing Aggregation

When summing components into a category total, flag observations with ANY missing component and set the total to missing for them:

```stata
* Step 1: Flag missing components
gen byte ag_missing = is_ag_asset & missing(asset_amount)
gen byte durables_missing = is_durables & missing(asset_amount)

* Step 2: Propagate to household level
bys hhid: egen any_ag_missing = max(ag_missing)
bys hhid: egen any_dur_missing = max(durables_missing)

* Step 3: Compute totals (treats missing as 0 — that's fine, we fix it next)
bys hhid: egen hh_ag_tot = total(cond(is_ag_asset, asset_amount, 0))
bys hhid: egen hh_dur_tot = total(cond(is_durables, asset_amount, 0))

* Step 4: Enforce strict propagation
replace hh_ag_tot = . if any_ag_missing
replace hh_dur_tot = . if any_dur_missing

* Step 5: Clean up flags
drop ag_missing durables_missing any_ag_missing any_dur_missing
```

This three-step pattern (flag → propagate → enforce) is more reliable than `rowtotal(..., missing)` because it gives explicit control over which components matter for each total.

### `fillin` with Origin Tracking

After `fillin` creates placeholder observations, always track which rows are synthetic:

```stata
fillin hhid activity
gen byte is_fillin = _fillin
label var is_fillin "1 = row created by fillin (not observed in survey)"

* Apply zero logic ONLY to filled-in rows
replace activity_engaged = 0 if is_fillin == 1 & activity_any == 0

drop _fillin
```

Without tracking, downstream code cannot distinguish "respondent reported zero" from "question was not asked for this activity."

---

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

---

## Advanced Debugging

### Understanding `if` in Stata: Command vs. Qualifier

**The `if` command (block if)**: Evaluates the expression based **only on the first observation**:

```stata
if some_condition == 1 {
    replace my_variable = 100  // Applies to ALL obs if first obs meets condition
}
```

Use for controlling flow of execution, not for row-wise conditional data changes.

**The `if` qualifier (inline if)**: Applies the command only to observations where expression is true:

```stata
replace my_variable = 100 if some_condition == 1  // Only where condition is true
```

This is the correct method for conditional data transformations.

**Common Pitfall**: Writing `if shortsurvey == 1 { replace x = 0 }` intending to set `x` to 0 only where `shortsurvey` is 1. This actually replaces `x` for *all* observations if the *first* observation has `shortsurvey == 1`.

**Best Practice**: Always use the `if` qualifier for row-wise conditional data operations.

### Iterative Assertion Refinement

1. **Define Initial Assertions**: After data manipulation, write `assert` statements reflecting expected state.
2. **Test by Running**: Execute your Stata script.
3. **Inspect Log on Failure**: Review which assertion failed and how many contradictions.
4. **Examine Contradictory Data**:

   ```stata
   * If 'assert cost == 0 if gateway == 2' failed
   preserve
   keep if gateway == 2
   list hhid cost gateway if cost != 0 & !missing(cost)
   restore
   ```

5. **Cross-Reference Documentation**: Consult the survey instrument, codebooks, etc.
6. **Refine Assertion or Code**: Based on investigation.
7. **Re-run and Iterate**: Until all assertions pass.

### General Guidance

- Code concisely but descriptively. Avoid frequent `di "..."` statements; let Stata code and console output speak for itself.
- Don't check existence of columns/files; just let the script fail and review logs.

```stata
* Avoid this pattern:
capture confirm file "`income_total_file'"
if !_rc {
    ...
}

* Instead, just open the file assuming it's there
use "`income_total_file'", clear
```

---

## Redundancy to Avoid

Avoid useless pieces of code in `if` conditions:

```stata
* Redundant - !mi() is implied by <0
assert lstock_total == -99 if lstock_total < 0 & !mi(lstock_total)

* Better
assert lstock_total == -99 if lstock_total < 0
```
