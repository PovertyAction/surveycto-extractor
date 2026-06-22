"""Tests for the compiled-XForm contract parser (`xml_contract.py`).

A small synthetic XForm exercises the parts the overlay relies on: primary-
instance detection, repeat (`jr:template`) discovery, bind types, `pulldata`
provenance, select_multiple vs select_one, and the `search()` choice source.
Then `map_column` is checked on the wide-column shapes that fuzzy matching
struggles with (repeat `_N` suffixes, select_multiple choice binaries, system
and unmapped columns).
"""
import json

import pytest

from xml_contract import (
    FormContract,
    parse_contract,
    build_contract,
    is_system_column,
    _parse_pulldata,
    _parse_search,
)

# A minimal but representative compiled XForm:
#   - primary instance <myform id="myform" version="...">
#   - select_one (fav_color), select_multiple (langs, search-from-file)
#   - pulldata preload (pre_score)
#   - a repeat group (members) with two leaf fields
_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <h:title>My Form</h:title>
    <model>
      <instance>
        <myform id="myform" version="2024010101">
          <name/>
          <fav_color/>
          <langs/>
          <pre_score/>
          <members jr:template="">
            <member_name/>
            <member_age/>
          </members>
          <meta>
            <instanceID/>
          </meta>
        </myform>
      </instance>
      <bind nodeset="/myform/name" type="string"/>
      <bind nodeset="/myform/fav_color" type="string"/>
      <bind nodeset="/myform/langs" type="select"/>
      <bind nodeset="/myform/pre_score" type="int"
            calculate="pulldata('preload', 'score', 'id', /myform/name)"/>
      <bind nodeset="/myform/members/member_name" type="string"/>
      <bind nodeset="/myform/members/member_age" type="int"/>
      <bind nodeset="/myform/meta/instanceID" type="string"/>
    </model>
  </h:head>
  <h:body>
    <select1 ref="/myform/fav_color"><label>Color</label></select1>
    <select ref="/myform/langs"
            appearance="search('choices', 'matches', 'list_name', 'langlist')">
      <label>Languages</label>
    </select>
    <group ref="/myform/members">
      <repeat nodeset="/myform/members">
        <input ref="/myform/members/member_name"><label>Name</label></input>
        <input ref="/myform/members/member_age"><label>Age</label></input>
      </repeat>
    </group>
  </h:body>
</h:html>
"""


@pytest.fixture
def contract(tmp_path) -> FormContract:
    p = tmp_path / "myform.xml"
    p.write_text(_XML, encoding="utf-8")
    return parse_contract(p)


class TestParseContract:
    def test_form_identity(self, contract):
        assert contract.formid == "myform"
        assert contract.formdef_version == "2024010101"

    def test_nodes_discovered(self, contract):
        names = {n.name for n in contract.nodes.values()}
        assert {"name", "fav_color", "langs", "pre_score",
                "member_name", "member_age"} <= names

    def test_repeat_detected(self, contract):
        assert "members" in contract.repeat_groups
        member = contract.nodes["members/member_name"]
        assert member.repeat_path == ["members"]
        assert member.repeat_depth == 1

    def test_select_multiple_vs_single(self, contract):
        assert contract.nodes["langs"].is_select_multiple is True
        assert contract.nodes["fav_color"].is_select_multiple is False
        assert contract.nodes["fav_color"].control == "select_one"

    def test_pulldata_provenance(self, contract):
        node = contract.nodes["pre_score"]
        assert node.pulldata == {"dataset": "preload", "value": "'score'", "key": "id"}
        assert node.data_source["kind"] == "pulldata"

    def test_search_data_source(self, contract):
        ds = contract.nodes["langs"].data_source
        assert ds["kind"] == "search"
        assert ds["dataset"] == "choices"
        assert ds["filter"] == {"list_name": "langlist"}

    def test_types(self, contract):
        # nodes are keyed by full slash-path, not leaf token
        assert contract.nodes["members/member_age"].xml_type == "int"
        assert contract.nodes["name"].xml_type == "string"


class TestMapColumn:
    def test_plain_field(self, contract):
        m = contract.map_column("name")
        assert m["kind"] == "matched"
        assert m["node_path"] == "name"
        assert m["repeat_iterations"] == []
        assert m["choice_code"] is None

    def test_select_multiple_choice_binary(self, contract):
        m = contract.map_column("langs_1")
        assert m["kind"] == "matched"
        assert m["node_path"] == "langs"
        assert m["is_select_multiple"] is True
        assert m["choice_code"] == 1

    def test_repeat_iteration_suffix(self, contract):
        m = contract.map_column("member_name_3")
        assert m["kind"] == "matched"
        assert m["node_path"] == "members/member_name"
        assert m["repeat_iterations"] == [("members", 3)]
        assert m["choice_code"] is None

    def test_system_column(self, contract):
        assert contract.map_column("KEY")["kind"] == "system"
        assert contract.map_column("SubmissionDate")["kind"] == "system"

    def test_unmapped_column(self, contract):
        m = contract.map_column("not_a_real_field")
        assert m["kind"] == "unmapped"


class TestBuildContractIO:
    def test_writes_json(self, tmp_path):
        xml = tmp_path / "myform.xml"
        xml.write_text(_XML, encoding="utf-8")
        out = tmp_path / "out" / "myform_contract.json"
        c = build_contract(xml, out)
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["formid"] == "myform"
        assert data["n_nodes"] == len(c.nodes)
        assert "members" in data["repeat_groups"]


_NESTED_XML = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <model>
      <instance>
        <nf id="nf" version="1">
          <hh jr:template="">
            <member jr:template="">
              <mname/>
              <mlangs/>
            </member>
          </hh>
        </nf>
      </instance>
      <bind nodeset="/nf/hh/member/mname" type="string"/>
      <bind nodeset="/nf/hh/member/mlangs" type="select"/>
    </model>
  </h:head>
  <h:body>
    <group ref="/nf/hh"><repeat nodeset="/nf/hh">
      <group ref="/nf/hh/member"><repeat nodeset="/nf/hh/member">
        <input ref="/nf/hh/member/mname"/>
        <select ref="/nf/hh/member/mlangs"/>
      </repeat></group>
    </repeat></group>
  </h:body>
</h:html>
"""


