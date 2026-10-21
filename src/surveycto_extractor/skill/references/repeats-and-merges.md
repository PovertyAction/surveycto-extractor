# Repeats, select_multiple, reshapes and merges

Read this before reshaping a repeat group, decomposing a select_multiple, or joining tables at different levels. Principles come first, then the Stata code.

## Contents

- [Select-Multiple Decomposition](#select-multiple-decomposition)
- [Reshape Safety](#reshape-safety)
- [Deterministic Joins](#deterministic-joins)
- [Final Merge Cardinality Checks](#final-merge-cardinality-checks)
- [In Stata](#in-stata)
  - [Select-Multiple String Parsing](#select-multiple-string-parsing)
  - [Frame-Based Reshape Isolation](#frame-based-reshape-isolation)
  - [Preserve+Tempfile for Split-and-Recombine](#preservetempfile-for-split-and-recombine)
  - [`fillin` with Origin Tracking](#fillin-with-origin-tracking)
  - [ID Verification](#id-verification)
  - [String/Tokenized ID Handling](#stringtokenized-id-handling)

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

```text
WRONG:  has_any = none_option - 1     --> produces -1 / 0 (invalid encoding)
RIGHT:  has_any = (none_option == 0)  --> produces  0 / 1 (correct 0/1 indicator)
```

The subtraction bug is easy to miss because downstream code may treat any nonzero value as "true," masking the -1.

## Reshape Safety

Reshape operations (wide-to-long or long-to-wide) are a common source of silent data corruption. Follow these rules:

### Before Reshape

1. **Verify uniqueness of i() variables**: Run `isid` on the variables in `i()` before reshaping. If `i()` contains non-key variables (e.g., flags that happen to be constant within ID), the reshape works by coincidence — if upstream data changes, it will silently produce wrong results.
2. **Only use true keys in i()**: The `i()` clause should contain only variables that structurally define a unique observation (e.g., `hhid`), not convenience variables that happen to be constant.

### After Reshape

1. **Verify the new key structure**: After reshape long, run `isid` on the new composite key (e.g., `hhid item_id`). After reshape wide, run `isid` on the original key (e.g., `hhid`).
2. **Assert expected dimension values**: After reshape long, assert that the `j()` variable contains exactly the expected values (e.g., `assert inlist(crop_order, 1, 2, 3, 4, 5)`).
3. **Assert expected row count**: If you know the expected expansion factor, assert the row count (e.g., `assert _N == N_households * N_items`).

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

## Final Merge Cardinality Checks

When combining multiple cleaned modules into a single analysis dataset (e.g., in a final merge script), validate expected sample sizes at every step.

### Pattern

1. **Start from a known frame**: Load the baseline/sampling-frame dataset and assert its expected row count.
2. **Assert after every merge**: After each `merge 1:1`, count matched observations and assert they equal the expected module sample size.
3. **Document expected counts**: Comment each assertion with the source of the expected number (e.g., "N = survey completions from fieldwork tracking sheet").

### Why This Matters

Without cardinality checks, a module that silently drops observations (e.g., due to a changed filter or failed reshape) will propagate undetected into the final dataset. The final merge is the last opportunity to catch sample-size discrepancies before analysis.

## In Stata

### Select-Multiple String Parsing

> For the principle (when to decompose, workflow), see [Select-Multiple Decomposition](#select-multiple-decomposition).

When a select-multiple variable stores responses as a space-delimited string (e.g., `"2 4 5"`), parse it into binary indicators by padding the string with spaces and searching for each choice value:

```stata
* Create binary indicator for choice value `i' in select-multiple variable
gen byte varname_`i' = 0 if !mi(select_multiple_var)
replace varname_`i' = 1 if strpos(" " + select_multiple_var + " ", " `i' ") > 0
replace varname_`i' = . if mi(select_multiple_var)
```

The space padding (`" " + var + " "`) prevents partial matches (e.g., searching for `" 2 "` won't match `"12"`).

**"None of the above" inversion** (see ["None of the Above" Option Inversion](#none-of-the-above-option-inversion)):

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

### ID Verification

After opening a dataset or changing the data structure (e.g., reshape), confirm uniqueness of ID:

```stata
use "${data}/mydata.dta", clear
isid hhid

* After reshape
reshape long loan_, i(hhid) j(loan_index)
isid hhid loan_index
```

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
