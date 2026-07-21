"""Shared value-sampling helpers.
=============================

Constraint-aware sampling and bound extraction used by the synthetic
SurveyCTO-shaped CSV generator (``synthetic_data.py``).

``numeric_bounds`` and ``text_max_length`` are pure parsers over a raw
SurveyCTO constraint string — they recognise the simple ``>= N`` / ``<= N``
patterns that show up in most real forms and ignore anything more exotic
(which would fall through to the evaluator instead).

``sample_python_value`` returns a typed Python value (int, float, str,
``datetime``, etc.). The CSV writer in ``synthetic_data.py`` serialises
each typed value with the appropriate SurveyCTO-export format.
"""

from __future__ import annotations

import datetime
import random
import re
from typing import Any

# ── Bound extraction ──────────────────────────────────────────────────────────

# LHS is the current-value token `.` ONLY. A bound clause about a DIFFERENT
# variable (e.g. `. >= 30 and ${x} <= 10`) must not contribute a bound on the
# current field -- the old `[A-Za-z_]\w*` alternative picked up `${x} <= 10` as
# an upper bound, producing an inverted (30, 10) range that then got swapped to
# (10, 30) and sampled a value violating the real `. >= 30`. (#28.7)
_NUM_BOUND_RX = re.compile(r"\.\s*(>=|<=|>(?!=)|<(?!=))\s*(-?\d+(?:\.\d+)?)")


def _has_top_level_or(constraint: str) -> bool:
    """Return True when an ``or`` appears at paren depth 0 in the
    constraint. We use this to bail out of bound extraction for
    disjunctive constraints (e.g. ``. <= 0 or . >= 18``), where ANDing
    the partial bounds would produce a wrong over-clamped range.
    """
    depth = 0
    i = 0
    n = len(constraint)
    lower = constraint.lower()
    while i < n:
        ch = constraint[i]
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0 and lower[i : i + 2] == "or":
            # require word boundaries on either side
            before_ok = i == 0 or not (
                constraint[i - 1].isalnum() or constraint[i - 1] == "_"
            )
            after_ok = (i + 2 == n) or not (
                constraint[i + 2].isalnum() or constraint[i + 2] == "_"
            )
            if before_ok and after_ok:
                return True
        i += 1
    return False


def numeric_bounds(constraint: str | None) -> tuple[float | None, float | None]:
    """Extract ``(lower, upper)`` numeric bounds from a constraint expression.

    Recognises clauses about the current value only -- ``. >= N``, ``. > N``,
    ``. <= N``, ``. < N`` -- on either side of ``and``. A clause about a
    different variable (``${x} <= N``) is deliberately ignored, since it does
    not bound the current field. Inclusive on ``>=`` /
    ``<=``; for strict ``>`` / ``<`` we pin by ``+/- 1`` to land on an
    integer-safe inclusive bound. Returns ``(None, None)`` if no clean
    bound is extractable.

    Bails on conditional constraints (``if(...)``) and on disjunctive
    constraints (top-level ``or``): a regex pass would pick bounds across
    unrelated branches and produce an inverted / over-clamped range.
    Example: ``if(index()=1, .>=18, if(index()=2, .>=3 and .<=6, .>=0
    and .<=120))`` would otherwise yield ``lo=18, hi=6`` -> swap ->
    ``(6, 18)``. Same hazard for ``. <= 0 or . >= 18``. Better to fall
    back to the type-default range than to silently clamp.
    """
    if not constraint:
        return (None, None)
    if re.search(r"\bif\s*\(", constraint):
        return (None, None)
    if _has_top_level_or(constraint):
        return (None, None)

    lo: float | None = None
    hi: float | None = None
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


def text_max_length(constraint: str | None) -> int | None:
    """Recognise ``string-length(.) <= N`` / ``<= N`` / ``= N`` in a
    constraint and return ``N`` as a cap. Equality is treated as a cap
    (sampled values up to N chars) since we can't always meet exact
    length and a shorter value still satisfies most downstream uses.

    The negative lookbehind ``(?<![>!<])`` is required to prevent the
    bare ``=`` alternative from matching the ``=`` inside ``>= N`` or
    ``!= N`` — a TEXT field with constraint ``. >= 1`` would otherwise
    be capped at 1 character.
    """
    if not constraint:
        return None
    m = re.search(r"(?<![>!<])(?:<=|=)\s*(\d+)", constraint)
    if m:
        return int(m.group(1))
    return None


_SM_COUNT_RX = re.compile(
    r"count-selected\(\s*\.\s*\)\s*(=|>=|<=|>(?!=)|<(?!=))\s*(\d+)"
)


def select_multiple_count_bounds(
    constraint: str | None,
) -> tuple[int | None, int | None]:
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

    lo: int | None = None
    hi: int | None = None
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


