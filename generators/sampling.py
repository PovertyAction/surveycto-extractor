"""
Shared value-sampling helpers
=============================

Constraint-aware sampling and bound extraction used by both the Stata
seed do-file generator (``seed_generator.py``) and the SurveyCTO-shaped
synthetic-data CSV generator (``synthetic_data.py``).

``numeric_bounds`` and ``text_max_length`` are pure parsers over a raw
SurveyCTO constraint string — they recognise the simple ``>= N`` / ``<= N``
patterns that show up in most real forms and ignore anything more exotic
(which would fall through to the evaluator instead).

``sample_python_value`` returns a typed Python value (int, float, str,
``datetime``, etc.). The Stata generator wraps these with literal-formatting;
the CSV generator writes them through pandas as-is.
"""

from __future__ import annotations

import datetime
import random
import re
from typing import Any, Dict, List, Optional, Tuple


# ── Bound extraction ──────────────────────────────────────────────────────────

_NUM_BOUND_RX = re.compile(
    r'(?:\.|[A-Za-z_]\w*)\s*(>=|<=|>(?!=)|<(?!=))\s*(-?\d+(?:\.\d+)?)'
)


def numeric_bounds(constraint: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """Extract ``(lower, upper)`` numeric bounds from a constraint expression.

    Recognises clauses like ``. >= N``, ``. > N``, ``. <= N``, ``. < N``,
    ``var >= N``, etc. on either side of ``and``. Inclusive on ``>=`` /
    ``<=``; for strict ``>`` / ``<`` we pin by ``+/- 1`` to land on an
    integer-safe inclusive bound. Returns ``(None, None)`` if no clean
    bound is extractable.

    Bails on conditional constraints (``if(...)``): a regex pass would
    pick bounds across unrelated branches and produce an inverted /
    over-clamped range. Example: ``if(index()=1, .>=18, if(index()=2,
    .>=3 and .<=6, .>=0 and .<=120))`` would otherwise yield ``lo=18,
    hi=6`` -> swap -> ``(6, 18)``. Better to fall back to the type-default
    range than to silently clamp.
    """
    if not constraint:
        return (None, None)
    if re.search(r"\bif\s*\(", constraint):
        return (None, None)

    lo: Optional[float] = None
    hi: Optional[float] = None
    for m in _NUM_BOUND_RX.finditer(constraint):
        op, val = m.group(1), float(m.group(2))
        if op == ">=":
            lo = val if lo is None else max(lo, val)
        elif op == ">":
            v = val + 1
            lo = v if lo is None else max(lo, v)
        elif op == "<=":
            hi = val if hi is None else min(hi, val)
        elif op == "<":
            v = val - 1
            hi = v if hi is None else min(hi, v)
    return (lo, hi)


def text_max_length(constraint: Optional[str]) -> Optional[int]:
    """Recognise ``string-length(.) <= N`` / ``<= N`` in a constraint."""
    if not constraint:
        return None
    m = re.search(r"<=\s*(\d+)", constraint)
    if m:
        return int(m.group(1))
    return None


_SM_COUNT_RX = re.compile(
    r"count-selected\(\s*\.\s*\)\s*(=|>=|<=|>(?!=)|<(?!=))\s*(\d+)"
)


def select_multiple_count_bounds(constraint: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """Extract ``(lo, hi)`` integer bounds on ``count-selected(.)`` from a
    select_multiple constraint.

    Recognises ``count-selected(.) = N`` (strict: lo=hi=N), ``>=``/``<=``
    (inclusive), and ``>``/``<`` (shifted by 1). Bails on conditional
    forms (``if(...)``) for the same reason :func:`numeric_bounds` does.
    Returns ``(None, None)`` if no clean bound is extractable.
    """
    if not constraint:
        return (None, None)
    if re.search(r"\bif\s*\(", constraint):
        return (None, None)

    lo: Optional[int] = None
    hi: Optional[int] = None
    for m in _SM_COUNT_RX.finditer(constraint):
        op, val = m.group(1), int(m.group(2))
        if op == "=":
            return (val, val)
        if op == ">=":
            lo = val if lo is None else max(lo, val)
        elif op == ">":
            v = val + 1
            lo = v if lo is None else max(lo, v)
        elif op == "<=":
            hi = val if hi is None else min(hi, val)
        elif op == "<":
            v = val - 1
            hi = v if hi is None else min(hi, v)
    return (lo, hi)


# ── Per-type sampler (Python values, for CSV output) ──────────────────────────

DEFAULT_GEO_BBOX: Tuple[float, float, float, float] = (-90.0, 90.0, -180.0, 180.0)
DEFAULT_EPOCH_START = datetime.date(2024, 1, 1)
DEFAULT_EPOCH_SPAN_DAYS = 5 * 365


def sample_python_value(
    q_type: str,
    choices: Optional[List[Dict[str, Any]]],
    constraint: Optional[str],
    rng: random.Random,
    epoch_start: Optional[datetime.date] = None,
    epoch_span_days: int = DEFAULT_EPOCH_SPAN_DAYS,
    geo_bbox: Tuple[float, float, float, float] = DEFAULT_GEO_BBOX,
) -> Any:
    """Sample one typed Python value for a SurveyCTO question type.

    For ``select_multiple`` returns a list of chosen value strings; the
    caller is responsible for emitting both the parent ``"v1 v2"`` cell
    and the per-choice indicator columns.

    For ``calculate`` / ``repeat_count`` / metadata types returns ``""``;
    these are not sampled here — the walker evaluates calc fields and
    populates metadata from its run-context.
    """
    if q_type == "integer":
        lo, hi = numeric_bounds(constraint)
        lo_i = int(lo) if lo is not None else 0
        hi_i = int(hi) if hi is not None else max(lo_i + 1, 100)
        if lo_i > hi_i:
            lo_i, hi_i = hi_i, lo_i
        return rng.randint(lo_i, hi_i)

    if q_type == "decimal":
        lo, hi = numeric_bounds(constraint)
        lo_f = float(lo) if lo is not None else 0.0
        hi_f = float(hi) if hi is not None else max(lo_f + 1.0, 100.0)
        if lo_f > hi_f:
            lo_f, hi_f = hi_f, lo_f
        return round(rng.uniform(lo_f, hi_f), 4)

    if q_type == "date":
        base = epoch_start or DEFAULT_EPOCH_START
        offset = rng.randint(0, epoch_span_days)
        return base + datetime.timedelta(days=offset)

    if q_type == "datetime":
        base = datetime.datetime.combine(
            epoch_start or DEFAULT_EPOCH_START, datetime.time()
        )
        secs = rng.randint(0, epoch_span_days * 86_400)
        return base + datetime.timedelta(seconds=secs)

    if q_type == "time":
        secs = rng.randint(0, 86_399)
        return datetime.time(secs // 3600, (secs % 3600) // 60, secs % 60)

    if q_type == "select_one":
        if choices:
            pick = rng.choice(choices)
            return str(pick.get("value", "")).strip()
        return ""

    if q_type == "geopoint":
        lat_min, lat_max, lon_min, lon_max = geo_bbox
        lat = rng.uniform(lat_min, lat_max)
        lon = rng.uniform(lon_min, lon_max)
        alt = rng.uniform(0.0, 1000.0)
        acc = rng.uniform(1.0, 20.0)
        return f"{lat:.6f} {lon:.6f} {alt:.2f} {acc:.2f}"

    if q_type in ("barcode", "image", "audio", "video", "file"):
        return f"synthetic_{rng.randint(1, 9999)}"

    if q_type == "text":
        n = text_max_length(constraint)
        token = f"text_{rng.randint(0, 9999)}"
        if n is not None:
            return token[:n]
        return token

    if q_type == "select_multiple":
        if not choices:
            return []
        lo_n, hi_n = select_multiple_count_bounds(constraint)
        max_n = len(choices)
        lo_n = 1 if lo_n is None else max(1, min(lo_n, max_n))
        hi_n = max_n if hi_n is None else max(lo_n, min(hi_n, max_n))
        n_pick = rng.randint(lo_n, hi_n)
        # rng.sample preserves uniqueness and respects the chosen count
        picks = rng.sample(choices, n_pick)
        return [str(c.get("value", "")).strip() for c in picks]

    # calculate / repeat_count are walker-computed; metadata is run-context
    return ""


def format_date_for_csv(d: datetime.date) -> str:
    """SurveyCTO exports dates as YYYY-MM-DD."""
    return d.strftime("%Y-%m-%d")


def format_datetime_for_csv(dt: datetime.datetime) -> str:
    """SurveyCTO exports datetimes as YYYY-MM-DDTHH:MM:SS.sss+00:00 normally;
    we use a more readable variant that pandas and Stata both ingest cleanly."""
    return dt.strftime("%b %d, %Y %I:%M:%S %p")


def format_time_for_csv(t: datetime.time) -> str:
    """SurveyCTO exports times as ``HH:MM:SS.000+HH:MM`` (24-hour, ms,
    UTC offset). Use UTC for synthetic so the format is deterministic;
    downstream parsers that expect this shape (HFC time-window checks)
    will see the right pattern."""
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.000+00:00"
