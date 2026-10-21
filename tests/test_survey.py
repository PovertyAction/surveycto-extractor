"""The survey module behind the MCP server and the skill's search_survey.py:
store robustness (#29, #24), the stdlib-only contract, the shell front end, the
MCP wrapper and the skill installer.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

from surveycto_extractor import pii
from surveycto_extractor import survey as srv
from surveycto_extractor.cli import skill

SURVEY_PY = Path(srv.__file__)


class TestUnicodeTokenizer:
    def test_accent_insensitive_both_ways(self):
        # An agent typing unaccented "cuantos ninos" must match accented text.
        assert srv._tokenize("cuántos niños") == srv._tokenize("cuantos ninos")

    def test_accented_tokens_not_shredded(self):
        toks = srv._tokenize("¿Cuántos niños?")
        assert "cuantos" in toks and "ninos" in toks

    def test_underscore_is_a_separator(self):
        assert srv._tokenize("crpsale_qty") == ["crpsale", "qty"]

    def test_non_latin_preserved(self):
        # Cyrillic survives (each word >= 2 chars).
        assert srv._tokenize("привет мир") == ["привет", "мир"]


class TestConfigEntryDegradesGracefully:
    def test_missing_output_json_does_not_crash_construction(self):
        # Constructing with a DATASETS entry that has no output_json must NOT
        # raise ValueError from Path("").with_name(...) -- it must degrade that
        # survey and leave the server usable. (#29.2)
        store = srv.SurveyStore({"broken": {}})
        assert store._vardicts["broken"] == {}
        assert store._questions["broken"] == []


def _store_with_summary(tmp_path, summary):
    """A store whose one survey's dictionary has the given summary block."""
    vd = tmp_path / "s_variable_dictionary.json"
    vd.write_text(
        json.dumps(
            {
                "dataset": {"n_observations": 10},
                "summary": {**summary, "pii": {"whitelisted": 1}},
                "variables": {"dummy": {"stata": {"type": "int"}, "survey": {}}},
            }
        ),
        encoding="utf-8",
    )
    return srv.SurveyStore({"s": {"output_json": str(vd)}})


class TestInfoPrefersPostXmlCount:
    def test_uses_matched_after_xml_when_present(self, tmp_path):
        store = _store_with_summary(
            tmp_path,
            {
                "total_variables": 543,
                "matched_to_questions": 526,
                "unmatched": 17,
                "matched_after_xml": 528,
            },
        )
        out = store.get_info()
        assert "528 (post-XML)" in out
        assert "unmatched          : 15" in out  # 543 - 528

    def test_falls_back_to_prexml_count(self, tmp_path):
        store = _store_with_summary(
            tmp_path,
            {
                "total_variables": 100,
                "matched_to_questions": 90,
                "unmatched": 10,
            },
        )
        out = store.get_info()
        assert "matched_to_form    : 90" in out
        assert "unmatched          : 10" in out


def test_survey_module_is_standard_library_only():
    """The skill copies survey.py into projects that may not have this package."""
    tree = ast.parse(SURVEY_PY.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)


def test_flagged_statuses_match_the_pii_module():
    assert srv._FLAGGED == pii.FLAGGED
    for status in ("contains_pii", "may_contain_pii", "no_hits", "whitelisted"):
        assert srv._shows_values(status) == pii.shows_values(status)


