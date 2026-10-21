# Missing values and sentinel codes

Read this before recoding a missing-value code (`-99`, `-88`, `.d`) or deciding whether a missing value is a skip or a non-response. Principles come first, then the Stata code.

## Contents

- [Structural vs. Response Missing](#structural-vs-response-missing)
- [Sentinel Code Mapping](#sentinel-code-mapping)
- [Missing Value Tracking](#missing-value-tracking)
- [In Stata](#in-stata)
  - [Structural vs. Response Missing (Stata)](#structural-vs-response-missing-stata)
  - [Documenting Special Code Proportions](#documenting-special-code-proportions)
  - [Missing Value Tracking](#missing-value-tracking)
  - [System Missing vs Extended Missing](#system-missing-vs-extended-missing)
  - [Arithmetic Destroys Extended Missing](#arithmetic-destroys-extended-missing)
  - [Raw Data May Have Mixed Sentinel Formats](#raw-data-may-have-mixed-sentinel-formats)

## Structural vs. Response Missing

Distinguish between two fundamentally different reasons a variable can be missing:

- **Structural missing**: The question was *not asked* because of skip logic. The respondent was never meant to answer. Example: "in-kind loan size" is structurally missing when the loan is cash-only.
- **Response missing**: The question *was asked* but the respondent could not or would not answer. Represented by sentinel codes like don't-know (-99) or refused (-88).

### Design Principles

1. **Create explicit flags** to record each reason separately (e.g., `mi_dk_var`, `mi_rf_var`, `mi_struct_var`).
2. **Assert structural missing implies missing content**: If the skip condition applies, the downstream variable must be missing.
3. **Assert non-structural paths supply data**: If the question was asked, the variable should be non-missing (subject to sentinel-code tolerances).
4. **Never conflate the two**: A structurally missing variable should not have the same missing code as a "don't know" response. Use extended missing values or separate flags.

## Sentinel Code Mapping

Survey instruments encode special responses as negative integers. These must be systematically recoded to proper missing values before any analysis.

### Standard Codes

| Code | Meaning | Target |
|------|---------|--------|
| -99 | Don't know | Missing (dk) |
| -88 | Refused | Missing (rf) |
| -77 | Not applicable | Missing (na) |
| -66 | Other (specify) | Keep or reclassify (see [Other-Specify Classification](other-specify.md#other-specify-classification)) |
| -98 | Don't remember | Missing (dk) |

### Workflow

1. **Document before recoding**: Before recoding any sentinel codes, summarize or tabulate the variable to record which codes are present and their frequency.
2. **Assert coverage**: All negative values should be accounted for by known sentinel codes. If `var < 0`, it should equal one of the known codes --- otherwise there is an unknown code.
3. **Assert tolerance**: The share of sentinel codes should be below a documented threshold (see [Tolerance Specification Guidelines](aggregates.md#tolerance-specification-guidelines)).
4. **Recode systematically**: Map all applicable sentinel codes in a single pass per variable or variable group. Do not recode only -99 when -88 and -77 are also present in the instrument.
5. **Recode before arithmetic that transforms values**: Integer sentinels must be recoded to missing *before* any arithmetic that changes their magnitude (especially division/reciprocal). `1/(-55)` produces `-0.018` — a real-looking negative value that a later recode on the derived variable will never match. Multiplication and addition are less dangerous (the result is still large/negative, so recode or non-negative assertions can catch it), but division and modular arithmetic can produce values in the plausible range. When sentinel recoding and arithmetic happen in different modules, ensure the recode runs first or add a non-negative assertion on the derived variable to catch leaks.
6. **Handle -66 separately**: Other-specify codes require classification before recoding (see [Other-Specify Classification](other-specify.md#other-specify-classification)).

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

## In Stata

### Structural vs. Response Missing (Stata)

> For the principle (structural vs. response missing, flag design), see [Structural vs. Response Missing](#structural-vs-response-missing).

```stata
* Create explicit flags
gen mi_struct_interest = (interest_percent_1 == 0) & mi(interest_rate_12m_1)

* Assert structural missing implies missing content
assert mi(interest_rate_12m_1) if mi_struct_interest

* Assert non-structural paths supply data
assert !mi(interest_rate_12m_1) if interest_percent_1 == 1
```

### Documenting Special Code Proportions

```stata
count if loan_amount == -99
local dk = r(N)
count
assert `dk'/r(N) <= 0.02  // <=2% don't know allowed; adjust if instrument changes
```

### Missing Value Tracking

> For the principle (flag design, when to create flags), see [Missing Value Tracking](#missing-value-tracking).

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
df, meta = pyreadstat.read_dta("file.dta", usecols=["var"], user_missing=True)
# Extended missings show as string tags: 'd', 'r', etc.
ext_miss = df["var"][df["var"].apply(lambda x: isinstance(x, str))]
```
