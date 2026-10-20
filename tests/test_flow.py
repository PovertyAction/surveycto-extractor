"""Multi-form links: candidate tables (surveycto-links), flow.json (surveycto-flow),
get_project and lineage across forms, on an invented two-survey project: baseline
publishes hh_roster, endline reads it.
"""

import json
import types

import pandas as pd
import pytest

from surveycto_extractor import flow, survey
from surveycto_extractor.config_loader import _load_toml


def _q(name, qtype="text", calculation=None, data_source=None, group_path=(), **kw):
    return {
        "variable_name": name,
        "type": qtype,
        "question_text": name.replace("_", " "),
        "calculation": calculation,
        "data_source": data_source,
        "group_path": list(group_path),
        "gates": [],
        "relevance": None,
        "choice_list": None,
        "choices": None,
        **kw,
    }


PULL = {"kind": "pulldata", "dataset": "hh_roster", "value": "'name'", "key": "hhid"}
QUESTIONS = {
    "baseline": [
        _q("hhid"),
        _q("name"),
        _q("members_count", "repeat_count", repeat_group_name="members"),
        _q("member_name", group_path=["members"]),
    ],
    "endline": [
        _q("hhid"),
        _q(
            "hh_name",
            "calculate",
            calculation="pulldata('hh_roster', 'name', 'hhid', ${hhid})",
            data_source={**PULL, "match": "${hhid}"},
        ),
        _q(
            "pick",
            "select_one",
            choice_list="hh",
            data_source={
                "kind": "search",
                "dataset": "hh_roster",
                "mode": "matches",
                "filter": {"hhid": None},
                "match": {"hhid": "${hhid}"},
            },
        ),
        _q(
            "prev",
            "calculate",
            calculation="pulldata('attach', 'score', 'id', ${hhid})",
        ),
    ],
}
ROWS = {
    "baseline": {
        "key": ["k1", "k2", "k3"],
        "hhid": ["H1", "H2", "H3"],
        "name": ["A", "B", "C"],
    },
    "endline": {"key": ["e1", "e2"], "hhid": ["H1", "H2"], "hh_name": ["A", "B"]},
}


def _project(tmp_path, server_datasets=None, entities=None, rows=None):
    datasets = {}
    for key, questions in QUESTIONS.items():
        folder = tmp_path / key
        folder.mkdir(parents=True)
        (folder / f"{key}_questions.json").write_text(json.dumps(questions), "utf-8")
        variables = {
            q["variable_name"]: {
                "non_null_count": 1,
                "stata": {"type": "str3"},
                "survey": {"original_variable_name": q["variable_name"]},
                "pii": {"status": "whitelisted", "basis": ["reviewed"]},
            }
            for q in questions
        }
        (folder / f"{key}_variable_dictionary.json").write_text(
            json.dumps({"summary": {"pii": {}}, "variables": variables}), "utf-8"
        )
        pd.DataFrame((rows or ROWS)[key]).to_parquet(folder / f"{key}.parquet")
        datasets[key] = {
            "data": folder / f"{key}.dta",
            "questions_json": folder / f"{key}_questions.json",
            "output_json": folder / f"{key}_variable_dictionary.json",
        }
    return types.SimpleNamespace(
        DATASETS=datasets,
        SERVER_DATASETS=server_datasets
        if server_datasets is not None
        else {"hh_roster": {"published_by": "baseline", "key": ["hhid"]}},
        ENTITIES=entities if entities is not None else {},
        PROJECT={"name": "study", "output_dir": tmp_path / "docs", "description": None},
    )


def test_links_suggest_the_publisher_key_and_entities(tmp_path):
    cfg = _project(tmp_path, server_datasets={})
    text = flow.suggest_links(cfg)
    assert '[server_datasets.hh_roster]\npublished_by = "baseline"' in text
    assert 'key = ["hhid"]' in text
    assert "# attach: read by endline (pulldata of score)" in text
    assert "No survey here carries its columns" in text
    assert '[entities.hhid]\nkey = "hhid"' in text
    assert "#   hhid: baseline, endline" in text