def _write_survey(root: Path, label="hh"):
    folder = root / "docs" / label
    folder.mkdir(parents=True)
    questions = [
        {
            "variable_name": "name",
            "type": "text",
            "question_text": "Respondent name",
            "relevance": "${consent} = 1",
        },
        {
            "variable_name": "consent",
            "type": "select_one",
            "choice_list": "yesno",
            "question_text": "Consent",
            "choices": [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}],
        },
        {
            "variable_name": "roster",
            "type": "select_one",
            "choice_list": "people",
            "question_text": "Pick a member",
            "choices": None,
            "choices_withheld": ["presidio:PERSON"],
        },
        {"variable_name": "intro", "type": "note", "question_text": "Welcome"},
        {
            "variable_name": "staff",
            "type": "select_multiple",
            "choice_list": "staff",
            "question_text": "Who visited",
            "choices": [{"value": "value", "label": "label"}],
            "choices_withheld": ["presidio:PERSON"],
        },
    ]
    # Keys every questions.json entry carries (the store refuses an older file).
    questions = [
        {
            "group_path": [],
            "gates": [],
            "data_source": None,
            "relevance": None,
            "choice_list": None,
            "calculation": None,
            **q,
        }
        for q in questions
    ]
    variables = {
        "name": {
            "non_null_count": 3,
            "stata": {"type": "str20"},
            "survey": {"original_variable_name": "name", "type": "text"},
            "pii": {"status": "may_contain_pii", "basis": ["type:text"]},
        },
        "consent": {
            "non_null_count": 3,
            "stata": {"type": "byte"},
            "survey": {
                "original_variable_name": "consent",
                "type": "select_one",
                "choice_list": "yesno",
            },
            "pii": {"status": "whitelisted", "basis": ["choices:inline"]},
        },
        "roster": {
            "non_null_count": 3,
            "stata": {"type": "str3"},
            "survey": {
                "original_variable_name": "roster",
                "type": "select_one",
                "choice_list": "people",
                "choices_withheld": ["presidio:PERSON"],
            },
            "pii": {"status": "no_hits", "basis": ["type:select_one"]},
        },
        # A withheld select_multiple: its binaries' names carry the CSV's codes.
        **{
            f"staff_{code}": {
                "non_null_count": 3,
                "stata": {"type": "byte"},
                "survey": {
                    "original_variable_name": "staff",
                    "type": "select_multiple",
                    "choice_list": "staff",
                    "choices": [{"value": "value", "label": "label"}],
                    "choices_withheld": ["presidio:PERSON"],
                },
                "contract": {
                    "kind": "matched",
                    "is_select_multiple": True,
                    "choice_code": code,
                },
                "pii": {"status": "no_hits", "basis": ["type:select_multiple"]},
            }
            for code in ("edavis", "mjohnson")
        },
    }
    (folder / f"{label}_questions.json").write_text(json.dumps(questions), "utf-8")
    (folder / f"{label}_variable_dictionary.json").write_text(
        json.dumps(
            {
                "dataset": {"n_observations": 3},
                "summary": {"pii": {"whitelisted": 3}},
                "variables": variables,
            }
        ),
        "utf-8",
    )
    graph = {
        "directed": True,
        "multigraph": True,
        "graph": {},
        "nodes": [{"id": "name"}, {"id": "consent"}],
        "edges": [
            {"source": "name", "target": "consent", "key": 0, "type": "gated_by"}
        ],
        "repeat_groups": {},
    }
    (folder / f"{label}_variable_graph.json").write_text(json.dumps(graph), "utf-8")
    return folder


def test_discover_pairs_files_and_skips_hidden_folders(tmp_path):
    folder = _write_survey(tmp_path)
    hidden = tmp_path / ".venv" / "x"
    hidden.mkdir(parents=True)
    (hidden / "junk_variable_dictionary.json").write_text("{}", "utf-8")
    found = srv.discover(tmp_path)
    assert list(found) == ["hh"]
    assert found["hh"]["questions_json"] == folder / "hh_questions.json"


def test_lookup_shows_pii_and_withheld_lists(tmp_path):
    _write_survey(tmp_path)
    store = srv.SurveyStore(srv.discover(tmp_path))
    name = store.lookup_var("name")
    assert "pii          : may_contain_pii (type:text)" in name
    assert "never its values" in name
    assert "relevance    : ${consent} = 1" in name
    assert "choices_withheld: presidio:PERSON" in store.lookup_var("roster")
    assert "Choices withheld: presidio:PERSON" in store.get_choice_list("people")
    assert "PII:may_contain_pii" in store.search("respondent name")
    # A form question with no dataset column still answers.
    assert "no dataset column" in store.lookup_var("intro")


