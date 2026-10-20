"""Long repeat tables (``<stem>_long/<repeat>.parquet``): through vardict on the
synthetic project (members, each with jobs), and placement edge cases by hand.
"""

import json
import math

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from test_pii import _run

from surveycto_extractor import survey
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
    cfg, _ = _run(tmp_path, monkeypatch, analyzer)
    long = tmp_path / "data" / "synth_long"
    # ccc reached no member, bbb one: instances holding only "" and NaN are dropped.
    # A nested repeat's count (job_count_1) sits in its parent's instances.
    assert _rows(long / "member.parquet") == [
        {
            "key": "uuid:aaa",
            "member_index": 1,
            "member_name": "Sofia Lopez",
            "member_age": 40.0,
            "member_tasks": "1 0",
            "member_tasks_1": 1.0,
            "member_tasks_0": 1.0,
            "job_count": 2.0,
        },
        {
            "key": "uuid:aaa",
            "member_index": 2,
            "member_name": "James Wilson",
            "member_age": 12.0,
            "member_tasks": "0",
            "member_tasks_1": 0.0,
            "member_tasks_0": 1.0,
            "job_count": 1.0,
        },
        {
            "key": "uuid:bbb",
            "member_index": 1,
            "member_name": "Daniel Clark",
            "member_age": 22.0,
            "member_tasks": "1",
            "member_tasks_1": 1.0,
            "member_tasks_0": 0.0,
            "job_count": 1.0,
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
    # SKILL.md: a long column's status is its wide columns', which this lists.
    store = survey.SurveyStore(survey.discover(cfg.SURVEYS["synth"]["output_dir"]))
    answer = store.lookup_var("member_name")
    assert "member_name_1" in answer and "member_name_2" in answer


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
        # SurveyCTO writes a character a Stata name cannot hold as "_".
        "uses_AB_12_4": _entry("uses", "AB-12", "select_multiple"),
        "village": _entry("village"),
    }
    placed, wide = placement(QUESTIONS, variables)
    assert placed == {
        "size1": ("hh", "size", [1]),
        "size_12": ("hh", "size", [12]),
        "crop_2_3": ("plot", "crop", [2, 3]),
        "uses__99_4": ("hh", "uses__99", [4]),
        "uses_1_4": ("hh", "uses_1", [4]),
        "uses_AB_12_4": ("hh", "uses_AB_12", [4]),
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
    (tmp_path / "s_long").mkdir()
    out = _write(tmp_path, {"village": [1, 2]}, {"village": _entry("village")}, [])
    assert not out.exists()


def test_instances_mixing_text_and_numbers_stay_wide(tmp_path, capsys):
    # The do-file destrings each wide column alone: size_1 numeric, size_2 text.
    out = _write(
        tmp_path,
        {"size_1": [1, 2], "size_2": ["3", "big"], "crop_1_1": [5, None]},
        {
            "size_1": _entry("size"),
            "size_2": _entry("size"),
            "crop_1_1": _entry("crop"),
        },
    )
    assert sorted(p.name for p in out.iterdir()) == ["plot.parquet"]
    printed = capsys.readouterr().out
    assert "2 repeat columns stay wide only: their instances mix text" in printed
    assert "No long table for hh: every column stays wide" in printed


def test_two_columns_on_one_long_column_raise(tmp_path):
    with pytest.raises(
        ValueError, match=r"two columns of size land on .* hh instance \[1\]"
    ):
        _write(
            tmp_path,
            {"size_1": [1, 2], "size1": [1, 2]},
            {"size_1": _entry("size"), "size1": _entry("size")},
        )


def test_a_long_column_named_like_a_key_raises(tmp_path):
    questions = [*QUESTIONS, _q("hh_index", ["hh"])]
    with pytest.raises(
        ValueError, match="hh_index: its long column is named like a key of hh.parquet"
    ):
        _write(
            tmp_path,
            {"hh_index_1": [1, 2]},
            {"hh_index_1": _entry("hh_index")},
            questions,
        )


def test_an_empty_instance_column_takes_the_others_type(tmp_path, capsys):
    # Stata types an empty column alone: size_2 is all NaN beside text in size_1,
    # crop_1_2 all "" beside numbers in crop_1_1.
    out = _write(
        tmp_path,
        {
            "size_1": ["a", "b"],
            "size_2": [float("nan")] * 2,
            "crop_1_1": [5.0, 6.0],
            "crop_1_2": ["", ""],
        },
        {
            "size_1": _entry("size"),
            "size_2": _entry("size"),
            "crop_1_1": _entry("crop"),
            "crop_1_2": _entry("crop"),
        },
    )
    assert _rows(out / "hh.parquet") == [
        {"key": "k1", "hh_index": 1, "size": "a"},
        {"key": "k2", "hh_index": 1, "size": "b"},
    ]
    assert [r["crop"] for r in _rows(out / "plot.parquet")] == [5.0, 6.0]
    assert "stay wide" not in capsys.readouterr().out


def test_a_stale_questions_json_is_refused():
    stale = [{"variable_name": "hh_count", "type": "repeat_count", "group_path": []}]
    with pytest.raises(SystemExit, match="rerun surveycto-extract"):
        repeat_parents(stale)


def test_stata_empty_strings_are_missing(tmp_path, monkeypatch, analyzer):
    # Stata stores a missing string as "": the dictionary counts it as missing, and
    # the sidecar and the long tables hold it as null, one missing code throughout.
    cfg, _ = _run(tmp_path, monkeypatch, analyzer)
    variables = json.loads(cfg.DATASETS["synth"]["output_json"].read_text("utf-8"))[
        "variables"
    ]
    entry = variables["member_name_2"]  # "James Wilson", "", ""
    assert (entry["non_null_count"], entry["missing_rate"]) == (1, 0.6667)
    data = tmp_path / "data"
    for path in [data / "synth.parquet", *(data / "synth_long").glob("*.parquet")]:
        table = pq.read_table(path)
        for name in table.column_names:
            if pa.types.is_string(table[name].type):
                assert not pc.any(pc.equal(table[name], "")).as_py(), (path.name, name)


def test_skill_project_section_and_claude_md(tmp_path, monkeypatch, analyzer):
    from surveycto_extractor.cli import skill

    cfg, _ = _run(tmp_path, monkeypatch, analyzer)
    section = skill.project_section(cfg)
    assert section.startswith("## This project: synthetic")
    assert "| `synth` | Synthetic | 3 |" in section
    # Parents come from questions.json: job sits inside member.
    assert "| `member` | submission | 2 | `member_count` | 3 |" in section
    assert "| `job` | member |" in section
    target = skill.install_skill(tmp_path / "skill", section)
    text = (target / "SKILL.md").read_text("utf-8")
    assert skill.MARKER not in text and "## This project: synthetic" in text
    assert (target / "references" / "SETUP.md").exists()
    # CLAUDE.md gets the section once; the team's text is never touched.
    (tmp_path / "CLAUDE.md").write_text("# Team notes\n", "utf-8")
    assert skill.claude_md(tmp_path) is True
    first = (tmp_path / "CLAUDE.md").read_text("utf-8")
    assert first.startswith("# Team notes\n") and "### Rulings" in first
    assert skill.claude_md(tmp_path) is False
    assert (tmp_path / "CLAUDE.md").read_text("utf-8") == first
