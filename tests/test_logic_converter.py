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
        # Whole comparison is meaningless once the aggregate is stripped.
        assert result is None or ("cond" not in result and "sum" not in result)

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