def test_shell_answers_as_the_store_does(tmp_path, capsys):
    _write_survey(tmp_path)
    store = srv.SurveyStore(srv.discover(tmp_path))
    cases = [
        (["var", "name", "--context", "2"], store.lookup_var("name", context=2)),
        (["var", "name", "consent"], store.lookup_vars_batch(["name", "consent"])),
        (["search", "consent"], store.search("consent")),
        (["choices", "yesno"], store.get_choice_list("yesno")),
        (["gates", "name"], store.get_gate_chain("name")),
        (["neighborhood", "name"], store.get_neighborhood("name")),
        (["repeats"], store.get_repeat_structure()),
        (["info"], store.get_info()),
    ]
    for argv, expected in cases:
        srv.main(["--dir", str(tmp_path), *argv])
        assert capsys.readouterr().out == expected + "\n"


def test_mcp_tools_call_the_store(tmp_path, monkeypatch):
    from surveycto_extractor.mcp_server import survey_server

    _write_survey(tmp_path)
    store = srv.SurveyStore(srv.discover(tmp_path))
    monkeypatch.setattr(survey_server, "_store", store)
    s = survey_server
    pairs = [
        (s.lookup_variable("name", 2), store.lookup_var("name", context=2)),
        (
            s.lookup_variables(["name", "staff_edavis"]),
            store.lookup_vars_batch(["name", "staff_edavis"]),
        ),
        (s.search_questions("visited", 5), store.search("visited", max_results=5)),
        (s.get_choice_list("people"), store.get_choice_list("people")),
        (s.get_gate_chain("name"), store.get_gate_chain("name")),
        (s.get_survey_info(), store.get_info()),
        (s.get_variable_neighborhood("name", 2), store.get_neighborhood("name", 2)),
        (s.get_repeat_structure(), store.get_repeat_structure()),
    ]
    for tool_answer, store_answer in pairs:
        assert tool_answer == store_answer


def test_withheld_select_multiple_columns_are_shown_by_position(tmp_path):
    _write_survey(tmp_path)
    store = srv.SurveyStore(srv.discover(tmp_path))
    answers = "\n".join(
        [
            store.lookup_var("staff_edavis", context=2),
            store.lookup_var("staff_<2>"),
            store.lookup_var("staff"),
            store.lookup_vars_batch(["staff_edavis", "staff_mjohnson"]),
            store.get_choice_list("staff"),
            store.search("who visited"),
            store.get_gate_chain("staff_edavis"),
        ]
    )
    assert "staff_<1>" in answers and "staff_<2>" in answers
    assert "edavis" not in answers.replace("staff_edavis", "")  # only our queries
    assert "mjohnson" not in answers.replace("staff_mjohnson", "")


def test_a_question_with_columns_points_to_them(tmp_path):
    _write_survey(tmp_path)
    store = srv.SurveyStore(srv.discover(tmp_path))
    out = store.lookup_var("staff")
    assert "stored in 2 columns" in out and "no dataset column" not in out


def test_a_dictionary_without_pii_is_refused(tmp_path):
    folder = _write_survey(tmp_path)
    path = folder / "hh_variable_dictionary.json"
    raw = json.loads(path.read_text("utf-8"))
    del raw["summary"]["pii"]
    path.write_text(json.dumps(raw), "utf-8")
    out = srv.SurveyStore(srv.discover(tmp_path)).lookup_var("name")
    assert "refused" in out and "re-run surveycto-vardict" in out


def test_shell_exits_1_on_a_miss(tmp_path):
    _write_survey(tmp_path)
    with pytest.raises(SystemExit) as stop:
        srv.main(["--dir", str(tmp_path), "var", "no_such_var"])
    assert stop.value.code == 1


