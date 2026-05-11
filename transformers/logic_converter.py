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
        """
        Given `s` and the index of an opening `(`, return the index of the
        matching closing `)`. Returns -1 if unbalanced.
        """
        depth = 0
        for i in range(open_idx, len(s)):
            if s[i] == '(':
                depth += 1
            elif s[i] == ')':
                depth -= 1
                if depth == 0:
                    return i
        return -1

    @staticmethod
    def _sub_function_balanced(
        expr: str,
        func_pattern: str,
        replacer,
    ) -> str:
        """
        Find every occurrence of `func_pattern(` in `expr` and replace the
        entire balanced call (including nested parens) using `replacer(args)`
        where `args` is the inner argument string.

        `func_pattern` is a regex matching the function NAME alone (without
        the opening paren).
        """
        out: List[str] = []
        i = 0
        rx = re.compile(rf'\b({func_pattern})\s*\(', re.IGNORECASE)
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
            out.append(replacer(args))
            i = close_paren + 1
        return ''.join(out)

    @staticmethod
    def _split_top_level_args(args_str: str) -> List[str]:
        """
        Split a comma-separated argument string respecting parenthesis depth.
        Returns list of stripped argument strings.
        """
        parts: List[str] = []
        depth = 0
        cur: List[str] = []
        for ch in args_str:
            if ch == '(':
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
    def _translate_selected(
        match: re.Match,
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
        """
        args_str = match.group(1)
        parts = [p.strip() for p in args_str.split(',', 1)]
        if len(parts) != 2:
            return match.group(0)

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
                _log_strip(varname, match.group(0), "PATTERN_B_NON_INT")
                return _SENTINEL
            return f"inlist({second}, {', '.join(str(c) for c in int_codes)})"

        # ---- Pattern A or C: first arg is a variable name -----------------
        var = first

        quoted   = re.match(r"^['\"](-?\d+)['\"]$", second)
        unquoted = re.match(r"^(-?\d+)$", second)
        numeric  = quoted or unquoted
        if not numeric:
            # Pattern C — dynamic second arg
            _log_strip(varname, match.group(0), "DYNAMIC_SELECTED")
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
    def _translate_coalesce(match: re.Match, varname: str) -> str:
        """
        N-ary coalesce: returns the first non-missing argument.

        coalesce(a, b)        → cond(missing(a), b, a)
        coalesce(a, b, c)     → cond(missing(a), cond(missing(b), c, b), a)
        coalesce(a, b, c, d)  → cond(missing(a), cond(missing(b), cond(missing(c), d, c), b), a)
        ...

        SurveyCTO docs say coalesce takes any number of non-repeated arguments,
        so we expand inductively from the right.
        """
        parts = LogicConverter._split_top_level_args(match.group(1))
        if len(parts) < 2:
            _log_strip(varname, match.group(0), "COALESCE_BAD_ARITY")
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

        prev = None
        while prev != expr:
            prev = expr
            # Strip _SENTINEL with adjacent comparison: `_SENTINEL op operand`
            expr = re.sub(
                rf'{re.escape(_SENTINEL)}\s*{_OP}\s*{_OPERAND}',
                _SENTINEL, expr)
            # Mirror: `operand op _SENTINEL`
            expr = re.sub(
                rf'{_OPERAND}\s*{_OP}\s*{re.escape(_SENTINEL)}',
                _SENTINEL, expr)
            # Remove sentinel surrounded by whitespace
            expr = re.sub(r'\s*' + re.escape(_SENTINEL) + r'\s*', ' ', expr)
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
        # var != '' → !missing(var)
        expr = re.sub(r'(\w+)\s*!=\s*[\'"][\'"]', r'!missing(\1)', expr)
        # var = '' → missing(var)
        expr = re.sub(r'(\w+)\s*=\s*[\'"][\'"]', r'missing(\1)', expr)

        # --- Step 3b: Single-quoted non-empty comparisons (e.g. var != '-55') ---
        # SurveyCTO uses single quotes around choice codes in direct comparisons:
        #   ${ag_practices_know} != '-55'
        # Single quotes are INVALID in Stata if-expressions.
        # Translation:
        #   select_multiple var: var != 'N' → var_N != 1  (binary col)
        #   other vars:          var != 'N' → var != N    (strip quotes, numeric)
        def _single_quoted_cmp(m: re.Match) -> str:
            vname = m.group(1)
            op    = m.group(2)    # != or = or ==
            code  = m.group(3)   # inner value without quotes
            if question_types.get(vname) == "select_multiple":
                suffix = LogicConverter._selected_column_suffix(code)
                col = vname + suffix
                stata_op = "!=" if op == "!=" else "=="
                return f"{col} {stata_op} 1"
            else:
                # strip quotes, treat code as numeric
                stata_op = "!=" if op == "!=" else "=="
                return f"{vname} {stata_op} {code}"
        expr = re.sub(
            r"(\w+)\s*(!=|==?)\s*'([^']+)'",
            _single_quoted_cmp, expr
        )

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
        expr = re.sub(r'\bnumber\s*\(\s*([^)]+?)\s*\)', r'\1', expr, flags=re.IGNORECASE)
        # int() — keep as-is; the regex below documents the intentional no-op
        # so future readers see we considered it.
        # expr = re.sub(r'\bint\s*\(([^)]+)\)', r'int(\1)', expr) — no change needed

        # --- Step 6: if() → cond() ------------------------------------------
        # Rename only; argument structure is identical.
        expr = re.sub(r'\bif\s*\(', 'cond(', expr, flags=re.IGNORECASE)

        # --- Step 7: coalesce() ---------------------------------------------
        # NOTE: [^)]+ patterns here and below don't handle nested parens —
        # e.g. coalesce(if(x, a, b), c) won't match.  These cases are rare in
        # SurveyCTO instruments and fall through unchanged.  A full balanced-paren
        # parser would fix this but is deferred until a real instrument triggers it.
        def _coalesce_sub(m: re.Match) -> str:
            return LogicConverter._translate_coalesce(m, varname)
        expr = re.sub(r'\bcoalesce\s*\(([^)]+)\)', _coalesce_sub, expr, flags=re.IGNORECASE)

        # --- Step 8: String functions ----------------------------------------

        # regex(var, 'pat') → regexm(var, "pat")
        def _regex_sub(m: re.Match) -> str:
            parts = [p.strip() for p in m.group(1).split(',', 1)]
            if len(parts) != 2:
                return m.group(0)
            v, pat = parts
            pat_inner = pat.strip("'\"")
            return f'regexm({v}, "{pat_inner}")'
        expr = re.sub(r'\bregex\s*\(([^)]+)\)', _regex_sub, expr, flags=re.IGNORECASE)

        # contains(var, 'str') → strpos(var, "str") > 0
        def _contains_sub(m: re.Match) -> str:
            parts = [p.strip() for p in m.group(1).split(',', 1)]
            if len(parts) != 2:
                return m.group(0)
            v, s = parts
            s_inner = s.strip("'\"")
            return f'strpos({v}, "{s_inner}") > 0'
        expr = re.sub(r'\bcontains\s*\(([^)]+)\)', _contains_sub, expr, flags=re.IGNORECASE)

        # starts-with(var, 'str') → substr(var, 1, len) == "str"
        def _starts_with_sub(m: re.Match) -> str:
            parts = [p.strip() for p in m.group(1).split(',', 1)]
            if len(parts) != 2:
                return m.group(0)
            v, s = parts
            s_inner = s.strip("'\"")
            return f'substr({v}, 1, {len(s_inner)}) == "{s_inner}"'
        expr = re.sub(r'\bstarts-with\s*\(([^)]+)\)', _starts_with_sub, expr, flags=re.IGNORECASE)

        # ends-with(var, 'str') → substr(var, -len, .) == "str"
        def _ends_with_sub(m: re.Match) -> str:
            parts = [p.strip() for p in m.group(1).split(',', 1)]
            if len(parts) != 2:
                return m.group(0)
            v, s = parts
            s_inner = s.strip("'\"")
            return f'substr({v}, -{len(s_inner)}, .) == "{s_inner}"'
        expr = re.sub(r'\bends-with\s*\(([^)]+)\)', _ends_with_sub, expr, flags=re.IGNORECASE)

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
        def _ir_replacer(args: str) -> str:
            parts = LogicConverter._split_top_level_args(args)
            if len(parts) < 3:
                _log_strip(varname, f"indexed-repeat({args})", "INDEXED_REPEAT_BAD_ARITY")
                return _SENTINEL
            if len(parts) > 3:
                _log_strip(varname, f"indexed-repeat({args})", "INDEXED_REPEAT_NESTED")
                return _SENTINEL
            target, _grp, idx = parts
            idx = idx.strip()
            if re.match(r'^-?\d+$', idx):
                return f"{target}_{idx}"
            _log_strip(varname, f"indexed-repeat({args})", "INDEXED_REPEAT_DYNAMIC")
            return _SENTINEL
        expr = LogicConverter._sub_function_balanced(expr, r'indexed-repeat', _ir_replacer)

        # --- Step 11: selected() — Patterns A and B -------------------------
        def _sel_sub(m: re.Match) -> str:
            return LogicConverter._translate_selected(m, question_types, varname)
        expr = re.sub(r'\bselected\s*\(([^)]+)\)', _sel_sub, expr, flags=re.IGNORECASE)

        # --- Step 12: Strip known-untranslatable function calls -------------
        # Any remaining selected() calls are Pattern C (dynamic).
        # (Patterns A and B were resolved above; residual = dynamic.)
        def _strip_sel(m: re.Match) -> str:
            _log_strip(varname, m.group(0), "DYNAMIC_SELECTED")
            return _SENTINEL
        expr = re.sub(r'\bselected\s*\([^)]+\)', _strip_sel, expr, flags=re.IGNORECASE)

        # position() / index()
        def _strip_pos(m: re.Match) -> str:
            _log_strip(varname, m.group(0), "POSITION")
            return _SENTINEL
        # Strip `index()/position()` together with any adjacent comparison:
        #   index() > hh_size_pre  →  _SENTINEL   (not just index())
        # Handles both: func() op value  and  value op func()
        _OP = r'(?:>=|<=|!=|==|>(?!=)|<(?!=))'
        _IDENT = r'\w+(?:\.\w+)?'
        expr = re.sub(
            rf'\b(?:position|index)\s*\([^)]*\)\s*{_OP}\s*{_IDENT}',
            _strip_pos, expr, flags=re.IGNORECASE)
        expr = re.sub(
            rf'{_IDENT}\s*{_OP}\s*(?:position|index)\s*\([^)]*\)',
            _strip_pos, expr, flags=re.IGNORECASE)
        # Bare calls (no comparison):
        expr = re.sub(r'\b(?:position|index)\s*\([^)]*\)', _strip_pos, expr, flags=re.IGNORECASE)

        # once()
        def _strip_once(m: re.Match) -> str:
            _log_strip(varname, m.group(0), "ONCE")
            return _SENTINEL
        expr = re.sub(r'\bonce\s*\([^)]+\)', _strip_once, expr, flags=re.IGNORECASE)

        # jr:choice-name() / choice-label()
        def _strip_cn(m: re.Match) -> str:
            _log_strip(varname, m.group(0), "CHOICE_NAME")
            return _SENTINEL
        expr = re.sub(r'jr:choice-name\s*\([^)]+\)', _strip_cn, expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bchoice-label\s*\([^)]+\)', _strip_cn, expr, flags=re.IGNORECASE)

        # count-selected() that couldn't be expanded (no codes available)
        def _strip_cs(m: re.Match) -> str:
            _log_strip(varname, m.group(0), "COUNT_SEL_NO_CODES")
            return _SENTINEL
        expr = re.sub(r'\bcount-selected\s*\([^)]+\)', _strip_cs, expr, flags=re.IGNORECASE)

        # selected-at()
        def _strip_sa(m: re.Match) -> str:
            _log_strip(varname, m.group(0), "SELECTED_AT")
            return _SENTINEL
        expr = re.sub(r'\bselected-at\s*\([^)]+\)', _strip_sa, expr, flags=re.IGNORECASE)

        # --- Step 12c: strip untranslatable function families with named reasons ---
        # Each entry: (regex, reason). Patterns use non-greedy [^)]+? so they
        # do not span balanced parens beyond a single function call.
        _UNTRANSLATABLE_FUNCS = [
            # repeat aggregates with conditional / multi-arg semantics that
            # we cannot resolve without per-iteration expansion
            (r'\bjoin\s*\([^)]+\)',          "JOIN"),
            (r'\bjoin-if\s*\([^)]+\)',       "JOIN_IF"),
            (r'\bcount-if\s*\([^)]+\)',      "COUNT_IF"),
            (r'\bsum-if\s*\([^)]+\)',        "SUM_IF"),
            (r'\bmin-if\s*\([^)]+\)',        "MIN_IF"),
            (r'\bmax-if\s*\([^)]+\)',        "MAX_IF"),
            (r'\brank-index(?:-if)?\s*\([^)]+\)', "RANK_INDEX"),
            # `count(${group})` — bare count of repeat instances, no Stata equivalent
            (r'\bcount\s*\(\s*\w+\s*\)',     "COUNT_REPEAT"),
            # list-of-items helpers
            (r'\bcount-items\s*\([^)]+\)',   "LIST_FUNCTION"),
            (r'\bitem-at\s*\([^)]+\)',       "LIST_FUNCTION"),
            (r'\bitem-index\s*\([^)]+\)',    "LIST_FUNCTION"),
            (r'\bitem-present\s*\([^)]+\)',  "LIST_FUNCTION"),
            (r'\bde-duplicate\s*\([^)]+\)',  "LIST_FUNCTION"),
            (r'\brank-value\s*\([^)]+\)',    "LIST_FUNCTION"),
            # geography
            (r'\bdistance-between\s*\([^)]+\)', "GEO_FUNCTION"),
            (r'\barea\s*\([^)]+\)',          "GEO_FUNCTION"),
            (r'\bgeo-scatter\s*\([^)]+\)',   "GEO_FUNCTION"),
            (r'\bshort-geopoint\s*\([^)]+\)',"GEO_FUNCTION"),
            # date / time conversion functions — Stata's date model differs
            (r'\bdecimal-date-time\s*\([^)]+\)', "DATE_FUNCTION"),
            (r'\bdecimal-time\s*\([^)]+\)',  "DATE_FUNCTION"),
            (r'\bdate-time\s*\([^)]+\)',     "DATE_FUNCTION"),
            (r'\bformat-date-time\s*\([^)]+\)', "DATE_FUNCTION"),
            (r'\bdate\s*\([^)]+\)',          "DATE_FUNCTION"),
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
            # randomization / identity / external data / plug-ins
            (r'\bhash\s*\([^)]+\)',          "HASH"),
            (r'\buuid\s*\(\s*\)',            "UUID"),
            (r'\brandom\s*\(\s*\)',          "RANDOM"),
            (r'\bpulldata\s*\([^)]+\)',      "PULLDATA"),
            (r'\bsearch\s*\([^)]+\)',        "SEARCH"),
            (r'\bplug-in-metadata\s*\([^)]+\)', "PLUGIN"),
            # 1-arg aggregates over repeats: bare ident inside ()
            # Stata can express these as rowtotal/rowmin/rowmax over enumerated
            # suffix columns, but only when we know the iteration count.
            # Without that, strip with a specific reason for visibility.
            (r'\bsum\s*\(\s*\w+\s*\)',       "AGGREGATE_REPEAT"),
            (r'\bmin\s*\(\s*\w+\s*\)',       "AGGREGATE_REPEAT"),
            (r'\bmax\s*\(\s*\w+\s*\)',       "AGGREGATE_REPEAT"),
            # string concat / formatting — no clean Stata translation in a
            # boolean expression context
            (r'\bconcat\s*\([^)]+\)',        "CONCAT"),
            (r'\blinebreak\s*\(\s*\)',       "CONCAT"),
            (r'\bformat-number\s*\([^)]+\)', "CONCAT"),
        ]
        for pattern, reason in _UNTRANSLATABLE_FUNCS:
            def _make_stripper(rsn: str):
                def _strip(m: re.Match) -> str:
                    _log_strip(varname, m.group(0), rsn)
                    return _SENTINEL
                return _strip
            expr = re.sub(pattern, _make_stripper(reason), expr, flags=re.IGNORECASE)

        # --- Step 12b: strip entire not(…) when its body contains a sentinel --
        # Stripping a clause from inside not() flips the boolean — unsafe.
        # Per §12.4 of the reference doc: strip the whole not(…) block instead.
        if _SENTINEL in expr:
            def _strip_not_with_sentinel(m: re.Match) -> str:
                if _SENTINEL in m.group(1):
                    _log_strip(varname, m.group(0), "NOT_SAFE_TO_STRIP")
                    return _SENTINEL
                return m.group(0)
            # Only handles one level of nesting — sufficient for all real survey patterns
            expr = re.sub(r'\bnot\s*\(([^)]+)\)', _strip_not_with_sentinel, expr, flags=re.IGNORECASE)

        # --- Step 13: not( → !( --------------------------------------------
        expr = re.sub(r'\bnot\s*\(', '!(', expr, flags=re.IGNORECASE)

        # --- Step 14: single = → == -----------------------------------------
        expr = re.sub(r'(?<![!><=])=(?!=)', '==', expr)

        # --- Step 14b: div → / ----------------------------------------------
        # SurveyCTO's documented division operator is `div`. Stata uses `/`.
        # Word boundaries protect against substrings like `divisor`.
        expr = re.sub(r'\bdiv\b', '/', expr, flags=re.IGNORECASE)

        # --- Step 14c: A mod B → mod(A, B) ----------------------------------
        # SurveyCTO uses the infix `mod` operator; Stata uses the `mod()`
        # function. We only convert when LHS and RHS are simple identifiers
        # or numeric literals — complex sub-expressions on either side stay
        # unchanged and will fail in Stata (rare in practice).
        expr = re.sub(
            r'\b(\w+)\s+mod\s+(-?\d+(?:\.\d+)?|\w+)\b',
            r'mod(\1, \2)', expr, flags=re.IGNORECASE)

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

    @staticmethod
    def validate_translations(conditions: list) -> None:
        """
        Scan a list of already-converted stata_skip_logic strings and report
        how many still contain untranslated SurveyCTO functions.

        Args:
            conditions: list of stata_skip_logic strings from the variable dictionary
        """
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
            (" div ",         re.compile(r'\bdiv\b',              re.IGNORECASE)),
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
