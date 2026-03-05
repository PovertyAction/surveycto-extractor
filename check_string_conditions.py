"""Find tabstat groups whose IF condition references a TRULY string-typed Stata variable.

Uses metadataonly=True to read Stata storage types (int8, float32, string, etc.)
so that variables stored as int8/float that pyreadstat returns as object at row_limit=1
are NOT falsely classified as strings.

Also filters out variables that appear in the do-file's destring block (ds_* locals),
since those will be converted to numeric before tabstat runs and are safe to use
in numeric comparisons in the IF clause.
"""
import re
import pyreadstat
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent.parent
DTA = PROJECT / "data/ltfu/raw_nopii/ugs_ltfu_hh_checked.dta"
DO  = PROJECT / "scripts/cleaning/cohort1/ltfu/ltfu_hh_summary_stats.do"

# Use metadataonly to get accurate Stata storage types (avoids row_limit=1 object ambiguity)
_, meta = pyreadstat.read_dta(str(DTA), metadataonly=True)
raw_str_vars = {col for col, vtype in meta.readstat_variable_types.items() if vtype == "string"}
print(f"String vars in dataset (by Stata type): {len(raw_str_vars)}")

content = open(DO, encoding="utf-8").read()

# Extract all variables in the destring locals (ds_1, ds_2, ...).
# These will be destringed before tabstat runs, so they become numeric and are safe.
destring_vars: set[str] = set()
for m in re.finditer(r"^\s*local ds_\d+ (.+)$", content, re.MULTILINE):
    destring_vars.update(m.group(1).split())
print(f"Vars in destring block    : {len(destring_vars)}")

# String vars that will REMAIN string after destring (the truly problematic ones)
str_vars_after_destring = raw_str_vars - destring_vars
print(f"Truly-string after destring: {len(str_vars_after_destring)}")

groups = re.findall(r"\* --- Group (\d+): if (.+?) \| (\d+) vars", content)

# Skip common function names
SKIP = {"missing", "rowtotal", "inlist", "cond"}
issues = []
for gnum, cond, nvars in groups:
    cond_vars = set(re.findall(r"\b([a-z_]\w+)\b", cond)) - SKIP
    str_in_cond = cond_vars & str_vars_after_destring
    if str_in_cond:
        issues.append((gnum, sorted(str_in_cond), cond[:100]))

print(f"\nGroups with TRULY-STRING var in condition (r(109) risk): {len(issues)}")
for gnum, vs, cond in issues:
    print(f"  G{gnum} ({vs}): {cond}")