def test_discover_refuses_two_dictionaries_for_one_survey(tmp_path):
    _write_survey(tmp_path / "a")
    _write_survey(tmp_path / "b")
    with pytest.raises(SystemExit, match="two dictionaries for survey 'hh'"):
        srv.discover(tmp_path)


def test_install_skill_writes_the_package_copy(tmp_path):
    target = skill.install_skill(tmp_path / ".claude" / "skills" / "survey-expert")
    assert (target / "SKILL.md").exists()
    assert (target / "references" / "missing-values.md").exists()
    copied = (target / "search_survey.py").read_bytes()
    assert copied == SURVEY_PY.read_bytes()


def test_server_entry(tmp_path):
    mcp_json = tmp_path / ".mcp.json"
    entry = skill.server_entry(mcp_json)
    assert entry == {"mcpServers": {"survey-expert": skill.SERVER}}
    mcp_json.write_text(json.dumps(entry), "utf-8")
    assert skill.server_entry(mcp_json) is None
    other = {"mcpServers": {"other": {"command": "x"}}}
    mcp_json.write_text(json.dumps(other), "utf-8")
    assert set(skill.server_entry(mcp_json)["mcpServers"]) == {"other", "survey-expert"}
    # The entry this toolkit documented before is migrated, its env kept.
    old = {"command": "uv", "args": skill._OLD_ARGS, "env": {"SURVEYCTO_CONFIG": "c"}}
    mcp_json.write_text(json.dumps({"mcpServers": {"survey-expert": old}}), "utf-8")
    migrated = skill.server_entry(mcp_json)["mcpServers"]["survey-expert"]
    assert migrated == {**skill.SERVER, "env": {"SURVEYCTO_CONFIG": "c"}}
    clash = {"mcpServers": {"survey-expert": {"command": "python"}}}
    mcp_json.write_text(json.dumps(clash), "utf-8")
    with pytest.raises(SystemExit, match="different survey-expert"):
        skill.server_entry(mcp_json)


def test_installer_refuses_the_toolkit_repository(monkeypatch):
    monkeypatch.setenv("UV", "uv")
    with pytest.raises(SystemExit, match="toolkit's own repository"):
        skill._refuse_outside_a_project(Path(__file__).resolve().parents[1])


def test_missing_models_by_distribution():
    assert skill.missing_models({"en": "en_core_web_sm", "xx": "xx_no_model"}) == [
        "xx_no_model"
    ]


def _project(tmp_path, monkeypatch, config_text):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "study"\n', "utf-8")
    if config_text is not None:
        (tmp_path / "config.toml").write_text(config_text, "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UV", "uv-bin")
    monkeypatch.delenv("SURVEYCTO_CONFIG", raising=False)
    monkeypatch.delitem(sys.modules, "config", raising=False)
    calls = []
    monkeypatch.setattr(skill.subprocess, "run", lambda args, **kw: calls.append(args))
    return calls


def test_installer_main_writes_everything(tmp_path, monkeypatch):
    calls = _project(
        tmp_path, monkeypatch, '[pii]\nlanguages = { xx = "xx_no_model" }\n'
    )
    skill.main([])
    target = tmp_path / ".claude" / "skills" / "survey-expert"
    assert (target / "SKILL.md").exists()
    assert (target / "search_survey.py").read_bytes() == SURVEY_PY.read_bytes()
    servers = json.loads((tmp_path / ".mcp.json").read_text("utf-8"))["mcpServers"]
    assert servers["survey-expert"] == skill.SERVER
    assert calls == [["uv-bin", "add", pii.MODEL_URL.format(model="xx_no_model")]]