def test_flow_records_datasets_joins_and_keys(tmp_path):
    out = flow.build_flow(_project(tmp_path))
    by_id = {d["id"]: d for d in out["datasets"]}
    assert by_id["hh_roster"] == {
        "id": "hh_roster",
        "kind": "server",
        "published_by": "baseline",
        "key": ["hhid"],
        "read_by": ["endline"],
        "repeated_keys": 0,
        "missing_keys": 0,
    }
    assert by_id["attach"]["kind"] == "attachment"
    assert out["joins"] == [
        {
            "reader": "endline",
            "field": "hh_name",
            "dataset": "hh_roster",
            "column": "name",
            "publisher": "baseline",
        }
    ]
    assert out["surveys"] == [
        {"key": "baseline", "label": "baseline"},
        {"key": "endline", "label": "endline"},
    ]
    hhid = next(k for k in out["keys"] if k["column"] == "hhid")
    assert hhid == {
        "column": "hhid",
        "key_of": ["hh_roster"],
        "matched_by": 1,
        "surveys": ["baseline", "endline"],
    }


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ({"published_by": "midline", "key": ["hhid"]}, "is not a \\[datasets\\] key"),
        ({"published_by": "baseline", "key": ["village"]}, "is not a field"),
    ],
)
def test_flow_refuses_a_key_or_publisher_the_forms_contradict(
    tmp_path, declared, message
):
    cfg = _project(tmp_path, {"hh_roster": declared})
    with pytest.raises(ValueError, match=message):
        flow.build_flow(cfg)


def test_repeated_and_blank_keys_are_counted_not_refused(tmp_path, capsys):
    # The server dataset keeps the latest row per key; Stata stores a missing
    # string as "".
    rows = {
        "baseline": {
            "key": ["k1", "k2", "k3", "k4"],
            "hhid": ["H1", "H1", "", "H3"],
            "name": ["A", "A2", "B", "C"],
        },
        "endline": ROWS["endline"],
    }
    cfg = _project(tmp_path, rows=rows)
    roster = next(d for d in flow.build_flow(cfg)["datasets"] if d["id"] == "hh_roster")
    assert (roster["repeated_keys"], roster["missing_keys"]) == (1, 1)


def _members_table(tmp_path, names):
    long = tmp_path / "baseline" / "baseline_long"
    long.mkdir()
    pd.DataFrame(
        {
            "key": ["k1", "k1", "k2", "k3"],
            "members_index": [1, 2, 1, 1],
            "member_name": names,
        }
    ).to_parquet(long / "members.parquet")


def _roster(cfg):
    return next(d for d in flow.build_flow(cfg)["datasets"] if d["id"] == "hh_roster")


def test_a_key_in_a_repeat_is_counted_on_its_long_table(tmp_path):
    # A repeat publishes one dataset row per instance: vardict's long table has them.
    declared = {"published_by": "baseline", "key": ["member_name"]}
    cfg = _project(tmp_path, {"hh_roster": declared})
    with pytest.raises(SystemExit, match="members.parquet is missing"):
        flow.build_flow(cfg)
    _members_table(tmp_path, ["Ana", "", "Ana", "Luis"])
    assert (_roster(cfg)["repeated_keys"], _roster(cfg)["missing_keys"]) == (1, 1)


def test_a_repeat_key_with_an_enclosing_field(tmp_path):
    # hhid from the submission, joined on KEY: Ana in k1 and Ana in k2 differ.
    rows = {
        "baseline": {
            "key": ["k1", "k2", "k3"],
            "hhid": ["H1", "H2", "H3"],
            "name": ["A", "B", "C"],
        },
        "endline": ROWS["endline"],
    }
    declared = {"published_by": "baseline", "key": ["hhid", "member_name"]}
    cfg = _project(tmp_path, {"hh_roster": declared}, rows=rows)
    _members_table(tmp_path, ["Ana", "Ana", "Ana", "Luis"])
    assert (_roster(cfg)["repeated_keys"], _roster(cfg)["missing_keys"]) == (1, 0)


def test_key_fields_in_sibling_repeats_are_refused(tmp_path):
    declared = {"published_by": "baseline", "key": ["member_name", "asset_id"]}
    cfg = _project(tmp_path, {"hh_roster": declared})
    path = cfg.DATASETS["baseline"]["questions_json"]
    questions = json.loads(path.read_text("utf-8"))
    questions += [
        _q("assets_count", "repeat_count", repeat_group_name="assets"),
        _q("asset_id", group_path=["assets"]),
    ]
    path.write_text(json.dumps(questions), "utf-8")
    with pytest.raises(ValueError, match="do not enclose one another"):
        flow.build_flow(cfg)


def test_a_key_column_found_through_the_dictionary(tmp_path):
    # Stata kept the field under another name; the dictionary maps it.
    rows = {
        "baseline": {"key": ["k1", "k2"], "HHID": ["H1", "H2"], "name": ["A", "B"]},
        "endline": ROWS["endline"],
    }
    cfg = _project(tmp_path, rows=rows)
    path = cfg.DATASETS["baseline"]["output_json"]
    vardict = json.loads(path.read_text("utf-8"))
    vardict["variables"]["HHID"] = vardict["variables"].pop("hhid")
    path.write_text(json.dumps(vardict), "utf-8")
    flow.build_flow(cfg)


