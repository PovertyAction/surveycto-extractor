"""End-to-end pipeline test against the bundled sample.

Runs instrument-day (cli.extract: csv/json/sections) and Phase 4
(cli.vardict) over the sample form + dataset in a hermetic tmp dir, asserting
the real outputs. Nothing exercised this before, which is why Phase 4 shipped
broken on a fresh env (#25). The pipeline modules read module-global config, so
we point those globals at tmp copies via monkeypatch.

Runs on the local env (Python 3.14, pandas 3): pyreadstat/pyarrow may be
absent, so the vardict counts are asserted as invariants/floors, not exact.
"""

import json
import shutil
import sys
import types

import pytest

from surveycto_extractor.pii import PiiConfig


def _sample_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "sample"


@pytest.fixture
def sample_env(tmp_path, monkeypatch):
    s = tmp_path / "sample"
    s.mkdir()
    src = _sample_dir()
    for f in (
        "Household_Survey.xlsx",
        "household_survey.dta",
        "baseline_enum_list.csv",
        "household_preloads.csv",
    ):
        shutil.copy(src / f, s / f)
    out = s / "output"
    out.mkdir()

    surveys = {
        "household_survey": {
            "input_file": s / "Household_Survey.xlsx",
            "external_choices_csv": None,
            "output_dir": out,
            "sections_dir": out / "sections",
            "name": "Household Survey (test)",
            "max_section_depth": 3,
        }
    }
    datasets = {
        "household_survey": {
            "data": s / "household_survey.dta",
            "questions_json": out / "household_survey_questions.json",
            "output_json": out / "household_survey_variable_dictionary.json",
            "output_xlsx": out / "household_survey_variable_dictionary.xlsx",
        }
    }

    # Inject a stub `config` module (the real config.py is gitignored and absent
    # on CI). The package's config loader honors an injected sys.modules["config"]
    # first, so the stub reaches every consumer; we also monkeypatch the
    # already-imported cli modules' module-level config/DATASETS globals.
    stub = types.ModuleType("config")
    stub.SURVEYS = surveys
    stub.DATASETS = datasets
    stub.SURVEY_COLUMNS = [
        "type",
        "name",
        "label",
        "constraint",
        "relevance",
        "required",
        "calculation",
    ]
    stub.CHOICES_COLUMNS = ["list_name", "value", "label"]
    stub.EXCLUDED_TYPES = [
        "begin group",
        "end group",
        "begin repeat",
        "end repeat",
        "note",
        "start",
        "end",
        "deviceid",
        "username",
        "simserial",
        "phonenumber",
    ]
    stub.SYSTEM_PREFIXES = ["instanceID", "instanceName", "KEY", "SET-OF-"]
    stub.PII = PiiConfig(languages={"en": "en_core_web_sm"})
    monkeypatch.setitem(sys.modules, "config", stub)

    from surveycto_extractor.cli import extract as main
    from surveycto_extractor.cli import vardict as cvd

    # main/cvd may have been imported earlier with config=None (no config.py);
    # point their module-level references at the stub for this test.
    monkeypatch.setattr(main, "config", stub, raising=False)
    monkeypatch.setattr(cvd, "config", stub, raising=False)
    monkeypatch.setattr(cvd, "DATASETS", datasets, raising=False)
    return out, main, cvd


def test_instrument_day_and_phase4(sample_env, monkeypatch):
    out, main, cvd = sample_env

    # Instrument day: csv + json + sections.
    monkeypatch.setattr(sys, "argv", ["main.py", "--survey", "household_survey"])
    main.main()

    questions_path = out / "household_survey_questions.json"
    assert questions_path.exists()
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    assert len(questions) == 236  # sample form ground truth
    by_name = {q["variable_name"]: q for q in questions}
    # A repeat's count sits at its parent's level: the parent's gates only.
    consented = [{"group": "consented_grp", "relevance": "${c_consent} = 1"}]
    assert by_name["f_hr_rpt_count"]["gates"] == consented
    assert by_name["f_hr_fn_r"]["group_path"][-1] == "f_hr_rpt"
    assert by_name["a_enum_name"]["data_source"] == {
        "kind": "pulldata",
        "dataset": "baseline_enum_list",
        "value": "'enum_name'",
        "key": "enum_id",
        "match": "${a_enum_id}",
    }
    assert (out / "household_survey_structure.txt").exists()
    assert (out / "sections").is_dir()

    # Phase 4: variable dictionary (+xlsx). Must not crash (the #25 regression).
    monkeypatch.setattr(
        sys, "argv", ["cvd.py", "--survey", "household_survey", "--xlsx"]
    )
    cvd.main()

    vd_path = out / "household_survey_variable_dictionary.json"
    assert vd_path.exists()
    vd = json.loads(vd_path.read_text(encoding="utf-8"))
    summary = vd["summary"]
    total = summary["total_variables"]
    matched = summary["matched_to_questions"]
    unmatched = summary["unmatched"]
    # Invariants (dtype-independent, so stable across pyreadstat/fallback):
    assert total == 805
    assert matched + unmatched == total
    assert vd["dataset"]["n_observations"] == 917
    assert (out / "household_survey_variable_dictionary.xlsx").exists()

    # A known repeat variable decodes with its iteration.
    variables = vd["variables"]
    assert "f_hr_fn_r1" in variables
    assert variables["f_hr_fn_r1"].get("repeat_iteration") == 1


def test_phase4_variable_graph_written(sample_env, monkeypatch):
    out, main, cvd = sample_env
    monkeypatch.setattr(sys, "argv", ["main.py", "--survey", "household_survey"])
    main.main()
    monkeypatch.setattr(sys, "argv", ["cvd.py", "--survey", "household_survey"])
    cvd.main()
    graph_path = out / "household_survey_variable_graph.json"
    g = json.loads(graph_path.read_text(encoding="utf-8"))
    assert len(g["nodes"]) > 0

    # survey.py walks the graph without networkx: same nodes, same distances.
    import networkx as nx

    from surveycto_extractor.survey import _Graph

    g.pop("repeat_groups")
    ours = _Graph(g)
    undirected = nx.node_link_graph(
        g, edges="edges" if "edges" in g else "links"
    ).to_undirected(as_view=True)
    for node in ours.nodes:
        for depth in (1, 2, 3):
            expected = nx.single_source_shortest_path_length(
                undirected, node, cutoff=depth
            )
            got = {n: len(p) - 1 for n, p in ours.shortest_paths(node, depth).items()}
            assert got == expected, (node, depth)
