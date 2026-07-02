"""Regression tests for `transformers/logic_converter.py`.

Seeded around three pre-existing defects found while reviewing PR #16, all in the
SurveyCTO->Stata relevance translator:

- #19: `_find_balanced` was string-literal-blind, so a quoted `)` ended a call
  early and orphaned the tail (`join(")", ...)`, `concat(${a}, ")")`).
- #18: a shorter registered function name matched the hyphen-suffix of a longer
  one (`date` ate `format-date`, `number` ate `format-number`, `index` ate
  `rank-index`), orphaning the `format-`/`rank-` prefix.
- #17: the `if(`->`cond(` rename matched inside the aggregate-if families
  (`count-if(`, `sum-if(`, `join-if(`), producing `*-cond(...)` that then leaked
  because the family strips no longer recognised it.

Plus guards that valid translations still work, so the boundary tightening did
not over-restrict.
"""
from transformers.logic_converter import LogicConverter, clear_strip_log

convert = LogicConverter.convert_to_stata


def setup_function(_):
    # Strip log is module-level state; keep tests independent.
    clear_strip_log()


class TestFindBalancedQuoteAware:
    """#19 -- a `)` inside a string literal must not end the call early."""

    def test_join_with_quoted_paren_consumed_as_unit(self):
        # Was: '", a, b) & x > 5 & !missing(x)'  (orphaned tail after quoted ')')
        result = convert('join(")", ${a}, ${b}) and ${x} > 5', {})
        assert result == "x > 5 & !missing(x)"
        assert '"' not in result          # no orphaned quote
        assert "join" not in result.lower()

    def test_concat_with_quoted_paren_fully_stripped(self):
        # Was: '")'  -- the whole concat() should be consumed, leaving nothing.
        result = convert('concat(${a}, ")")', {})
        assert result is None

    def test_find_balanced_direct(self):
        s = 'join(")", a, b) rest'
        # opening paren is at index 4; matching close is the one before " rest"
        close = LogicConverter._find_balanced(s, 4)
        assert s[close] == ")"
        assert s[close + 1:] == " rest"

    def test_split_top_level_args_quote_aware(self):
        # A comma inside a string literal is not a separator.
        parts = LogicConverter._split_top_level_args("${x}, ',', ${y}")
        assert parts == ["${x}", "','", "${y}"]

    def test_backslash_before_quote_still_closes(self):
        # XPath has no backslash escape; a literal ending in '\' (e.g. a path or
        # regex) must still close. Treating '\' as an escape leaves the string
        # "open", _find_balanced returns -1, and the whole call leaks verbatim.
        result = convert(r"concat(${a}, 'x\') and ${y} > 1", {})
        assert result == "y > 1 & !missing(y)"
        assert "concat" not in result

    def test_find_balanced_backslash_literal(self):
        s = r"join('C:\', a, b)"  # literal: join('C:\', a, b)
        close = LogicConverter._find_balanced(s, 4)
        assert close == len(s) - 1  # the final ')'


class TestHyphenatedFunctions:
    """#18 -- hyphenated functions must strip whole, never orphan a prefix."""

    def test_format_date_no_orphan(self):
        result = convert("${x} > 5 and format-date(${d}, '%Y')", {})
        assert result == "x > 5 & !missing(x)"
        assert "format" not in result
        assert "date" not in result

    def test_format_date_alone_stripped(self):
        assert convert("format-date(${d}, '%Y')", {}) is None

    def test_format_number_no_orphan(self):
        result = convert("${x} > 5 and format-number(${n}, '%.2f')", {})
        assert result == "x > 5 & !missing(x)"
        assert "format" not in result
        assert "%" not in result

    def test_rank_index_no_orphan(self):
        result = convert("${x} > 5 and rank-index(${a}, ${b}, 1)", {})
        assert result == "x > 5 & !missing(x)"
        assert "rank" not in result
        assert "index" not in result


class TestAggregateIfFamilies:
    """#17 -- `*-if(...)` families must be stripped, not leak as `*-cond(...)`."""

    def test_count_if_no_cond_leak(self):
        result = convert("count-if(${g}, ${x} > 0) and ${y} = 1", {})
        assert result == "y == 1"
        assert "cond" not in result
        assert "count" not in result
        assert "-if" not in result

    def test_sum_if_stripped(self):
        result = convert("sum-if(${g}, ${x} > 0) > 2", {})
        # Whole comparison is meaningless once the aggregate is stripped -> None.
        assert result is None

    def test_join_if_no_cond_leak(self):
        result = convert("${x} > 5 and join-if(${a}, ',', ${b} > 0)", {})
        assert result == "x > 5 & !missing(x)"
        assert "cond" not in result
        assert "join" not in result


