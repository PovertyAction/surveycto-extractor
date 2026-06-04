"""
SurveyCTO Expression Evaluator
==============================

Distinct from the converter in ``logic_converter.py`` (which translates
SurveyCTO expressions to Stata strings), this module executes a SurveyCTO
expression against a row state and returns a Python value. Used by the
synthetic-data generator to evaluate relevance, calculate, and dynamic
``choice_filter`` expressions at row-generation time, plus ``pulldata()``
lookups.

Authoritative spec: ``coding_guidelines/surveycto_refs/expressions.md``.

Phase A coverage (this commit):
- Operators: + - * div mod = != < <= > >= and or, unary -
- Primary forms: numeric / string literals, ``${var}``, ``.``, function calls,
  parenthesised sub-expressions, bare identifiers (resolved against the
  current choice row in choice_filter contexts)
- Functions: selected, count-selected, if, coalesce, indexed-repeat, regex,
  not, number, int, string, position, index, empty, pulldata, true, false

Phase B / C will add: dates, aggregates (count, sum, min, max with -if
variants), geography, strings (concat, substr, contains, starts-with,
ends-with, translate, string-length, lower, upper), trig / math, uuid,
once, random.

Unsupported functions raise :class:`UnsupportedFunctionError` so the
caller can decide how to degrade (default relevance to True, calculate
to empty, choice_filter to full list).
"""

from __future__ import annotations

import datetime
import math
import re
import uuid as _uuid_mod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple


_EPOCH_DATE = datetime.date(1970, 1, 1)
_EPOCH_DATETIME = datetime.datetime(1970, 1, 1)


_ISO_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?$"
)


# ── Exceptions ────────────────────────────────────────────────────────────────

class ExpressionError(Exception):
    """Base error for evaluator failures."""


class ParseError(ExpressionError):
    """Lexer / parser error."""


class EvaluationError(ExpressionError):
    """Runtime evaluation error (type mismatch, missing variable, etc.)."""


class UnsupportedFunctionError(ExpressionError):
    """The expression uses a function not yet implemented in the registry."""

    def __init__(self, name: str):
        super().__init__(f"unsupported function: {name}()")
        self.name = name


# ── Tokens ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    pos: int


_KEYWORDS = {"and", "or", "div", "mod"}
_OP_CHARS = set("+-*=!<>")


# ── Lexer ─────────────────────────────────────────────────────────────────────

def _tokenize(src: str) -> List[Token]:
    tokens: List[Token] = []
    i, n = 0, len(src)

    while i < n:
        c = src[i]

        # Whitespace
        if c.isspace():
            i += 1
            continue

        # ${var} reference
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            j = src.find("}", i + 2)
            if j < 0:
                raise ParseError(f"unterminated ${{...}} at position {i}")
            tokens.append(Token("VAR", src[i + 2:j], i))
            i = j + 1
            continue

        # String literal: single or double quoted
        if c in ("'", '"'):
            j = src.find(c, i + 1)
            if j < 0:
                raise ParseError(f"unterminated string at position {i}")
            tokens.append(Token("STRING", src[i + 1:j], i))
            i = j + 1
            continue

        # Number literal (with optional leading minus handled by parser)
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            start = i
            saw_dot = (c == ".")
            i += 1
            while i < n and (src[i].isdigit() or (src[i] == "." and not saw_dot)):
                if src[i] == ".":
                    saw_dot = True
                i += 1
            tokens.append(Token("NUMBER", float(src[start:i]), start))
            continue

        # Dot (current-value marker). Must come after numeric-leading-dot
        # so that `.5` lexes as 0.5 and not DOT + NUMBER.
        if c == ".":
            tokens.append(Token("DOT", ".", i))
            i += 1
            continue

        # Identifier / keyword (function names may contain - and :, e.g.
        # count-selected, jr:choice-name).
        if c.isalpha() or c == "_":
            start = i
            i += 1
            while i < n and (src[i].isalnum() or src[i] in "_-:"):
                i += 1
            ident = src[start:i]
            if ident in _KEYWORDS:
                tokens.append(Token("KEYWORD", ident, start))
            else:
                tokens.append(Token("IDENT", ident, start))
            continue

        # Punctuation
        if c == "(":
            tokens.append(Token("LPAREN", "(", i))
            i += 1
            continue
        if c == ")":
            tokens.append(Token("RPAREN", ")", i))
            i += 1
            continue
        if c == ",":
            tokens.append(Token("COMMA", ",", i))
            i += 1
            continue

        # Operators
        if c in _OP_CHARS:
            # Two-character operators: !=, <=, >=
            if i + 1 < n and src[i:i + 2] in ("!=", "<=", ">="):
                tokens.append(Token("OP", src[i:i + 2], i))
                i += 2
                continue
            if c in "+-*=<>":
                tokens.append(Token("OP", c, i))
                i += 1
                continue

        raise ParseError(f"unexpected character {c!r} at position {i}")

    tokens.append(Token("EOF", None, n))
    return tokens


# ── AST nodes ─────────────────────────────────────────────────────────────────

@dataclass
class NumberLit:
    value: float


@dataclass
class StringLit:
    value: str


@dataclass
class BoolLit:
    value: bool


@dataclass
class VarRef:
    name: str


@dataclass
class DotRef:
    pass


@dataclass
class IdentRef:
    name: str


@dataclass
class FuncCall:
    name: str
    args: List[Any]


@dataclass
class BinOp:
    op: str
    left: Any
    right: Any


@dataclass
class UnaryOp:
    op: str
    operand: Any


# ── Parser ────────────────────────────────────────────────────────────────────

