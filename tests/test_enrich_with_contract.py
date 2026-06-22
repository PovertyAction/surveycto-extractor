"""End-to-end test for the XML-contract overlay (`enrich_with_contract.py`).

Builds a minimal variable_dictionary.json in the exact schema Phase 4
(`create_variable_dictionaries.py`) writes, plus a questions.json and a compiled
XForm, then runs the overlay on explicit paths (no config needed) and asserts the
authoritative-XML behaviour: fuzzy misses get backfilled (`resolved_by: xml`),
select_multiple choice codes decode against a search() CSV, system/legacy columns
are classified, and the summary/audit counters are correct.
"""
import json

import pytest

from enrich_with_contract import enrich_contract

_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <model>
      <instance>
        <myform id="myform" version="3">
          <name/>
          <langs/>
          <pre_score/>
        </myform>
      </instance>
      <bind nodeset="/myform/name" type="string"/>
      <bind nodeset="/myform/langs" type="select"/>
      <bind nodeset="/myform/pre_score" type="int"
            calculate="pulldata('preload', 'score', 'id', /myform/name)"/>
    </model>
  </h:head>
  <h:body>
    <input ref="/myform/name"><label>Name</label></input>
    <select ref="/myform/langs"
            appearance="search('choices', 'matches', 'list_name', 'langlist')">
      <label>Languages</label>
    </select>
  </h:body>
</h:html>
"""

_QUESTIONS = [
    {"variable_name": "name", "question_text": "Your name", "type": "text"},
    {"variable_name": "langs", "question_text": "Languages", "type": "select_multiple"},
    {"variable_name": "pre_score", "question_text": "Preloaded score", "type": "integer"},
]

# Phase-4 output schema: top-level dataset/summary/variables, per-var survey block.
_VARDICT = {
    "dataset": {"name": "t", "n_observations": 1, "n_variables": 5},
    "summary": {
        "total_variables": 5, "matched_to_questions": 1, "unmatched": 4,
        "from_repeat_groups": 0, "select_multiple_choices": 0,
    },
    "variables": {
        # fuzzy already matched correctly -> survey kept, contract added
        "name": {"variable_order": 1, "non_null_count": 1, "stata": {"type": "str"},
                 "survey": {"original_variable_name": "name", "question_text": "Your name"}},
        # fuzzy missed -> XML resolves (pulldata provenance)
        "pre_score": {"variable_order": 2, "non_null_count": 1, "stata": {"type": "int"},
                      "survey": {"original_variable_name": None}},
        # select_multiple choice binary -> XML resolves + decodes choice 1
        "langs_1": {"variable_order": 3, "non_null_count": 1, "stata": {"type": "byte"},
                    "survey": {"original_variable_name": None}},
        # SurveyCTO transport column -> system
        "KEY": {"variable_order": 4, "non_null_count": 1, "stata": {"type": "str"},
                "survey": {"original_variable_name": None}},
        # has data but no node in the deployed form -> legacy
        "old_var_9": {"variable_order": 5, "non_null_count": 1, "stata": {"type": "byte"},
                      "survey": {"original_variable_name": None}},
    },
}

_CHOICES_CSV = "list_name,value,label\nlanglist,1,English\nlanglist,2,Spanish\n"


@pytest.fixture
def enriched(tmp_path):
    (tmp_path / "myform.xml").write_text(_XML, encoding="utf-8")
    (tmp_path / "choices.csv").write_text(_CHOICES_CSV, encoding="utf-8")
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_QUESTIONS), encoding="utf-8")
    vd = tmp_path / "vardict.json"
    vd.write_text(json.dumps(_VARDICT), encoding="utf-8")

    enrich_contract(
        output_json=vd,
        questions_json=q,
        xml_path=tmp_path / "myform.xml",
        attachment_dirs=[tmp_path],
    )
    return json.loads(vd.read_text(encoding="utf-8"))


class TestOverlay:
    def test_top_level_form_identity(self, enriched):
        assert enriched["formid"] == "myform"
        assert enriched["contract_source_version"] == "3"
        assert enriched["sources"]["xml"].endswith("myform.xml")

    def test_correctly_matched_kept(self, enriched):
        e = enriched["variables"]["name"]
        assert e["survey"]["original_variable_name"] == "name"
        assert e["contract"]["kind"] == "matched"
        assert "resolved_by" not in e["contract"]
        assert "corrected_from" not in e["contract"]

    def test_fuzzy_miss_resolved_by_xml(self, enriched):
        e = enriched["variables"]["pre_score"]
        assert e["survey"]["original_variable_name"] == "pre_score"
        assert e["contract"]["resolved_by"] == "xml"
        assert e["contract"]["data_source"]["kind"] == "pulldata"
        assert e["survey"]["question_text"] == "Preloaded score"

    def test_select_multiple_choice_decoded(self, enriched):
        e = enriched["variables"]["langs_1"]
        assert e["contract"]["is_select_multiple"] is True
        assert e["contract"]["choice_code"] == 1
        assert e["contract"]["choice_label"] == "English"
        # survey choices backfilled from the search() CSV
        assert {"value": "1", "label": "English"} in e["survey"]["choices"]

    def test_system_column(self, enriched):
        assert enriched["variables"]["KEY"]["contract"]["kind"] == "system"

    def test_legacy_column(self, enriched):
        assert enriched["variables"]["old_var_9"]["contract"]["kind"] == "legacy"

    def test_summary_counters(self, enriched):
        s = enriched["summary"]
        assert s["matched_after_xml"] == 3        # name, pre_score, langs_1
        assert s["resolved_by_xml"] == 2          # pre_score, langs_1
        assert s["corrected_by_xml"] == 0
        assert s["system_columns"] == 1
        assert s["legacy_columns"] == 1
        assert s["choice_lists_resolved_from_xml"] == 1
        assert enriched["audit"]["legacy_columns"] == ["old_var_9"]