class TestRegressionGuards:
    """The boundary tightening must not break valid translations."""

    def test_standalone_if_becomes_cond(self):
        assert convert("if(${a}, ${b}, ${c})", {}) == "cond(a, b, c)"

    def test_multi_arg_min_preserved(self):
        # Valid Stata min(a, b) -- the bare-ident aggregate strip must not touch it.
        assert convert("min(${a}, ${b})", {}) == "min(a, b)"

    def test_bare_aggregate_min_still_stripped(self):
        # Single bare ident inside () is a repeat aggregate -> stripped.
        assert convert("min(${g})", {}) is None

    def test_selected_select_one(self):
        assert convert("selected(${food}, '3')", {"food": "select_one"}) == "(food == 3)"

    def test_selected_select_multiple(self):
        assert convert("selected(${ls}, '2')", {"ls": "select_multiple"}) == "(ls_2 == 1)"


class TestStructuralIssues:
    """#27.7 -- structural_issues must flag every corruption mode a strip or
    translate bug can leave behind. Inputs are the exact corrupted outputs
    quoted in issues #22 and #27."""

    issues = staticmethod(LogicConverter.structural_issues)

    def test_leftover_sentinel(self):
        assert "leftover __STRIP__ sentinel" in self.issues("a == 1 & __STRIP__")

    def test_residual_single_quote(self):
        # From #22 case 3: contains needle emptied with orphan quote tail.
        assert "residual single quote" in self.issues("strpos(a, \"\") > 0')")

    def test_orphan_comma(self):
        # From #27.6: sentinel-as-argument leaves cond( , 2, 3).
        assert "orphan comma" in self.issues("cond( , 2, 3) > 1")

    def test_unbalanced_parens(self):
        # From #22 case 2: index(pos(${a})) > 2 -> ') > 2'.
        assert "unbalanced parentheses" in self.issues(") > 2")

    def test_dangling_bang(self):
        # From #22 case 1: 'a == 1 & !' -- invalid Stata.
        assert "dangling !" in self.issues("a == 1 & !")

    def test_leaked_function(self):
        assert any(i.startswith("leaked function: pulldata")
                   for i in self.issues("pulldata('x', 'y', 'z', a) == 1"))

    def test_quoted_content_ignored(self):
        # Parens, commas, quotes inside a double-quoted Stata literal are fine.
        assert self.issues('regexm(a, "(x,y) \'q\'") & b == 1') == []

    def test_clean_conditions_pass(self):
        assert self.issues("x > 5 & !missing(x)") == []
        assert self.issues("cond(missing(a), b, a) == 2") == []
        assert self.issues("!(a == 1) | inlist(b, 1, 2)") == []


class TestLiteralMasking:
    """#27 Class B -- late-stage operator rewrites must not reach inside the
    string literals the converter itself emits at step 8."""

    def test_regex_needle_equals_not_doubled(self):
        # Step 14 (= -> ==) must not touch the '=' inside the emitted needle.
        result = convert("regex(${id}, '^ab=')", {})
        assert result == 'regexm(id, "^ab=")'

    def test_contains_needle_and_not_ampersanded(self):
        # Step 15 (and -> &) must not touch ' and ' inside the needle.
        result = convert("contains(${occ}, ' and ')", {})
        assert result == 'strpos(occ, " and ") > 0'

    def test_contains_needle_or_preserved(self):
        result = convert("contains(${v}, 'x or y')", {})
        assert result == 'strpos(v, "x or y") > 0'

    def test_regex_needle_no_spurious_missing_guard(self):
        # A '>' inside the needle must not trigger _add_missing_guards.
        result = convert("regex(${v}, 'x > 5')", {})
        assert result == 'regexm(v, "x > 5")'
        assert "missing" not in result

    def test_needle_multispace_survives_whitespace_collapse(self):
        # Step 18 collapses whitespace; a masked needle keeps its two spaces.
        result = convert("regex(${v}, 'a  b')", {})
        assert result == 'regexm(v, "a  b")'

    def test_needle_div_mod_words_untouched(self):
        # Steps 14b/14c must not rewrite 'div'/'mod' inside a needle.
        result = convert("contains(${v}, 'a div b mod c')", {})
        assert result == 'strpos(v, "a div b mod c") > 0'


