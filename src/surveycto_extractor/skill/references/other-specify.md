# Other-specify answers and one-off corrections

Read this before recoding "other, specify" text into codes or correcting a single observation's value. Principles come first, then the Stata code.

## Contents

- [Other-Specify Classification](#other-specify-classification)
- [One-Off Data Corrections](#one-off-data-corrections)
- [In Stata](#in-stata)
  - [Other-Specify Classification](#other-specify-classification)

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

```text
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

## In Stata

### Other-Specify Classification

> For the principle (blank=missing, priority ordering, absence responses, QC table), see [Other-Specify Classification](#other-specify-classification).

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