class Parser:
    """Recursive-descent parser following XPath-style precedence."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self, offset: int = 0) -> Token:
        return self.tokens[self.pos + offset]

    def _eat(self, kind: str, value: Any = None) -> Token:
        tok = self._peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise ParseError(
                f"expected {kind}{'=' + repr(value) if value else ''} "
                f"but got {tok.kind}={tok.value!r} at position {tok.pos}"
            )
        self.pos += 1
        return tok

    def parse(self):
        ast = self._parse_or()
        if self._peek().kind != "EOF":
            tok = self._peek()
            raise ParseError(
                f"unexpected token {tok.kind}={tok.value!r} at position {tok.pos}"
            )
        return ast

    def _parse_or(self):
        node = self._parse_and()
        while self._peek().kind == "KEYWORD" and self._peek().value == "or":
            self.pos += 1
            right = self._parse_and()
            node = BinOp("or", node, right)
        return node

    def _parse_and(self):
        node = self._parse_equality()
        while self._peek().kind == "KEYWORD" and self._peek().value == "and":
            self.pos += 1
            right = self._parse_equality()
            node = BinOp("and", node, right)
        return node

    def _parse_equality(self):
        node = self._parse_comparison()
        while self._peek().kind == "OP" and self._peek().value in ("=", "!="):
            op = self._peek().value
            self.pos += 1
            right = self._parse_comparison()
            node = BinOp(op, node, right)
        return node

    def _parse_comparison(self):
        node = self._parse_additive()
        while self._peek().kind == "OP" and self._peek().value in ("<", "<=", ">", ">="):
            op = self._peek().value
            self.pos += 1
            right = self._parse_additive()
            node = BinOp(op, node, right)
        return node

    def _parse_additive(self):
        node = self._parse_multiplicative()
        while self._peek().kind == "OP" and self._peek().value in ("+", "-"):
            op = self._peek().value
            self.pos += 1
            right = self._parse_multiplicative()
            node = BinOp(op, node, right)
        return node

    def _parse_multiplicative(self):
        node = self._parse_unary()
        while True:
            tok = self._peek()
            if tok.kind == "OP" and tok.value == "*":
                self.pos += 1
                right = self._parse_unary()
                node = BinOp("*", node, right)
            elif tok.kind == "KEYWORD" and tok.value in ("div", "mod"):
                op = tok.value
                self.pos += 1
                right = self._parse_unary()
                node = BinOp(op, node, right)
            else:
                break
        return node

    def _parse_unary(self):
        tok = self._peek()
        if tok.kind == "OP" and tok.value == "-":
            self.pos += 1
            return UnaryOp("-", self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self):
        tok = self._peek()

        if tok.kind == "NUMBER":
            self.pos += 1
            return NumberLit(tok.value)

        if tok.kind == "STRING":
            self.pos += 1
            return StringLit(tok.value)

        if tok.kind == "VAR":
            self.pos += 1
            return VarRef(tok.value)

        if tok.kind == "DOT":
            self.pos += 1
            return DotRef()

        if tok.kind == "LPAREN":
            self.pos += 1
            inner = self._parse_or()
            self._eat("RPAREN")
            return inner

        if tok.kind == "IDENT":
            self.pos += 1
            # Function call: IDENT ( args )
            if self._peek().kind == "LPAREN":
                self.pos += 1
                args: List[Any] = []
                if self._peek().kind != "RPAREN":
                    args.append(self._parse_or())
                    while self._peek().kind == "COMMA":
                        self.pos += 1
                        args.append(self._parse_or())
                self._eat("RPAREN")
                return FuncCall(tok.value, args)
            # Bare identifier — resolves at runtime (e.g. a choices-sheet column
            # name in a choice_filter context).
            return IdentRef(tok.value)

        raise ParseError(
            f"unexpected token {tok.kind}={tok.value!r} at position {tok.pos}"
        )


# ── EvalContext ───────────────────────────────────────────────────────────────

@dataclass
class EvalContext:
    """Carries everything the interpreter needs at evaluation time."""

    row: Dict[str, Any] = field(default_factory=dict)
    """Current respondent's partial row (variable_name -> value)."""

    choices: Optional[Dict[str, List[Dict]]] = None
    """{list_name: [{value, label, ...}, ...]} for choice-label lookups."""

    pulldata_lookup: Optional[Callable[[str, str, str, Any], Any]] = None
    """Callable invoked for pulldata(); receives (csv, col, key_col, key_val)."""

    repeat_stack: List[Tuple[str, int]] = field(default_factory=list)
    """[(repeat_name, current_index_1based), ...] innermost last."""

    rng: Any = None
    """Seeded random.Random for random() / uuid()."""

    now: Any = None
    """Frozen datetime for today() / now()."""

    current_var: Optional[str] = None
    """Variable name the `.` token resolves to (constraint / choice_filter)."""

    choice_row: Optional[Dict[str, Any]] = None
    """When set, bare identifiers resolve against this choices-sheet row."""

    repeat_values: Optional[Dict[str, List[Any]]] = None
    """{var_name: [val_iter1, val_iter2, ...]} for aggregates / count()."""

    var_to_choice_list: Optional[Dict[str, str]] = None
    """{variable_name: choice_list_name} so choice-label() / jr:choice-name()
    can find the right choice list from a ${var} reference."""

    duration_secs: Optional[float] = None
    """Synthesised submission-duration seconds for the duration() function."""

    def get_var(self, name: str) -> Any:
        """Resolve a ${var} reference against the row state, accounting for
        wide-format repeat-suffix expansion if we're inside a repeat."""
        if self.repeat_stack:
            # Innermost first — try the deepest single-suffix match. Covers
            # single-level repeats (``var_i``) and the evaluator's own repeat
            # aggregation (count/sum/join push one level at a time).
            for _rep, idx in reversed(self.repeat_stack):
                key = f"{name}_{idx}"
                if key in self.row:
                    return self.row[key]
            # Nested repeats store combined suffixes (``var_o_i``). Try the
            # full ancestor→self chain, then progressively shorter prefixes so
            # an inner-context reference to an OUTER-level field (stored
            # ``var_o``) still resolves. Outermost-first matches how
            # ``_repeat_chain`` builds the suffix.
            idxs = [idx for _rep, idx in self.repeat_stack]
            for k in range(len(idxs), 1, -1):
                key = name + "".join(f"_{j}" for j in idxs[:k])
                if key in self.row:
                    return self.row[key]
        if name in self.row:
            return self.row[name]
        # Plain miss — return empty string to match SurveyCTO's empty-field
        # behaviour. This is what `empty()` checks for.
        return ""


# ── Helpers: type coercion / truthiness ───────────────────────────────────────

_NUM_RX = re.compile(r"^-?\d+(\.\d+)?$")


def _to_number(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s == "":
        return 0.0  # XPath number('') is NaN, but SurveyCTO behaves like 0 in arithmetic
    if _NUM_RX.match(s):
        return float(s)
    # Non-numeric string -> NaN-like; we use 0.0 to keep arithmetic from
    # propagating exceptions. Comparisons against this should be careful.
    return float("nan")


def _to_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # SurveyCTO does not render trailing .0 for integer floats
        if value.is_integer():
            return str(int(value))
        return str(value)
    return str(value) if value is not None else ""


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) and value == value  # NaN -> False
    if isinstance(value, str):
        return value != ""
    return value is not None


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    return False


