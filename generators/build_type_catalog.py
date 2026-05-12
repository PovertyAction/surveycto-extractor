"""
Build a JSON type catalog from coding_guidelines/surveycto_refs/xlsform.md.

The catalog records each SurveyCTO ``type`` value's default Stata storage
type plus structural classifications (numeric / string / structural /
hidden / supports_constraint). Useful as a reference artifact for
documentation and dictionary tooling; not currently loaded at run time
by any pipeline. Regenerate whenever the vendored xlsform.md changes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "coding_guidelines" / "surveycto_refs" / "xlsform.md"
OUTPUT = REPO_ROOT / "coding_guidelines" / "surveycto_refs" / "_type_catalog.json"


# Default Stata storage types per SurveyCTO field type.
# Numeric integer-coded fields get `long`; numeric continuous get `double`;
# strings get a default `str32` (text size hints come from constraint at use
# time downstream, not here). Structural rows have no storage type.
_STATA_TYPE_DEFAULTS: Dict[str, str] = {
    "text":          "str32",
    "integer":       "long",
    "decimal":       "double",
    "select_one":    "long",
    "select_multiple": "byte",   # binary per-choice columns
    "enumerator":    "str32",
    "geopoint":      "double",   # may be split into 4 components on import
    "geoshape":      "str244",
    "geotrace":      "str244",
    "barcode":       "str32",
    "datetime":      "double",
    "date":          "long",
    "time":          "long",
    "image":         "str244",
    "audio":         "str244",
    "video":         "str244",
    "file":          "str244",
    "note":          None,        # display-only, no data column
    "start":         "double",
    "end":           "double",
    "deviceid":      "str32",
    "subscriberid":  "str32",
    "simserial":     "str32",
    "phonenumber":   "str32",
    "username":      "str32",
    "caseid":        "str32",
    "comments":      "str244",
    "calculate":     "long",       # SurveyCTO exports as text but most are numeric
    "calculate_here":"long",
    "speed violations audit": "str244",
    "speed violations count": "long",
    "speed violations list":  "str244",
    "text audit":    "str244",
    "audio audit":   "str244",
    "sensor_statistic": "double",
    "sensor_stream": "str244",
    # Structural types have no storage
    "begin group":   None,
    "end group":     None,
    "begin repeat":  None,
    "end repeat":    None,
    # Legacy XLSForm/ODK aliases sometimes seen on inherited forms
    "today":         "long",
    "email":         "str244",
    "acknowledge":   "byte",
}


_NUMERIC_TYPES = {
    "integer", "decimal", "select_one", "select_multiple",
    "date", "time", "datetime", "geopoint", "calculate",
    "calculate_here", "today", "acknowledge",
    "speed violations count", "sensor_statistic",
    "start", "end",
}
_STRING_TYPES = {
    "text", "enumerator", "geoshape", "geotrace", "barcode",
    "image", "audio", "video", "file", "deviceid",
    "subscriberid", "simserial", "phonenumber", "username",
    "caseid", "comments", "speed violations audit",
    "speed violations list", "text audit", "audio audit",
    "sensor_stream", "email",
}
_STRUCTURAL_TYPES = {
    "begin group", "end group", "begin repeat", "end repeat", "note",
}


def _storage_class(t: str) -> str:
    if t in _NUMERIC_TYPES:
        return "numeric"
    if t in _STRING_TYPES:
        return "string"
    if t in _STRUCTURAL_TYPES:
        return "structural"
    return "string"   # safe default


def _supports_constraint(t: str) -> bool:
    # SurveyCTO documents `constraint` as applicable to "editable fields".
    # Calculate and structural rows do not collect user input.
    return t in (
        "text", "integer", "decimal", "select_one", "select_multiple",
        "enumerator", "geopoint", "geoshape", "geotrace", "barcode",
        "datetime", "date", "time", "image", "audio", "video", "file",
    )


def parse_field_types(md_text: str) -> Dict[str, Dict]:
    """Parse the '## Field Types' table from xlsform.md."""
    # Locate the section
    m = re.search(
        r'^## Field Types\s*\n(.*?)(?=^## )',
        md_text, re.MULTILINE | re.DOTALL)
    if not m:
        raise RuntimeError("Could not find '## Field Types' section in xlsform.md")
    section = m.group(1)

    # Extract table rows: lines starting with `| `<type-value>`
    catalog: Dict[str, Dict] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| `type`"):
            continue
        # split by | and discard outer empties
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 7:
            # | <empty> | col0 | col1 | col2 | col3 | col4 | <empty>
            continue
        type_cell = cells[1]
        visible = cells[2].lower()
        what    = cells[3]

        # The type cell looks like:
        #   `text`
        #   `select_one listname`
        #   `select_multiple listname`
        #   `today`, `email`, `acknowledge`           ← multi-type combined
        #   `begin group` / `end group`               ← structural pair
        #   `sensor_statistic ...`                    ← variant
        if not type_cell.startswith("`"):
            continue
        for raw in re.findall(r'`([^`]+)`', type_cell):
            # Normalize variants
            t = raw.strip()
            if t.endswith(" listname"):
                t = t.replace(" listname", "")
            if t.endswith(" ..."):
                t = t.replace(" ...", "").strip()
            if t in catalog:
                continue
            catalog[t] = {
                "surveycto_type": t,
                "visible": visible.startswith("y"),
                "hidden":  visible == "hidden",
                "structural": visible == "structural",
                "storage_class": _storage_class(t),
                "stata_type": _STATA_TYPE_DEFAULTS.get(t),
                "supports_constraint": _supports_constraint(t),
                "description": what,
            }

    return catalog


def main() -> None:
    md_text = SOURCE.read_text(encoding="utf-8")
    catalog = parse_field_types(md_text)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, sort_keys=True)
    print(f"[OK] {OUTPUT.relative_to(REPO_ROOT)}: {len(catalog)} types")


if __name__ == "__main__":
    main()
