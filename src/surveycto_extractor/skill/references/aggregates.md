# Aggregates and consistency checks

Read this before summing components into a total (income, expenditure, time) or checking that related variables agree. Principles come first, then the Stata code.

## Contents

- [Aggregate Creation with Missing Handling](#aggregate-creation-with-missing-handling)
- [Cross-Variable Consistency Checks](#cross-variable-consistency-checks)
- [Tolerance Specification Guidelines](#tolerance-specification-guidelines)
- [In Stata](#in-stata)
  - [Documenting Expected Relationships With Tolerances](#documenting-expected-relationships-with-tolerances)
  - [Using `egen rowtotal()`](#using-egen-rowtotal)
  - [`rowmiss()` Danger with Structurally Sparse Data](#rowmiss-danger-with-structurally-sparse-data)
  - [All-or-Missing Aggregation](#all-or-missing-aggregation)
  - [Stratum-Specific Calculations](#stratum-specific-calculations)

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

## In Stata

### Documenting Expected Relationships With Tolerances

> For the principle of counting against relevant denominators and choosing thresholds, see [Tolerance Specification Guidelines](#tolerance-specification-guidelines).

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

### Using `egen rowtotal()`

> For the principle (strict propagation only), see [Aggregate Creation with Missing Handling](#aggregate-creation-with-missing-handling).

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

**Critical:** The DKR flag must check **source** (raw) variables, not derived variables. If the flag checks a variable that was produced by arithmetic (e.g., `qty * price`), the extended missing will already have been destroyed (see [Arithmetic Destroys Extended Missing](missing-values.md#arithmetic-destroys-extended-missing)).

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