# ── Conditional exclusive-choice SM constraints ───────────────────────────────

_COND_SM_RX = re.compile(
    r"if\s*\(\s*(?P<pred>.+?)"
    r"\s*,\s*count-selected\(\s*\.\s*\)\s*"
    r"(?P<then_op>=|>=|<=|>(?!=)|<(?!=))\s*(?P<then_n>\d+)"
    r"\s*,\s*count-selected\(\s*\.\s*\)\s*"
    r"(?P<else_op>=|>=|<=|>(?!=)|<(?!=))\s*(?P<else_n>\d+)\s*\)",
    re.DOTALL,
)

_SELECTED_LITERAL_RX = re.compile(
    # selected(., LIT): accept integer literals (quoted or bare) AND
    # quoted string literals (e.g. ``selected(., 'dontknow')``). The
    # three alternation groups capture: single-quoted | double-quoted |
    # bare integer. Callers should read whichever group matched.
    r"""selected\(\s*\.\s*,\s*(?:'([^']+)'|"([^"]+)"|(-?\d+))\s*\)"""
)


def _selected_literal_value(match: re.Match) -> str:
    """Return the captured literal value from a ``_SELECTED_LITERAL_RX``
    match, regardless of which alternation matched (single-quoted,
    double-quoted, or bare integer).
    """
    return match.group(1) or match.group(2) or match.group(3) or ""


def select_multiple_conditional_bounds(
    constraint: str | None,
) -> tuple[list[str], int | None, int | None]:
    """Parse the common conditional SM-constraint pattern.

        if(selected(., X) [or selected(., Y)]*,
           count-selected(.) <op> M,
           count-selected(.) <op> N)

    Returns ``(exclusive_values, else_lo, else_hi)`` — the list of choice
    values whose selection forces an exclusive pick (because the
    ``then``-branch typically pins ``count-selected(.) = 1``), and the
    bounds that apply when none of those are selected.

    Returns ``([], None, None)`` when the pattern doesn't match.
    """
    if not constraint:
        return ([], None, None)
    m = _COND_SM_RX.search(constraint)
    if not m:
        return ([], None, None)

    pred = m.group("pred")
    # Predicate must be a chain of selected(., LIT) joined by `or`; anything
    # else (e.g. references to other variables) is too dynamic to handle here.
    if not _SELECTED_LITERAL_RX.search(pred):
        return ([], None, None)
    # Strip the OR-joined chain of selected(.,LIT) calls; if anything else
    # remains, bail.
    stripped = _SELECTED_LITERAL_RX.sub("", pred)
    stripped = re.sub(r"\s*or\s*", "", stripped, flags=re.IGNORECASE).strip()
    if stripped:
        return ([], None, None)

    exclusives = [
        _selected_literal_value(m2) for m2 in _SELECTED_LITERAL_RX.finditer(pred)
    ]

    def _bound(op: str, n: int, side: str) -> tuple[int | None, int | None]:
        if op == "=":
            return (n, n)
        if op == ">=":
            return (n, None)
        if op == ">":
            return (n + 1, None)
        if op == "<=":
            return (None, n)
        if op == "<":
            return (None, n - 1)
        return (None, None)

    else_lo, else_hi = _bound(m.group("else_op"), int(m.group("else_n")), "else")
    return (exclusives, else_lo, else_hi)


# ── Per-type sampler (Python values, for CSV output) ──────────────────────────

DEFAULT_GEO_BBOX: tuple[float, float, float, float] = (-90.0, 90.0, -180.0, 180.0)
DEFAULT_EPOCH_START = datetime.date(2024, 1, 1)
DEFAULT_EPOCH_SPAN_DAYS = 5 * 365


def _appearance_tokens(appearance: str | None) -> set:
    """Split a SurveyCTO appearance string into lowercase tokens. The
    appearance column can chain multiple appearances with whitespace
    (e.g. ``"multiline numbers_decimal"``); we tokenise on whitespace
    so exact-token checks (``"numbers" in tokens``) don't fire on
    unrelated appearances that happen to contain the substring.
    """
    if not appearance:
        return set()
    return {tok.strip().lower() for tok in str(appearance).split() if tok.strip()}


