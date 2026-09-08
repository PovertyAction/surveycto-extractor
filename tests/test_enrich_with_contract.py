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

from surveycto_extractor.cli.enrich import enrich_contract

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
    {
        "variable_name": "pre_score",
        "question_text": "Preloaded score",
        "type": "integer",
    },
]

# Phase-4 output schema: top-level dataset/summary/variables, per-var survey block.
_VARDICT = {
    "dataset": {"name": "t", "n_observations": 1, "n_variables": 5},
    "summary": {
        "total_variables": 5,
        "matched_to_questions": 1,
        "unmatched": 4,
        "from_repeat_groups": 0,
        "select_multiple_choices": 0,
    },
    "variables": {
        # fuzzy already matched correctly -> survey kept, contract added
        "name": {
            "variable_order": 1,
            "non_null_count": 1,
            "stata": {"type": "str"},
            "survey": {"original_variable_name": "name", "question_text": "Your name"},
        },
        # fuzzy missed -> XML resolves (pulldata provenance)
        "pre_score": {
            "variable_order": 2,
            "non_null_count": 1,
            "stata": {"type": "int"},
            "survey": {"original_variable_name": None},
        },
        # select_multiple choice binary -> XML resolves + decodes choice 1
        "langs_1": {
            "variable_order": 3,
            "non_null_count": 1,
            "stata": {"type": "byte"},
            "survey": {"original_variable_name": None},
        },
        # SurveyCTO transport column -> system
        "KEY": {
            "variable_order": 4,
            "non_null_count": 1,
            "stata": {"type": "str"},
            "survey": {"original_variable_name": None},
        },
        # has data but no node in the deployed form -> legacy
        "old_var_9": {
            "variable_order": 5,
            "non_null_count": 1,
            "stata": {"type": "byte"},
            "survey": {"original_variable_name": None},
        },
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
        assert s["matched_after_xml"] == 3  # name, pre_score, langs_1
        assert s["resolved_by_xml"] == 2  # pre_score, langs_1 (both exact)
        assert s["resolved_by_xml_heuristic"] == 0
        assert s["corrected_by_xml"] == 0
        assert s["system_columns"] == 1
        assert s["legacy_columns"] == 1
        assert s["choice_lists_resolved_from_xml"] == 1
        assert enriched["audit"]["legacy_columns"] == ["old_var_9"]

    def test_resolved_miss_keeps_existing_metadata(self, enriched):
        # A resolved miss merges (doesn't wholesale-replace), so any field the
        # column already carried survives. exact match, not heuristic.
        assert enriched["variables"]["pre_score"]["contract"]["match"] == "exact"


# --- correction, idempotence, heuristic resolution -------------------------

_CORR_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance><cf id="cf" version="1"><age/><Pre/></cf></instance>
    <bind nodeset="/cf/age" type="int"/>
    <bind nodeset="/cf/Pre" type="string"/>
  </model></h:head>
  <h:body/></h:html>"""

_CORR_QUESTIONS = [
    {
        "variable_name": "age",
        "question_text": "Age in years",
        "type": "integer",
        "constraint": ". >= 0",
        "stata_constraint": "age >= 0",
    },
]

_CORR_VARDICT = {
    "summary": {"total_variables": 2},
    "variables": {
        # fuzzy WRONGLY matched 'age' to question 'agee' -> XML corrects it
        "age": {
            "survey": {
                "original_variable_name": "agee",
                "question_text": "wrong",
                "constraint": ". > 200",
            }
        },
        # 'Pre_xyz' has no node; the string-key fallback resolves it to node 'Pre'
        # -> must be flagged heuristic, not authoritative.
        "Pre_xyz": {"survey": {"original_variable_name": None}},
    },
}


def _write_corr(tmp_path):
    (tmp_path / "cf.xml").write_text(_CORR_XML, encoding="utf-8")
    q = tmp_path / "q.json"
    q.write_text(json.dumps(_CORR_QUESTIONS), encoding="utf-8")
    vd = tmp_path / "vd.json"
    vd.write_text(json.dumps(_CORR_VARDICT), encoding="utf-8")
    return vd, q, tmp_path / "cf.xml"


class TestCorrectionHeuristicIdempotence:
    def test_correction_overrides_and_refills(self, tmp_path):
        vd, q, xml = _write_corr(tmp_path)
        d = enrich_contract(output_json=vd, questions_json=q, xml_path=xml)
        age = d["variables"]["age"]
        assert age["contract"]["corrected_from"] == "agee"
        assert "resolved_by" not in age["contract"]
        assert age["survey"]["original_variable_name"] == "age"  # XML wins
        # refilled from the CORRECT node, incl. the wider allowlist (constraint),
        # and the wrong fuzzy constraint is gone.
        assert age["survey"]["constraint"] == ". >= 0"
        assert age["survey"]["stata_constraint"] == "age >= 0"
        assert d["summary"]["corrected_by_xml"] == 1

    def test_string_key_match_flagged_heuristic(self, tmp_path):
        vd, q, xml = _write_corr(tmp_path)
        d = enrich_contract(output_json=vd, questions_json=q, xml_path=xml)
        c = d["variables"]["Pre_xyz"]["contract"]
        assert c["match"] == "string_key"
        assert c["string_key"] == "xyz"
        assert c["resolved_by"] == "xml-heuristic"  # NOT plain "xml"
        # the heuristic is counted separately and does NOT inflate resolved_by_xml
        assert d["summary"]["resolved_by_xml_heuristic"] == 1
        assert d["summary"]["resolved_by_xml"] == 0

    def test_idempotent_rerun(self, tmp_path):
        # A standalone re-run on an already-enriched dictionary must not drop
        # provenance markers or zero the counters. (The Phase-4 hook writes a fresh
        # dict each build, so this guards the standalone enrich_with_contract path.)
        vd, q, xml = _write_corr(tmp_path)
        first = enrich_contract(output_json=vd, questions_json=q, xml_path=xml)
        first_json = json.loads(vd.read_text(encoding="utf-8"))
        second = enrich_contract(output_json=vd, questions_json=q, xml_path=xml)
        second_json = json.loads(vd.read_text(encoding="utf-8"))
        assert second_json == first_json
        assert (
            second["summary"]["corrected_by_xml"]
            == first["summary"]["corrected_by_xml"]
            == 1
        )
        assert (
            second["summary"]["resolved_by_xml"] == first["summary"]["resolved_by_xml"]
        )
        assert (
            second["summary"]["resolved_by_xml_heuristic"]
            == first["summary"]["resolved_by_xml_heuristic"]
            == 1
        )
        assert second["variables"]["age"]["contract"]["corrected_from"] == "agee"
        assert (
            second["variables"]["Pre_xyz"]["contract"]["resolved_by"] == "xml-heuristic"
        )


# A `search()` select whose choice VALUES are dashed strings, with the XML naming
# the value/label COLUMNS of the attached CSV rather than listing literals -- the
# shape that made a sanitised wide column irreversible without the form's own
# choice universe. `-88` sits alongside as a genuine literal extra.
_STRCHOICE_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <model>
      <instance>
        <sform id="sform" version="1">
          <sites/>
        </sform>
      </instance>
      <bind nodeset="/sform/sites" type="select"/>
    </model>
  </h:head>
  <h:body>
    <select ref="/sform/sites"
            appearance="search('sitelist', 'matches', 'list_name', 'active')">
      <label>Sites</label>
      <item><label>site_label</label><value>site_id</value></item>
      <item><label>Other</label><value>-88</value></item>
    </select>
  </h:body>
</h:html>
"""

# `AB-12` sanitises to `AB_12`; the inactive row must NOT enter the universe,
# because the literal `list_name='active'` filter is applied.
_SITES_CSV = (
    "list_name,site_id,site_label\n"
    "active,AB-12,Alpha Beta 12\n"
    "active,CD-34,Charlie Delta 34\n"
    "inactive,ZZ-99,Should Not Appear\n"
)


class TestChoiceIndexFromAttachedCsv:
    """The choice index built from the real `search()` CSV, not an injected stub."""

    @pytest.fixture
    def contract(self, tmp_path):
        from surveycto_extractor.cli.enrich import _build_choice_index
        from surveycto_extractor.parsers.xml_contract import parse_contract

        (tmp_path / "sform.xml").write_text(_STRCHOICE_XML, encoding="utf-8")
        (tmp_path / "sitelist.csv").write_text(_SITES_CSV, encoding="utf-8")
        c = parse_contract(tmp_path / "sform.xml")
        c.choice_index = _build_choice_index(c.nodes, [tmp_path])
        return c

    def test_dashed_value_recovers_original_and_label(self, contract):
        m = contract.map_column("sites_AB_12")
        assert m["kind"] == "matched"
        # Without the index this is the sanitised token "AB_12"; the CSV is the
        # only thing that knows the dash.
        assert m["choice_code"] == "AB-12"
        assert m["choice_label"] == "Alpha Beta 12"

    def test_literal_extra_alongside_a_column_reference(self, contract):
        # `-88` is a real literal item, not a column name, so it stays a code.
        assert contract.map_column("sites__88")["choice_code"] == -88

    def test_literal_filter_keeps_another_list_out(self, contract):
        # The inactive row is filtered out, so its token is unknown and the
        # mapper falls back to carrying it verbatim rather than inventing a value.
        assert contract.map_column("sites_ZZ_99")["choice_code"] == "ZZ_99"

    def test_column_name_never_becomes_a_choice_value(self, contract):
        assert "site_id" not in [v for v, _ in contract.choice_index["sites"].values()]


_PLAIN_SM_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><f id="f" version="1"><sites/></f></instance>
    <bind nodeset="/f/sites" type="select"/>
  </model></h:head>
  <h:body>
    <select ref="/f/sites"><label>S</label>
      <item><label>Wheat flour</label><value>wheat-flour</value></item>
      <item><label>Other</label><value>-88</value></item>
    </select>
  </h:body>
</h:html>
"""

_SEARCH_SM_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><f id="f" version="1"><sites/></f></instance>
    <bind nodeset="/f/sites" type="select"/>
  </model></h:head>
  <h:body>
    <select ref="/f/sites"
            appearance="search('sitelist', 'matches', 'list_name', 'active')">
      <label>S</label>
      <item><label>site_label</label><value>site_id</value></item>
      <item><label>Other</label><value>-88</value></item>
    </select>
  </h:body>
</h:html>
"""


class TestChoiceIndexHardCases:
    """Cases from adversarial review; each one defeated the first version."""

    def _build(self, tmp_path, xml, csv_text=None, extra=None):
        from surveycto_extractor.cli.enrich import _build_choice_index
        from surveycto_extractor.parsers.xml_contract import parse_contract

        (tmp_path / "f.xml").write_text(xml, encoding="utf-8")
        if csv_text is not None:
            (tmp_path / "sitelist.csv").write_text(csv_text, encoding="utf-8")
        for nm, txt in (extra or {}).items():
            (tmp_path / nm).write_text(txt, encoding="utf-8")
        c = parse_contract(tmp_path / "f.xml")
        c.choice_index = _build_choice_index(c.nodes, [tmp_path])
        return c

    def test_plain_select_keeps_its_literal_string_choices(self, tmp_path):
        # The "non-numeric value names a COLUMN" rule holds only for search();
        # applied to a plain select it discarded every string-valued choice.
        c = self._build(tmp_path, _PLAIN_SM_XML)
        m = c.map_column("sites_wheat_flour")
        assert m["choice_code"] == "wheat-flour"
        assert m["choice_label"] == "Wheat flour"
        assert c.map_column("sites__88")["choice_code"] == -88

    def test_sanitisation_collision_abstains_instead_of_guessing(self, tmp_path):
        # `A-B` and `A.B` both render `A_B`. Dropping the entry sent the mapper to
        # its verbatim fallback, which returns `A_B` -- indistinguishable from a
        # resolved code, and possibly one of the two colliding values.
        c = self._build(
            tmp_path,
            _SEARCH_SM_XML,
            "list_name,site_id,site_label\nactive,A-B,One\nactive,A.B,Two\nactive,OK-1,Three\n",
        )
        m = c.map_column("sites_A_B")
        assert m["choice_code"] is None
        assert m["ambiguous_choice"] is True
        assert c.map_column("sites_OK_1")["choice_code"] == "OK-1"

    def test_absent_label_column_is_not_used_as_the_label(self, tmp_path):
        c = self._build(tmp_path, _SEARCH_SM_XML, "list_name,site_id\nactive,AB-12\n")
        m = c.map_column("sites_AB_12")
        assert m["choice_code"] == "AB-12"
        assert m.get("choice_label") is None

    @pytest.mark.parametrize(
        ("label", "csv_text"),
        [
            ("bom", "﻿list_name,site_id,site_label\nactive,AB-12,Alpha\n"),
            ("padded", "list_name, site_id ,site_label\nactive,AB-12,Alpha\n"),
            ("dup", "list_name,site_id,site_id,site_label\nactive,AB-12,ZZ-99,Alpha\n"),
        ],
    )
    def test_csv_header_pathologies(self, tmp_path, label, csv_text):
        # Each of these silently emptied or corrupted the index: a BOM lands
        # inside the first header name, a padded name fails every lookup, and a
        # duplicate name let a neighbouring column overwrite real values.
        c = self._build(tmp_path, _SEARCH_SM_XML, csv_text)
        assert c.map_column("sites_AB_12")["choice_code"] == "AB-12", label

    def test_exact_dataset_csv_beats_a_same_prefix_decoy(self, tmp_path):
        # `sitelist-2024.csv` sorts BEFORE `sitelist.csv`, so the glob's first
        # match was the wrong file and the whole universe came from the decoy.
        c = self._build(
            tmp_path,
            _SEARCH_SM_XML,
            "list_name,site_id,site_label\nactive,AB-12,Alpha\n",
            extra={
                "sitelist-2024.csv": "list_name,site_id,site_label\nactive,DECOY-1,No\n"
            },
        )
        assert c.map_column("sites_AB_12")["choice_code"] == "AB-12"

    def test_index_resolved_composite_integer_is_kept(self, tmp_path):
        # A declared value of `1_2` is a real choice. The composite-int
        # abstention must apply only when the index did NOT resolve the token,
        # or it discards the answer it just looked up.
        c = self._build(
            tmp_path,
            _SEARCH_SM_XML,
            "list_name,site_id,site_label\nactive,1_2,Composite\n",
        )
        m = c.map_column("sites_1_2")
        assert m["choice_code"] == "1_2"
        assert m.get("ambiguous_choice") is None

    def test_unresolved_composite_integer_still_abstains(self, tmp_path):
        c = self._build(
            tmp_path,
            _SEARCH_SM_XML,
            "list_name,site_id,site_label\nactive,AB-12,Alpha\n",
        )
        m = c.map_column("sites_9_9")
        assert m["choice_code"] is None
        assert m["ambiguous_choice"] is True
