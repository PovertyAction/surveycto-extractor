"""Single source of truth for sentinel / special-missing codes.

A "sentinel" is a numeric code standing in for a non-response reason
(don't-know, refused, ...). The mapping from code to human label and Stata
extended-missing is a **project convention**, not a fixed standard -- IPA
commonly uses -99/-88/..., but another team may use an entirely different set.
So the table is overridable: define ``SENTINEL_MEANINGS`` in your ``config.py``
to replace the default wholesale.

Both ``create_variable_dictionaries.py`` (labels choice codes, scans data for
sentinel cells) and ``load_survey_metadata.py`` (emits the Stata ``mvdecode``
recode) read from here, so the two can no longer disagree (they previously did
-- see issue #26.8, where -99 meant "Refused" in one module and "Don't know" in
the other).

Format: ``{code:int -> (label:str, stata_missing:str)}``. ``stata_missing`` is
the Stata extended-missing target (``.d``, ``.r``, ...) used by ``mvdecode``.
"""
from typing import Dict, List, Optional, Tuple

# IPA default convention. Override by defining SENTINEL_MEANINGS in config.py.
DEFAULT_SENTINEL_MEANINGS: Dict[int, Tuple[str, str]] = {
    -99: ("Don't know", ".d"),
    -88: ("Refused to answer", ".r"),
    -77: ("Not applicable", ".n"),
    -66: ("Other (specify)", ".o"),
    -55: ("Not in list", ".m"),
}

# Codes scanned/counted in the data as sentinels but NOT recoded (no Stata
# missing target). Kept for continuity with the historical scan list, which
# included -98. Override with SENTINEL_SCAN_ONLY in config.py.
DEFAULT_SCAN_ONLY: List[int] = [-98]


def resolve_sentinel_meanings(config=None) -> Dict[int, Tuple[str, str]]:
    """Resolve the active meanings table: ``config.SENTINEL_MEANINGS`` if the
    config module defines it (wholesale override), else the IPA default."""
    if config is not None and getattr(config, "SENTINEL_MEANINGS", None):
        return {int(k): tuple(v) for k, v in config.SENTINEL_MEANINGS.items()}
    return dict(DEFAULT_SENTINEL_MEANINGS)


def resolve_scan_only(config=None) -> List[int]:
    """Extra scan-only codes (counted but never recoded)."""
    if config is not None and getattr(config, "SENTINEL_SCAN_ONLY", None) is not None:
        return [int(c) for c in config.SENTINEL_SCAN_ONLY]
    return list(DEFAULT_SCAN_ONLY)


def sentinel_scan_codes(meanings: Dict[int, Tuple[str, str]] = None,
                        scan_only: List[int] = None) -> List[int]:
    """All integer codes to scan/count as sentinels (meanings + scan-only),
    ordered most-negative first for stable, human-readable output."""
    meanings = meanings if meanings is not None else DEFAULT_SENTINEL_MEANINGS
    scan_only = scan_only if scan_only is not None else DEFAULT_SCAN_ONLY
    return sorted(set(meanings) | set(scan_only))


def sentinel_scan_strings(meanings: Dict[int, Tuple[str, str]] = None,
                          scan_only: List[int] = None) -> set:
    """String forms of the scan codes (for scanning string-typed columns)."""
    return {str(c) for c in sentinel_scan_codes(meanings, scan_only)}


def sentinel_meaning(code, meanings: Dict[int, Tuple[str, str]] = None) -> Optional[str]:
    """Human label for a choice/sentinel code, or None if not a sentinel."""
    meanings = meanings if meanings is not None else DEFAULT_SENTINEL_MEANINGS
    s = str(code).strip()
    if not s.lstrip("-").isdigit():
        return None
    entry = meanings.get(int(s))
    return entry[0] if entry else None


def mvdecode_spec(meanings: Dict[int, Tuple[str, str]] = None) -> str:
    """Render the Stata ``mvdecode ... mv(...)`` body from the meanings table,
    e.g. ``-99=.d \\ -88=.r \\ ...`` (codes ascending, so -99 leads -- matching
    the historical hand-written recode for the default table)."""
    meanings = meanings if meanings is not None else DEFAULT_SENTINEL_MEANINGS
    return " \\ ".join(f"{code}={miss}" for code, (_label, miss)
                       in sorted(meanings.items()))