def _as_comparable_date(value: Any) -> Optional[datetime.datetime]:
    """If ``value`` looks like a date or datetime (object or ISO string),
    return it as a normalised datetime for comparison; else None."""
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    if isinstance(value, str):
        s = value.strip()
        if _ISO_DATE_RX.match(s) or _ISO_DATETIME_RX.match(s):
            return _coerce_datetime(s)
    return None


def _equal(a: Any, b: Any) -> bool:
    """SurveyCTO `=` comparison. Numeric-numeric uses numeric equality;
    otherwise falls back to string equality."""
    a_is_num = isinstance(a, (int, float)) and not isinstance(a, bool)
    b_is_num = isinstance(b, (int, float)) and not isinstance(b, bool)
    if a_is_num and b_is_num:
        return float(a) == float(b)
    # One side numeric, one side string-of-number → coerce
    if a_is_num and isinstance(b, str) and _NUM_RX.match(b.strip()):
        return float(a) == float(b)
    if b_is_num and isinstance(a, str) and _NUM_RX.match(a.strip()):
        return float(b) == float(a)
    return _to_string(a) == _to_string(b)


# ── Function registry ─────────────────────────────────────────────────────────

FunctionImpl = Callable[[List[Any], EvalContext], Any]
FUNCTIONS: Dict[str, FunctionImpl] = {}


def _register(name: str) -> Callable[[FunctionImpl], FunctionImpl]:
    def deco(fn: FunctionImpl) -> FunctionImpl:
        FUNCTIONS[name] = fn
        return fn
    return deco


@_register("selected")
def _fn_selected(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"selected() expects 2 args, got {len(args)}")
    haystack = _to_string(args[0])
    needle = _to_string(args[1])
    if haystack == "":
        return False
    return needle in haystack.split()


@_register("count-selected")
def _fn_count_selected(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"count-selected() expects 1 arg, got {len(args)}")
    s = _to_string(args[0])
    if s == "":
        return 0.0
    return float(len(s.split()))


@_register("if")
def _fn_if(args, ctx):
    if len(args) != 3:
        raise EvaluationError(f"if() expects 3 args, got {len(args)}")
    return args[1] if _to_bool(args[0]) else args[2]


@_register("coalesce")
def _fn_coalesce(args, ctx):
    for a in args:
        if not _is_empty(a):
            return a
    return ""


@_register("not")
def _fn_not(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"not() expects 1 arg, got {len(args)}")
    return not _to_bool(args[0])


@_register("number")
def _fn_number(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"number() expects 1 arg, got {len(args)}")
    return _to_number(args[0])


@_register("int")
def _fn_int(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"int() expects 1 arg, got {len(args)}")
    n = _to_number(args[0])
    if n != n:  # NaN
        return 0.0
    return float(int(n))


@_register("string")
def _fn_string(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"string() expects 1 arg, got {len(args)}")
    return _to_string(args[0])


@_register("regex")
def _fn_regex(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"regex() expects 2 args, got {len(args)}")
    s = _to_string(args[0])
    pattern = _to_string(args[1])
    try:
        return re.search(pattern, s) is not None
    except re.error as exc:
        # Don't silently mark every row "no match": a malformed pattern is a
        # real error. Raise so safe_evaluate() applies the caller's fallback
        # and logs it, per the evaluator's "log or raise -- never hide" rule.
        raise EvaluationError(f"regex() pattern failed to compile: {pattern!r} ({exc})")


@_register("index")
def _fn_index(args, ctx):
    if args:
        raise EvaluationError(f"index() expects 0 args, got {len(args)}")
    if not ctx.repeat_stack:
        return 1.0
    return float(ctx.repeat_stack[-1][1])


@_register("position")
def _fn_position(args, ctx):
    # SurveyCTO docs prefer index(); position(..) is the XPath legacy form.
    return _fn_index([], ctx)


@_register("empty")
def _fn_empty(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"empty() expects 1 arg, got {len(args)}")
    return _is_empty(args[0])


@_register("true")
def _fn_true(args, ctx):
    return True


@_register("false")
def _fn_false(args, ctx):
    return False


@_register("indexed-repeat")
def _fn_indexed_repeat(args, ctx):
    """Look up a variable's value at a specific repeat instance.

    Wide-format row state stores ``var_N`` for the N-th instance, so the
    lookup is a direct row-dict access. We rely on the parser surfacing
    the first arg as a ``VarRef`` (the target variable name) before
    interpretation strips it.
    """
    if len(args) < 3 or len(args) % 2 != 1:
        raise EvaluationError(
            f"indexed-repeat() expects 3, 5, 7, ... args, got {len(args)}"
        )
    # args = [target_value (pre-evaluated as ${target}_<current_idx>), group_value, idx, ...]
    # We need the TARGET NAME, not its current-context value. The interpreter
    # passes the raw AST args via a side channel for this function — see
    # interpreter dispatch below.
    raise EvaluationError("indexed-repeat must be dispatched via raw AST args")


@_register("pulldata")
def _fn_pulldata(args, ctx):
    if len(args) != 4:
        raise EvaluationError(f"pulldata() expects 4 args, got {len(args)}")
    csv_name = _to_string(args[0])
    col = _to_string(args[1])
    key_col = _to_string(args[2])
    key_val = args[3]
    if ctx.pulldata_lookup is None:
        return ""
    try:
        return ctx.pulldata_lookup(csv_name, col, key_col, key_val)
    except Exception:
        return ""


# ── Date / time helpers ──────────────────────────────────────────────────────

def _coerce_date(value: Any) -> Optional[datetime.date]:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if "T" in s or " " in s and ":" in s:
                return _coerce_datetime(s).date() if _coerce_datetime(s) else None
            return datetime.date.fromisoformat(s)
        except (ValueError, TypeError):
            return None
    return None


