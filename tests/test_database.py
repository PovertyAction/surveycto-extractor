"""``<project>.duckdb`` (surveycto-duckdb) and its map, read by get_structure_db: on
the synthetic project (one survey, nested repeats) and the two-survey flow project.
"""

import json

import duckdb
import pytest
import synthetic_project
from test_flow import QUESTIONS, _project
from test_pii import _run

from surveycto_extractor import database, flow, survey


def _flow(cfg):
    out = cfg.PROJECT["output_dir"]
    out.mkdir(exist_ok=True)
    (out / "flow.json").write_text(json.dumps(flow.build_flow(cfg)), "utf-8")
    return out / "flow.json"


@pytest.fixture
def synthetic(tmp_path, monkeypatch, analyzer):
    cfg, _ = _run(tmp_path, monkeypatch, analyzer)
    flow_path = _flow(cfg)
    return cfg, database.write_database(cfg), flow_path


def test_views_read_the_wide_and_long_parquets(synthetic):
    cfg, path, _ = synthetic
    con = duckdb.connect(str(path), read_only=True)
    columns = con.sql("SELECT * FROM synth.submissions LIMIT 0").columns
    # A column in a long table is read there, not in submissions.
    assert "hhid" in columns and "member_name_1" not in columns
    assert con.sql("SELECT count(*) FROM synth.submissions").fetchone() == (3,)
    assert con.sql("SELECT count(*) FROM synth.member").fetchone() == (3,)
    assert con.sql(
        "SELECT sum(job_wage) FROM synth.job JOIN synth.member "
        "USING (key, member_index) WHERE member_age > 30"
    ).fetchone() == (250.0,)
    assert con.sql(
        "SELECT view_name, view_column, pii_status FROM synth.dictionary "
        "WHERE column_name = 'member_name_1'"
    ).fetchone()[:2] == ("member", "member_name")
    assert con.sql(
        "SELECT count(*) FROM synth.questions WHERE question = 'job_wage' "
        "AND repeat_group = 'job'"
    ).fetchone() == (1,)
    con.close()


def test_the_map_names_every_object_and_hides_withheld_codes(synthetic):
    cfg, path, _ = synthetic
    objects = json.loads(path.with_name("synthetic_db.json").read_text("utf-8"))
    names = [o["name"] for o in objects]
    assert names[:2] == ["synth.submissions", "synth.job"]
    assert {"surveys", "datasets", "reads", "joins", "synth.dictionary"} <= set(names)
    member = next(o for o in objects if o["name"] == "synth.member")
    assert member["key"] == ["key", "member_index"]
    assert member["joins"] == [
        '"synth"."member"."key" = "synth"."submissions"."key"  (many to one)'
    ]
    submissions = next(o for o in objects if o["name"] == "synth.submissions")
    shown = {c["name"]: c.get("shown_as") for c in submissions["columns"]}
    assert shown["staff_multi_edavis"] == "staff_multi_<1>"
    # Neither holds a response value. The JSON keeps the real column names beside
    # shown_as, as the dictionary does; the rendering shows the aliases only.
    json_text, sql = (
        path.with_name(f"synthetic_db.{ext}").read_text("utf-8")
        for ext in ("json", "sql")
    )
    assert [c for c in synthetic_project.CANARIES if c in json_text + sql] == []
    assert [c for c in synthetic_project.CODE_CANARIES if c in sql] == []
    assert "staff_multi_<1>" in sql


def test_get_structure_db(synthetic):
    cfg, _, flow_path = synthetic
    store = survey.SurveyStore(cfg.DATASETS, flow_path)
    summary = store.get_structure_db()
    assert summary.startswith("-- synthetic.duckdb:")
    assert "-- schema synth: submissions (" in summary
    assert "repeat views: 2, nested up to 2 deep" in summary
    schema = store.get_structure_db("synth")
    assert "-- view synth.member: one row per instance" in schema
    table = store.get_structure_db("synth", "member")
    assert "member_name VARCHAR" in table
    assert store.get_structure_db("nope").startswith("No schema 'nope'")
    assert store.get_structure_db("synth", "nope").startswith("No table")
    for answer in (
        summary,
        schema,
        table,
        store.get_structure_db("synth", "submissions"),
    ):
        assert "edavis" not in answer and "mjones" not in answer


