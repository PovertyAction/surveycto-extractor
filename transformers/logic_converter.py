"""
Convert SurveyCTO relevance logic to Stata syntax.

Reference: docs/coding_guidelines/SURVEYCTO_RELEVANCE_TRANSLATION.md
"""
import re
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Module-level strip log — accumulated across convert_to_stata() calls.
# Clear between pipeline runs with clear_strip_log().
# ---------------------------------------------------------------------------
_STRIP_LOG: List[Dict[str, str]] = []


def clear_strip_log() -> None:
    """Reset the strip log between pipeline runs."""
    _STRIP_LOG.clear()


def get_strip_log() -> List[Dict[str, str]]:
    """Return a copy of all strip entries recorded so far."""
    return list(_STRIP_LOG)


def _log_strip(varname: str, clause: str, reason: str) -> None:
    """Record one stripped clause and print a log line."""
    entry = {"var": varname, "clause": clause[:120], "reason": reason}
    _STRIP_LOG.append(entry)
    print(f"[STRIP] var={varname!r:30s}  reason={reason:28s}  clause={clause[:80]!r}")


# ---------------------------------------------------------------------------
# Sentinel used internally to mark stripped sub-expressions before
# the cleanup pass removes dangling logical operators.
# ---------------------------------------------------------------------------
_SENTINEL = "__STRIP__"


