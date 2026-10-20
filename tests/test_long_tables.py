"""Long repeat tables (``<stem>_long/<repeat>.parquet``): through vardict on the
synthetic project (members, each with jobs), and placement edge cases by hand.
"""

import math

import pandas as pd
import pyarrow.parquet as pq
import pytest
from test_pii import _run

from surveycto_extractor.long_tables import (
    index_columns,
    placement,
    repeat_parents,
    write_long_tables,
)


def _rows(path):
    return [
        {k: None if isinstance(v, float) and math.isnan(v) else v for k, v in r.items()}
        for r in pq.read_table(path).to_pylist()
    ]


def test_vardict_writes_one_row_per_instance_with_a_value(
    tmp_path, monkeypatch, analyzer, capsys
):
    _run(tmp_path, monkeypatch, analyzer)
    long = tmp_path / "data" / "synth_long"
    # ccc reached no member, bbb one: instances holding only "" and NaN are dropped.
    assert _rows(long / "member.parquet") == [
        {
            "key": "uuid:aaa",
            "member_index": 1,
            "member_name": "Sofia Lopez",
            "member_age": 40.0,
            "member_tasks": "1 0",
            "member_tasks_1": 1.0,
            "member_tasks_0": 1.0,
        },
        {
            "key": "uuid:aaa",
            "member_index": 2,
            "member_name": "James Wilson",
            "member_age": 12.0,
            "member_tasks": "0",
            "member_tasks_1": 0.0,
            "member_tasks_0": 1.0,
        },
        {
            "key": "uuid:bbb",
            "member_index": 1,
            "member_name": "Daniel Clark",
            "member_age": 22.0,
            "member_tasks": "1",
            "member_tasks_1": 1.0,
            "member_tasks_0": 0.0,
        },
    ]
    assert _rows(long / "job.parquet") == [
        {"key": "uuid:aaa", "member_index": 1, "job_index": 1, "job_wage": 100.0},
        {"key": "uuid:aaa", "member_index": 1, "job_index": 2, "job_wage": 150.0},
        {"key": "uuid:aaa", "member_index": 2, "job_index": 1, "job_wage": 50.0},
        {"key": "uuid:bbb", "member_index": 1, "job_index": 1, "job_wage": 200.0},
    ]
    out = capsys.readouterr().out
    assert "Long tables in synth_long: job (4 rows), member (3 rows)" in out
    assert "stay wide" not in out


def _q(name, group_path, qtype="integer", **kw):
    return {"variable_name": name, "type": qtype, "group_path": group_path, **kw}


QUESTIONS = [
    _q("hh_count", [], "repeat_count", repeat_group_name="hh"),
    _q("plot_count", ["grp", "hh"], "repeat_count", repeat_group_name="plot"),
    _q("size", ["grp", "hh"]),
    _q("crop", ["grp", "hh", "plot"]),
    _q("uses", ["grp", "hh"], "select_multiple"),
    _q("village", ["grp"]),
]


def _entry(question, choice=None, qtype="integer"):
    survey = {"original_variable_name": question, "type": qtype}
    if choice is not None:
        survey["choices"] = {"value": choice}
    return {"survey": survey}


def test_repeat_chain_from_the_count_questions():
    parents = repeat_parents(QUESTIONS)
    assert parents == {"hh": None, "plot": "hh"}
    assert index_columns("plot", parents) == ["hh_index", "plot_index"]


def test_placement_reads_the_instance_numbers_from_the_name():
    variables = {
        "size1": _entry("size"),  # SurveyCTO's suffix with no underscore
        "size_12": _entry("size"),
        "crop_2_3": _entry("crop"),
        "uses__99_4": _entry("uses", "-99", "select_multiple"),
        "uses_1_4": _entry("uses", "1", "select_multiple"),
        "village": _entry("village"),
    }
    placed, wide = placement(QUESTIONS, variables)
    assert placed == {
        "size1": ("hh", "size", [1]),
        "size_12": ("hh", "size", [12]),
        "crop_2_3": ("plot", "crop", [2, 3]),
        "uses__99_4": ("hh", "uses__99", [4]),
        "uses_1_4": ("hh", "uses_1", [4]),
    }
    assert wide == []


def test_one_unread_column_keeps_its_question_wide():
    # crop_2 lacks the plot number: a long crop column would miss that instance.
    variables = {
        "crop_1_1": _entry("crop"),
        "crop_2": _entry("crop"),
        "size_1": _entry("size"),
    }
    placed, wide = placement(QUESTIONS, variables)
    assert placed == {"size_1": ("hh", "size", [1])}
    assert sorted(wide) == ["crop_1_1", "crop_2"]


def _write(tmp_path, columns, variables, questions=QUESTIONS):
    path = tmp_path / "s.parquet"
    pd.DataFrame({"key": ["k1", "k2"], **columns}).to_parquet(path)
    return write_long_tables(questions, {"variables": variables}, path)


def test_a_repeat_nobody_reached_is_its_keys(tmp_path):
    (tmp_path / "s_long").mkdir()
    (tmp_path / "s_long" / "gone.parquet").write_bytes(b"stale")
    out = _write(
        tmp_path,
        {"size_1": [None, None], "crop_1_1": [None, None]},
        {"size_1": _entry("size"), "crop_1_1": _entry("crop")},
    )
    assert sorted(p.name for p in out.iterdir()) == ["hh.parquet", "plot.parquet"]
    plot = pq.read_table(out / "plot.parquet")
    assert (plot.column_names, plot.num_rows) == (
        ["key", "hh_index", "plot_index", "crop"],
        0,
    )


def test_no_repeats_writes_no_folder(tmp_path):
    out = _write(tmp_path, {"village": [1, 2]}, {"village": _entry("village")}, [])
    assert not out.exists()


def test_two_columns_on_one_long_column_raise(tmp_path):
    with pytest.raises(ValueError, match="size_1 and size1 both land on 'size'"):
        _write(
            tmp_path,
            {"size_1": [1, 2], "size1": [1, 2]},
            {"size_1": _entry("size"), "size1": _entry("size")},
        )


def test_a_long_column_named_like_a_key_raises(tmp_path):
    questions = [*QUESTIONS, _q("hh_index", ["hh"])]
    with pytest.raises(ValueError, match="'hh_index' is a key of hh.parquet"):
        _write(
            tmp_path,
            {"hh_index_1": [1, 2]},
            {"hh_index_1": _entry("hh_index")},
            questions,
        )
