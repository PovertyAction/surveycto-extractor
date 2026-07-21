"""Regression tests for nested-repeat handling in the synthetic generator.

Before this fix, `_walk_one` expanded each in-repeat field by only its
*innermost* repeat (`_find_innermost_repeat`), so a field in an inner repeat
nested under an outer repeat was flattened by the inner count alone and the
outer multiplicity was dropped silently. For outer=2 / inner=3 the simulator
emitted `inner_count, mem_1, mem_2, mem_3` (7 content columns) instead of the
true SurveyCTO wide-export shape `inner_count_1, inner_count_2, mem_1_1 ..
mem_2_3` (11 content columns). `--simulate` reported an undercounted, mis-shaped
dataset contract for the household -> members -> jobs/visits shape — exactly
where reviewers rely on it.

The suffix model mirrors `create_variable_dictionaries._build_repeat_tree`: a
field's suffix is the chain of iteration indices of all its enclosing repeats,
and a repeat's count variable lives at its parent level (carrying the ancestor
suffix only).
"""

import csv
import json

from surveycto_extractor.generators.synthetic_data import generate_synthetic_csv

_META = {
    "CompletionDate",
    "SubmissionDate",
    "starttime",
    "endtime",
    "deviceid",
    "subscriberid",
    "simid",
    "devicephonenum",
    "username",
    "duration",
    "caseid",
    "KEY",
    "formdef_version",
}


def _run(tmp_path, form, *, n_rows=1, seed=1, **kwargs):
    qp = tmp_path / "q.json"
    qp.write_text(json.dumps(form), encoding="utf-8")
    out = tmp_path / "synth.csv"
    generate_synthetic_csv(
        qp,
        out,
        pulldata_search_dirs=[],
        survey_name="t",
        n_rows=n_rows,
        seed=seed,
        **kwargs,
    )
    with out.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)
    content = [c for c in header if c not in _META]
    return content, rows


def test_nested_two_level_column_contract(tmp_path):
    """outer=2, inner=3 yields the rectangular 2x3 grid plus per-outer counts."""
    form = [
        {
            "type": "repeat_count",
            "variable_name": "outer_count",
            "repeat_group_name": "outer",
            "calculation": "2",
            "group_path": [],
        },
        {"type": "text", "variable_name": "omem", "group_path": ["outer"]},
        {
            "type": "repeat_count",
            "variable_name": "inner_count",
            "repeat_group_name": "inner",
            "calculation": "3",
            "group_path": ["outer"],
        },
        {"type": "text", "variable_name": "mem", "group_path": ["outer", "inner"]},
    ]
    content, _ = _run(tmp_path, form)
    assert content == [
        "outer_count",
        "omem_1",
        "omem_2",
        "inner_count_1",
        "inner_count_2",
        "mem_1_1",
        "mem_1_2",
        "mem_1_3",
        "mem_2_1",
        "mem_2_2",
        "mem_2_3",
    ]


def test_repeat_count_zero_emits_no_member_columns(tmp_path):
    """A repeat whose count is 0 produces 0 iterations, not a clamped-up 1
    (#28.6). The count cell reads 0 and no member columns are emitted.
    """
    form = [
        {
            "type": "repeat_count",
            "variable_name": "rc",
            "repeat_group_name": "rpt",
            "calculation": "0",
            "group_path": [],
        },
        {"type": "text", "variable_name": "m", "group_path": ["rpt"]},
    ]
    content, rows = _run(tmp_path, form, n_rows=1)
    assert content == ["rc"]  # count only; no m_* columns
    assert rows[0]["rc"] == "0"