def test_flow_refuses_a_pulled_column_the_publisher_lacks(tmp_path):
    cfg = _project(tmp_path)
    cfg.SERVER_DATASETS["attach"] = {"published_by": "baseline", "key": ["hhid"]}
    with pytest.raises(ValueError, match="baseline has no field 'score'"):
        flow.build_flow(cfg)


def test_flow_refuses_an_unread_dataset_and_bad_entities(tmp_path):
    cfg = _project(tmp_path)
    cfg.SERVER_DATASETS["ghost"] = {"published_by": "baseline", "key": ["hhid"]}
    with pytest.raises(ValueError, match="no survey reads this dataset"):
        flow.build_flow(cfg)
    entity = {"key": ["village"], "description": "", "parent": None}
    with pytest.raises(ValueError, match="no survey has a field 'village'"):
        flow.build_flow(_project(tmp_path / "b", entities={"v": entity}))
    entity = {"key": ["hhid"], "description": "", "parent": "village"}
    with pytest.raises(ValueError, match="parent 'village' is not an entity"):
        flow.build_flow(_project(tmp_path / "c", entities={"hh": entity}))
    loop = {
        "a": {"key": ["hhid"], "description": "", "parent": "b"},
        "b": {"key": ["hhid"], "description": "", "parent": "a"},
    }
    with pytest.raises(ValueError, match="parents loop"):
        flow.build_flow(_project(tmp_path / "d", entities=loop))


def test_pulldata_in_relevance_counts_and_a_keyless_one_raises(tmp_path):
    cfg = _project(tmp_path)
    path = cfg.DATASETS["endline"]["questions_json"]
    questions = json.loads(path.read_text("utf-8"))
    questions.append(
        _q("gate", relevance="pulldata('panel', 'ok', 'hhid', ${hhid}) = 1")
    )
    path.write_text(json.dumps(questions), "utf-8")
    assert any(r["dataset"] == "panel" for r in flow.reads_of(flow._questions(cfg)))
    questions.append(_q("bad", "calculate", calculation="pulldata('panel', 'ok')"))
    path.write_text(json.dumps(questions), "utf-8")
    with pytest.raises(ValueError, match="needs a dataset, a column and a key"):
        flow.reads_of(flow._questions(cfg))


def test_pasted_suggestions_pass_surveycto_flow(tmp_path):
    cfg = _project(tmp_path, server_datasets={})
    text = flow.suggest_links(cfg)
    config = tmp_path / "config.toml"
    config.write_text(text.replace('output_dir = ""', 'output_dir = "docs"'), "utf-8")
    parsed = _load_toml(config)
    cfg.SERVER_DATASETS, cfg.ENTITIES = parsed.SERVER_DATASETS, parsed.ENTITIES
    out = flow.build_flow(cfg)
    assert out["joins"] and out["project"]["entities"]


def test_links_for_a_self_publishing_and_a_search_only_dataset(tmp_path):
    cfg = _project(tmp_path, server_datasets={})
    for key, extra in (
        # endline pulls its own earlier rows: it is the only survey with the column.
        (
            "endline",
            _q(
                "prev_visit",
                "calculate",
                calculation="pulldata('endline_ds', 'hh_name', 'hhid', ${hhid})",
            ),
        ),
        # baseline's roster read only by a search(): its filter column decides.
        (
            "endline",
            _q(
                "pick_member",
                "select_one",
                data_source={
                    "kind": "search",
                    "dataset": "members_ds",
                    "mode": "matches",
                    "filter": {"member_name": None},
                    "match": {"member_name": "${hhid}"},
                },
            ),
        ),
    ):
        path = cfg.DATASETS[key]["questions_json"]
        questions = json.loads(path.read_text("utf-8"))
        questions.append(extra)
        path.write_text(json.dumps(questions), "utf-8")
    text = flow.suggest_links(cfg)
    assert '[server_datasets.endline_ds]\npublished_by = "endline"' in text
    assert '[server_datasets.members_ds]\npublished_by = "baseline"' in text
    assert "read by search() only: ask the user" in text


def _store(tmp_path):
    entity = {"key": ["hhid"], "description": "A household", "parent": None}
    cfg = _project(tmp_path, entities={"household": entity})
    path = tmp_path / "flow.json"
    path.write_text(json.dumps(flow.build_flow(cfg)), "utf-8")
    return survey.SurveyStore(cfg.DATASETS, path)


