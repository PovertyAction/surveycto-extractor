# SurveyCTO Relevance → Stata Translation Reference (v2.0)

This document defines the canonical rules for converting SurveyCTO relevance expressions
into Stata `if` conditions. It covers every function/pattern encountered in your project's
survey instruments, the correct Stata equivalent, and what cannot be translated.

**Audience**: developers extending `logic_converter.py`; analysts debugging skip-logic errors
in `tabstat` or `replace if` contexts.

**Sources**:
- SurveyCTO Expressions Reference — https://docs.surveycto.com/02-designing-forms/01-core-concepts/09.expressions.html
- SurveyCTO Data Export Format — https://docs.surveycto.com/05-exporting-and-publishing-data/01-overview/09.data-format.html

**Downstream consumer**: `transformers/logic_converter.py`

---

## Table of Contents

1. [SurveyCTO Data Model in Stata Wide](#1-surveycto-data-model-in-stata-wide)
2. [Variable Name Substitution](#2-variable-name-substitution)
3. [Operators](#3-operators)
4. [The Stata Missing-Value Trap for Numeric Comparisons](#4-the-stata-missing-value-trap-for-numeric-comparisons)
5. [The `selected()` Function (Critical)](#5-the-selected-function-critical)
6. [Empty-String and Empty-Field Semantics](#6-empty-string-and-empty-field-semantics)
7. [String Functions](#7-string-functions)
8. [Numeric and Math Functions](#8-numeric-and-math-functions)
9. [Date and Time Functions](#9-date-and-time-functions)
10. [Repeat-Group Functions](#10-repeat-group-functions)
11. [Device and Metadata Functions](#11-device-and-metadata-functions)
12. [Clause-Stripping Fallback](#12-clause-stripping-fallback)
13. [What Cannot Be Translated](#13-what-cannot-be-translated)
14. [Implementation Checklist](#14-implementation-checklist)

---

## 1. SurveyCTO Data Model in Stata Wide

After SurveyCTO exports a dataset as wide CSV and it is read into Stata, each question type
maps to one or more Stata columns:

| SurveyCTO question type | Stata representation |
|-------------------------|----------------------|
| `select_one list` | Single integer column `var` (value = numeric choice code) |
| `select_multiple list` | **Two forms in the export**: (1) string column `var` containing space-separated selected codes; and (2) one 0/1 column per choice using the naming convention in §1.1. |
| `integer`, `decimal`, `calculate` | Single numeric column `var` |
| `text`, `barcode` | String column `var` |
| `date` | Numeric Stata date column `var` (days since 1960-01-01), or string depending on export settings |
| `datetime` | Numeric Stata datetime column `var` (ms since 1960-01-01), or string |
| `geopoint` | 4 columns: `var_latitude`, `var_longitude`, `var_altitude`, `var_accuracy` |
| Repeat group (N iterations) | Creates a `var_repeat_count` column, then all fields inside get suffix `_1` … `_N`: e.g., `${hhmem_age}` in iteration 5 → `hhmem_age_5` |

### 1.1 Column naming for `select_multiple`

SurveyCTO appends the choice code to the base variable name with a `_` separator.
When the choice code is negative, the `-` sign itself becomes a second `_` because
Stata variable names cannot contain `-`:

| Choice code | Column name | Rule |
|-------------|-------------|------|
| `1` | `var_1` | `var` + `_` + `1` |
| `99` | `var_99` | `var` + `_` + `99` |
| `-66` (Other) | `var__66` | `var` + `_` + `-66` → `-` becomes `_` → `var__66` |
| `-88` (Refused) | `var__88` | Same — double underscore from sign conversion |
| `-99` (Don't know) | `var__99` | Same |

**Implementation**: to construct the column suffix from a choice code string `N`:
- If `N` is non-negative: suffix = `_N` (e.g., `_1`, `_99`)
- If `N` is negative: suffix = `__abs(N)` (e.g., `__66`, `__99`)

Or equivalently: replace `-` with `_` in the code string, then prepend `_`:
`suffix = "_" + code.replace("-", "_")` — giving `_1`, `_99`, `__66`, `__99`.

### 1.2 Nested repeat + `select_multiple`

For a `select_multiple` inside a repeat group at iteration x with choice code N:
`var_N_x` (positive code) or `var__abs(N)_x` (negative code).

For nested repeats (outer iteration x, inner iteration y): `var_x_y`.

*(Per SurveyCTO documentation and observed data; verify if using deeply nested repeats.)*

**Critical rule**: to translate `selected(${var}, 'N')` correctly, you MUST know whether
`var` is `select_one` or `select_multiple`. The translation differs. See §5.

---

## 2. Variable Name Substitution

### 2.1 Basic substitution

Every `${varname}` reference is replaced with `varname` (bare identifier, no braces).

```
${consent}      →  consent
${hh_size}      →  hh_size
${hhmem_age}    →  hhmem_age        (then suffixed if inside a repeat — see §2.2)
```

### 2.2 Repeat-group subscripting

Inside a repeat group, fields that belong to the **same** repeat get an iteration suffix.
Fields from **outer scope** (the main questionnaire or a parent group) do NOT get a suffix.

| Reference | Context | Rule | Result |
|-----------|---------|------|--------|
| `${hhmem_age}` | Inside `hhmem` roster, iteration 5 | Own-group var → suffix N | `hhmem_age_5` |
| `${hh_size}` | Inside any roster, any iteration | Outer-scope var → no suffix | `hh_size` |

For nested repeats: own-group var in outer iteration x, inner iteration y → `var_x_y`.

**Implementation note**: The `skip_logic_iteration_specific` field in the variable
dictionary already applies subscript substitution at extraction time. `logic_converter.py`
receives the already-substituted string; it does NOT need to re-derive iteration indices.

### 2.3 Implementation order

Variable substitution (`${…}` → bare name) must happen **before** any function translation,
because functions reference the bare names:

```
Step 1: ${food_source} → food_source
Step 2: selected(food_source, '3') → food_source == 3    (select_one)
                                   → food_source_3 == 1   (select_multiple, positive code)
```

---

## 3. Operators

### 3.1 Comparison operators

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `= N` (single `=`) | `== N` | Use regex with lookbehind/lookahead — see §3.3 |
| `!= N` | `!= N` | Unchanged |
| `> N` | `> N & !missing(var)` | **Must add missing guard** — see §4 |
| `>= N` | `>= N & !missing(var)` | **Must add missing guard** — see §4 |
| `< N` | `< N & !missing(var)` | **Must add missing guard** — see §4 |
| `<= N` | `<= N & !missing(var)` | **Must add missing guard** — see §4 |

### 3.2 Logical operators

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `and` | `&` | Word-boundary match: `\band\b` |
| `or` | `\|` | Word-boundary match: `\bor\b` |
| `not(expr)` | `!(expr)` | Replace opener only; closing paren stays balanced |

### 3.3 = to == translation — exact regex

The Python regex for safe single-`=` replacement:

```python
re.sub(r'(?<![!><=])=(?!=)', '==', expr)
```

The negative lookbehind `(?<![!><=])` prevents firing on `!=`, `>=`, `<=`, and the
negative lookahead `(?!=)` prevents firing on already-doubled `==`. Standard string
`.replace('=', '==')` will corrupt all existing operators and must **never** be used.

---

## 4. The Stata Missing-Value Trap for Numeric Comparisons

**This is one of the most dangerous translation pitfalls.**

In SurveyCTO, if a numeric field is unanswered (blank), any comparison involving it
evaluates to `false`. In Stata, an unanswered numeric field becomes `.` (system missing).
Because Stata treats `.` as **positive infinity** for all relational operators:

```
SurveyCTO: ${age} > 18   → false when age is blank
Stata:      age > 18      → TRUE  when age == .   (because . > any finite number)
Stata:      age < 18      → FALSE when age == .   (because . > any finite number)
```

**Rule**: whenever translating `>`, `>=`, `<`, or `<=`, append `& !missing(var)`:

| SurveyCTO | Stata (correct) |
|-----------|-----------------|
| `${age} > 18` | `age > 18 & !missing(age)` |
| `${hh_size} >= 5` | `hh_size >= 5 & !missing(hh_size)` |
| `${income} < 0` | `income < 0 & !missing(income)` |

**Extended missing codes** (`.d`, `.r`, etc.): Stata sentinel recodes produce extended
missing values, which are also treated as greater than any finite number. The
`!missing(var)` guard catches all of these because `missing(var)` returns 1 for `.` and
`.a`–`.z`.

---

## 5. The `selected()` Function (Critical)

`selected()` is the most common skip-logic function and requires the most care.

### 5.1 Pattern A — Static literal code (most common)

Second arg is a quoted integer code. Translation depends on question type.

**`select_one`**: emit `var == N`

```
selected(${plot_use}, '2')          →  plot_use == 2
selected(${empact_earnfreq}, '-66') →  empact_earnfreq == -66
not(selected(${plot_use}, '4'))     →  !(plot_use == 4)
```

**`select_multiple`**: construct column name using the sign-to-underscore rule (§1.1),
then emit `colname == 1`

```
selected(${lstock_own_6m}, '1')    →  lstock_own_6m_1 == 1     (positive: single _)
selected(${lstock_own_6m}, '-88')  →  lstock_own_6m__88 == 1   (negative: double __)
selected(${lstock_own_6m}, '-66')  →  lstock_own_6m__66 == 1   (negative: double __)
```

Verify the resulting column exists in the dataset before emitting; if absent, strip the
clause (r(111) prevention).

### 5.2 Pattern B — List in first arg (translatable as `inlist`)

The first arg is a space-separated list of codes. After `string()` stripping:
`selected('2 3 4', var)`. This means "is `var`'s value one of these codes?" → `inlist()`.

```
selected(string('2 3 4'), ${hhmem_status})     →  inlist(hhmem_status, 2, 3, 4)
selected(string('1 2'), ${hhmem_relation})     →  inlist(hhmem_relation, 1, 2)
not(selected('-99 0', ${plot_dispute}))        →  !inlist(plot_dispute, -99, 0)
selected(string('1 2 9 12 13 25'), ${unit})   →  inlist(unit, 1, 2, 9, 12, 13, 25)
```

Detect this pattern: after `string()` stripping, the first arg (after removing quotes)
contains spaces — split on spaces and emit `inlist(var, N1, N2, …)`.

Stata's `inlist()` accepts up to 250 arguments. For longer lists, nest:
`(inlist(var, N1, …, N250) | inlist(var, N251, …))`.

### 5.3 Pattern C — Dynamic second arg (untranslatable)

The second arg is a `${variable}` reference. Appears exclusively in repeat loops where
a loop-level calculate field holds the current iteration's code at runtime.

```
selected(${lstock_own_6m}, ${lstock_value})      →  strip (DYNAMIC_SELECTED)
selected(${hh_nothome_who}, ${hhmemnothome_pos}) →  strip (DYNAMIC_SELECTED)
selected(${asset_own_list}, ${asset_value})       →  strip (DYNAMIC_SELECTED)
```

These cannot be translated because `lstock_value_3` (for example) is a dataset column
whose value varies by household — it is not a static code.

### 5.4 `count-selected()` — Count of selected choices

Translates to the sum of all 0/1 exploded columns. Use `rowtotal()` (not `+`) because
`rowtotal()` treats missing as 0 while `+` propagates missing.

```
// SurveyCTO: count-selected(${food_source}) >= 2
// Stata:
rowtotal(food_source_1 food_source_2 food_source_3 … food_source_K) >= 2
```

SurveyCTO standard exports set unselected choices to `0`, not `.`, so `+` would also
work in practice. Use `rowtotal()` as the safe default.

### 5.5 `selected-at()` — Positional selection

`selected-at(field, N)` returns the Nth selected choice from a `select_multiple` (0-based).
Positional lookup; no direct Stata equivalent in wide format. → **Strip**.

### 5.6 Implementation: `question_types` dict

`convert_to_stata()` must accept an optional dict mapping bare variable names to their
SurveyCTO type string:

```python
def convert_to_stata(
    expr: Optional[str],
    question_types: dict[str, str],
    ...
) -> Optional[str]:
    ...
```

`question_types` is **required**. Pass `{}` if question types are genuinely unknown
(all `selected()` calls will fall back to `select_one` → `var == N`).

---

## 6. Empty-String and Empty-Field Semantics

In SurveyCTO relevance expressions, `''` means "this field has no answer".

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `${var} != ''` | `!missing(var)` | Field was answered → not missing in Stata |
| `${var} = ''` | `missing(var)` | Field is blank → missing in Stata |
| `${var} != ""` | `!missing(var)` | Same with double quotes |
| `${var} = ""` | `missing(var)` | Same with double quotes |
| `string-length(${var}) > 0` | `!missing(var)` | Length > 0 ≡ answered |
| `string-length(${var}) = 0` | `missing(var)` | Length == 0 ≡ empty |
| `empty(${var})` | `missing(var)` | SurveyCTO `empty()` function |

**Note**: `missing(var)` returns 1 if `var` is `.`, `.a`–`.z`, or `""` (strings).
It works correctly for both numeric and string variables.

**Implementation order**: Run empty-string substitution **before** the single-`=` → `==`
step, because `${var} = ''` must become `missing(var)`, not `missing(var ==)`.

---

## 7. String Functions

### 7.1 Function translations

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `string(field)` | `string(var)` | Convert numeric to string |
| `string-length(${var})` | `length(var)` | `strlen()` also works |
| `substr(${var}, s, e)` | `substr(var, s+1, e-s)` | **Index mismatch**: SurveyCTO is 0-based with exclusive end; Stata is 1-based with length. See §7.2 |
| `concat(a, b, c, …)` | `a + b + c + …` | Stata string `+`. If any arg is numeric, wrap it in `string()`. See §7.3 |
| `linebreak()` | `char(10)` | Newline character |
| `lower(${var})` | `lower(var)` | Same name |
| `upper(${var})` | `upper(var)` | Same name |
| `regex(${var}, 'pat')` | `regexm(var, "pat")` | Change quote style; Stata uses ERE (SurveyCTO uses Java regex engine, but syntax overlaps for most patterns) |
| `contains(${var}, 'str')` | `strpos(var, "str") > 0` | `strpos` returns 0 if not found |
| `starts-with(${var}, 'str')` | `substr(var, 1, length("str")) == "str"` | |
| `ends-with(${var}, 'str')` | `substr(var, -length("str"), .) == "str"` | Negative offset = from right |

### 7.2 `substr()` index translation

SurveyCTO `substr(str, startindex, endindex)`:
- `startindex`: 0-based (0 = first character)
- `endindex`: exclusive (character at `endindex` is NOT included)

Stata `substr(str, start, len)`:
- `start`: 1-based (1 = first character)
- `len`: number of characters to return

Translation: `substr(str, s, e)` → `substr(str, s+1, e-s)`

```
substr(${phone}, 0, 3)   →  substr(phone, 1, 3)   // first 3 chars
substr(${id}, 2, 5)      →  substr(id, 3, 3)       // chars at index 2,3,4
```

### 7.3 `concat()` type mismatch

In Stata, string concatenation with `+` requires both operands to be strings. If a
numeric variable is concatenated with a string literal, Stata throws a type mismatch error.

```
// SurveyCTO: concat('Age: ', ${age})
// Stata (broken):  "Age: " + age
// Stata (correct): "Age: " + string(age)
```

**Rule**: when any `concat()` argument is a bare variable (not a string literal), wrap it
in `string()` if the variable is numeric type. If type is unknown, wrap defensively.

### 7.4 Space-separated string list functions (strip all)

| SurveyCTO | Action |
|-----------|--------|
| `count-items(sep, field)` | Strip |
| `item-at(sep, field, N)` | Strip |
| `item-index(sep, field, val)` | Strip |
| `item-present(sep, field, val)` | Strip |
| `de-duplicate(sep, field)` | Strip |
| `rank-value(val, list)` | Strip |

---

## 8. Numeric and Math Functions

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `number(${var})` | `real(var)` | Convert string to number |
| `int(${var})` | `int(var)` | Truncate toward zero |
| `round(${var}, digits)` | `round(var, 10^(-digits))` | SurveyCTO takes decimal places; Stata takes a unit |
| `abs(${var})` | `abs(var)` | Same |
| `pow(base, exp)` | `base^exp` | Stata uses `^` operator |
| `sqrt(${var})` | `sqrt(var)` | Same |
| `log10(${var})` | `log10(var)` | Same |
| `exp(${var})` | `exp(var)` | Same |
| `sin/cos/tan/asin/acos/atan` | Same names | |
| `atan2(x, y)` | `atan2(y, x)` | **Arg order reversed** in Stata |
| `pi()` | `_pi` | Stata built-in constant, not a function |
| `${var} mod N` | `mod(var, N)` | Stata uses function, not operator |
| `${var} div N` | `floor(var / N)` | SurveyCTO integer division operator; Stata has no `div`, use `floor()` |
| `if(cond, t, f)` | `cond(cond, t, f)` | Rename function only |
| `coalesce(${a}, ${b})` | `cond(missing(a), b, a)` | 2-arg: first non-missing |
| `coalesce(${a}, ${b}, ${c})` | `cond(missing(a), cond(missing(b), c, b), a)` | 3-arg: nest `cond()` |
| `coalesce(a, b, c, d, …)` | *(strip — `COALESCE_TOO_MANY`)* | > 3 args: too complex |
| `min(field1, field2, …)` | `min(var1, var2, …)` | Multi-field version |
| `max(field1, field2, …)` | `max(var1, var2, …)` | Multi-field version |
| `format-number(field)` | *(strip)* | Display formatting, not a filter |

---

## 9. Date and Time Functions

SurveyCTO stores dates as strings (YYYY-MM-DD) or numeric epoch values depending on the
export format. Stata stores dates as integers (days since 1960-01-01) or datetimes
(milliseconds since 1960-01-01). Comparisons between them require explicit casting.

### 9.1 Function translations

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `today()` | `mdy(month(c(current_date)), day(c(current_date)), year(c(current_date)))` | Stata date of today |
| `now()` | *(strip)* | Date+time; rarely useful as a static `if` condition |
| `date('YYYY-MM-DD')` | `date("YYYY-MM-DD", "YMD")` | Convert string literal to Stata date integer |
| `date(${var})` | `date(var, "YMD")` | Cast string date field to Stata date integer |
| `date-time('…')` | `clock("…", "YMDhms")` | Cast to Stata datetime (ms). **Warning**: SurveyCTO docs note that `date-time()` does not account for the time part in comparisons — use `decimal-date-time()` when time matters (but that is not translatable; strip instead) |
| `decimal-date-time(${var})` | *(strip)* | Unix epoch fractional days; different origin from Stata |
| `decimal-time(${var})` | *(strip)* | Fractional day (0–1); no Stata equivalent |
| `format-date-time(${var}, fmt)` | *(strip)* | String formatting; not a filter |
| `duration()` | *(strip)* | Device-level survey duration; not in dataset |

### 9.2 Date comparison pattern

```stata
// SurveyCTO: ${survey_date} > date('2023-01-01')
// Stata:
survey_date > date("2023-01-01", "YMD") & !missing(survey_date)
```

Always add `& !missing(var)` for date comparisons (same infinity-trap as §4).

### 9.3 Date export format uncertainty

Whether `date` and `datetime` fields arrive as numeric Stata dates or string columns
depends on export settings. Before emitting a date comparison, check the variable type in
`readstat_variable_types`. If string-typed, wrap in `date(var, "YMD")` before comparing;
if numeric, compare directly.

---

## 10. Repeat-Group Functions

| SurveyCTO | Stata | Notes |
|-----------|-------|-------|
| `count(repeatgroup)` | *(strip)* | Count of repeat instances; available as `var_repeat_count` column but not as an inline function |
| `count-if(repeatgroup, expr)` | *(strip)* | Conditional count |
| `sum(repeatedfield)` | *(strip)* | Sum across instances; could emit `rowtotal(var_1 … var_N)` but N must be known |
| `sum-if(…)`, `min-if(…)`, `max-if(…)`, `count-if(…)` | *(strip)* | Conditional aggregates |
| `min(repeatedfield)`, `max(repeatedfield)` | *(strip)* | Min/max across repeat instances |
| `join(sep, repeatedfield)` | *(strip)* | Concatenate repeat values |
| `join-if(sep, field, expr)` | *(strip)* | |
| `index()` | *(strip)* | Current repeat-group row index; meaningless in wide format |
| `position()` | *(strip)* | Same as `index()`; older syntax |
| `indexed-repeat(field, group, N)` | *(strip)* | Access Nth repeat instance; becomes `field_N` in wide format but N must be a static literal |
| `rank-index(idx, field)` | *(strip)* | Ranking within repeat group |
| `rank-index-if(idx, field, expr)` | *(strip)* | |

---

## 11. Device and Metadata Functions

These return survey device state, not household data. No meaning as row-level conditions
in a Stata dataset. → **Strip all**.

| SurveyCTO function | Reason to strip |
|--------------------|----------------|
| `enumerator-name()` | Device/login metadata |
| `enumerator-id()` | Device/login metadata |
| `phone-call-log()` / `phone-call-duration()` | Device telephony |
| `collect-is-phone-app()` | Device type check |
| `relevant(field)` | Returns whether a field was shown in this session |
| `once(expr)` | Evaluate-once cache; no Stata equivalent |
| `pulldata(source, col, key, val)` | Pre-loaded CSV lookup; data merged at export time |
| `hash(field, …)` | Cryptographic hash |
| `uuid()` | Random UUID |
| `username()` / `version()` / `device-info()` | Session metadata |
| `plug-in-metadata(field)` | Field plugin metadata |
| `jr:choice-name(list, ${var})` | Display label lookup; not a filter. Old function — SurveyCTO now recommends `choice-label()` instead |
| `choice-label(field, value)` | Display label lookup (newer version of `jr:choice-name()`); not a filter |
| `distance-between(p1, p2)` | Geospatial calculation |
| `area(geopoints)` / `geo-scatter(…)` / `short-geopoint(…)` | Geospatial |

---

## 12. Clause-Stripping Fallback

When a clause **cannot** be translated without risking a Stata error, strip that clause
only — not the entire condition.

### 12.1 Stripping rules

1. Identify the clause (the minimal subexpression that cannot be resolved).
2. Remove it and all adjacent `and`/`or` connectors that become dangling.
3. Merge the remaining clauses with `&`.
4. If **all** clauses strip → the variable becomes unconditional (included in the
   baseline tabstat group).

### 12.2 When to strip

| Situation | Reason code |
|-----------|-------------|
| `selected()` with dynamic variable as second arg | `DYNAMIC_SELECTED` |
| `selected()` and question type unknown | `UNKNOWN_TYPE_SELECTED` |
| `position()` / `index()` | `POSITION` |
| `once(expr)` | `ONCE` |
| `jr:choice-name()` | `CHOICE_NAME` |
| Any repeat-group aggregate (§10) | `REPEAT_AGGREGATE` |
| Any device/metadata function (§11) | `DEVICE_META` |
| `(0)` literal — always false | Drop entire variable: `ALWAYS_FALSE` |
| Comparison variable absent from dataset | `VAR_ABSENT` — r(111) prevention |
| String-typed variable with numeric comparison | `STRING_TYPE` — r(109) prevention |
| `coalesce()` with > 3 arguments | `COALESCE_TOO_MANY` |

### 12.3 Logging requirement

Every strip must emit one log line:

```
[STRIP] var=<varname>  clause=<original_clause>  reason=<reason_code>
```

### 12.4 Safety caveat — stripping inside `not()` changes semantics

Stripping a clause from a **top-level `AND` chain** only broadens the denominator. Safe.

**However, stripping inside `not()` is NOT safe.** Example:

```
not(position() = 1 and ${age} > 18)
```

Stripping `position() = 1` gives `not(age > 18)`. The original excluded people where
`position=1 AND age>18`; the stripped version excludes ALL people where `age > 18`.
This **shrinks** the denominator.

**Rule**: if any untranslatable clause appears inside `not(…)`, log `NOT_SAFE_TO_STRIP`
and strip the **entire** `not(…)` block (broadens — safer than corrupting semantics).
A full parse tree would be required to handle all boolean nesting correctly.

---

## 13. What Cannot Be Translated

| Construct | Why |
|-----------|-----|
| `position()` / `index()` | Repeat-group row index; meaningless in wide format |
| `once(expr)` | Evaluate-once cache; no Stata counterpart |
| `jr:choice-name()` | Display label lookup; not a filter |
| Dynamic `selected(var, other_var)` | Second arg is a variable whose value varies by row |
| `selected-at(field, N)` | Positional lookup in choice list; no Stata equivalent |
| Repeat aggregates | Would need hard-coded column lists; N unknown statically |
| Device/metadata functions | Not present in exported dataset |
| `decimal-date-time()`, `decimal-time()` | Unix-epoch based; different origin from Stata |
| Space-separated list functions | String-splitting; no static Stata equivalent |

---

## 14. Implementation Checklist

### Step order (critical — later steps assume earlier steps have run)

1. Strip `None`/empty inputs → return `None`
2. Replace `${var}` → `var`
3. Strip `string('X')` wrapper → `'X'` (before `selected()`)
4. Replace empty-string comparisons: `var = ''` → `missing(var)`, `var != ''` → `!missing(var)`
5. Replace `string-length(var) > 0` → `!missing(var)`, `= 0` → `missing(var)`
6. Replace `empty(var)` → `missing(var)`
7. Translate `if(cond, t, f)` → `cond(cond, t, f)`
8. Translate `coalesce(a, b)` → `cond(missing(a), b, a)`; 3-arg nested; strip if > 3
9. Translate `regex()`, `contains()`, `starts-with()`, `ends-with()`
10. Translate `substr(str, s, e)` → `substr(str, s+1, e-s)` (index adjustment)
11. Translate `concat(…)` with numeric→string casting
12. Translate `count-selected(var)` → `rowtotal(var_1 var_2 … var_K)` (requires choice list)
13. Translate Pattern B `selected('list', var)` → `inlist(var, N1, N2, …)`
14. Translate Pattern A `selected(var, 'N')` → `var == N` (select_one) or `var_N == 1` / `var__N == 1` (select_multiple)
15. Translate `not(` → `!(`
16. Replace single `=` → `==` using `(?<![!><=])=(?!=)`
17. Replace `and` → `&`, `or` → `|` (word-boundary)
18. Add `& !missing(var)` guard to `>`, `>=`, `<`, `<=` comparisons
19. Strip untranslatable clauses — check for `not()` context (§12.4)
20. Clean up extra whitespace

### Test cases

```python
# Column naming — positive vs negative codes
assert convert("selected(${ls}, '1')", {"ls": "select_multiple"}) == "(ls_1 == 1)"
assert convert("selected(${ls}, '-66')", {"ls": "select_multiple"}) == "(ls__66 == 1)"
assert convert("selected(${ls}, '-88')", {"ls": "select_multiple"}) == "(ls__88 == 1)"

# Pattern B: list-first-arg → inlist
assert convert("selected(string('2 3 4'), ${status})") == "inlist(status, 2, 3, 4)"
assert convert("not(selected('-99 0', ${var}))") == "!inlist(var, -99, 0)"

# Empty-string semantics
assert convert("${consent} != ''") == "!missing(consent)"
assert convert("${name} = ''") == "missing(name)"
assert convert("string-length(${var}) > 0") == "!missing(var)"

# Missing guard for relational operators
assert convert("${age} > 18") == "age > 18 & !missing(age)"
assert convert("${hh_size} >= 5") == "hh_size >= 5 & !missing(hh_size)"

# count-selected → rowtotal
# (K known from choice list at call time)
assert convert("count-selected(${food})") == "rowtotal(food_1 food_2 food_3)"

# substr index adjustment
assert convert("substr(${phone}, 0, 3)") == "substr(phone, 1, 3)"
assert convert("substr(${id}, 2, 5)") == "substr(id, 3, 3)"

# select_one (unchanged)
assert convert("selected(${food}, '3')", {"food": "select_one"}) == "(food == 3)"

# String functions
assert convert("regex(${var}, 'pat')") == 'regexm(var, "pat")'
assert convert("contains(${city}, 'Kampala')") == 'strpos(city, "Kampala") > 0'

# Ternary + missing guard inside
assert convert("if(${age} > 18, 1, 0)") == "cond(age > 18 & !missing(age), 1, 0)"

# coalesce
assert convert("coalesce(${a}, ${b})") == "cond(missing(a), b, a)"
assert convert("coalesce(${a}, ${b}, ${c})") == "cond(missing(a), cond(missing(b), c, b), a)"
```

---

## See Also

- [STATA.md — SurveyCTO Skip Logic in Stata](STATA.md#surveycto-skip-logic-in-stata) — runtime pitfalls (`r(109)`, `r(111)`, `r(2000)`)
- `transformers/logic_converter.py` — implementation file
- `extractors/json_extractor.py` — caller that builds the `question_types` dict
- `scan_skip_logic_funcs.py` — scan tool to count untranslated functions
- SurveyCTO Expressions Reference: https://docs.surveycto.com/02-designing-forms/01-core-concepts/09.expressions.html
- SurveyCTO Data Export Format: https://docs.surveycto.com/05-exporting-and-publishing-data/01-overview/09.data-format.html
