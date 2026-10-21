# Stata style and guardrails

Read this before writing the first do-file in a project whose `CLAUDE.md` sets no Stata conventions, or when a batch run fails in a way you do not recognise.

## Contents

- [Batch Mode and Error Detection](#batch-mode-and-error-detection)
- [Environment and Initialization](#environment-and-initialization)
- [Coding Style](#coding-style)
- [Guardrails](#guardrails)
- [Advanced Debugging](#advanced-debugging)
- [Redundancy to Avoid](#redundancy-to-avoid)

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
- **No `merge m:m`** — it is non-deterministic. Use `joinby` + `collapse` or reshape to get a proper key structure. See [Deterministic Joins](repeats-and-merges.md#deterministic-joins).
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

> For the principle, see [Final Merge Cardinality Checks](repeats-and-merges.md#final-merge-cardinality-checks).

### `isid` Before Saving

Verify the expected unique identifier before writing any dataset:

```stata
isid hhid
qui compress
save "${analysis_data}/05_loans.dta", replace
```

### One-Off Data Corrections

Avoid hard-coded ID patch rules. Prefer structural/value signatures (e.g., label + quantity + amount) with explicit `count`/`assert` guards on expected match counts. This keeps corrections valid after ID tokenization and fails loudly if data drift changes match cardinality. See [One-Off Data Corrections](other-specify.md#one-off-data-corrections).

### Path Separators

Always use forward slashes (`/`) in Stata path strings. On Linux, Stata treats backslashes (`\`) as literal filename characters, creating files like `for_analysis\file.dta` instead of navigating directories. Windows Stata handles both, so `/` works everywhere.

### Macro Name Length

Stata macro names are limited to **32 characters**. Registry-style names like `deid_required_<long_dataset_id>` will fail with `invalid name` (`r(198)`). Plan naming conventions with headroom for suffixes.

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

## Redundancy to Avoid

Avoid useless pieces of code in `if` conditions:

```stata
* Redundant - !mi() is implied by <0
assert lstock_total == -99 if lstock_total < 0 & !mi(lstock_total)

* Better
assert lstock_total == -99 if lstock_total < 0
```
