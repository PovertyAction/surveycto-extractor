# Potential Improvements

## 1. Sentinel scan: detect raw_int after destring

**File:** `create_variable_dictionaries.py`

Currently the scan runs on the raw pyreadstat DataFrame, so sentinel codes in
string columns show as `string: -98(32)`. After destring they become numeric
-98 values and would be `raw_int`. The scan catches them either way, but the
label is slightly misleading. Could run a second raw_int pass on destringed
columns, or re-label string sentinels that match known codes.

## 2. search_survey.py: sentinels for --search and --choice-list

**File:** `skill/search_survey.py`

Sentinel info only displays for `--var` lookups (goes through vardict).
`--search` and `--choice-list` go straight to questions.json and skip the
vardict, so no sentinel data. Could do a vardict lookup when the variable
name is known from the question match.

## 3. find_question_for_variable: second-level cache

**File:** `create_variable_dictionaries.py`

`_build_question_index` gives O(1) for exact and base-name matches. But the
fallback loop (split on `_`, try progressively shorter prefixes) still runs
when the first lookups miss. A resolved-name cache would help on wide datasets
with lots of nested repeat variables (~15k+ vars in g2r).

## 4. _ord.dta: global toggle

**File:** `config.template.py`, `create_variable_dictionaries.py`

`skip_ord_dta` exists per-dataset in config. No global toggle for projects
that never use the reordered dataset. Minor — just wasted I/O.

## 5. CSV extractor: warn on empty choice values from formula cells

**File:** `extractors/csv_extractor.py`

When an XLSX survey form uses Excel formulas in the choice `value` column
(e.g. `=L34`) that reference empty or unresolved cells, pandas reads them as
empty strings. The extractor correctly converts these to `pd.NA` and writes
them as blank, but does so silently — producing rows like
`followup_list,,${sn_hhrel_name20}` with no value.

Discovered in the G2R midline instrument (`UGS_midline_v25.xlsx`), where
`followup_list` rows 34+ have formula refs to an empty column L. The labels
(`${sn_hhrel_name}` dynamic refs) are present but the integer choice codes
are missing.

**Improvement:** After loading choices, check for rows where `list_name` and
`label` are non-empty but `value` is missing. Emit a warning like:
`WARNING: N choice(s) in list 'X' have empty values — check source XLSX for
unresolved formulas.` This won't fix the source but prevents silent
propagation into downstream docs and MCP lookups.

## 6. shares_choices edge filtering

**File:** `create_variable_dictionaries.py`

`shares_choices` edges from universal choice lists (yesnodk, yesno, yesnoref)
connect large portions of the survey into uninformative clusters. In the
sample survey, the top 4 yes/no lists account for 87 of 236 variables.

Could filter by variable count per list (e.g., only create edges when <= 5
variables share a list) or auto-detect universal lists by threshold. The
useful signal is rare/specific lists (crop_type shared by 3 vars, transport
shared by 3 vars). Needs testing on g2r to see if agents are confused by the
noise or just ignore it.