def test_get_project_describes_entities_and_datasets(tmp_path):
    out = _store(tmp_path).get_project()
    assert out.startswith("About: not described yet.")
    assert "  household (hhid): A household; in baseline, endline" in out
    assert (
        "  dataset hh_roster: published by baseline, key hhid; read by endline" in out
    )
    assert "  dataset attach: an attachment" in out


def test_lineage_crosses_into_the_publisher(tmp_path):
    out = _store(tmp_path).trace_lineage("hh_name", survey="endline").splitlines()
    assert out == [
        "[endline] Where hh_name comes from:",
        "  hh_name (calculate) = pulldata('hh_roster', 'name', 'hhid', ${hhid})",
        "    <- pulldata: column name of dataset hh_roster, the row where hhid = ${hhid}",
        "    <- hh_roster is published by baseline: column name is its field "
        "[baseline] name",
        "      name (text): name",
        "    hhid (text): hhid",
    ]


def test_shell_labels_from_file_names_still_cross(tmp_path):
    cfg = _project(tmp_path)
    old = cfg.DATASETS["baseline"]["output_json"]
    renamed = old.with_name("bl_variable_dictionary.json")
    old.rename(renamed)
    old.with_name("baseline_questions.json").rename(old.with_name("bl_questions.json"))
    cfg.DATASETS["baseline"]["output_json"] = renamed
    cfg.DATASETS["baseline"]["questions_json"] = old.with_name("bl_questions.json")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "flow.json").write_text(
        json.dumps(flow.build_flow(cfg)), "utf-8"
    )
    store = survey.SurveyStore(survey.discover(tmp_path), survey.find_flow(tmp_path))
    assert "[bl] name" in store.trace_lineage("hh_name", survey="endline")


def test_a_broken_flow_is_reported_not_raised(tmp_path):
    path = tmp_path / "flow.json"
    path.write_text("{not json", "utf-8")
    store = survey.SurveyStore(_project(tmp_path).DATASETS, path)
    assert "flow.json is broken" in store.get_project()


def test_find_flow_takes_only_flow_json(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "workflow.json").write_text("{}", "utf-8")
    assert survey.find_flow(tmp_path) is None
    (tmp_path / "a" / "flow.json").write_text("{}", "utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "flow.json").write_text("{}", "utf-8")
    with pytest.raises(SystemExit, match="two flow.json files"):
        survey.find_flow(tmp_path)


def test_flow_and_links_commands(tmp_path, monkeypatch, capsys):
    cfg = _project(tmp_path)
    monkeypatch.setattr(flow, "load_config", lambda: cfg)
    flow.flow_main([])
    written = json.loads((tmp_path / "docs" / "flow.json").read_text("utf-8"))
    assert written["joins"]
    assert "[flow]" in capsys.readouterr().out
    flow.links_main([])
    assert "# hh_roster: read by endline" in capsys.readouterr().out
    cfg.PROJECT["output_dir"] = None
    with pytest.raises(SystemExit, match="output_dir is not set"):
        flow.flow_main([])


def test_no_flow_says_how_to_make_one(tmp_path):
    cfg = _project(tmp_path)
    out = survey.SurveyStore(cfg.DATASETS).get_project()
    assert "surveycto-links" in out and "surveycto-flow" in out


def test_shell_and_mcp_answer_get_project(tmp_path, capsys, monkeypatch):
    from surveycto_extractor.mcp_server import survey_server

    store = _store(tmp_path)
    survey.main(["--dir", str(tmp_path), "project"])
    assert capsys.readouterr().out == store.get_project() + "\n"
    monkeypatch.setattr(survey_server, "_store", store)
    assert survey_server.get_project() == store.get_project()


def test_config_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[project]\nname = "s"\noutput_dir = "docs"\n'
        '[server_datasets.hh_roster]\npublished_by = "baseline"\nkey = "hhid"\n'
        '[entities.household]\nkey = ["hhid", "round"]\ndescription = "x"\n',
        "utf-8",
    )
    cfg = _load_toml(path)
    assert cfg.PROJECT["output_dir"] == tmp_path / "docs"
    assert cfg.SERVER_DATASETS == {
        "hh_roster": {"published_by": "baseline", "key": ["hhid"]}
    }
    assert cfg.ENTITIES["household"] == {
        "key": ["hhid", "round"],
        "description": "x",
        "parent": None,
    }
    path.write_text('[server_datasets.x]\npublished = "a"\nkey = "k"\n', "utf-8")
    with pytest.raises(ValueError, match="unknown key"):
        _load_toml(path)