class TestNestedRepeats:
    @pytest.fixture
    def nested(self, tmp_path):
        p = tmp_path / "nf.xml"
        p.write_text(_NESTED_XML, encoding="utf-8")
        return parse_contract(p)

    def test_two_level_repeat_depth(self, nested):
        n = nested.nodes["hh/member/mname"]
        assert n.repeat_path == ["hh", "member"]  # outer -> inner
        assert n.repeat_depth == 2

    def test_two_level_repeat_column(self, nested):
        m = nested.map_column("mname_2_5")
        assert m["repeat_iterations"] == [("hh", 2), ("member", 5)]
        assert m["choice_code"] is None

    def test_select_multiple_inside_nested_repeat_choice_is_last(self, nested):
        # The wide column orders repeat indices first, choice code LAST:
        # mlangs_<hh>_<member>_<choice>. This locks that ordering assumption.
        m = nested.map_column("mlangs_2_5_1")
        assert m["repeat_iterations"] == [("hh", 2), ("member", 5)]
        assert m["choice_code"] == 1
        assert m["is_select_multiple"] is True


class TestDeterminismAndSecondaryInstance:
    def test_homonym_resolution_is_deterministic(self, tmp_path):
        # Two leaves named `income` in different groups. The candidate order (and
        # thus which node a bare `income` column maps to) must be stable, not
        # PYTHONHASHSEED-dependent.
        xml = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance><af id="af" version="1"><ga><income/></ga><gb><income/></gb></af></instance>
    <bind nodeset="/af/ga/income" type="int"/>
    <bind nodeset="/af/gb/income" type="string"/>
  </model></h:head>
  <h:body/></h:html>"""
        p = tmp_path / "af.xml"
        p.write_text(xml, encoding="utf-8")
        c = parse_contract(p)
        assert c._by_name["income"] == sorted(c._by_name["income"])  # stable order
        m = c.map_column("income")
        assert m["node_path"] == "ga/income"  # lexicographically-first, deterministic
        assert m["ambiguous"] is True

    def test_secondary_instance_does_not_hijack(self, tmp_path):
        # A select_from_file / lookup compiles to a SECONDARY <instance id="...">
        # whose child can satisfy id==tag. The primary <instance> (no id) must win.
        xml = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml">
  <h:head><model>
    <instance id="citychoices">
      <citychoices id="citychoices"><item><name/></item></citychoices>
    </instance>
    <instance>
      <realform id="realform" version="2"><hh_income/></realform>
    </instance>
    <bind nodeset="/realform/hh_income" type="int"/>
  </model></h:head>
  <h:body/></h:html>"""
        p = tmp_path / "rf.xml"
        p.write_text(xml, encoding="utf-8")
        c = parse_contract(p)
        assert c.formid == "realform"
        assert c.map_column("hh_income")["kind"] == "matched"


class TestPureHelpers:
    def test_is_system_column(self):
        assert is_system_column("KEY")
        assert is_system_column("meta-instanceID")
        assert is_system_column("foo-Comment")
        assert not is_system_column("hh_income")

    def test_parse_pulldata(self):
        pd = _parse_pulldata("pulldata('roster', 'name', 'hhid', ${x})")
        assert pd == {"dataset": "roster", "value": "'name'", "key": "hhid"}
        assert _parse_pulldata("today()") is None

    def test_parse_search(self):
        s = _parse_search("search('choices', 'matches', 'list_name', 'crops')")
        assert s["dataset"] == "choices"
        assert s["filter"] == {"list_name": "crops"}
        assert _parse_search("minimal") is None
