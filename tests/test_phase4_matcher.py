"""Tests for the Phase-4 suffix matcher (determine_variable_source).

The matcher decodes wide-export column names (var, var_i, var_a_b, var__n)
back to a form question + repeat/select_multiple metadata. It used to
fabricate that metadata: it stamped select_multiple + choice_code on any
double-suffix column regardless of type, and repeat_iteration on any
single-suffix column regardless of whether the field was in a repeat. (#26)
"""
from create_variable_dictionaries import (
    determine_variable_source,
    _build_question_index,
)

# outer repeat "orpt" containing inner repeat "irpt"; a plain int inside the
# inner repeat; a top-level int; a select_multiple (with a negative code) in a
# repeat; a valid single-level repeat field.
QUESTIONS = [
    {"variable_name": "orpt_count", "type": "repeat_count",
     "repeat_group_name": "orpt", "group_path": []},
    {"variable_name": "irpt_count", "type": "repeat_count",
     "repeat_group_name": "irpt", "group_path": ["orpt"]},
    {"variable_name": "plot_area", "type": "integer",
     "group_path": ["orpt", "irpt"]},
    {"variable_name": "income", "type": "integer", "group_path": []},
    {"variable_name": "colors", "type": "select_multiple", "group_path": ["orpt"],
     "choices": [{"value": "1"}, {"value": "2"}, {"value": "-99"}]},
]
IDX = _build_question_index(QUESTIONS)


def _src(name):
    return determine_variable_source(name, QUESTIONS, _index=IDX)


def test_nested_plain_field_not_fabricated_as_select_multiple():
    # plot_area_2_3: nested int -> repeat only (iteration = last suffix),
    # never select_multiple choice 2. (#26.1)
    r = _src("plot_area_2_3")
    assert r["is_repeat"] is True
    assert r["repeat_iteration"] == 3
    assert r["is_select_multiple"] is False
    assert r["choice_code"] is None


def test_top_level_field_gets_no_fabricated_iteration():
    # income2: top-level int with a post-processing digit suffix -> maps to
    # `income` with NO fabricated repeat_iteration. (#26.2)
    r = _src("income2")
    assert r["original_variable_name"] == "income"
    assert r["is_repeat"] is False
    assert r["repeat_iteration"] is None


def test_negative_code_select_multiple_decoded():
    # colors__99: negative-code SM indicator -> choice_code "-99", not a
    # silently-demoted plain field. (#26.3)
    r = _src("colors__99")
    assert r["is_select_multiple"] is True
    assert r["choice_code"] == "-99"


def test_valid_positive_code_select_multiple():
    r = _src("colors_2")
    assert r["is_select_multiple"] is True
    assert r["choice_code"] == "2"


def test_single_level_repeat_field():
    # A field directly in a repeat gets repeat_iteration from its single suffix.
    r = _src("colors_1")   # choice 1 indicator (SM wins over repeat here)
    assert r["is_select_multiple"] is True
    assert r["choice_code"] == "1"


def test_unmatched_column_returns_empty_source():
    r = _src("totally_unknown_var")
    assert r["original_variable_name"] is None
