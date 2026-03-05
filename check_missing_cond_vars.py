"""Find tabstat groups whose IF condition references a variable that does not exist."""
import re
import pyreadstat
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent.parent
DTA = PROJECT / "data/ltfu/raw_nopii/ugs_ltfu_hh_checked.dta"
DO  = PROJECT / "scripts/cleaning/cohort1/ltfu/ltfu_hh_summary_stats.do"

df, _ = pyreadstat.read_dta(str(DTA), apply_value_formats=False, row_limit=1)
all_vars = set(df.columns)

content = open(DO, encoding="utf-8").read()
groups = re.findall(r"\* --- Group (\d+): if (.+?) \| (\d+) vars", content)

SKIP = {"missing", "rowtotal", "inlist", "cond", "if"}
missing_issues = []
for gnum, cond, nvars in groups:
    cond_vars = set(re.findall(r"\b([a-z_]\w+)\b", cond)) - SKIP
    missing = cond_vars - all_vars
    if missing:
        missing_issues.append((gnum, sorted(missing), cond[:100]))

print(f"Groups with missing condition var: {len(missing_issues)}")
for gnum, vs, cond in missing_issues:
    print(f"  G{gnum} ({vs}): {cond}")
