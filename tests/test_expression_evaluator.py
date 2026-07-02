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
    def test_blank_numeric_field_comparison_is_false_both_ways(self):
        # A blank/skipped numeric field is NaN (XPath number('')), so a
        # comparison is False in BOTH directions -- matching SurveyCTO, where a
        # skipped field does not satisfy a gate. (Previously blank coerced to 0,
        # so `${age} < 18` wrongly fired and the synth populated questions real
        # SurveyCTO would hide -- #28.1.)
        c = ctx(row={"age": ""})
        assert evaluate_bool("${age} > 18", c) is False
        assert evaluate_bool("${age} < 18", c) is False

    def test_blank_arithmetic_propagates_blank(self):
        # Blank + blank is NaN, which serialises to an empty string, not 0 or
        # "nan" -- matching a blank SurveyCTO export cell. (#28.2)
        c = ctx(row={"inc1": "", "inc2": ""})
        assert evaluate("${inc1} + ${inc2}", c) != evaluate("${inc1} + ${inc2}", c)  # NaN
        from transformers.expression_evaluator import _to_string
        assert _to_string(evaluate("${inc1} + ${inc2}", c)) == ""

    def test_explicit_nan_operand_makes_comparison_false(self):
        # Only an explicit NaN operand (here, an unparseable date) forces the
        # comparison to False on both sides.
        assert evaluate_bool("decimal-date-time('nope') > 1", ctx()) is False
        assert evaluate_bool("decimal-date-time('nope') < 1", ctx()) is False

    def test_empty_string_equality(self):
        c = ctx(row={"name": ""})
        assert evaluate_bool("${name} = ''", c) is True
        assert evaluate_bool("${name} != ''", c) is False

    def test_equal_semantics_audit(self):
        # Guards for the NaN change: blank must NOT equal 0, blank equals blank,
        # numeric-string equals numeric.
        assert evaluate_bool("${x} = 0", ctx(row={"x": ""})) is False
        assert evaluate_bool("${x} = ''", ctx(row={"x": ""})) is True
        assert evaluate_bool("'2' = 2", ctx()) is True


class TestDateArithmetic:
    def test_age_calc_yields_real_age_not_zero(self):
        # int((today() - date(${dob})) div 365.25) -- the ubiquitous age calc.
        # Dates must coerce to decimal days so this is the true age, not 0
        # (which every respondent got when date subtraction fell to NaN). #28.3
        c = ctx(row={"dob": "1990-01-01"}, now=datetime.datetime(2020, 1, 1))
        age = evaluate("int((today() - date(${dob})) div 365.25)", c)
        assert age == 29 or age == 30  # ~30 years, floor after div

    def test_age_gate_now_reachable(self):
        c = ctx(row={"dob": "1990-01-01"}, now=datetime.datetime(2020, 1, 1))
        assert evaluate_bool(
            "int((today() - date(${dob})) div 365.25) >= 18", c) is True

    def test_blank_dob_age_is_nan_not_zero(self):
        c = ctx(row={"dob": ""}, now=datetime.datetime(2020, 1, 1))
        age = evaluate("int((today() - date(${dob})) div 365.25)", c)
        assert age != age  # NaN, not 0


class TestPulldataErrorContract:
    def test_pulldata_without_lookup_returns_empty(self):
        assert evaluate("pulldata('f', 'c', 'k', 1)", ctx()) == ""

    def test_pulldata_raising_lookup_surfaces_error(self):
        def _boom(*a):
            raise KeyError("no such column")
        c = ctx(pulldata_lookup=_boom)
        # safe_evaluate should log/raise, not silently blank (#28.9).
        with pytest.raises(EvaluationError):
            evaluate("pulldata('f', 'c', 'k', 1)", c)


class TestNumericSemantics:
    def test_round_half_up(self):
        assert evaluate("round(2.5)", ctx()) == 3
        assert evaluate("round(3.5)", ctx()) == 4
        assert evaluate("round(-2.5)", ctx()) == -2

    def test_mod_truncates_toward_zero(self):
        assert evaluate("-3 mod 2", ctx()) == -1.0


class TestNestedRepeatScoping:
    def test_outer_field_not_bled_from_inner_index(self):
        # At (outer=2, inner=3), a reference to an outer-scoped field `a`
        # (stored a_2) must resolve to a_2, NOT a_3 (a different outer
        # iteration) -- the cross-iteration bleed of #28.4.
        c = ctx(row={"a_2": "two", "a_3": "three"},
                repeat_stack=[("outer", 2), ("inner", 3)])
        assert evaluate("${a}", c) == "two"

    def test_nested_field_resolves_full_chain(self):
        c = ctx(row={"b_2_3": "correct", "b_2_1": "other", "b_1_3": "wrong"},
                repeat_stack=[("outer", 2), ("inner", 3)])
        assert evaluate("${b}", c) == "correct"

    def test_sum_over_nested_field_scoped_to_current_outer(self):
        # sum(${plot}) inside parcel 2 sums only parcel 2's plots (#28.5) --
        # the old single-suffix enumeration found nothing for nested storage.
        c = ctx(row={"plot_1_1": "10", "plot_1_2": "20",
                     "plot_2_1": "3", "plot_2_2": "4"},
                repeat_stack=[("parcel", 2)])
        assert evaluate("sum(${plot})", c) == 7  # 3 + 4, not 37 and not 0

    def test_single_repeat_sum_unchanged(self):
        # Regression guard: a plain single-level repeat still sums all.
        c = ctx(row={"x_1": "1", "x_2": "2", "x_3": "3"})
        assert evaluate("sum(${x})", c) == 6

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