def test_without_the_map_it_says_what_to_run(tmp_path):
    cfg = _project(tmp_path)
    store = survey.SurveyStore(cfg.DATASETS, tmp_path / "docs" / "flow.json")
    assert "run surveycto-flow, then surveycto-duckdb" in store.get_structure_db()
    store = survey.SurveyStore(cfg.DATASETS, _flow(cfg))
    assert store.get_structure_db().startswith("No study_db.json")


def _graphs(cfg):
    for key in cfg.DATASETS:
        folder = cfg.DATASETS[key]["output_json"].parent
        (folder / f"{key}_variable_graph.json").write_text(
            json.dumps({"nodes": [], "edges": []}), "utf-8"
        )


def test_project_tables_from_flow(tmp_path):
    cfg = _project(tmp_path)
    _graphs(cfg)
    _flow(cfg)
    path = database.write_database(cfg)
    con = duckdb.connect(str(path), read_only=True)
    assert con.sql("SELECT * FROM joins").fetchall() == [
        (
            "endline",
            "submissions",
            "hh_name",
            "hh_roster",
            "name",
            "baseline",
            "submissions",
            "name",
        )
    ]
    assert con.sql(
        "SELECT published_by, key FROM datasets WHERE dataset = 'hh_roster'"
    ).fetchone() == ("baseline", ["hhid"])
    # The join the map gives runs as written.
    objects = json.loads(path.with_name("study_db.json").read_text("utf-8"))
    (line,) = next(o for o in objects if o["name"] == "joins")["joins"]
    condition = line.split("  (")[0]
    assert con.sql(
        'SELECT count(*) FROM "endline"."submissions" JOIN "baseline"."submissions" '
        f"ON {condition}"
    ).fetchone() == (2,)
    con.close()


def test_columns_that_differ_only_in_case_are_refused(tmp_path):
    rows = {
        "baseline": {"key": ["k1"], "hhid": ["H1"], "name": ["A"], "Name": ["B"]},
        "endline": {"key": ["e1"], "hhid": ["H1"], "hh_name": ["A"]},
    }
    cfg = _project(tmp_path, rows=rows)
    _graphs(cfg)
    _flow(cfg)
    with pytest.raises(ValueError, match="DuckDB ignores case"):
        database.write_database(cfg)


def test_a_repeat_named_like_a_table_is_refused(tmp_path):
    cfg = _project(tmp_path)
    _graphs(cfg)
    path = cfg.DATASETS["baseline"]["questions_json"]
    questions = [
        q if q["type"] != "repeat_count" else {**q, "repeat_group_name": "edges"}
        for q in QUESTIONS["baseline"]
    ]
    questions = [
        {**q, "group_path": ["edges"]} if q["variable_name"] == "member_name" else q
        for q in questions
    ]
    path.write_text(json.dumps(questions), "utf-8")
    _flow(cfg)
    with pytest.raises(ValueError, match="has the name of the baseline.edges table"):
        database.write_database(cfg)


def test_it_needs_flow_json_and_a_named_project(tmp_path):
    cfg = _project(tmp_path)
    with pytest.raises(SystemExit, match="run surveycto-flow"):
        database.write_database(cfg)
    cfg.PROJECT["name"] = None
    with pytest.raises(SystemExit, match="needs name and output_dir"):
        database.write_database(cfg)


def test_aliases_keep_every_instance_number():
    # Nested binaries of one code differ only in the outer instance.
    entry = {
        "survey": {
            "original_variable_name": "x",
            "type": "select_multiple",
            "choices": {"value": "e-1"},
            "choices_withheld": ["presidio"],
        },
        "repeat_iteration": 3,
    }
    aliases = survey.coded_aliases({"x_e_1_1_3": entry, "x_e_1_2_3": entry}, [])
    assert aliases == {"x_e_1_1_3": "x_<1>_1_3", "x_e_1_2_3": "x_<1>_2_3"}
