"""Seed regression tests for the constraint parsers in `generators/sampling.py`.

These three parsers extract sampling bounds from SurveyCTO `constraint`
expressions via regex, which is exactly the kind of surface where a small
pattern change silently breaks a case (cf. commit 2a6c591, "Pass 5 fix:
text_max_length regex matched = inside >= / !="). Cover the operator variants,
the disjunction/conditional bail-outs, and the known quote-tracking limitation.
"""

from surveycto_extractor.generators.sampling import (
    _has_top_level_or,
    numeric_bounds,
    select_multiple_count_bounds,
    text_max_length,
)


class TestNumericBounds:
    def test_two_sided_inclusive(self):
        assert numeric_bounds(". >= 0 and . <= 120") == (0, 120)

    def test_strict_greater_shifts_up_by_one(self):
        # `> 0` is pinned to an integer-safe inclusive lower bound of 1.
        assert numeric_bounds(". > 0") == (1, None)

    def test_strict_less_shifts_down_by_one(self):
        assert numeric_bounds(". < 18") == (None, 17)

    def test_named_var_bound_ignored(self):
        # A bound on a DIFFERENT variable is not a bound on the current field
        # (constraints use `.` for the current value), so it must be ignored
        # rather than mis-applied. (#28.7)
        assert numeric_bounds("hh_size >= 5") == (None, None)

    def test_foreign_clause_does_not_invert_current_bound(self):
        # `. >= 30 and ${x} <= 10`: the foreign `${x} <= 10` must NOT become an
        # upper bound of 10 on the current field (which produced an inverted
        # (30, 10) -> swapped (10, 30) range and sampled a value violating the
        # real `. >= 30`). Only the current-value clause counts. (#28.7)
        assert numeric_bounds(". >= 30 and ${x} <= 10") == (30, None)

    def test_decimal_bounds_preserved(self):
        assert numeric_bounds(". >= 1.5 and . <= 9.5") == (1.5, 9.5)

    def test_tightest_bound_wins_when_repeated(self):
        # max of lowers, min of uppers
        assert numeric_bounds(". >= 2 and . >= 5 and . <= 90 and . <= 80") == (5, 80)

    def test_top_level_or_bails(self):
        # ANDing partial bounds across an OR would over-clamp -> bail to (None, None).
        assert numeric_bounds(". <= 0 or . >= 18") == (None, None)

    def test_conditional_if_bails(self):
        assert numeric_bounds("if(index()=1, .>=18, .>=0 and .<=120)") == (None, None)

    def test_empty_and_none(self):
        assert numeric_bounds(None) == (None, None)
        assert numeric_bounds("") == (None, None)

    def test_no_extractable_bound(self):
        assert numeric_bounds("selected(${x}, '1')") == (None, None)


class TestTextMaxLength:
    def test_leq_cap(self):
        assert text_max_length("string-length(.) <= 50") == 50

    def test_equality_treated_as_cap(self):
        assert text_max_length("string-length(.) = 10") == 10

    def test_does_not_match_equals_inside_geq(self):
        # Regression for commit 2a6c591: the `=` inside `>= 1` must NOT be read
        # as an equality length cap, or a text field constrained `. >= 1` would
        # be capped at one character.
        assert text_max_length(". >= 1") is None

    def test_does_not_match_equals_inside_neq(self):
        assert text_max_length("string-length(.) != 5") is None

    def test_empty_and_none(self):
        assert text_max_length(None) is None
        assert text_max_length("") is None


class TestSelectMultipleCountBounds:
    def test_equality_pins_both(self):
        assert select_multiple_count_bounds("count-selected(.) = 3") == (3, 3)

    def test_geq_lower(self):
        assert select_multiple_count_bounds("count-selected(.) >= 2") == (2, None)

    def test_leq_upper(self):
        assert select_multiple_count_bounds("count-selected(.) <= 5") == (None, 5)

    def test_strict_greater_shifts(self):
        assert select_multiple_count_bounds("count-selected(.) > 1") == (2, None)

    def test_strict_less_shifts(self):
        assert select_multiple_count_bounds("count-selected(.) < 4") == (None, 3)

    def test_conditional_if_bails(self):
        assert select_multiple_count_bounds(
            "if(selected(., -99), count-selected(.) = 1, count-selected(.) <= 3)"
        ) == (None, None)

    def test_empty_and_none(self):
        assert select_multiple_count_bounds(None) == (None, None)
        assert select_multiple_count_bounds("") == (None, None)


class TestHasTopLevelOr:
    def test_detects_top_level_or(self):
        assert _has_top_level_or(". <= 0 or . >= 18") is True

    def test_ignores_or_inside_parens(self):
        # The `or` is at paren depth 1, so it is not a top-level disjunction.
        assert _has_top_level_or("(. = 1 or . = 2) and . <= 9") is False
        assert _has_top_level_or("not(. = 1 or . = 2)") is False

    def test_word_boundary_not_fooled_by_word_containing_or(self):
        # "for"/"corn" etc. contain the letters "or" but are not the operator.
        assert _has_top_level_or(". = 1 and color = 2") is False

    def test_known_limitation_quotes_not_tracked(self):
        # DOCUMENTS a known gap (issue #11): the paren-walk does not track string
        # literals, so an `or` inside a quoted literal is still seen as a
        # top-level operator. If quote-tracking is added later, flip this assert.
        assert _has_top_level_or("selected(., 'a') or x = 'left or right'") is True