def test_installer_main_writes_nothing_on_a_clash(tmp_path, monkeypatch):
    calls = _project(tmp_path, monkeypatch, "[pii]\nno_personal_data = true\n")
    clash = {"mcpServers": {"survey-expert": {"command": "python"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(clash), "utf-8")
    with pytest.raises(SystemExit, match="different survey-expert"):
        skill.main([])
    assert not (tmp_path / ".claude").exists()
    assert calls == []


def test_installer_main_needs_a_config(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, None)
    with pytest.raises(SystemExit, match="no config.toml"):
        skill.main([])


def _write_form(root: Path):
    """A form with nested groups and a lineage chain, and a dictionary for it."""
    gate = {"group": "hh", "relevance": "${consent} = 1"}
    sub = {"group": "visit", "relevance": "${n_visits} > 0"}
    q = {
        "choices": None,
        "choice_list": None,
        "relevance": None,
        "calculation": None,
        "data_source": None,
    }
    questions = [
        {
            **q,
            "variable_name": "consent",
            "type": "select_one",
            "choice_list": "yesno",
            "question_text": "Consent",
            "group_path": [],
            "gates": [],
        },
        {
            **q,
            "variable_name": "hhid",
            "type": "text",
            "question_text": "Household ID",
            "group_path": ["hh"],
            "gates": [gate],
        },
        {
            **q,
            "variable_name": "n_visits",
            "type": "integer",
            "question_text": "Visits",
            "group_path": ["hh"],
            "gates": [gate],
            "relevance": "${hhid} != ''",
        },
        {
            **q,
            "variable_name": "owner",
            "type": "calculate",
            "calculation": "pulldata('roster', 'owner', 'hhid', ${hhid})",
            "data_source": {
                "kind": "pulldata",
                "dataset": "roster",
                "value": "'owner'",
                "key": "hhid",
                "match": "${hhid}",
            },
            "group_path": ["hh", "visit"],
            "gates": [gate, sub],
        },
        {
            **q,
            "variable_name": "greeting",
            "type": "calculate",
            "calculation": "concat('Hi ', ${owner}, ${greeting})",
            "group_path": ["hh", "visit"],
            "gates": [gate, sub],
        },
    ]
    folder = root / "docs"
    folder.mkdir(parents=True)
    (folder / "f_questions.json").write_text(json.dumps(questions), "utf-8")
    variables = {
        q["variable_name"]: {
            "non_null_count": 1,
            "stata": {"type": "str9"},
            "survey": {"original_variable_name": q["variable_name"], "type": q["type"]},
            "pii": {"status": "no_hits", "basis": ["type:text"]},
        }
        for q in questions
    }
    (folder / "f_variable_dictionary.json").write_text(
        json.dumps({"summary": {"pii": {}}, "variables": variables}), "utf-8"
    )
    return srv.SurveyStore(srv.discover(root))


def test_get_group_shows_each_gate_once(tmp_path):
    out = _write_form(tmp_path).get_group("hh")
    assert out.splitlines() == [
        "[f] hh: 4 questions, asked if ${consent} = 1",
        "  hhid (text)",
        "  n_visits (integer)  if ${hhid} != ''",
        "  -- visit: if ${n_visits} > 0",
        "  owner (calculate) [visit]",
        "  greeting (calculate) [visit]",
    ]
    inner = _write_form(tmp_path / "b").get_group("visit")
    assert "inside hh, asked if ${consent} = 1" in inner
    assert (
        _write_form(tmp_path / "c").get_group("visits")
        == "No group named 'visits'. Close: visit"
    )


def test_trace_lineage_walks_calculates_and_pulldata(tmp_path):
    out = _write_form(tmp_path).trace_lineage("greeting")
    assert out.splitlines() == [
        "[f] Where greeting comes from:",
        "  greeting (calculate) = concat('Hi ', ${owner}, ${greeting})",
        "    owner (calculate) = pulldata('roster', 'owner', 'hhid', ${hhid})",
        "      <- pulldata: column owner of dataset roster, the row where hhid = ${hhid}",
        "      hhid (text): Household ID",
        "    greeting (traced above)",
    ]


def test_new_tools_through_the_shell_and_mcp(tmp_path, capsys, monkeypatch):
    from surveycto_extractor.mcp_server import survey_server

    store = _write_form(tmp_path)
    for argv, expected in (
        (["group", "hh"], store.get_group("hh")),
        (["lineage", "owner"], store.trace_lineage("owner")),
    ):
        srv.main(["--dir", str(tmp_path), *argv])
        assert capsys.readouterr().out == expected + "\n"
    monkeypatch.setattr(survey_server, "_store", store)
    assert survey_server.get_group("hh") == store.get_group("hh")
    assert survey_server.trace_lineage("owner") == store.trace_lineage("owner")


def test_lineage_reads_every_field_and_names_repeats(tmp_path):
    store = _write_form(tmp_path)
    path = tmp_path / "docs" / "f_questions.json"
    questions = json.loads(path.read_text("utf-8"))
    base = {k: None for k in ("choices", "choice_list", "relevance", "data_source")}
    questions += [
        {
            **base,
            "variable_name": "guarded",
            "type": "calculate",
            "question_text": "",
            "group_path": [],
            "gates": [],
            "calculation": "if(${consent} = 1, pulldata('roster', 'owner', 'hhid', "
            "${hhid}), pulldata('staff', 'name', 'id', ${n_visits}))",
            "data_source": {
                "kind": "pulldata",
                "dataset": "roster",
                "value": "'owner'",
                "key": "hhid",
                "match": "${hhid}",
            },
        },
        {
            **base,
            "variable_name": "n_members",
            "type": "calculate",
            "question_text": "",
            "group_path": [],
            "gates": [],
            "calculation": "count(${members})",
        },
        {
            **base,
            "variable_name": "members_count",
            "type": "repeat_count",
            "question_text": "",
            "group_path": [],
            "gates": [],
            "repeat_group_name": "members",
        },
        {
            **base,
            "variable_name": "pick",
            "type": "select_one",
            "question_text": "Pick a member",
            "choice_list": "roster",
            "group_path": [],
            "gates": [],
            "data_source": {
                "kind": "search",
                "dataset": "roster",
                "mode": "matches",
                "filter": {"hhid": None},
                "match": {"hhid": "${hhid}"},
            },
        },
    ]
    path.write_text(json.dumps(questions), "utf-8")
    store = srv.SurveyStore(srv.discover(tmp_path))
    guarded = store.trace_lineage("guarded")
    for field in ("consent (select_one)", "hhid (text)", "n_visits (integer)"):
        assert field in guarded
    assert "pulldata('staff', 'name', 'id', ${n_visits})" in guarded
    assert "members (repeat group; its instances)" in store.trace_lineage("n_members")
    pick = store.trace_lineage("pick").splitlines()
    assert pick[1] == "  pick (select_one): Pick a member  [choices: roster]"
    assert pick[2].startswith("    <- choices from search: a row of dataset roster")
    assert pick[3] == "    hhid (text): Household ID"


def test_an_older_questions_json_is_refused(tmp_path):
    _write_form(tmp_path)
    path = tmp_path / "docs" / "f_questions.json"
    questions = json.loads(path.read_text("utf-8"))
    for q in questions:
        del q["gates"]
    path.write_text(json.dumps(questions), "utf-8")
    out = srv.SurveyStore(srv.discover(tmp_path)).get_group("hh")
    assert "refused" in out and "re-run surveycto-extract" in out


def test_lookup_caps_a_long_column_list_and_shows_missing_rate(tmp_path):
    folder = _write_survey(tmp_path)
    path = folder / "hh_variable_dictionary.json"
    raw = json.loads(path.read_text("utf-8"))
    for i in range(1, 13):
        raw["variables"][f"name_{i}"] = {
            **raw["variables"]["name"],
            "repeat_iteration": i,
            "missing_rate": 0.25,
        }
    del raw["variables"]["name"]
    path.write_text(json.dumps(raw), "utf-8")
    store = srv.SurveyStore(srv.discover(tmp_path))
    out = store.lookup_var("name")
    assert "stored in 12 columns" in out and "(2 more)" in out
    assert "missing_rate : 25.0%" in store.lookup_var("name_1")