def _coerce_datetime(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Normalise common SurveyCTO variants
        s2 = s.replace("Z", "+00:00")
        try:
            return datetime.datetime.fromisoformat(s2)
        except (ValueError, TypeError):
            pass
        for fmt in ("%b %d, %Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None
    return None


# SurveyCTO format-date-time tokens. Most map cleanly to Python strftime; %n,
# %e, %h, %3 don't and need post-processing.
_FMT_TOKEN_RX = re.compile(r"%[YymnbdeaHhMS3]")


def _format_date_time(value: Any, fmt: str) -> str:
    dt = _coerce_datetime(value) or _coerce_date(value)
    if dt is None:
        return ""
    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime.combine(dt, datetime.time())

    def replace(m):
        tok = m.group(0)
        if tok == "%Y":
            return f"{dt.year:04d}"
        if tok == "%y":
            return f"{dt.year % 100:02d}"
        if tok == "%m":
            return f"{dt.month:02d}"
        if tok == "%n":
            return str(dt.month)
        if tok == "%b":
            return dt.strftime("%b")
        if tok == "%d":
            return f"{dt.day:02d}"
        if tok == "%e":
            return str(dt.day)
        if tok == "%a":
            return dt.strftime("%a")
        if tok == "%H":
            return f"{dt.hour:02d}"
        if tok == "%h":
            return str(dt.hour)
        if tok == "%M":
            return f"{dt.minute:02d}"
        if tok == "%S":
            return f"{dt.second:02d}"
        if tok == "%3":
            return f"{dt.microsecond // 1000:03d}"
        return tok

    return _FMT_TOKEN_RX.sub(replace, fmt)


def _ctx_now(ctx: EvalContext) -> datetime.datetime:
    if isinstance(ctx.now, datetime.datetime):
        return ctx.now
    if isinstance(ctx.now, datetime.date):
        return datetime.datetime.combine(ctx.now, datetime.time())
    return datetime.datetime(2026, 1, 1)


@_register("today")
def _fn_today(args, ctx):
    return _ctx_now(ctx).date()


@_register("now")
def _fn_now(args, ctx):
    return _ctx_now(ctx)


@_register("date")
def _fn_date(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"date() expects 1 arg, got {len(args)}")
    d = _coerce_date(args[0])
    return d if d is not None else ""


@_register("date-time")
def _fn_date_time(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"date-time() expects 1 arg, got {len(args)}")
    dt = _coerce_datetime(args[0])
    return dt if dt is not None else ""


@_register("format-date-time")
def _fn_format_date_time(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"format-date-time() expects 2 args, got {len(args)}")
    return _format_date_time(args[0], _to_string(args[1]))


@_register("format-date")
def _fn_format_date(args, ctx):
    # Older alias; SurveyCTO docs treat them as similar
    return _fn_format_date_time(args, ctx)


@_register("decimal-date-time")
def _fn_decimal_date_time(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"decimal-date-time() expects 1 arg, got {len(args)}")
    dt = _coerce_datetime(args[0])
    if dt is None:
        return float("nan")
    delta = dt - _EPOCH_DATETIME
    return delta.total_seconds() / 86_400.0


@_register("decimal-time")
def _fn_decimal_time(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"decimal-time() expects 1 arg, got {len(args)}")
    v = args[0]
    if isinstance(v, datetime.datetime):
        t = v.time()
    elif isinstance(v, datetime.time):
        t = v
    elif isinstance(v, str) and v.strip():
        # HH:MM:SS optionally followed by .ms or timezone
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?", v.strip())
        if not m:
            return float("nan")
        h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        t = datetime.time(h, mn, s)
    else:
        return float("nan")
    return (t.hour * 3600 + t.minute * 60 + t.second) / 86_400.0


@_register("duration")
def _fn_duration(args, ctx):
    if ctx.duration_secs is not None:
        return float(ctx.duration_secs)
    return 0.0


@_register("if-empty-date")
def _fn_if_empty_date(args, ctx):
    # if-empty-date(value, fallback) — if value parses as a date, return it
    # (as a date), else fallback. Not in the published reference table but
    # appears in SurveyCTO conversion guides.
    if len(args) != 2:
        raise EvaluationError(f"if-empty-date() expects 2 args, got {len(args)}")
    d = _coerce_date(args[0])
    if d is not None:
        return d
    return args[1]


# ── Aggregates over repeated fields ──────────────────────────────────────────

def _collect_repeat_values(var_name: str, ctx: EvalContext) -> List[Any]:
    """Return [row[var_1], row[var_2], ...] in iteration order, dropping
    empty cells. Used by sum/min/max/count/join and their -if variants."""
    out: List[Any] = []
    i = 1
    while True:
        key = f"{var_name}_{i}"
        if key not in ctx.row:
            break
        v = ctx.row[key]
        if v != "" and v is not None:
            out.append(v)
        i += 1
    return out


def _aggregate_arity_error(name: str, got: int, expected: int) -> EvaluationError:
    return EvaluationError(f"{name}() expects {expected} arg(s), got {got}")


@_register("count")
def _fn_count(args, ctx):
    # count(${repeat_group}) → number of iterations. We don't have the group
    # name directly here (the parser passed the resolved value, not the ref),
    # so the FuncCall dispatcher special-cases this. Fall back to: count the
    # number of non-empty items in a delimited string when called on a value.
    return float(len(_to_string(args[0]).split())) if args else 0.0


@_register("count-non-empty")
def _fn_count_non_empty(args, ctx):
    return _fn_count(args, ctx)


@_register("sum")
def _fn_sum(args, ctx):
    # Special-cased to receive raw AST args in the dispatcher
    raise EvaluationError("sum() must be dispatched via raw AST args")


@_register("min")
def _fn_min(args, ctx):
    # If called with multiple non-repeated arguments → return the min.
    # The single-arg-repeated form is special-cased in the dispatcher.
    if len(args) == 0:
        return float("nan")
    nums = [_to_number(a) for a in args]
    nums = [n for n in nums if n == n]  # drop NaN
    return min(nums) if nums else float("nan")


@_register("max")
def _fn_max(args, ctx):
    if len(args) == 0:
        return float("nan")
    nums = [_to_number(a) for a in args]
    nums = [n for n in nums if n == n]
    return max(nums) if nums else float("nan")


@_register("count-if")
def _fn_count_if(args, ctx):
    raise EvaluationError("count-if() must be dispatched via raw AST args")


@_register("sum-if")
def _fn_sum_if(args, ctx):
    raise EvaluationError("sum-if() must be dispatched via raw AST args")


@_register("min-if")
def _fn_min_if(args, ctx):
    raise EvaluationError("min-if() must be dispatched via raw AST args")


@_register("max-if")
def _fn_max_if(args, ctx):
    raise EvaluationError("max-if() must be dispatched via raw AST args")


@_register("join")
def _fn_join(args, ctx):
    raise EvaluationError("join() must be dispatched via raw AST args")


@_register("join-if")
def _fn_join_if(args, ctx):
    raise EvaluationError("join-if() must be dispatched via raw AST args")


@_register("rank-index")
def _fn_rank_index(args, ctx):
    raise EvaluationError("rank-index() must be dispatched via raw AST args")


@_register("rank-index-if")
def _fn_rank_index_if(args, ctx):
    raise EvaluationError("rank-index-if() must be dispatched via raw AST args")


# ── Select / choice-label functions ──────────────────────────────────────────

@_register("selected-at")
def _fn_selected_at(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"selected-at() expects 2 args, got {len(args)}")
    tokens = _to_string(args[0]).split()
    try:
        i = int(_to_number(args[1]))
    except (TypeError, ValueError):
        return ""
    if 0 <= i < len(tokens):
        return tokens[i]
    return ""


def _lookup_choice_label(field_ast: Any, value: Any, ctx: EvalContext) -> str:
    """Resolve the label for ``value`` in the choice list of the field that
    ``field_ast`` (a ``VarRef``) refers to."""
    if not isinstance(field_ast, VarRef):
        return ""
    if ctx.var_to_choice_list is None or ctx.choices is None:
        return ""
    list_name = ctx.var_to_choice_list.get(field_ast.name)
    if not list_name:
        return ""
    choices = ctx.choices.get(list_name)
    if not choices:
        return ""
    val_str = _to_string(value).strip()
    for c in choices:
        if str(c.get("value", "")).strip() == val_str:
            return str(c.get("label", "")).strip()
    return ""


@_register("choice-label")
def _fn_choice_label(args, ctx):
    raise EvaluationError("choice-label() must be dispatched via raw AST args")


@_register("jr:choice-name")
def _fn_jr_choice_name(args, ctx):
    raise EvaluationError("jr:choice-name() must be dispatched via raw AST args")


# ── Strings ──────────────────────────────────────────────────────────────────

@_register("concat")
def _fn_concat(args, ctx):
    return "".join(_to_string(a) for a in args)


@_register("substr")
def _fn_substr(args, ctx):
    if len(args) not in (2, 3):
        raise EvaluationError(f"substr() expects 2 or 3 args, got {len(args)}")
    s = _to_string(args[0])
    start = int(_to_number(args[1]))
    if start < 0:
        start = max(0, len(s) + start)
    if len(args) == 3:
        end = int(_to_number(args[2]))
        if end < 0:
            end = max(0, len(s) + end)
        return s[start:end]
    return s[start:]


@_register("string-length")
def _fn_string_length(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"string-length() expects 1 arg, got {len(args)}")
    return float(len(_to_string(args[0])))


@_register("lower")
def _fn_lower(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"lower() expects 1 arg, got {len(args)}")
    return _to_string(args[0]).lower()


@_register("upper")
def _fn_upper(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"upper() expects 1 arg, got {len(args)}")
    return _to_string(args[0]).upper()


@_register("contains")
def _fn_contains(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"contains() expects 2 args, got {len(args)}")
    return _to_string(args[1]) in _to_string(args[0])


@_register("starts-with")
def _fn_starts_with(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"starts-with() expects 2 args, got {len(args)}")
    return _to_string(args[0]).startswith(_to_string(args[1]))


@_register("ends-with")
def _fn_ends_with(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"ends-with() expects 2 args, got {len(args)}")
    return _to_string(args[0]).endswith(_to_string(args[1]))


@_register("translate")
def _fn_translate(args, ctx):
    if len(args) != 3:
        raise EvaluationError(f"translate() expects 3 args, got {len(args)}")
    s = _to_string(args[0])
    from_chars = _to_string(args[1])
    to_chars = _to_string(args[2])
    table = {}
    for i, ch in enumerate(from_chars):
        table[ch] = to_chars[i] if i < len(to_chars) else ""
    return "".join(table.get(ch, ch) for ch in s)


@_register("linebreak")
def _fn_linebreak(args, ctx):
    return "\n"


# ── Lists of items ───────────────────────────────────────────────────────────

@_register("count-items")
def _fn_count_items(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"count-items() expects 2 args, got {len(args)}")
    sep = _to_string(args[0])
    s = _to_string(args[1])
    if not s:
        return 0.0
    return float(len(s.split(sep)))


@_register("item-at")
def _fn_item_at(args, ctx):
    if len(args) != 3:
        raise EvaluationError(f"item-at() expects 3 args, got {len(args)}")
    sep = _to_string(args[0])
    s = _to_string(args[1])
    try:
        i = int(_to_number(args[2]))
    except (TypeError, ValueError):
        return ""
    parts = s.split(sep) if s else []
    if 0 <= i < len(parts):
        return parts[i]
    return ""


@_register("item-index")
def _fn_item_index(args, ctx):
    if len(args) != 3:
        raise EvaluationError(f"item-index() expects 3 args, got {len(args)}")
    sep = _to_string(args[0])
    s = _to_string(args[1])
    needle = _to_string(args[2])
    parts = s.split(sep) if s else []
    if needle in parts:
        return float(parts.index(needle))
    return -1.0


@_register("item-present")
def _fn_item_present(args, ctx):
    if len(args) != 3:
        raise EvaluationError(f"item-present() expects 3 args, got {len(args)}")
    sep = _to_string(args[0])
    s = _to_string(args[1])
    needle = _to_string(args[2])
    parts = s.split(sep) if s else []
    return needle in parts


@_register("de-duplicate")
def _fn_de_duplicate(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"de-duplicate() expects 2 args, got {len(args)}")
    sep = _to_string(args[0])
    s = _to_string(args[1])
    if not s:
        return ""
    seen: List[str] = []
    for p in s.split(sep):
        if p not in seen:
            seen.append(p)
    return sep.join(seen)


@_register("rank-value")
def _fn_rank_value(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"rank-value() expects 2 args, got {len(args)}")
    val = _to_number(args[0])
    items = [_to_number(x) for x in _to_string(args[1]).split()]
    items = [x for x in items if x == x]
    if val != val:  # NaN
        return 999.0
    sorted_items = sorted(items, reverse=True)
    if val in sorted_items:
        return float(sorted_items.index(val) + 1)
    return 999.0


# ── Math ─────────────────────────────────────────────────────────────────────

def _safe_math(fn, *args):
    try:
        return float(fn(*args))
    except (ValueError, ZeroDivisionError, OverflowError):
        return float("nan")


@_register("abs")
def _fn_abs(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"abs() expects 1 arg, got {len(args)}")
    return abs(_to_number(args[0]))


@_register("round")
def _fn_round(args, ctx):
    if len(args) not in (1, 2):
        raise EvaluationError(f"round() expects 1 or 2 args, got {len(args)}")
    n = _to_number(args[0])
    digits = int(_to_number(args[1])) if len(args) == 2 else 0
    if n != n:
        return float("nan")
    return round(n, digits)


@_register("pow")
def _fn_pow(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"pow() expects 2 args, got {len(args)}")
    return _safe_math(math.pow, _to_number(args[0]), _to_number(args[1]))


@_register("log")
def _fn_log(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"log() expects 1 arg, got {len(args)}")
    return _safe_math(math.log, _to_number(args[0]))


@_register("log10")
def _fn_log10(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"log10() expects 1 arg, got {len(args)}")
    return _safe_math(math.log10, _to_number(args[0]))


@_register("sin")
def _fn_sin(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"sin() expects 1 arg, got {len(args)}")
    return _safe_math(math.sin, _to_number(args[0]))


@_register("cos")
def _fn_cos(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"cos() expects 1 arg, got {len(args)}")
    return _safe_math(math.cos, _to_number(args[0]))


@_register("tan")
def _fn_tan(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"tan() expects 1 arg, got {len(args)}")
    return _safe_math(math.tan, _to_number(args[0]))


@_register("asin")
def _fn_asin(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"asin() expects 1 arg, got {len(args)}")
    return _safe_math(math.asin, _to_number(args[0]))


@_register("acos")
def _fn_acos(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"acos() expects 1 arg, got {len(args)}")
    return _safe_math(math.acos, _to_number(args[0]))


@_register("atan")
def _fn_atan(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"atan() expects 1 arg, got {len(args)}")
    return _safe_math(math.atan, _to_number(args[0]))


@_register("atan2")
def _fn_atan2(args, ctx):
    if len(args) != 2:
        raise EvaluationError(f"atan2() expects 2 args, got {len(args)}")
    return _safe_math(math.atan2, _to_number(args[0]), _to_number(args[1]))


@_register("sqrt")
def _fn_sqrt(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"sqrt() expects 1 arg, got {len(args)}")
    return _safe_math(math.sqrt, _to_number(args[0]))


@_register("exp")
def _fn_exp(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"exp() expects 1 arg, got {len(args)}")
    return _safe_math(math.exp, _to_number(args[0]))


@_register("pi")
def _fn_pi(args, ctx):
    return math.pi


@_register("format-number")
def _fn_format_number(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"format-number() expects 1 arg, got {len(args)}")
    n = _to_number(args[0])
    if n != n:
        return ""
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.4f}".rstrip("0").rstrip(".")


@_register("boolean")
def _fn_boolean(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"boolean() expects 1 arg, got {len(args)}")
    return _to_bool(args[0])


@_register("boolean-from-string")
def _fn_boolean_from_string(args, ctx):
    if len(args) != 1:
        raise EvaluationError(f"boolean-from-string() expects 1 arg, got {len(args)}")
    s = _to_string(args[0]).strip().lower()
    return s in ("1", "true", "yes", "y")


# ── Identity / randomization ─────────────────────────────────────────────────

@_register("uuid")
def _fn_uuid(args, ctx):
    rng = ctx.rng
    if rng is None:
        return str(_uuid_mod.uuid4())
    bits = rng.getrandbits(128)
    # version=4 sets the RFC 4122 variant/version bits so the result
    # passes a uuid.UUID(value).version == 4 check.
    return str(_uuid_mod.UUID(int=bits, version=4))


@_register("random")
def _fn_random(args, ctx):
    rng = ctx.rng
    if rng is None:
        import random as _r
        return _r.random()
    return rng.random()


@_register("once")
def _fn_once(args, ctx):
    # SurveyCTO caches once() per form; for synthetic data, each respondent
    # re-evaluates — so once(expr) == expr.
    if len(args) != 1:
        raise EvaluationError(f"once() expects 1 arg, got {len(args)}")
    return args[0]


@_register("relevant")
def _fn_relevant(args, ctx):
    # Without a full relevance graph, we approximate: relevant(${f}) is true
    # if the field has a non-empty value in the current row (matches
    # SurveyCTO's "hidden fields have empty responses" behaviour).
    if len(args) != 1:
        raise EvaluationError(f"relevant() expects 1 arg, got {len(args)}")
    return not _is_empty(args[0])


@_register("hash")
def _fn_hash(args, ctx):
    import hashlib
    payload = "|".join(_to_string(a) for a in args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@_register("version")
def _fn_version(args, ctx):
    return ""


@_register("device-info")
def _fn_device_info(args, ctx):
    return ""


@_register("enumerator-name")
def _fn_enumerator_name(args, ctx):
    return ""


@_register("enumerator-id")
def _fn_enumerator_id(args, ctx):
    return ""


@_register("username")
def _fn_username(args, ctx):
    return ""


# ── Interpreter ───────────────────────────────────────────────────────────────

def _eval(node, ctx: EvalContext) -> Any:
    if isinstance(node, NumberLit):
        return node.value
    if isinstance(node, StringLit):
        return node.value
    if isinstance(node, BoolLit):
        return node.value
    if isinstance(node, VarRef):
        return ctx.get_var(node.name)
    if isinstance(node, DotRef):
        if ctx.current_var is None:
            return ""
        return ctx.get_var(ctx.current_var)
    if isinstance(node, IdentRef):
        # Bare identifier — resolve against the candidate choice row (in
        # a choice_filter context) or return empty.
        if ctx.choice_row is not None and node.name in ctx.choice_row:
            return ctx.choice_row[node.name]
        return ""
    if isinstance(node, UnaryOp):
        v = _eval(node.operand, ctx)
        if node.op == "-":
            return -_to_number(v)
        raise EvaluationError(f"unknown unary op {node.op}")
    if isinstance(node, BinOp):
        return _eval_binop(node, ctx)
    if isinstance(node, FuncCall):
        return _eval_funcall(node, ctx)
    raise EvaluationError(f"unknown AST node: {type(node).__name__}")


def _eval_binop(node: BinOp, ctx: EvalContext) -> Any:
    op = node.op
    # Short-circuit for and / or
    if op == "and":
        return _to_bool(_eval(node.left, ctx)) and _to_bool(_eval(node.right, ctx))
    if op == "or":
        return _to_bool(_eval(node.left, ctx)) or _to_bool(_eval(node.right, ctx))

    left = _eval(node.left, ctx)
    right = _eval(node.right, ctx)

    if op == "=":
        return _equal(left, right)
    if op == "!=":
        return not _equal(left, right)
    if op in ("<", "<=", ">", ">="):
        # Date-aware comparison: if both sides are date/datetime objects or
        # ISO date strings, compare chronologically; otherwise fall back to
        # numeric.
        ld = _as_comparable_date(left)
        rd = _as_comparable_date(right)
        if ld is not None and rd is not None:
            if op == "<":  return ld < rd
            if op == "<=": return ld <= rd
            if op == ">":  return ld > rd
            if op == ">=": return ld >= rd
        ln = _to_number(left)
        rn = _to_number(right)
        if ln != ln or rn != rn:  # NaN
            return False
        if op == "<":
            return ln < rn
        if op == "<=":
            return ln <= rn
        if op == ">":
            return ln > rn
        if op == ">=":
            return ln >= rn
    if op == "+":
        return _to_number(left) + _to_number(right)
    if op == "-":
        return _to_number(left) - _to_number(right)
    if op == "*":
        return _to_number(left) * _to_number(right)
    if op == "div":
        r = _to_number(right)
        if r == 0:
            return float("nan")
        return _to_number(left) / r
    if op == "mod":
        r = _to_number(right)
        if r == 0:
            return float("nan")
        return _to_number(left) % r
    raise EvaluationError(f"unknown binary op {op}")


def _eval_funcall(node: FuncCall, ctx: EvalContext) -> Any:
    name = node.name

    # indexed-repeat needs the target VarRef untouched so we can build the
    # wide-format suffix key.
    if name == "indexed-repeat":
        return _eval_indexed_repeat(node.args, ctx)

    # Aggregates over a repeated field: first arg must be a VarRef so we
    # know which base variable to walk in wide-format suffix order.
    if name in _REPEAT_AGGREGATES:
        return _eval_repeat_aggregate(name, node.args, ctx)

    # count() can be called either with a repeated-field VarRef or with a
    # plain value (string of space-separated tokens). Dispatch by AST shape.
    if name in ("count", "count-non-empty") and len(node.args) == 1 and isinstance(node.args[0], VarRef):
        return _eval_count_repeat(node.args[0], ctx)

    # sum/min/max with a single VarRef arg → repeat aggregate; otherwise
    # treat as the multi-arg form (min of several scalars).
    if name == "sum" and len(node.args) == 1 and isinstance(node.args[0], VarRef):
        return _eval_sum_repeat(node.args[0], ctx)
    if name in ("min", "max") and len(node.args) == 1 and isinstance(node.args[0], VarRef):
        return _eval_minmax_repeat(name, node.args[0], ctx)

    # choice-label / jr:choice-name need the field VarRef to find its
    # choice list.
    if name == "choice-label":
        return _eval_choice_label(node.args, ctx)
    if name == "jr:choice-name":
        return _eval_jr_choice_name(node.args, ctx)

    impl = FUNCTIONS.get(name)
    if impl is None:
        raise UnsupportedFunctionError(name)
    evaluated = [_eval(a, ctx) for a in node.args]
    return impl(evaluated, ctx)


_REPEAT_AGGREGATES = {
    "count-if", "sum-if", "min-if", "max-if",
    "join", "join-if",
    "rank-index", "rank-index-if",
}


def _eval_count_repeat(field_node: "VarRef", ctx: EvalContext) -> float:
    """``count(${group})`` returns either the iteration count for a repeat
    group (when ``<group>_count`` is set in the row) or the count of
    populated ``<field>_N`` iterations otherwise."""
    name = field_node.name
    count_key = f"{name}_count"
    if count_key in ctx.row:
        try:
            return float(ctx.row[count_key] or 0)
        except (TypeError, ValueError):
            pass
    return float(len(_collect_repeat_values(name, ctx)))


def _eval_sum_repeat(field_node: "VarRef", ctx: EvalContext) -> float:
    vals = _collect_repeat_values(field_node.name, ctx)
    total = 0.0
    for v in vals:
        n = _to_number(v)
        if n == n:
            total += n
    return total


def _eval_minmax_repeat(name: str, field_node: "VarRef", ctx: EvalContext) -> float:
    vals = _collect_repeat_values(field_node.name, ctx)
    nums = [_to_number(v) for v in vals]
    nums = [n for n in nums if n == n]
    if not nums:
        return float("nan")
    return min(nums) if name == "min" else max(nums)


def _eval_repeat_aggregate(name: str, raw_args: List[Any], ctx: EvalContext) -> Any:
    """Dispatcher for the -if family and join/join-if/rank-index variants.

    For each iteration of the leading VarRef, push (i) onto ``current_var``
    and the repeat stack so that the predicate's ``${var}`` references and
    ``.`` token resolve to per-iteration values, then evaluate the predicate
    (if any) and the value expression.
    """
    if not raw_args:
        raise EvaluationError(f"{name}() expects at least 1 arg")

    if name in ("count-if", "sum-if", "min-if", "max-if"):
        if len(raw_args) != 2:
            raise EvaluationError(f"{name}() expects 2 args, got {len(raw_args)}")
        field_node, pred_node = raw_args
    elif name in ("join",):
        if len(raw_args) != 2:
            raise EvaluationError(f"{name}() expects 2 args, got {len(raw_args)}")
        sep_node, field_node = raw_args
        pred_node = None
    elif name in ("join-if",):
        if len(raw_args) != 3:
            raise EvaluationError(f"{name}() expects 3 args, got {len(raw_args)}")
        sep_node, field_node, pred_node = raw_args
    elif name == "rank-index":
        if len(raw_args) != 2:
            raise EvaluationError(f"{name}() expects 2 args, got {len(raw_args)}")
        idx_node, field_node = raw_args
        pred_node = None
    elif name == "rank-index-if":
        if len(raw_args) != 3:
            raise EvaluationError(f"{name}() expects 3 args, got {len(raw_args)}")
        idx_node, field_node, pred_node = raw_args
    else:
        raise EvaluationError(f"unknown repeat aggregate {name}")

    if not isinstance(field_node, VarRef):
        raise EvaluationError(
            f"{name}(): expected a ${{field}} reference as the repeated-field arg"
        )

    base = field_node.name
    # Enumerate every iteration with the same prefix in the row dict.
    instances: List[Tuple[int, Any]] = []
    i = 1
    while True:
        key = f"{base}_{i}"
        if key not in ctx.row:
            break
        instances.append((i, ctx.row[key]))
        i += 1

    # Apply predicate (if any) by re-binding the repeat stack so that inner
    # ${var} references resolve to that iteration's values.
    selected: List[Tuple[int, Any]] = []
    for idx, value in instances:
        if pred_node is None:
            selected.append((idx, value))
            continue
        sub_ctx = _push_iter_ctx(ctx, base, idx)
        if _to_bool(_eval(pred_node, sub_ctx)):
            selected.append((idx, value))

    if name == "count-if":
        return float(sum(1 for _, v in selected if v != "" and v is not None))
    if name == "sum-if":
        return sum(_to_number(v) for _, v in selected if _to_number(v) == _to_number(v))
    if name in ("min-if", "max-if"):
        nums = [_to_number(v) for _, v in selected if _to_number(v) == _to_number(v)]
        if not nums:
            return float("nan")
        return min(nums) if name == "min-if" else max(nums)
    if name == "join":
        sep = _to_string(_eval(sep_node, ctx))
        return sep.join(_to_string(v) for _, v in selected if v != "" and v is not None)
    if name == "join-if":
        sep = _to_string(_eval(sep_node, ctx))
        return sep.join(_to_string(v) for _, v in selected if v != "" and v is not None)
    if name in ("rank-index", "rank-index-if"):
        # Rank of the requested instance among the selected ones (highest=1).
        try:
            wanted = int(_to_number(_eval(idx_node, ctx)))
        except (TypeError, ValueError):
            return 999.0
        # Find the value at that instance index from the full instances list.
        target_value = None
        for idx, v in instances:
            if idx == wanted:
                target_value = v
                break
        if target_value is None:
            return 999.0
        # Sort by numeric value descending; ties keep insertion order.
        numeric_selected = [
            (_to_number(v), idx) for idx, v in selected
            if _to_number(v) == _to_number(v)
        ]
        if not numeric_selected:
            return 999.0
        ordered = sorted(numeric_selected, key=lambda t: -t[0])
        for rank, (_, idx) in enumerate(ordered, start=1):
            if idx == wanted:
                return float(rank)
        return 999.0

    raise EvaluationError(f"unhandled repeat aggregate {name}")


def _push_iter_ctx(ctx: EvalContext, base: str, idx: int) -> EvalContext:
    """Return a child context whose repeat_stack has ``(base, idx)`` pushed
    onto it. ``${var}`` lookups inside this iteration resolve via the suffix
    rule in :meth:`EvalContext.get_var`."""
    return EvalContext(
        row=ctx.row,
        choices=ctx.choices,
        pulldata_lookup=ctx.pulldata_lookup,
        repeat_stack=ctx.repeat_stack + [(base, idx)],
        rng=ctx.rng,
        now=ctx.now,
        current_var=ctx.current_var,
        choice_row=ctx.choice_row,
        repeat_values=ctx.repeat_values,
        var_to_choice_list=ctx.var_to_choice_list,
        duration_secs=ctx.duration_secs,
    )


def _eval_choice_label(raw_args: List[Any], ctx: EvalContext) -> str:
    """``choice-label(${field}, value)`` returns the static-choice label
    for ``value``. Phase B implementation; dynamic (search-based) choices
    fall back to empty (per SurveyCTO docs)."""
    if len(raw_args) != 2:
        raise EvaluationError(f"choice-label() expects 2 args, got {len(raw_args)}")
    value = _eval(raw_args[1], ctx)
    return _lookup_choice_label(raw_args[0], value, ctx)


def _eval_jr_choice_name(raw_args: List[Any], ctx: EvalContext) -> str:
    """``jr:choice-name(value, '${field}')`` — older ODK-compatible variant
    of ``choice-label`` with reversed parameter order. The second arg is
    documented as a quoted field name in older forms; we accept both a
    ``VarRef`` and a string literal that happens to spell ``${field}``."""
    if len(raw_args) != 2:
        raise EvaluationError(f"jr:choice-name() expects 2 args, got {len(raw_args)}")
    value = _eval(raw_args[0], ctx)
    field_node = raw_args[1]
    if isinstance(field_node, StringLit):
        # Try to peel ${...} off the literal so we can look up the choice list.
        m = re.match(r"\$\{([^}]+)\}", field_node.value.strip())
        if m:
            field_node = VarRef(m.group(1))
    return _lookup_choice_label(field_node, value, ctx)


def _eval_indexed_repeat(raw_args: List[Any], ctx: EvalContext) -> Any:
    """``indexed-repeat(${target}, ${group}, idx[, ${group2}, idx2, ...])``.

    In our wide row format, the value of ``target`` at instance N lives at
    ``<target>_N``. Nested repeats compose suffixes from outermost inward.
    """
    if len(raw_args) < 3 or len(raw_args) % 2 != 1:
        raise EvaluationError(
            f"indexed-repeat() expects 3, 5, 7, ... args, got {len(raw_args)}"
        )
    target_node = raw_args[0]
    if not isinstance(target_node, VarRef):
        raise EvaluationError("indexed-repeat() first arg must be a ${var} reference")
    target_name = target_node.name

    # Pair up the (group, idx) pairs; we only need the index values to build
    # the suffix. We don't strictly need the group names since wide format
    # encodes nesting as `_iA_iB` (outer-first) — but the convention in this
    # codebase is single-level suffixes per repeat. We follow that here:
    # apply suffixes in pair order (outermost first).
    suffixes: List[int] = []
    for k in range(1, len(raw_args), 2):
        idx_value = _eval(raw_args[k + 1], ctx)
        try:
            suffixes.append(int(_to_number(idx_value)))
        except (TypeError, ValueError):
            return ""

    key = target_name + "".join(f"_{s}" for s in suffixes)
    if key in ctx.row:
        return ctx.row[key]
    # Fallback: try the bare name (matches SurveyCTO's "invalid instance →
    # instance 1" rule for the simple case).
    if target_name in ctx.row:
        return ctx.row[target_name]
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=4096)
def parse(expr: str):
    """Parse and cache. The AST is immutable, so caching is safe."""
    return Parser(_tokenize(expr)).parse()


def evaluate(expr: str, ctx: EvalContext) -> Any:
    """Parse ``expr`` (cached) and evaluate against ``ctx``.

    Raises :class:`ExpressionError` on parse / evaluation failure. The
    caller is responsible for deciding how to degrade — relevance defaults
    to True, calculate to empty, etc.
    """
    if expr is None or expr.strip() == "":
        return ""
    ast = parse(expr)
    return _eval(ast, ctx)


def evaluate_bool(expr: str, ctx: EvalContext) -> bool:
    """Evaluate ``expr`` and coerce to a Python bool."""
    return _to_bool(evaluate(expr, ctx))


def safe_evaluate(
    expr: str,
    ctx: EvalContext,
    default: Any = None,
    on_error: Optional[Callable[[str, Exception], None]] = None,
) -> Any:
    """Evaluate; on any :class:`ExpressionError`, call ``on_error`` and
    return ``default``. Useful when the caller has a sensible fallback
    (relevance default True, calc default empty, filter default full list)."""
    try:
        return evaluate(expr, ctx)
    except ExpressionError as exc:
        if on_error is not None:
            on_error(expr, exc)
        return default