class LogicConverter:
    """Convert SurveyCTO expressions to Stata syntax."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _selected_column_suffix(code_str: str) -> str:
        """
        Column suffix for a select_multiple choice code.
        Positive code '1'  → '_1'
        Negative code '-66' → '__66'  (dash → underscore)
        """
        return "_" + code_str.replace("-", "_")

    @staticmethod
    def _find_balanced(s: str, open_idx: int) -> int:
        r"""
        Given `s` and the index of an opening `(`, return the index of the
        matching closing `)`. Returns -1 if unbalanced.

        String-literal aware: parens inside `'...'` / `"..."` do not affect
        depth, so a quoted `)` (e.g. `join(")", ${a}, ${b})`) no longer ends the
        call early. SurveyCTO expressions are XPath-based, which has no backslash
        string escape (a `\` is a literal char, e.g. a regex pattern or path), so
        a literal closes on the next matching quote — full stop. Treating `\` as
        an escape would wrongly keep a string open past `'...\'` and orphan the
        whole call.
        """
        depth = 0
        in_str: Optional[str] = None
        for i in range(open_idx, len(s)):
            ch = s[i]
            if in_str:
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"'):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
        return -1

    @staticmethod
    def _mask_literals(expr: str):
        """Replace every quoted string literal (both ' and " styles) with a
        neutral placeholder token ``__STATA_LIT_{i}__`` and return
        ``(masked_expr, literals)``.

        Late-stage rewrites (not()->!(, = -> ==, div/mod, and/or -> &/|,
        _clean_sentinels, missing-guards, whitespace collapse) must never
        reach inside a literal -- a needle like ``'x > 5'`` or ``' and '`` in
        an emitted ``regexm``/``strpos`` call would otherwise be corrupted.
        Masking after the string-function step (which needs real quotes) and
        unmasking at the very end (after whitespace collapse, so multi-space
        needles survive) prevents that.

        XPath has no backslash escape (see ``_find_balanced``), so a literal
        closes on the next matching quote; an unterminated quote keeps the
        remainder verbatim. The placeholder is a single ``\\w+`` run distinct
        from ``_SENTINEL`` -- inert to every late-stage regex, and unmasked by
        id so a placeholder absorbed by sentinel cleanup simply never returns.
        """
        out: List[str] = []
        lits: List[str] = []
        i = 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch in ("'", '"'):
                j = expr.find(ch, i + 1)
                if j < 0:
                    out.append(expr[i:])   # unterminated — verbatim
                    break
                lits.append(expr[i:j + 1])
                out.append(f"__STATA_LIT_{len(lits) - 1}__")
                i = j + 1
            else:
                out.append(ch)
                i += 1
        return ''.join(out), lits

    @staticmethod
    def _unmask_literals(expr: str, lits: List[str]) -> str:
        """Restore literals masked by ``_mask_literals`` (by id; missing ids,
        e.g. a placeholder absorbed by sentinel cleanup, are simply skipped)."""
        for idx, lit in enumerate(lits):
            expr = expr.replace(f"__STATA_LIT_{idx}__", lit)
        return expr

    @staticmethod
    def _literal_spans(expr: str) -> List[tuple]:
        """Return (start, end) index pairs of quoted string-literal spans (both
        ' and " styles; end is one past the closing quote). Same next-matching-
        quote, no-backslash-escape convention as ``_find_balanced``."""
        spans: List[tuple] = []
        i, n = 0, len(expr)
        while i < n:
            ch = expr[i]
            if ch in ("'", '"'):
                j = expr.find(ch, i + 1)
                if j < 0:
                    spans.append((i, n))
                    break
                spans.append((i, j + 1))
                i = j + 1
            else:
                i += 1
        return spans

    @staticmethod
    def _sub_outside_literals(pattern: 're.Pattern', repl, expr: str) -> str:
        """Apply ``pattern`` substitutions but skip any match that BEGINS inside
        a quoted literal. This stops a comparison regex (steps 3/3b) from
        matching a ``word op '...'`` shape that actually lies inside a
        function's quoted argument -- e.g. ``contains(a, 'x=')`` where the `x`
        sits inside the literal and `'([^']+)'` would otherwise greedily latch
        onto the literal's closing quote and swallow the rest of the
        expression."""
        spans = LogicConverter._literal_spans(expr)

        def _in_span(pos: int) -> bool:
            return any(s <= pos < e for s, e in spans)

        out: List[str] = []
        last = 0
        for m in pattern.finditer(expr):
            if _in_span(m.start()):
                continue
            out.append(expr[last:m.start()])
            out.append(repl(m))
            last = m.end()
        out.append(expr[last:])
        return ''.join(out)

    @staticmethod
    def _sub_function_balanced(
        expr: str,
        func_pattern: str,
        replacer,
    ) -> str:
        """
        Find every occurrence of `func_pattern(` in `expr` and replace the
        entire balanced call (including nested parens) using
        `replacer(args, full_call)` where `args` is the inner argument string
        and `full_call` is the complete matched call text (for logging).

        `func_pattern` is a regex matching the function NAME alone (without
        the opening paren).
        """
        out: List[str] = []
        i = 0
        # Left boundary `(?<![\w-])` (not `\b`) so a shorter registered name does
        # not match the hyphen-suffix of a longer SurveyCTO function — e.g. the
        # `date` family pattern must not eat the `-date(` of `format-date(`, nor
        # `concat`/`join`/etc. the tail of a hyphenated relative. The trailing
        # `\s*\(` anchor keeps prefixes distinct in the forward direction.
        rx = re.compile(rf'(?<![\w-])({func_pattern})\s*\(', re.IGNORECASE)
        while i < len(expr):
            m = rx.search(expr, i)
            if not m:
                out.append(expr[i:])
                break
            out.append(expr[i:m.start()])
            open_paren = m.end() - 1
            close_paren = LogicConverter._find_balanced(expr, open_paren)
            if close_paren < 0:
                # Unbalanced — preserve the rest verbatim
                out.append(expr[m.start():])
                break
            args = expr[open_paren + 1:close_paren]
            full_call = expr[m.start():close_paren + 1]
            out.append(replacer(args, full_call))
            i = close_paren + 1
        return ''.join(out)

    @staticmethod
    def _strip_balanced(expr: str, func_pattern: str, varname: str, reason: str) -> str:
        """Strip every balanced `func_pattern(...)` call to `_SENTINEL`, logging
        each with `reason`. Balanced matching means a nested call inside the
        args (e.g. `join(',', if(${a}, ${b}, ${c}))`) is removed as one unit
        rather than truncated at the first `)`, which would orphan a paren."""
        def _replacer(_args: str, full_call: str) -> str:
            _log_strip(varname, full_call, reason)
            return _SENTINEL
        return LogicConverter._sub_function_balanced(expr, func_pattern, _replacer)

    @staticmethod
    def _split_top_level_args(args_str: str) -> List[str]:
        """
        Split a comma-separated argument string respecting parenthesis depth
        and string literals. A comma or paren inside `'...'` / `"..."` is part
        of the literal, not a separator/nesting token (e.g.
        `selected(${x}, ',')`). XPath has no backslash escape, so a literal
        closes on the next matching quote. Returns list of stripped argument
        strings.
        """
        parts: List[str] = []
        depth = 0
        in_str: Optional[str] = None
        cur: List[str] = []
        for ch in args_str:
            if in_str:
                cur.append(ch)
                if ch == in_str:
                    in_str = None
            elif ch in ("'", '"'):
                in_str = ch
                cur.append(ch)
            elif ch == '(':
                depth += 1
                cur.append(ch)
            elif ch == ')':
                depth -= 1
                cur.append(ch)
            elif ch == ',' and depth == 0:
                parts.append(''.join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append(''.join(cur).strip())
        return parts

    @staticmethod
    def _unwrap_one_quote(s: str) -> str:
        """Strip exactly one matching outer quote pair from a needle, leaving
        any inner quotes/parens intact. `.strip("'\\"")` would over-strip a
        needle like `'')'` -- we only want to remove the enclosing pair."""
        s = s.strip()
        if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
            return s[1:-1]
        return s

    @staticmethod
    def _translate_selected(
        args_str: str,
        full_call: str,
        question_types: Dict[str, str],
        varname: str,
    ) -> str:
        """
        Translate selected(…) — three patterns:

        Pattern A  selected(var, 'N') / selected(var, N)
          select_one       → (var == N)
          select_multiple  → (var_N == 1)  or  (var__N == 1) for negative codes
          unknown type     → (var == N)   [backward-compatible fallback]

        Pattern B  selected('N1 N2 …', var)  [after string() has been stripped]
          → inlist(var, N1, N2, …)

        Pattern C  selected(var, other_var)  — dynamic second arg
          → _SENTINEL   (logged as DYNAMIC_SELECTED)

        `args_str` is the inner argument string from balanced-paren matching and
        `full_call` the complete call text. Args are split at top-level commas
        (paren-depth aware) so a nested call in the second arg — e.g.
        `selected(${x}, format-date(${y}, '%Y'))` — is kept intact (and stripped
        as Pattern C) instead of being truncated at the inner comma/paren.
        """
        parts = LogicConverter._split_top_level_args(args_str)
        if len(parts) != 2:
            return full_call

        first, second = parts

        # ---- Pattern B: first arg is a quoted space-separated list --------
        if first.startswith("'") or first.startswith('"'):
            inner = first[1:]
            if inner.endswith("'") or inner.endswith('"'):
                inner = inner[:-1]
            codes = inner.split()
            try:
                int_codes = [int(c) for c in codes]
            except ValueError:
                _log_strip(varname, full_call, "PATTERN_B_NON_INT")
                return _SENTINEL
            return f"inlist({second}, {', '.join(str(c) for c in int_codes)})"

        # ---- Pattern A or C: first arg is a variable name -----------------
        var = first

        quoted   = re.match(r"^['\"](-?\d+)['\"]$", second)
        unquoted = re.match(r"^(-?\d+)$", second)
        numeric  = quoted or unquoted
        if not numeric:
            # Pattern C — dynamic second arg
            _log_strip(varname, full_call, "DYNAMIC_SELECTED")
            return _SENTINEL

        code_str = numeric.group(1)
        qtype = question_types.get(var, "select_one")

        if qtype == "select_multiple":
            suffix = LogicConverter._selected_column_suffix(code_str)
            return f"({var}{suffix} == 1)"
        else:
            return f"({var} == {code_str})"

    @staticmethod
    def _translate_count_selected(
        match: re.Match,
        choice_codes: Optional[Dict[str, List[str]]],
        varname: str,
    ) -> str:
        """
        count-selected(var) → rowtotal(var_1 var_2 … var_K)
        Falls back to _SENTINEL if choice_codes not provided for this var.
        """
        var = match.group(1).strip()
        codes = (choice_codes or {}).get(var)
        if not codes:
            _log_strip(varname, match.group(0), "COUNT_SEL_NO_CODES")
            return _SENTINEL
        cols = [f"{var}{LogicConverter._selected_column_suffix(c)}" for c in codes]
        return f"rowtotal({' '.join(cols)})"

    @staticmethod
    def _translate_coalesce(args_str: str, full_call: str, varname: str) -> str:
        """
        N-ary coalesce: returns the first non-missing argument.

        coalesce(a, b)        → cond(missing(a), b, a)
        coalesce(a, b, c)     → cond(missing(a), cond(missing(b), c, b), a)
        coalesce(a, b, c, d)  → cond(missing(a), cond(missing(b), cond(missing(c), d, c), b), a)
        ...

        SurveyCTO requires coalesce to have at least 2 arguments and accepts
        any larger number of non-repeated arguments. We expand inductively
        from the right; 0- or 1-arg calls are reported as a strip reason.
        Called via balanced-paren matching, so a nested call in any argument
        (e.g. coalesce(if(${a},${b},${c}), ${d})) is kept intact.
        """
        parts = LogicConverter._split_top_level_args(args_str)
        if len(parts) < 2:
            _log_strip(varname, full_call, "COALESCE_BAD_ARITY")
            return _SENTINEL

        # Build the nested cond() from the right: result_n = parts[-1]
        # result_{i-1} = cond(missing(parts[i-1]), result_i, parts[i-1])
        result = parts[-1]
        for arg in reversed(parts[:-1]):
            result = f"cond(missing({arg}), {result}, {arg})"
        return result


    @staticmethod
    def _translate_substr(match: re.Match) -> str:
        """
        substr(str, s, e) → substr(str, s+1, e-s)
        SurveyCTO: 0-based start, exclusive end.
        Stata:     1-based start, length.
        Falls back to unchanged if indices are non-literal.
        """
        parts = [p.strip() for p in match.group(1).split(',')]
        if len(parts) != 3:
            return match.group(0)
        str_arg, s_arg, e_arg = parts
        try:
            s = int(s_arg)
            e = int(e_arg)
            return f"substr({str_arg}, {s + 1}, {e - s})"
        except ValueError:
            return match.group(0)

    @staticmethod
    def _add_missing_guards(expr: str) -> str:
        """
        Append & !missing(var) to simple relational comparisons:
            var > N  →  var > N & !missing(var)
            var >= N, var < N, var <= N  →  same pattern

        Conservative approach: only matches when the LHS is a bare identifier
        (word characters only) and the RHS is a numeric literal (int or decimal).
        Does not add a second guard if one already exists for that variable.
        """
        # Track which variables already have a guard in this expression
        already_guarded = set(re.findall(r'!missing\((\w+)\)', expr))

        def guard_sub(m: re.Match) -> str:
            var = m.group(1)
            op  = m.group(2)
            val = m.group(3)
            if var in already_guarded:
                return m.group(0)
            already_guarded.add(var)
            return f"{var} {op} {val} & !missing({var})"

        # Match: identifier  (>=|<=|>|<)  numeric_literal
        # LHS must start with a letter or underscore (not a digit), so we
        # don't add a spurious !missing() guard when the LHS is a numeric
        # literal (e.g. `100 < 5` from a stripped `<fn>() < 5`).
        # Negative lookahead on > to avoid matching ==, !=.
        expr = re.sub(
            r'\b([A-Za-z_]\w*)\s*(>=|<=|>(?!=)|<(?!=))\s*(-?\d+(?:\.\d+)?)',
            guard_sub,
            expr,
        )
        return expr

    @staticmethod
    def _clean_sentinels(expr: str) -> str:
        """
        Remove _SENTINEL markers left by clause stripping and clean up
        dangling logical operators (&, |) and empty parentheses.
        Runs repeatedly until stable.
        """
        # Comparison-cleanup tokens: _SENTINEL on either side of a relational
        # operator means the comparison is meaningless. The RHS/LHS operand
        # token is anything that isn't a logical operator or paren.
        _OP = r'(?:>=|<=|!=|==|>(?!=)|<(?!=))'
        _OPERAND = r'[^\s&|()]+'
        # Arithmetic operand excludes the arithmetic operators too, so an
        # adjacency collapse doesn't run past the next operator.
        _ARITH_OPERAND = r'[^\s&|()+*/-]+'
        _SEN = re.escape(_SENTINEL)

        prev = None
        while prev != expr:
            prev = expr
            # A call whose argument was stripped is itself unknowable:
            # `fn(...__STRIP__...)` with no inner parens -> _SENTINEL. Bottom-up
            # via the fixpoint loop, since only paren-free bodies match. (#27.6)
            expr = re.sub(
                rf'\w+\s*\([^()]*{_SEN}[^()]*\)', _SENTINEL, expr)
            # Balanced form: any call carrying a sentinel among its (possibly
            # nested, paren-containing) args is unknowable -> _SENTINEL. Catches
            # cond(cond1, __STRIP__) where a sibling arg has parens so the
            # [^()]* rule above cannot span it (e.g. a cond() whose branch was
            # stripped, leaving an orphan-comma `cond(x, )`). (#27.6)
            if _SENTINEL in expr:
                def _collapse_if_sentinel(args: str, _full: str) -> str:
                    return _SENTINEL if _SENTINEL in args else _full
                expr = LogicConverter._sub_function_balanced(
                    expr, r'[A-Za-z_]\w*', _collapse_if_sentinel)
            # Bare parenthesised sentinel: `(__STRIP__)` -> _SENTINEL
            expr = re.sub(rf'\(\s*{_SEN}\s*\)', _SENTINEL, expr)
            # Dangling `!` in front of a sentinel: `!__STRIP__` -> _SENTINEL,
            # so the negation doesn't survive as a bare `!`. (#22.1)
            expr = re.sub(rf'!\s*{_SEN}', _SENTINEL, expr)
            # Arithmetic adjacency: a sentinel next to +,-,*,/ makes the whole
            # arithmetic operand unknowable. `operand OP __STRIP__` and mirror. (#27.6)
            expr = re.sub(
                rf'{_ARITH_OPERAND}\s*[+\-*/]\s*{_SEN}', _SENTINEL, expr)
            expr = re.sub(
                rf'{_SEN}\s*[+\-*/]\s*{_ARITH_OPERAND}', _SENTINEL, expr)
            # Strip _SENTINEL with adjacent comparison: `_SENTINEL op operand`
            expr = re.sub(
                rf'{_SEN}\s*{_OP}\s*{_OPERAND}',
                _SENTINEL, expr)
            # Mirror: `operand op _SENTINEL`
            expr = re.sub(
                rf'{_OPERAND}\s*{_OP}\s*{_SEN}',
                _SENTINEL, expr)
            # Remove sentinel surrounded by whitespace
            expr = re.sub(r'\s*' + _SEN + r'\s*', ' ', expr)
            # Dangling & or | at start / end of full expression
            expr = re.sub(r'^\s*[&|]\s*', '', expr)
            expr = re.sub(r'\s*[&|]\s*$', '', expr)
            # Dangling & or | immediately after ( or before )
            expr = re.sub(r'\(\s*[&|]\s*', '(', expr)
            expr = re.sub(r'\s*[&|]\s*\)', ')', expr)
            # Double operators:  & & → &   | | → |
            expr = re.sub(r'\s*&\s*&\s*', ' & ', expr)
            expr = re.sub(r'\s*\|\s*\|\s*', ' | ', expr)
            # Empty parens
            expr = re.sub(r'\(\s*\)', '', expr)
            # Trailing/leading parens that are now unbalanced — basic trim
            expr = expr.strip()

        return expr

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def convert_to_stata(
        surveycto_expr: Optional[str],
        question_types: Dict[str, str],
        choice_codes: Optional[Dict[str, List[str]]] = None,
        varname: str = "",
    ) -> Optional[str]:
        """
        Convert a SurveyCTO relevance expression to Stata syntax.

        Args:
            surveycto_expr: Raw SurveyCTO relevance string.
            question_types: Dict mapping bare variable names to their SurveyCTO
                type string (e.g. {"food": "select_one", "ls": "select_multiple"}).
                Pass {} if question types are unknown (all selected() fall back to
                select_one behaviour).
            choice_codes: Dict mapping bare variable names to their ordered list
                of choice code strings (e.g. {"food": ["1","2","3","-99"]}).
                Required for count-selected() expansion.
            varname: Name of the Stata variable being translated (for strip logging).

        Returns:
            Stata-compatible expression string, or None if input is None/empty
            or the expression is always-false (relevance == "0").
        """
        if not surveycto_expr or not isinstance(surveycto_expr, str):
            return None

        expr = surveycto_expr.strip()
        if not expr:
            return None

        # --- Step 0: always-false -------------------------------------------
        # relevance = "0" means the question is permanently disabled.
        if expr == "0":
            return None

        # --- Step 1: ${var} → var -------------------------------------------
        expr = re.sub(r'\$\{([^}]+)\}', r'\1', expr)

        # --- Step 2: Strip string() wrapper around quoted literals ----------
        # string('X') → 'X'
        # Runs before selected() so selected(string('1 2'), var) becomes
        # selected('1 2', var) → Pattern B.
        # Only strip when the inner content is a quoted literal (starts with ' or ").
        def _strip_string_literal(m: re.Match) -> str:
            inner = m.group(1).strip()
            if inner.startswith("'") or inner.startswith('"'):
                return inner
            return m.group(0)  # keep string(var) — it means numeric→string cast
        expr = re.sub(r'\bstring\s*\(([^)]+)\)', _strip_string_literal, expr, flags=re.IGNORECASE)

        # --- Step 3: Empty-string comparisons (before = → ==) ---------------
        # var != '' → !missing(var) ; var = '' → missing(var). Routed through
        # _sub_outside_literals so a `''`/`""` pair that is really the tail of
        # one literal and the head of the next is not mistaken for an
        # empty-string comparison. (#27.2)
        _EMPTY_NE = re.compile(r'(\w+)\s*!=\s*([\'"])\2')
        _EMPTY_EQ = re.compile(r'(\w+)\s*=\s*([\'"])\2')
        expr = LogicConverter._sub_outside_literals(
            _EMPTY_NE, lambda m: f'!missing({m.group(1)})', expr)
        expr = LogicConverter._sub_outside_literals(
            _EMPTY_EQ, lambda m: f'missing({m.group(1)})', expr)

        # --- Step 3b: Single-quoted non-empty comparisons (e.g. var != '-55') ---
        # SurveyCTO uses single quotes around choice codes in direct comparisons:
        #   ${ag_practices_know} != '-55'
        # Single quotes are INVALID in Stata if-expressions.
        # Translation:
        #   select_multiple var: var != 'N' → var_N != 1  (binary col)
        #   numeric code:        var != 'N' → var != N    (strip quotes)
        #   non-numeric code:    var != 'X' → var != "X"  (Stata string literal)
        def _single_quoted_cmp(m: re.Match) -> str:
            vname = m.group(1)
            op    = m.group(2)    # != or = or ==
            code  = m.group(3)   # inner value without quotes
            stata_op = "!=" if op == "!=" else "=="
            if question_types.get(vname) == "select_multiple":
                suffix = LogicConverter._selected_column_suffix(code)
                return f"{vname}{suffix} {stata_op} 1"
            if re.match(r'^-?\d+(?:\.\d+)?$', code):
                # numeric choice code -> unquoted numeric comparison
                return f"{vname} {stata_op} {code}"
            # non-numeric literal -> a real Stata (double-quoted) string compare,
            # not a bare identifier. (#27.1)
            return f'{vname} {stata_op} "{code}"'
        # _sub_outside_literals skips a match whose word starts inside a literal,
        # so `contains(a, 'x=') or contains(b, 'y=')` is left for step 8. (#27.2)
        expr = LogicConverter._sub_outside_literals(
            re.compile(r"(\w+)\s*(!=|==?)\s*'([^']+)'"),
            _single_quoted_cmp, expr)

        # --- Step 4: string-length() ----------------------------------------
        # string-length(var) > 0 → !missing(var)
        expr = re.sub(
            r'\bstring-length\s*\(\s*(\w+)\s*\)\s*>\s*0',
            r'!missing(\1)', expr, flags=re.IGNORECASE)
        # string-length(var) = 0 → missing(var)
        expr = re.sub(
            r'\bstring-length\s*\(\s*(\w+)\s*\)\s*=\s*0',
            r'missing(\1)', expr, flags=re.IGNORECASE)
        # Remaining string-length(...) → strlen(...) [Stata's string length fn].
        # Catches comparisons like `string-length(var) > 5`.
        expr = re.sub(r'\bstring-length\s*\(', 'strlen(', expr, flags=re.IGNORECASE)

        # --- Step 5: empty(var) → missing(var) ------------------------------
        expr = re.sub(r'\bempty\s*\(\s*(\w+)\s*\)', r'missing(\1)', expr, flags=re.IGNORECASE)

        # --- Step 5b: relevant(var) → !missing(var) -------------------------
        # SurveyCTO `relevant(${x})` is true when the field is currently
        # relevant AND has a value. In the Stata wide-export world, an
        # irrelevant field is missing, so `!missing(x)` is a faithful proxy.
        expr = re.sub(r'\brelevant\s*\(\s*(\w+)\s*\)', r'!missing(\1)', expr, flags=re.IGNORECASE)

        # --- Step 5c: number(x) / int(x) — type-cast passthroughs -----------
        # `number(x)` in SurveyCTO converts text to number; Stata is already
        # numeric in numeric context, so drop the wrapper.
        # `int(x)` in SurveyCTO truncates toward zero; Stata `int(x)` matches.
        # `(?<![\w-])` (not `\b`) so this does not unwrap the `-number(` tail of
        # `format-number(...)`, which is stripped whole at step 12c instead.
        expr = re.sub(r'(?<![\w-])number\s*\(\s*([^)]+?)\s*\)', r'\1', expr, flags=re.IGNORECASE)
        # int() — keep as-is; the regex below documents the intentional no-op
        # so future readers see we considered it.
        # expr = re.sub(r'\bint\s*\(([^)]+)\)', r'int(\1)', expr) — no change needed

        # --- Step 6: if() → cond() ------------------------------------------
        # Rename only; argument structure is identical. `(?<![\w-])` (not `\b`)
        # so the `if(` inside an aggregate-if family (`count-if(`, `sum-if(`,
        # `join-if(`, `rank-index-if(`) is NOT rewritten to `-cond(` — those are
        # stripped whole at step 12c, and a `-cond(` rename would make them leak.
        expr = re.sub(r'(?<![\w-])if\s*\(', 'cond(', expr, flags=re.IGNORECASE)

        # --- Step 7: coalesce() ---------------------------------------------
        # Balanced-paren matching so a nested call in any argument
        # (coalesce(if(x, a, b), c)) is handled as a unit, not truncated at
        # the first inner ')'. (#27.4)
        def _coalesce_replacer(args: str, full_call: str) -> str:
            return LogicConverter._translate_coalesce(args, full_call, varname)
        expr = LogicConverter._sub_function_balanced(expr, r'coalesce', _coalesce_replacer)

        # --- Step 8: String functions ----------------------------------------
        # All four route through balanced-paren matching + top-level arg split,
        # so a needle containing ')' (e.g. regex(${a}, '[0-9])')) is parsed
        # correctly instead of truncated by a bounded [^)]+ regex. (#22.3)

        # regex(var, 'pat') → regexm(var, "pat")
        def _regex_replacer(args: str, full_call: str) -> str:
            parts = LogicConverter._split_top_level_args(args)
            if len(parts) != 2:
                return full_call
            v, pat = parts
            return f'regexm({v}, "{LogicConverter._unwrap_one_quote(pat)}")'
        expr = LogicConverter._sub_function_balanced(expr, r'regex', _regex_replacer)

        # contains(var, 'str') → strpos(var, "str") > 0
        def _contains_replacer(args: str, full_call: str) -> str:
            parts = LogicConverter._split_top_level_args(args)
            if len(parts) != 2:
                return full_call
            v, s = parts
            return f'strpos({v}, "{LogicConverter._unwrap_one_quote(s)}") > 0'
        expr = LogicConverter._sub_function_balanced(expr, r'contains', _contains_replacer)

        # starts-with(var, 'str') → substr(var, 0, len) == "str"
        # Emit in the SurveyCTO 0-based/exclusive-end convention so Step 9's
        # _translate_substr performs the single 0->1-based shift for us:
        # substr(var, 0, N) becomes substr(var, 1, N). Emitting the Stata form
        # (1, N) directly here was the bug -- Step 9 re-shifted it to
        # substr(var, 2, N-1), so the comparison could never be true. (#22 / review)
        def _starts_with_replacer(args: str, full_call: str) -> str:
            parts = LogicConverter._split_top_level_args(args)
            if len(parts) != 2:
                return full_call
            v, s = parts
            s_inner = LogicConverter._unwrap_one_quote(s)
            return f'substr({v}, 0, {len(s_inner)}) == "{s_inner}"'
        expr = LogicConverter._sub_function_balanced(expr, r'starts-with', _starts_with_replacer)

        # ends-with(var, 'str') → substr(var, -len, .) == "str"
        def _ends_with_replacer(args: str, full_call: str) -> str:
            parts = LogicConverter._split_top_level_args(args)
            if len(parts) != 2:
                return full_call
            v, s = parts
            s_inner = LogicConverter._unwrap_one_quote(s)
            return f'substr({v}, -{len(s_inner)}, .) == "{s_inner}"'
        expr = LogicConverter._sub_function_balanced(expr, r'ends-with', _ends_with_replacer)

        # lower(x) → strlower(x) ; upper(x) → strupper(x)
        expr = re.sub(r'\blower\s*\(', 'strlower(', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bupper\s*\(', 'strupper(', expr, flags=re.IGNORECASE)

        # --- Step 9: substr index adjustment ---------------------------------
        expr = re.sub(r'\bsubstr\s*\(([^)]+)\)', LogicConverter._translate_substr, expr, flags=re.IGNORECASE)

        # --- Step 10: count-selected(var) → rowtotal(cols) ------------------
        def _cs_sub(m: re.Match) -> str:
            return LogicConverter._translate_count_selected(m, choice_codes, varname)
        expr = re.sub(r'\bcount-selected\s*\(\s*(\w+)\s*\)', _cs_sub, expr, flags=re.IGNORECASE)

        # --- Step 10b: indexed-repeat(target, group, idx) → target_<idx> ---
        # Uses balanced-paren matching so nested calls like
        # `indexed-repeat(${a}, ${g}, index())` are handled correctly.
        def _ir_replacer(args: str, full_call: str) -> str:
            parts = LogicConverter._split_top_level_args(args)
            if len(parts) < 3:
                _log_strip(varname, full_call, "INDEXED_REPEAT_BAD_ARITY")
                return _SENTINEL
            if len(parts) > 3:
                # >3 args at the top level is an arity error, not a nested
                # call. Nesting (indexed-repeat inside another function) is
                # handled by _sub_function_balanced walking inner calls
                # separately, before this replacer runs.
                _log_strip(varname, full_call, "INDEXED_REPEAT_BAD_ARITY")
                return _SENTINEL
            target, _grp, idx = parts
            idx = idx.strip()
            # Only a positive integer literal maps to a real wide column
            # `target_N`. A negative literal would emit `target_-1`, an invalid
            # Stata name, so strip it instead of emitting garbage. (#27.9)
            if re.match(r'^\d+$', idx):
                return f"{target}_{idx}"
            reason = ("INDEXED_REPEAT_BAD_INDEX" if re.match(r'^-\d+$', idx)
                      else "INDEXED_REPEAT_DYNAMIC")
            _log_strip(varname, full_call, reason)
            return _SENTINEL
        expr = LogicConverter._sub_function_balanced(expr, r'indexed-repeat', _ir_replacer)

        # --- Step 10c: count-selected() with no codes → strip ---------------
        # Done before the selected() step so the trailing `selected(` substring
        # inside `count-selected(` is never seen by a bare selected matcher.
        expr = LogicConverter._strip_balanced(expr, r'count-selected', varname, "COUNT_SEL_NO_CODES")

        # --- Step 11: selected() — Patterns A and B (balanced-paren) --------
        # Balanced matching captures the whole call, so a nested call in the
        # second arg (selected(${x}, format-date(${y}, '%Y'))) is handled as a
        # unit and stripped as Pattern C rather than truncated at the first ')'.
        def _sel_replacer(args: str, full_call: str) -> str:
            return LogicConverter._translate_selected(args, full_call, question_types, varname)
        expr = LogicConverter._sub_function_balanced(expr, r'selected', _sel_replacer)

        # --- Step 12: Strip known-untranslatable function calls -------------
        # Safety net: any selected() still present is Pattern C (dynamic).
        # _translate_selected already strips those, so this rarely fires.
        expr = LogicConverter._strip_balanced(expr, r'selected', varname, "DYNAMIC_SELECTED")

        # position() / index() -> _SENTINEL, balanced so a nested call in the
        # args (index(pos(${a}))) is consumed as a unit rather than truncated
        # at the first inner ')' (which orphaned a stray ')' -- #22.2). Any
        # adjacent comparison (index() > hh_size_pre) is then absorbed by
        # _clean_sentinels' comparison rule, so no bespoke comparison-eating
        # regex is needed. `(?<![\w-])` (in _sub_function_balanced) keeps
        # `index` from matching the tail of `rank-index`.
        expr = LogicConverter._strip_balanced(expr, r'position|index', varname, "POSITION")

        # once() / jr:choice-name() / choice-label() / selected-at() — each can
        # carry a nested call in its args, so strip the whole balanced call.
        expr = LogicConverter._strip_balanced(expr, r'once', varname, "ONCE")
        expr = LogicConverter._strip_balanced(expr, r'jr:choice-name', varname, "CHOICE_NAME")
        expr = LogicConverter._strip_balanced(expr, r'choice-label', varname, "CHOICE_NAME")
        expr = LogicConverter._strip_balanced(expr, r'selected-at', varname, "SELECTED_AT")

        # --- Step 12c: strip untranslatable function FAMILIES (balanced) -----
        # Multi-argument families whose args may contain nested calls. Balanced
        # matching strips the entire call so a nested function (e.g.
        # join(',', if(${a},${b},${c}))) cannot leave an orphan ')'. The `\s*\(`
        # anchor keeps name-prefix pairs distinct (`join` won't eat `join-if`,
        # `date` won't eat `date-time`/`decimal-date-time`), so order is only for
        # readability. Run before the narrow regex strips so an enclosing family
        # call is removed before its bare-ident inner (sum/min/max) is reached.
        _BALANCED_STRIP_FUNCS = [
            (r'join-if',           "JOIN_IF"),
            (r'join',              "JOIN"),
            (r'count-if',          "COUNT_IF"),
            (r'sum-if',            "SUM_IF"),
            (r'min-if',            "MIN_IF"),
            (r'max-if',            "MAX_IF"),
            (r'rank-index(?:-if)?', "RANK_INDEX"),
            (r'count-items',       "LIST_FUNCTION"),
            (r'item-at',           "LIST_FUNCTION"),
            (r'item-index',        "LIST_FUNCTION"),
            (r'item-present',      "LIST_FUNCTION"),
            (r'de-duplicate',      "LIST_FUNCTION"),
            (r'rank-value',        "LIST_FUNCTION"),
            (r'distance-between',  "GEO_FUNCTION"),
            (r'area',              "GEO_FUNCTION"),
            (r'geo-scatter',       "GEO_FUNCTION"),
            (r'short-geopoint',    "GEO_FUNCTION"),
            (r'decimal-date-time', "DATE_FUNCTION"),
            (r'decimal-time',      "DATE_FUNCTION"),
            (r'format-date-time',  "DATE_FUNCTION"),
            (r'format-date',       "DATE_FUNCTION"),
            (r'date-time',         "DATE_FUNCTION"),
            (r'date',              "DATE_FUNCTION"),
            (r'hash',              "HASH"),
            (r'pulldata',          "PULLDATA"),
            (r'search',            "SEARCH"),
            (r'plug-in-metadata',  "PLUGIN"),
            (r'concat',            "CONCAT"),
            (r'format-number',     "CONCAT"),
        ]
        for name_pat, reason in _BALANCED_STRIP_FUNCS:
            expr = LogicConverter._strip_balanced(expr, name_pat, varname, reason)

        # --- Step 12d: strip narrow / no-arg untranslatable calls (regex) ----
        # These are deliberately shape-specific: the aggregate forms match a
        # SINGLE bare identifier only (so multi-field `min(a, b)` valid Stata is
        # left alone), and the metadata/phone/date forms take no args. No nested
        # calls are possible, so a bounded regex is correct and simplest.
        def _make_stripper(rsn: str):
            def _strip(m: re.Match) -> str:
                _log_strip(varname, m.group(0), rsn)
                return _SENTINEL
            return _strip

        _REGEX_STRIP_FUNCS = [
            # `count(${group})` — bare count of repeat instances
            (r'\bcount\s*\(\s*\w+\s*\)',     "COUNT_REPEAT"),
            # 1-arg aggregates over repeats: bare ident inside () only
            (r'\bsum\s*\(\s*\w+\s*\)',       "AGGREGATE_REPEAT"),
            (r'\bmin\s*\(\s*\w+\s*\)',       "AGGREGATE_REPEAT"),
            (r'\bmax\s*\(\s*\w+\s*\)',       "AGGREGATE_REPEAT"),
            # date / time no-arg functions
            (r'\bnow\s*\(\s*\)',             "DATE_FUNCTION"),
            (r'\btoday\s*\(\s*\)',           "DATE_FUNCTION"),
            (r'\bduration\s*\(\s*\)',        "DATE_FUNCTION"),
            # enumerator / session metadata
            (r'\benumerator-name\s*\(\s*\)', "METADATA_FUNCTION"),
            (r'\benumerator-id\s*\(\s*\)',   "METADATA_FUNCTION"),
            (r'\busername\s*\(\s*\)',        "METADATA_FUNCTION"),
            (r'\bversion\s*\(\s*\)',         "METADATA_FUNCTION"),
            (r'\bdevice-info\s*\(\s*\)',     "METADATA_FUNCTION"),
            # phone-call (Android-only)
            (r'\bphone-call-log\s*\(\s*\)', "PHONE_FUNCTION"),
            (r'\bphone-call-duration\s*\(\s*\)', "PHONE_FUNCTION"),
            (r'\bcollect-is-phone-app\s*\(\s*\)', "PHONE_FUNCTION"),
            # randomization / identity
            (r'\buuid\s*\(\s*\)',            "UUID"),
            (r'\brandom\s*\(\s*\)',          "RANDOM"),
            # newline literal
            (r'\blinebreak\s*\(\s*\)',       "CONCAT"),
        ]
        for pattern, reason in _REGEX_STRIP_FUNCS:
            expr = re.sub(pattern, _make_stripper(reason), expr, flags=re.IGNORECASE)

        # --- Step 12f: residual single-quoted comparison operands -> Stata ----
        # By now every translatable function is converted (emitting double
        # quotes) and every untranslatable one is stripped, so any surviving
        # single-quoted literal is a comparison operand whose LHS/RHS step 3b
        # could not match as a bare identifier -- e.g. substr(v, 1, 1) = '0'.
        # Single quotes are never valid in a Stata if-expr, so convert the pair
        # to a double-quoted string literal. (#27.1, generalised)
        #
        # But these patterns are not literal-aware, so a single quote sitting
        # INSIDE an already-emitted double-quoted needle -- e.g. step 8's
        # strpos(x, "a='b'") -- would be mistaken for a comparison operand and
        # corrupt the needle (-> "a= "b""). Mask the double-quoted needles for
        # the duration of these two rewrites so only genuine top-level single-
        # quoted operands are converted, then restore. (review #9)
        _dq_needles: List[str] = []

        def _mask_dq(m: 're.Match') -> str:
            _dq_needles.append(m.group(0))
            return f'__DQ_NEEDLE_{len(_dq_needles) - 1}__'

        expr = re.sub(r'"[^"]*"', _mask_dq, expr)
        expr = re.sub(r"(!=|<=|>=|==?|<|>)\s*'([^']*)'", r'\1 "\2"', expr)
        expr = re.sub(r"'([^']*)'\s*(!=|<=|>=|==?|<|>)", r'"\1" \2', expr)
        for _i, _needle in enumerate(_dq_needles):
            expr = expr.replace(f'__DQ_NEEDLE_{_i}__', _needle)

        # --- Step 12e: mask string literals from the late-stage rewrites -----
        # Everything up to here needs real quotes (step 3b consumes quoted
        # RHSs; step 8 emits double-quoted needles; _find_balanced /
        # _split_top_level_args are quote-aware). Everything after -- 12b's
        # not() scan, step 13 not(->!(, step 14 = -> ==, 14b/14c div/mod,
        # step 15 and/or -> &/|, _clean_sentinels, missing-guards, whitespace
        # collapse -- must NEVER see inside a literal, or a needle like
        # 'x > 5' / ' and ' gets silently corrupted. Restored at step 19.
        expr, _literals = LogicConverter._mask_literals(expr)

        # --- Step 12b: strip entire not(…) when its body contains a sentinel --
        # Stripping a clause from inside not() flips the boolean — unsafe.
        # Per §12.4 of the reference doc: strip the whole not(…) block instead.
        # Balanced-paren matching so a not() whose body contains parens (e.g. a
        # translated selected() -> `(x == 1)`) is still recognised — the old
        # [^)]+ regex failed to match once the body had a paren, leaving the
        # sentinel to be cleaned normally and the boolean wrongly flipped. (#27.5)
        if _SENTINEL in expr:
            def _not_replacer(args: str, full_call: str) -> str:
                if _SENTINEL in args:
                    _log_strip(varname, full_call, "NOT_SAFE_TO_STRIP")
                    return _SENTINEL
                return full_call
            expr = LogicConverter._sub_function_balanced(expr, r'not', _not_replacer)

        # --- Step 13: not( → !( --------------------------------------------
        expr = re.sub(r'\bnot\s*\(', '!(', expr, flags=re.IGNORECASE)

        # --- Step 14: single = → == -----------------------------------------
        expr = re.sub(r'(?<![!><=])=(?!=)', '==', expr)

        # --- Step 14b: div → / ----------------------------------------------
        # SurveyCTO's documented division operator is `div`. Stata uses `/`.
        # Word boundaries protect against substrings like `divisor`; the
        # negative-lookahead `(?!\s*\()` guards against the (undocumented)
        # case of someone writing `div(...)` as if it were a function call,
        # which would otherwise translate to a nonsense `/(...)`.
        expr = re.sub(r'\bdiv\b(?!\s*\()', '/', expr, flags=re.IGNORECASE)

        # --- Step 14c: A mod B → mod(A, B) ----------------------------------
        # SurveyCTO uses the infix `mod` operator; Stata uses the `mod()`
        # function. We only convert when LHS and RHS are simple identifiers
        # or numeric literals — complex sub-expressions on either side stay
        # unchanged and will fail in Stata (rare in practice).
        # Skip when the operand is directly adjacent to `*` or `/`: `2 * x mod 3`
        # is `(2*x) mod 3` in XPath, but `2 * mod(x, 3)` in Stata changes the
        # result, so leave it unconverted (the validator flags the residual). (#27.8)
        def _mod_sub(m: re.Match) -> str:
            s = m.string
            left = s[:m.start()].rstrip()
            right = s[m.end():].lstrip()
            if left[-1:] in ('*', '/') or right[:1] in ('*', '/'):
                return m.group(0)
            return f'mod({m.group(1)}, {m.group(2)})'
        expr = re.sub(
            r'\b(\w+)\s+mod\s+(-?\d+(?:\.\d+)?|\w+)\b',
            _mod_sub, expr, flags=re.IGNORECASE)

        # --- Step 15: and/or → &/| ------------------------------------------
        expr = re.sub(r'\band\b', '&', expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bor\b',  '|', expr, flags=re.IGNORECASE)

        # --- Step 15a: clean sentinels (and orphan comparisons) BEFORE guards.
        # Running cleanup before missing-guards prevents the guard regex from
        # producing nonsense like `& !missing(__STRIP__)` on a stripped LHS.
        if _SENTINEL in expr:
            expr = LogicConverter._clean_sentinels(expr)

        # --- Step 16: add !missing() guard for relational operators ----------
        expr = LogicConverter._add_missing_guards(expr)

        # --- Step 17: final sentinel sweep (defensive — usually a no-op) -----
        if _SENTINEL in expr:
            expr = LogicConverter._clean_sentinels(expr)

        # --- Step 18: final whitespace normalisation -------------------------
        expr = ' '.join(expr.split())

        # --- Step 19: restore masked string literals -------------------------
        # After whitespace collapse, so multi-space needle content survives.
        expr = LogicConverter._unmask_literals(expr, _literals)

        return expr if expr else None

    # ------------------------------------------------------------------

    @staticmethod
    def convert_constraint_to_stata(
        surveycto_expr: Optional[str],
        var_name: str,
        question_types: Dict[str, str],
        choice_codes: Optional[Dict[str, List[str]]] = None,
    ) -> Optional[str]:
        """
        Convert a SurveyCTO `constraint` expression to Stata syntax.

        Constraint expressions use the same syntax as relevance, with one
        extra token: `.` represents the proposed current value of the
        field. We substitute it with the variable name and then run the
        standard relevance pipeline.

        Args:
            surveycto_expr: Raw SurveyCTO constraint string.
            var_name: Name of the Stata variable the constraint binds to.
            question_types: As for convert_to_stata().
            choice_codes: As for convert_to_stata().

        Returns:
            Stata-compatible expression, or None if input is None/empty.
        """
        if not surveycto_expr or not isinstance(surveycto_expr, str):
            return None
        expr = surveycto_expr.strip()
        if not expr:
            return None

        # Substitute `.` (current proposed value) with the variable name.
        # Guarded with lookbehind and lookahead so we don't hit decimal
        # literals like `0.5` or the dot in `${a.b}` (XLSForm field names
        # cannot contain dots, but be safe anyway).
        substituted = re.sub(r'(?<![\w.])\.(?![\w])', var_name, expr)

        return LogicConverter.convert_to_stata(
            substituted, question_types, choice_codes, var_name
        )

    # ------------------------------------------------------------------

    # Every function name the converter can legitimately EMIT into Stata
    # output. Anything else followed by `(` in a converted condition is a
    # leaked (untranslated or half-stripped) call.
    _EMIT_WHITELIST = frozenset({
        "cond", "missing", "inlist", "strpos", "regexm", "substr",
        "strlen", "strlower", "strupper", "rowtotal", "mod", "int",
        "min", "max",
    })

    # Math functions valid AS-IS in Stata that the converter passes through
    # untranslated (same name in SurveyCTO and Stata). These are NOT leaks --
    # e.g. `round(a) > 5` is legitimate Stata output -- so structural_issues
    # must not flag them. (review #12)
    _STATA_PASSTHROUGH = frozenset({
        "round", "sqrt", "abs", "floor", "ceil", "exp", "ln", "log",
        "log10", "sign", "trunc",
    })

    @staticmethod
    def _remove_double_quoted(s: str) -> str:
        """Replace double-quoted literal spans (the converter's own emissions,
        e.g. regexm(v, "...")) with a neutral placeholder token so structural
        checks don't false-positive on quotes/parens/commas that live inside a
        legitimate Stata string. A placeholder (not deletion) keeps argument
        structure intact -- deleting the literal from `regexm(a, "x")` would
        leave `regexm(a, )` and trip the orphan-comma check."""
        out: List[str] = []
        in_str = False
        for ch in s:
            if in_str:
                if ch == '"':
                    in_str = False
                    out.append("_LIT_")
            elif ch == '"':
                in_str = True
            else:
                out.append(ch)
        return ''.join(out)

    @staticmethod
    def structural_issues(condition: str) -> List[str]:
        """
        Return a list of structural-corruption findings in an
        already-converted Stata condition. Empty list = structurally sound.

        These are the corruption modes a strip/translate bug leaves behind:
        a leftover __STRIP__ sentinel, a residual single quote (Stata does
        not allow them), orphan commas from a half-stripped call, unbalanced
        parentheses, a dangling `!`, or a leaked function name the converter
        never emits. All checks ignore double-quoted string literals.
        """
        issues: List[str] = []
        if _SENTINEL in condition:
            issues.append("leftover __STRIP__ sentinel")

        bare = LogicConverter._remove_double_quoted(condition)

        if "'" in bare:
            issues.append("residual single quote")

        if (re.search(r'\(\s*,', bare) or re.search(r',\s*\)', bare)
                or re.search(r',\s*,', bare)
                or re.match(r'^\s*,', bare) or re.search(r',\s*$', bare)):
            issues.append("orphan comma")

        depth = 0
        for ch in bare:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth < 0:
                    break
        if depth != 0:
            issues.append("unbalanced parentheses")

        if re.search(r'!\s*(?:$|[&|,)])', bare):
            issues.append("dangling !")

        # Case-insensitive so an uppercase leak (e.g. PULLDATA(...)) is caught
        # too; normalize before the whitelist check. (review #12)
        for m in re.finditer(r'\b([a-z][a-z0-9:-]*)\s*\(', bare, flags=re.IGNORECASE):
            name = m.group(1).lower()
            if (name not in LogicConverter._EMIT_WHITELIST
                    and name not in LogicConverter._STATA_PASSTHROUGH):
                issues.append(f"leaked function: {m.group(1)}()")

        return issues

    @staticmethod
    def validate_translations(conditions: list) -> None:
        """
        Scan a list of already-converted stata_skip_logic strings and report
        how many still contain untranslated SurveyCTO functions.

        Args:
            conditions: list of stata_skip_logic strings from the variable dictionary
        """
        # Each entry's regex matches the UNTRANSLATED SurveyCTO form. A
        # hit means the converter left an untranslated call in the output
        # (so the translation step for that function is missing or buggy).
        # The two infix-operator entries are deliberately written to
        # match the SurveyCTO infix form (``A div B`` / ``A mod B``), not
        # the converted form (``A / B`` / ``mod(A, B)``); a hit here means
        # the operator conversion at step 14b/14c did not fire.
        CHECKS = [
            ("selected()",    re.compile(r'\bselected\s*\(',      re.IGNORECASE)),
            ("not()",         re.compile(r'\bnot\s*\(',           re.IGNORECASE)),
            ("string()",      re.compile(r'\bstring\s*\(',        re.IGNORECASE)),
            ("index()",       re.compile(r'\bindex\s*\(',         re.IGNORECASE)),
            ("count-sel()",   re.compile(r'count-selected\s*\(',  re.IGNORECASE)),
            ("position()",    re.compile(r'\bposition\s*\(',      re.IGNORECASE)),
            ("once()",        re.compile(r'\bonce\s*\(',          re.IGNORECASE)),
            ("jr:choice-name",re.compile(r'jr:choice-name\s*\(',  re.IGNORECASE)),
            ("choice-label()",re.compile(r'\bchoice-label\s*\(',  re.IGNORECASE)),
            ("selected-at()", re.compile(r'\bselected-at\s*\(',   re.IGNORECASE)),
            ("empty()",       re.compile(r'\bempty\s*\(',         re.IGNORECASE)),
            ("indexed-repeat",re.compile(r'\bindexed-repeat\s*\(',re.IGNORECASE)),
            ("pulldata()",    re.compile(r'\bpulldata\s*\(',      re.IGNORECASE)),
            ("today()/now()", re.compile(r'\b(?:today|now|date|date-time|decimal-time|decimal-date-time|format-date-time)\s*\(', re.IGNORECASE)),
            (" div ",         re.compile(r'\bdiv\b(?!\s*\()',     re.IGNORECASE)),
            (" mod ",         re.compile(r'\bmod\s+\w',           re.IGNORECASE)),
        ]
        print("=== validate_translations ===")
        print(f"  Total conditions scanned: {len(conditions)}")
        for fname, pat in CHECKS:
            hits = [c for c in conditions if c and pat.search(c)]
            status = "OK (0)" if not hits else f"REMAINING: {len(hits)}"
            print(f"  {fname:22s}  {status}")
            if hits:
                print(f"    example: {hits[0][:110]}")

        # Structural corruption sweep: catches half-stripped calls, orphan
        # punctuation, and leaked functions that the per-function checks
        # above cannot see (they only match known SurveyCTO names).
        print("  --- STRUCTURAL ---")
        struct_hits: Dict[str, List[str]] = {}
        for c in conditions:
            if not c:
                continue
            for issue in LogicConverter.structural_issues(c):
                struct_hits.setdefault(issue, []).append(c)
        if not struct_hits:
            print("  no structural issues       OK (0)")
        for issue, examples in sorted(struct_hits.items()):
            print(f"  {issue:26s}  REMAINING: {len(examples)}")
            print(f"    example: {examples[0][:110]}")

    @staticmethod
    def combine_conditions(conditions: List[str], operator: str = "&") -> Optional[str]:
        """
        Combine multiple conditions with specified operator.

        Args:
            conditions: List of Stata conditions
            operator: Operator to join with ("&" or "|")

        Returns:
            Combined condition or None if no valid conditions
        """
        valid_conditions = [c for c in conditions if c]
        if not valid_conditions:
            return None
        if len(valid_conditions) == 1:
            return valid_conditions[0]
        wrapped = [f"({c})" for c in valid_conditions]
        return f" {operator} ".join(wrapped)

    @staticmethod
    def convert_with_inheritance(
        question_relevance: Optional[str],
        group_relevances: List[Optional[str]],
        question_types: Dict[str, str],
        choice_codes: Optional[Dict[str, List[str]]] = None,
        varname: str = "",
    ) -> Optional[str]:
        """
        Convert question relevance with inherited group conditions.

        Args:
            question_relevance: Question-level relevance
            group_relevances: List of parent group relevances (outermost first)
            question_types: Dict mapping variable names to their SurveyCTO type
            choice_codes: Dict mapping variable names to choice code lists
            varname: Variable name for strip logging

        Returns:
            Combined Stata condition or None if no conditions
        """
        stata_group_conditions = [
            LogicConverter.convert_to_stata(r, question_types, choice_codes, varname)
            for r in group_relevances if r
        ]
        stata_question_condition = LogicConverter.convert_to_stata(
            question_relevance, question_types, choice_codes, varname
        )
        all_conditions = (
            stata_group_conditions
            + ([stata_question_condition] if stata_question_condition else [])
        )
        return LogicConverter.combine_conditions(all_conditions, operator="&")