class TestQuoteAwareStripsAndCleanup:
    """C7 -- #22 (all) + #27.1,2,4,5,6,8,9. Each asserts the output is also
    structurally clean, so a fix can't trade one corruption for another."""

    def _clean(self, result):
        if result is not None:
            assert LogicConverter.structural_issues(result) == [], result

    def test_string_code_double_quoted_not_bare(self):  # #27.1
        result = convert("${region} = 'north'", {})
        assert result == 'region == "north"'
        self._clean(result)

    def test_numeric_code_stays_unquoted(self):  # #27.1 regression guard
        assert convert("${x} != '-55'", {}) == "x != -55"

    def test_quoted_arg_not_eaten_by_step3b(self):  # #27.2
        # 'x=' inside contains() must not be matched as a comparison.
        result = convert("contains(${a}, 'x=') or contains(${b}, 'y=')", {})
        assert result == 'strpos(a, "x=") > 0 | strpos(b, "y=") > 0'
        self._clean(result)

    def test_contains_needle_with_close_paren(self):  # #22.3
        result = convert("contains(${a}, ')')", {})
        assert result == 'strpos(a, ")") > 0'
        self._clean(result)

    def test_regex_needle_with_close_paren(self):  # #22.3
        result = convert("regex(${a}, '[0-9])')", {})
        assert result == 'regexm(a, "[0-9])")'
        self._clean(result)

    def test_index_with_nested_call_no_orphan(self):  # #22.2
        result = convert("index(pos(${a})) > 2", {})
        assert result is None
        self._clean(result)

    def test_position_nested_arg_stripped(self):  # #22.2
        result = convert("position(${a}, foo(${b})) > 2", {})
        assert result is None
        self._clean(result)

    def test_dangling_bang_index_cleaned(self):  # #22.1
        result = convert("!index(${a})", {})
        assert result is None
        self._clean(result)

    def test_bang_index_in_conjunction(self):  # #22.1
        result = convert("${a} = 1 and !index(${b})", {})
        assert result == "a == 1"
        self._clean(result)

    def test_sentinel_arithmetic_adjacency(self):  # #27.6
        result = convert("${a} + duration() > 5", {})
        assert result is None
        self._clean(result)

    def test_sentinel_as_cond_argument(self):  # #27.6
        result = convert("if(pulldata('f','k','q') = 1, 2, 3) > 1", {})
        assert result is None
        self._clean(result)

    def test_coalesce_with_nested_call(self):  # #27.4
        # coalesce(if(...), d) -- nested call kept intact, expands correctly.
        result = convert("coalesce(if(${a}, ${b}, ${c}), ${d})", {})
        assert result == "cond(missing(cond(a, b, c)), d, cond(a, b, c))"
        self._clean(result)

    def test_not_with_sentinel_body_stripped_whole(self):  # #27.5
        # not(selected(...) and pulldata(...)) -- selected translates to (x==1),
        # the pulldata strips; the whole not() must be stripped, not flipped.
        result = convert(
            "not(selected(${x}, '1') and pulldata('f','k','q'))",
            {"x": "select_one"},
        )
        assert result is None
        self._clean(result)

    def test_mod_precedence_left_adjacent_star(self):  # #27.8
        # 2 * x mod 3 must NOT become 2 * mod(x, 3) (wrong precedence).
        result = convert("2 * ${x} mod 3", {})
        assert "mod(" not in result

    def test_indexed_repeat_negative_index_stripped(self):  # #27.9
        result = convert("indexed-repeat(${a}, ${g}, -1)", {})
        assert result is None
        self._clean(result)

    def test_indexed_repeat_positive_index_ok(self):  # #27.9 guard
        assert convert("indexed-repeat(${a}, ${g}, 2)", {}) == "a_2"


class TestConstraintCorruptionsFromSample:
    """C7 -- three real corruptions the structural validator surfaced in the
    sample's own constraint expressions (all `.`-form constraints)."""

    constraint = staticmethod(LogicConverter.convert_constraint_to_stata)

    def test_substr_compared_to_quoted_literal(self):
        # substr(., 0, 1) = '0' : LHS is a function result, so step 3b's bare
        # identifier pattern misses it; the single quote must still become a
        # Stata double-quoted string, not survive as an invalid ' literal.
        result = self.constraint(
            ". = -888 or (substr(., 0, 1) = '0' and string-length(.) = 10)",
            "resp_contact", {}, {})
        assert result == ('resp_contact == -888 | (substr(resp_contact, 1, 1) '
                          '== "0" & strlen(resp_contact) == 10)')
        assert LogicConverter.structural_issues(result) == []

    def test_index_dependent_constraint_dropped_cleanly(self):
        # A per-iteration constraint gated on index() cannot be evaluated in
        # the wide-export Stata world -> dropped whole, not left as cond(X, ).
        result = self.constraint(
            "if(index() = 1, . >= 18, if(index() = 2, . >= 3, . >= 0))",
            "age", {}, {})
        assert result is None

    def test_cond_with_stripped_branch_no_orphan_comma(self):
        # cond() whose sibling branch has parens and the other was stripped:
        # the balanced sentinel-collapse must remove the whole call, not leave
        # cond(a >= 18 & !missing(a), ).
        result = convert("if(${a} >= 18, once(${b}), ${c})", {})
        assert LogicConverter.structural_issues(result or "") == []
