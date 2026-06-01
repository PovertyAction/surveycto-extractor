"""Seed regression tests for the XLSForm expression evaluator
(`transformers/expression_evaluator.py`): the recursive-descent parser and the
tree-walking interpreter that the synthetic generator leans on. Covers operator
precedence, short-circuit logic, missing-value propagation, the select / count
string functions, repeat aggregates, pulldata, dates, and the regex() error
contract (issue #12)."""
import datetime
import math

import pytest

from transformers.expression_evaluator import (
    EvalContext,
    EvaluationError,
    ExpressionError,
    evaluate,
    evaluate_bool,
    safe_evaluate,
)


def ctx(**kw):
    return EvalContext(**kw)


class TestPrecedenceAndArithmetic:
    def test_mul_binds_tighter_than_add(self):
        assert evaluate("2 + 3 * 4", ctx()) == 14

    def test_parens_override(self):
        assert evaluate("(2 + 3) * 4", ctx()) == 20

    def test_comparison_then_and(self):
        assert evaluate_bool("1 = 1 and 2 = 2", ctx()) is True
        assert evaluate_bool("1 = 1 and 2 = 3", ctx()) is False

    def test_div_is_float_division(self):
        assert evaluate("7 div 2", ctx()) == 3.5


class TestShortCircuit:
    def test_or_short_circuits_before_error(self):
        # A true LHS must not evaluate the RHS — which here would raise on a
        # malformed regex pattern. If `or` did not short-circuit, this raises.
        assert evaluate_bool("1 = 1 or regex('x', '[')", ctx()) is True

    def test_and_short_circuits_before_error(self):
        assert evaluate_bool("1 = 2 and regex('x', '[')", ctx()) is False

    def test_basic_truth_tables(self):
        assert evaluate_bool("true() or false()", ctx()) is True
        assert evaluate_bool("true() and false()", ctx()) is False


class TestMissingValues:
    def test_blank_numeric_field_coerces_to_zero_in_comparison(self):
        # The evaluator coerces a blank field to 0 in numeric context (see
        # _to_number), so a blank behaves as 0 -- NOT as Stata's +inf missing
        # and NOT as an always-False NaN. This is the synth-vs-Stata gap
        # documented in docs/synthetic-generator.md.
        c = ctx(row={"age": ""})
        assert evaluate_bool("${age} > 18", c) is False   # 0 > 18
        assert evaluate_bool("${age} < 18", c) is True     # 0 < 18

    def test_explicit_nan_operand_makes_comparison_false(self):
        # Only an explicit NaN operand (here, an unparseable date) forces the
        # comparison to False on both sides.
        assert evaluate_bool("decimal-date-time('nope') > 1", ctx()) is False
        assert evaluate_bool("decimal-date-time('nope') < 1", ctx()) is False

    def test_empty_string_equality(self):
        c = ctx(row={"name": ""})
        assert evaluate_bool("${name} = ''", c) is True
        assert evaluate_bool("${name} != ''", c) is False

    def test_empty_function(self):
        assert evaluate_bool("empty(${missing})", ctx(row={})) is True


class TestSelectFunctions:
    def test_selected_against_space_separated(self):
        c = ctx(row={"fav": "1 3 5"})
        assert evaluate_bool("selected(${fav}, '3')", c) is True
        assert evaluate_bool("selected(${fav}, '2')", c) is False

    def test_selected_empty_field_is_false(self):
        assert evaluate_bool("selected(${fav}, '1')", ctx(row={"fav": ""})) is False

    def test_count_selected(self):
        assert evaluate("count-selected(${fav})", ctx(row={"fav": "1 3 5"})) == 3.0
        assert evaluate("count-selected(${fav})", ctx(row={"fav": ""})) == 0.0


class TestRegexErrorContract:
    def test_match_and_nonmatch(self):
        assert evaluate_bool("regex('abc123', '[0-9]+')", ctx()) is True
        assert evaluate_bool("regex('abc', '[0-9]+')", ctx()) is False

    def test_bad_pattern_raises_not_silent_false(self):
        # Issue #12: a malformed pattern must surface as an error, not silently
        # return False (which would mark every row "no match").
        with pytest.raises(EvaluationError):
            evaluate("regex('x', '[')", ctx())

    def test_safe_evaluate_applies_fallback_and_logs(self):
        logged = []
        out = safe_evaluate(
            "regex('x', '[')", ctx(), default=True,
            on_error=lambda e, exc: logged.append((e, exc)),
        )
        assert out is True            # caller's documented fallback
        assert len(logged) == 1       # surfaced, not hidden
        assert isinstance(logged[0][1], ExpressionError)


class TestPulldata:
    def test_pulldata_uses_lookup_callable(self):
        table = {("cases", "phone", "id", "c1"): "0700000000"}
        c = ctx(
            row={"caseid": "c1"},
            pulldata_lookup=lambda csv, col, key_col, key_val: table.get(
                (csv, col, key_col, str(key_val)), ""
            ),
        )
        assert evaluate("pulldata('cases', 'phone', 'id', ${caseid})", c) == "0700000000"

    def test_pulldata_without_lookup_returns_empty(self):
        assert evaluate("pulldata('cases', 'phone', 'id', 'c1')", ctx()) == ""


class TestDates:
    def test_decimal_date_time_orders_correctly(self):
        earlier = evaluate("decimal-date-time('2024-06-17T00:00:00')", ctx())
        later = evaluate("decimal-date-time('2024-06-18T00:00:00')", ctx())
        assert math.isclose(later - earlier, 1.0, abs_tol=1e-6)

    def test_decimal_date_time_of_unparseable_is_nan(self):
        v = evaluate("decimal-date-time('not a date')", ctx())
        assert v != v  # NaN

    def test_duration_reads_context(self):
        assert evaluate("duration()", ctx(duration_secs=42.0)) == 42.0
        assert evaluate("duration()", ctx()) == 0.0


class TestRepeatAggregates:
    def test_sum_over_repeat_columns(self):
        c = ctx(row={"x_1": "2", "x_2": "3", "x_3": "5"})
        assert evaluate("sum(${x})", c) == 10.0

    def test_count_over_repeat_count_column(self):
        assert evaluate("count(${grp})", ctx(row={"grp_count": "3"})) == 3.0

    def test_count_if_predicate_per_iteration(self):
        c = ctx(row={"age_1": "10", "age_2": "20", "age_3": "30"})
        # Two of the three iterations satisfy ${age} > 15.
        assert evaluate("count-if(${age}, ${age} > 15)", c) == 2.0

    def test_sum_if_predicate_per_iteration(self):
        c = ctx(row={"amt_1": "100", "amt_2": "200", "amt_3": "50"})
        assert evaluate("sum-if(${amt}, ${amt} >= 100)", c) == 300.0


class TestParseErrors:
    def test_unbalanced_parens_raises_expression_error(self):
        with pytest.raises(ExpressionError):
            evaluate("1 + (2 * 3", ctx())