def sample_python_value(
    q_type: str,
    choices: list[dict[str, Any]] | None,
    constraint: str | None,
    rng: random.Random,
    epoch_start: datetime.date | None = None,
    epoch_span_days: int = DEFAULT_EPOCH_SPAN_DAYS,
    geo_bbox: tuple[float, float, float, float] = DEFAULT_GEO_BBOX,
    appearance: str | None = None,
    now: datetime.datetime | None = None,
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
        # Cap the span so we never emit a date in the future. SurveyCTO
        # real exports can't contain post-collection-day dates, and HFC
        # checks routinely flag future dates as out-of-range. `now` is passed
        # from the generator's frozen run-context so two passes at the same
        # seed stay byte-identical even across a midnight boundary; it falls
        # back to the wall clock for standalone callers.
        today = now.date() if now is not None else datetime.date.today()
        max_offset = max(0, min(epoch_span_days, (today - base).days))
        offset = rng.randint(0, max_offset)
        return base + datetime.timedelta(days=offset)

    if q_type == "datetime":
        base = datetime.datetime.combine(
            epoch_start or DEFAULT_EPOCH_START, datetime.time()
        )
        now_dt = now if now is not None else datetime.datetime.now()
        max_secs = max(
            0, min(epoch_span_days * 86_400, int((now_dt - base).total_seconds()))
        )
        secs = rng.randint(0, max_secs)
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
        # SurveyCTO ``appearance='numbers'`` / ``numbers_decimal`` /
        # ``numbers_phone`` shows a numeric keypad on the device so the
        # respondent can only type digits, but the storage type stays
        # text (so leading zeros are preserved -- e.g. for phone numbers
        # like ``0777816905``). Honour that here by emitting digit-only
        # content. ``numbers_decimal`` also accepts ``.``; emit a real
        # ``dd.dd`` form so downstream type-inference doesn't flag the
        # synth value as integer-shaped where real respondents typed a
        # decimal point. Exact-token match (not substring) so other
        # appearances containing the substring "numbers" don't falsely
        # trip this branch.
        appearance_tokens = _appearance_tokens(appearance)
        if "numbers_decimal" in appearance_tokens:
            int_n = rng.randint(0, 10)
            dec_n = rng.randint(0, 99)
            out = f"{int_n}.{dec_n:02d}"
            if n is not None:
                return out[:n]
            return out
        if appearance_tokens & {"numbers", "numbers_phone"}:
            digits = "".join(str(rng.randint(0, 9)) for _ in range(10))
            if n is not None:
                return digits[:n]
            return digits
        token = f"text_{rng.randint(0, 9999)}"
        if n is not None:
            return token[:n]
        return token

    if q_type == "select_multiple":
        if not choices:
            return []
        max_n = len(choices)

        # Conditional exclusive-choice pattern: predicates of the form
        # `if(selected(., X) or selected(., Y), count-selected(.)=1, ...)`
        # mark X/Y as "exclusive sentinels" — when present in the row,
        # they must be the only pick. Honour this by sometimes picking
        # an exclusive value alone (and otherwise picking from the
        # non-exclusive subset under the else-branch bounds).
        exclusives, else_lo, else_hi = select_multiple_conditional_bounds(constraint)
        if exclusives:
            choice_values = {str(c.get("value", "")).strip(): c for c in choices}
            present_exclusives = [v for v in exclusives if v in choice_values]
            non_exclusive = [
                c
                for c in choices
                if str(c.get("value", "")).strip() not in present_exclusives
            ]
            # 1 / (1 + len(non_exclusive)) probability of going exclusive:
            # roughly proportional to how "rare" the sentinel branch should
            # be relative to the substantive choices.
            denom = 1 + len(non_exclusive)
            if present_exclusives and rng.randrange(denom) == 0:
                return [rng.choice(present_exclusives)]
            # Non-exclusive sample under the else-branch bounds
            ne_max = max(1, len(non_exclusive))
            lo_n = 1 if else_lo is None else max(1, min(else_lo, ne_max))
            hi_n = ne_max if else_hi is None else max(lo_n, min(else_hi, ne_max))
            n_pick = rng.randint(lo_n, hi_n)
            picks = rng.sample(non_exclusive, n_pick)
            return [str(c.get("value", "")).strip() for c in picks]

        lo_n, hi_n = select_multiple_count_bounds(constraint)
        # When the constraint is non-trivial but unparseable (e.g. it
        # references other variables), cap the upper bound conservatively
        # rather than picking up to len(choices). Empirically, large
        # multi-choice picks under unknown constraints violate them most
        # of the time.
        if (lo_n is None and hi_n is None) and constraint:
            hi_n = min(3, max_n)
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
    we use a more readable variant that pandas and Stata both ingest cleanly.
    """
    return dt.strftime("%b %d, %Y %I:%M:%S %p")


def format_time_for_csv(t: datetime.time) -> str:
    """SurveyCTO exports times as ``HH:MM:SS.000+HH:MM`` (24-hour, ms,
    UTC offset). Use UTC for synthetic so the format is deterministic;
    downstream parsers that expect this shape (HFC time-window checks)
    will see the right pattern.
    """
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.000+00:00"