def test_three_level_nesting_grid(tmp_path):
    """2x2x2 leaf gets the full 8-cell grid; counts carry the ancestor suffix."""
    form = [
        {
            "type": "repeat_count",
            "variable_name": "a_count",
            "repeat_group_name": "a",
            "calculation": "2",
            "group_path": [],
        },
        {
            "type": "repeat_count",
            "variable_name": "b_count",
            "repeat_group_name": "b",
            "calculation": "2",
            "group_path": ["a"],
        },
        {
            "type": "repeat_count",
            "variable_name": "c_count",
            "repeat_group_name": "c",
            "calculation": "2",
            "group_path": ["a", "b"],
        },
        {"type": "text", "variable_name": "leaf", "group_path": ["a", "b", "c"]},
    ]
    content, _ = _run(tmp_path, form)
    leaf = [c for c in content if c.startswith("leaf")]
    assert leaf == [
        "leaf_1_1_1",
        "leaf_1_1_2",
        "leaf_1_2_1",
        "leaf_1_2_2",
        "leaf_2_1_1",
        "leaf_2_1_2",
        "leaf_2_2_1",
        "leaf_2_2_2",
    ]
    # c_count is nested two repeats deep -> 2x2 = 4 ancestor-suffixed columns.
    assert [c for c in content if c.startswith("c_count")] == [
        "c_count_1_1",
        "c_count_1_2",
        "c_count_2_1",
        "c_count_2_2",
    ]
    # b_count is nested one repeat deep -> 2 columns; a_count is top-level.
    assert [c for c in content if c.startswith("b_count")] == ["b_count_1", "b_count_2"]
    assert "a_count" in content


def test_ragged_inner_count_per_outer_iteration(tmp_path):
    """Each outer member sets its own inner count; cells fill ragged, header pads."""
    form = [
        {
            "type": "repeat_count",
            "variable_name": "hh_count",
            "repeat_group_name": "hh",
            "calculation": "3",
            "group_path": [],
        },
        {
            "type": "integer",
            "variable_name": "njobs",
            "group_path": ["hh"],
            "constraint": ".>=1 and .<=4",
        },
        {
            "type": "repeat_count",
            "variable_name": "job_count",
            "repeat_group_name": "job",
            "calculation": "${njobs}",
            "group_path": ["hh"],
        },
        {"type": "text", "variable_name": "jobname", "group_path": ["hh", "job"]},
    ]
    content, rows = _run(tmp_path, form, seed=11)
    row = rows[0]
    # Inner count per member resolves to that member's njobs (nested ${var}
    # reference resolves through the repeat stack to the outer-suffixed key).
    for o in (1, 2, 3):
        assert row[f"job_count_{o}"] == row[f"njobs_{o}"]
    # Filled jobname cells == sum of per-member counts; the rest are blank pads.
    filled = [
        c for c in content if c.startswith("jobname_") and row[c] not in ("", None)
    ]
    assert len(filled) == sum(int(row[f"njobs_{o}"]) for o in (1, 2, 3))


def test_select_multiple_inside_nested_repeat(tmp_path):
    """sm-in-repeat keeps choice-then-iteration-chain indicator columns, no parent."""
    form = [
        {
            "type": "repeat_count",
            "variable_name": "outer_count",
            "repeat_group_name": "outer",
            "calculation": "2",
            "group_path": [],
        },
        {
            "type": "repeat_count",
            "variable_name": "inner_count",
            "repeat_group_name": "inner",
            "calculation": "2",
            "group_path": ["outer"],
        },
        {
            "type": "select_multiple",
            "variable_name": "tags",
            "group_path": ["outer", "inner"],
            "choices": [{"value": "1", "label": "a"}, {"value": "2", "label": "b"}],
        },
    ]
    content, _ = _run(tmp_path, form)
    tag_cols = [c for c in content if c.startswith("tags")]
    # choice value, then the full outer/inner iteration chain; no bare parent cell.
    assert tag_cols == [
        "tags_1_1_1",
        "tags_2_1_1",
        "tags_1_1_2",
        "tags_2_1_2",
        "tags_1_2_1",
        "tags_2_2_1",
        "tags_1_2_2",
        "tags_2_2_2",
    ]
    assert "tags_1_1" not in content  # no parent space-separated cell in a repeat


def test_single_level_repeat_shape_unchanged(tmp_path):
    """A non-nested repeat keeps the original var_i shape (no regression)."""
    form = [
        {
            "type": "repeat_count",
            "variable_name": "r_count",
            "repeat_group_name": "r",
            "calculation": "3",
            "group_path": [],
        },
        {"type": "text", "variable_name": "v", "group_path": ["r"]},
    ]
    content, _ = _run(tmp_path, form)
    assert content == ["r_count", "v_1", "v_2", "v_3"]
