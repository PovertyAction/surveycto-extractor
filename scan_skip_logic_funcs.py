"""
Scan all stata_skip_logic values in the ltfu_hh variable dictionary for
untranslated SurveyCTO functions.  Report-only — modifies nothing.
"""
import json
import re
from pathlib import Path

JSON_PATH = Path(__file__).resolve().parents[3] / (
    "docs/ltfu/survey_documentation/ltfu_hh/ltfu_hh_variable_dictionary.json"
)

with open(JSON_PATH, encoding="utf-8") as f:
    data = json.load(f)

skip_logic: dict[str, str] = {}
for var_name, var_data in data["variables"].items():
    sl = (var_data.get("survey") or {}).get("stata_skip_logic") or ""
    if sl:
        skip_logic[var_name] = sl

print(f"Total vars with non-empty stata_skip_logic: {len(skip_logic)}")
print()

# Each entry: (display_name, compiled_regex)
FUNCS = [
    ("selected()",       re.compile(r"\bselected\s*\(",       re.IGNORECASE)),
    ("count-selected()", re.compile(r"count-selected\s*\(",   re.IGNORECASE)),
    ("if()",             re.compile(r"\bif\s*\(",              re.IGNORECASE)),
    ("position()",       re.compile(r"\bposition\s*\(",        re.IGNORECASE)),
    ("once()",           re.compile(r"\bonce\s*\(",            re.IGNORECASE)),
    ("coalesce()",       re.compile(r"\bcoalesce\s*\(",        re.IGNORECASE)),
    ("regex()",          re.compile(r"\bregex\s*\(",           re.IGNORECASE)),
    ("contains()",       re.compile(r"\bcontains\s*\(",        re.IGNORECASE)),
    ("starts-with()",    re.compile(r"starts-with\s*\(",       re.IGNORECASE)),
    ("ends-with()",      re.compile(r"ends-with\s*\(",         re.IGNORECASE)),
    ("not()",            re.compile(r"\bnot\s*\(",             re.IGNORECASE)),
    ("string()",         re.compile(r"\bstring\s*\(",          re.IGNORECASE)),
    ("number()",         re.compile(r"\bnumber\s*\(",          re.IGNORECASE)),
    ("jr:choice-name",   re.compile(r"jr:choice-name",         re.IGNORECASE)),
    ("index()",          re.compile(r"\bindex\s*\(",           re.IGNORECASE)),
    ("count()",          re.compile(r"\bcount\s*\(",           re.IGNORECASE)),
]

found_any = False
for name, pattern in FUNCS:
    hits = {v: sl for v, sl in skip_logic.items() if pattern.search(sl)}
    if not hits:
        continue
    found_any = True
    unique_conds = sorted(set(hits.values()))
    print(f"{name:20s}  {len(hits):5d} vars  |  {len(unique_conds)} unique conditions")
    for cond in unique_conds[:3]:
        print(f"    {cond[:110]}")
    if len(unique_conds) > 3:
        print(f"    ... ({len(unique_conds) - 3} more unique conditions)")
    print()

if not found_any:
    print("No untranslated SurveyCTO functions found.")
