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

from surveycto_extractor.parsers.xml_contract import (
    FormContract,
    _parse_pulldata,
    _parse_search,
    build_contract,
    is_system_column,
    parse_contract,
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
        assert {
            "name",
            "fav_color",
            "langs",
            "pre_score",
            "member_name",
            "member_age",
        } <= names

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

    def test_negative_choice_code(self, contract):
        # langs__66 (dash->double-underscore) is select_multiple choice -66,
        # not a string-key fallback with choice_code None. (#23.1)
        m = contract.map_column("langs__66")
        assert m["kind"] == "matched"
        assert m["node_path"] == "langs"
        assert m["is_select_multiple"] is True
        assert m["choice_code"] == -66

    def test_leading_zero_choice_preserved_as_string(self, contract):
        # langs_01 keeps "01" as a string rather than collapsing to int 1. (#23.1)
        m = contract.map_column("langs_01")
        assert m["choice_code"] == "01"

    def test_ragged_under_indexed_repeat_flagged(self, contract):
        # A repeat field with fewer indices than its depth is flagged ragged
        # instead of silently dropping the level. (#23.2)
        m = contract.map_column("member_name")  # depth 1, zero indices
        assert m.get("ragged") is True

    def test_ragged_over_indexed_repeat_flagged(self, contract):
        # More indices than the node's depth must ALSO be flagged: the zip
        # truncation would otherwise drop the surplus index silently. Was only
        # flagged for the under-indexed case (< n_iter). (#23.2 / review #11)
        m = contract.map_column("member_name_3_4")  # depth 1, two indices
        assert m["node_path"] == "members/member_name"
        assert m.get("ragged") is True


class TestBalancedArgParsing:
    """#23.4 -- search()/pulldata() args parsed balanced + quote-aware."""

    def test_search_value_with_close_paren(self):
        r = _parse_search("search('choices', 'matches', 'list', 'a)b')")
        assert r["filter"] == {"list": "a)b"}

    def test_pulldata_nested_concat_value_intact(self):
        r = _parse_pulldata("pulldata('ds', concat('a', 'b'), 'k', node)")
        assert r["dataset"] == "ds"
        assert r["value"] == "concat('a', 'b')"
        assert r["key"] == "k"

    def test_search_dynamic_node_ref_value_is_none(self):
        r = _parse_search("search('choices', 'matches', 'col', node_ref)")
        assert r["filter"] == {"col": None}


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

    def test_select_multiple_inside_repeat_choice_is_first(self, nested):
        # SurveyCTO wide export orders the choice code FIRST, then the repeat
        # chain: base_<choice>_<outer>_<inner>. This is NOT the code's own
        # assumption -- it is the convention the production Phase-4 matcher
        # (create_variable_dictionaries.py:745-752, choice=first group) and the
        # concordance-validated synthetic generator (synthetic_data.py emits
        # `_<choice>{repeat suffix}`) both use against real exports. So for
        # choice=7 in repeat (hh=2, member=3) the column is `mlangs_7_2_3`.
        m = nested.map_column("mlangs_7_2_3")
        assert m["choice_code"] == 7
        assert m["repeat_iterations"] == [("hh", 2), ("member", 3)]
        assert m["is_select_multiple"] is True

    def test_single_repeat_decode_agrees_with_phase4(self, tmp_path):
        # Cross-check the single-repeat select_multiple decode against the actual
        # Phase-4 regex (base_(\d+)_(\d+): group1=choice, group2=repeat) so the
        # two paths can't silently diverge again.
        import re

        xml = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms" xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance><sf id="sf"><roster jr:template=""><lang/></roster></sf></instance>
    <bind nodeset="/sf/roster/lang" type="select"/>
  </model></h:head>
  <h:body><group ref="/sf/roster"><repeat nodeset="/sf/roster">
    <select ref="/sf/roster/lang"/>
  </repeat></group></h:body></h:html>"""
        p = tmp_path / "sf.xml"
        p.write_text(xml, encoding="utf-8")
        c = parse_contract(p)
        m = c.map_column("lang_2_5")
        ph = re.match(r"lang_(\d+)_(\d+)$", "lang_2_5")
        phase4_choice, phase4_repeat = int(ph.group(1)), int(ph.group(2))
        assert m["choice_code"] == phase4_choice == 2
        assert m["repeat_iterations"] == [("roster", phase4_repeat)] == [("roster", 5)]


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


# A select_multiple whose choice VALUES are strings, not codes. SurveyCTO renders
# a choice value into a wide column name by replacing every [^A-Za-z0-9_] with an
# underscore, so a `search()`-sourced key like `AB-12` arrives as `AB_12` -- and a
# value that contains digits arrives with those digits looking exactly like repeat
# iteration suffixes. `tags` sits inside a repeat so the two can collide.
_XML_STRING_CHOICES = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head>
    <h:title>Str Form</h:title>
    <model>
      <instance>
        <strform id="strform" version="2026010101">
          <langs/>
          <members jr:template="">
            <tags/>
          </members>
          <meta><instanceID/></meta>
        </strform>
      </instance>
      <bind nodeset="/strform/langs" type="select"/>
      <bind nodeset="/strform/members/tags" type="select"/>
      <bind nodeset="/strform/meta/instanceID" type="string"/>
    </model>
  </h:head>
  <h:body>
    <select ref="/strform/langs"
            appearance="search('choices', 'matches', 'list_name', 'langlist')">
      <label>Languages</label>
    </select>
    <group ref="/strform/members">
      <repeat nodeset="/strform/members">
        <select ref="/strform/members/tags"
                appearance="search('tagsets', 'matches', 'list_name', 'taglist')">
          <label>Tags</label>
        </select>
      </repeat>
    </group>
  </h:body>
</h:html>
"""


@pytest.fixture
def strform(tmp_path) -> FormContract:
    p = tmp_path / "strform.xml"
    p.write_text(_XML_STRING_CHOICES, encoding="utf-8")
    return parse_contract(p)


class TestStringValuedChoiceCodes:
    """A select_multiple choice value that is not a bare integer.

    The old mapper peeled EVERY trailing `_<int>` as a repeat index before it
    looked at the node, then required the remainder to be a node name. That holds
    only when the choice value is a bare positive integer, so a string-valued
    choice fell into a fallback that hardcoded `choice_code = None` -- and, when
    the value contained digits, read the value's OWN digits as repeat iterations.
    Downstream, `vardict` gates per-choice resolution on `choice_code` being
    non-null, so the binary was labelled with its question's whole choice list
    instead of its own choice.
    """

    def test_string_choice_is_reported_not_discarded(self, strform):
        m = strform.map_column("langs_ES")
        assert m["kind"] == "matched"
        assert m["is_select_multiple"] is True
        # Was None, with the value demoted to a `string_key` heuristic.
        assert m["choice_code"] == "ES"

    def test_multi_token_string_choice_still_matches(self, strform):
        # Was `unmapped` outright: `langs_en` is not a node, so nothing matched.
        m = strform.map_column("langs_en_GB")
        assert m["kind"] == "matched"
        assert m["node_path"] == "langs"
        assert m["choice_code"] == "en_GB"

    def test_choice_digits_are_not_read_as_repeat_iterations(self, strform):
        """The regression that corrupts rather than merely omits.

        `tags` has repeat depth 1, so exactly ONE trailing integer token is an
        iteration index. The old mapper peeled `3013` (part of the choice value)
        and reported it as the iteration, silently dropping the real one.
        """
        m = strform.map_column("tags_KAR2019_3013_2")
        assert m["kind"] == "matched"
        assert m["repeat_iterations"] == [("members", 2)]
        assert m["choice_code"] == "KAR2019_3013"

    def test_numeric_choice_codes_are_unchanged(self, strform):
        # The shapes that already worked must keep their exact previous values,
        # ints included -- this is what keeps the dictionary output stable.
        assert strform.map_column("langs_1")["choice_code"] == 1
        assert strform.map_column("langs__66")["choice_code"] == -66
        assert strform.map_column("langs_01")["choice_code"] == "01"
        inner = strform.map_column("tags_1_2")
        assert inner["choice_code"] == 1
        assert inner["repeat_iterations"] == [("members", 2)]

    def test_unknowable_choice_index_boundary_abstains(self, strform):
        """`tags_1_1_2`: depth 1, so `1_1` is left over as the choice value.

        A real numeric choice value sanitises to exactly ONE token, so a
        multi-token all-integer remainder means the choice/index boundary cannot
        be recovered from the contract. Abstain and say so rather than emit a
        fabricated out-of-domain code.
        """
        m = strform.map_column("tags_1_1_2")
        assert m["choice_code"] is None
        assert m["ambiguous_choice"] is True

    def test_choice_index_recovers_the_original_value_and_label(self, tmp_path):
        """With the choice universe supplied, the ORIGINAL value comes back.

        The sanitised token is lossy (`AB-12` and `AB_12` both render `AB_12`),
        so the parser stays dependency-free and the caller injects the universe
        it already resolves for labels.
        """
        p = tmp_path / "strform.xml"
        p.write_text(_XML_STRING_CHOICES, encoding="utf-8")
        c = parse_contract(p)
        c.choice_index = {"langs": {"AB_12": ("AB-12", "Alpha Beta 12")}}
        m = c.map_column("langs_AB_12")
        assert m["choice_code"] == "AB-12"
        assert m["choice_label"] == "Alpha Beta 12"


# Node selection must consider whether the leftover tokens COULD be integer
# indices, not just how many there are. Both forms below come from adversarial
# review of the first version of this mapper; each defeated it.
_XML_HOMONYM = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance>
      <f id="f" version="1">
        <langs/>
        <hh jr:template=""><langs/></hh>
      </f>
    </instance>
    <bind nodeset="/f/langs" type="select"/>
    <bind nodeset="/f/hh/langs" type="string"/>
  </model></h:head>
  <h:body>
    <select ref="/f/langs"><label>L</label></select>
    <group ref="/f/hh"><repeat nodeset="/f/hh">
      <input ref="/f/hh/langs"><label>L2</label></input>
    </repeat></group>
  </h:body>
</h:html>
"""

_XML_SUFFIX_NAME = """<?xml version="1.0"?>
<h:html xmlns="http://www.w3.org/2002/xforms"
        xmlns:h="http://www.w3.org/1999/xhtml"
        xmlns:jr="http://openrosa.org/javarosa">
  <h:head><model>
    <instance>
      <f id="f" version="1">
        <x/>
        <H jr:template=""><J jr:template=""><x_1/></J></H>
      </f>
    </instance>
    <bind nodeset="/f/x" type="select"/>
    <bind nodeset="/f/H/J/x_1" type="string"/>
  </model></h:head>
  <h:body>
    <select ref="/f/x"><label>X</label></select>
    <group ref="/f/H"><repeat nodeset="/f/H">
      <group ref="/f/H/J"><repeat nodeset="/f/H/J">
        <input ref="/f/H/J/x_1"><label>X1</label></input>
      </repeat></group>
    </repeat></group>
  </h:body>
</h:html>
"""


class TestNodeSelectionFeasibility:
    """A candidate node needs enough trailing INTEGER tokens to own the column."""

    def _c(self, tmp_path, xml):
        p = tmp_path / "f.xml"
        p.write_text(xml, encoding="utf-8")
        return parse_contract(p)

    def test_homonym_in_repeat_does_not_steal_a_string_choice(self, tmp_path):
        """`langs_ES`: `ES` can never be an iteration index.

        Scoring by token COUNT gave the depth-1 `hh/langs` a perfect fit and lost
        the choice entirely -- reproducing, with a homonym present, the exact bug
        the node-anchored rewrite was written to fix.
        """
        c = self._c(tmp_path, _XML_HOMONYM)
        m = c.map_column("langs_ES")
        assert m["node_path"] == "langs"
        assert m["choice_code"] == "ES"
        assert m.get("string_key") is None
        assert c.map_column("langs_KAR2019")["choice_code"] == "KAR2019"

    def test_exact_depth_fit_still_wins_when_the_token_is_an_integer(self, tmp_path):
        # `langs_1` is genuinely ambiguous -- choice 1 of the depth-0 select, or
        # iteration 1 of the depth-1 homonym. The exact-depth fit is preferred,
        # which is the pre-existing behaviour and stays deliberate.
        c = self._c(tmp_path, _XML_HOMONYM)
        m = c.map_column("langs_1")
        assert m["node_path"] == "hh/langs"
        assert m["repeat_iterations"] == [("hh", 1)]

    def test_suffix_shaped_node_name_does_not_steal_a_choice_binary(self, tmp_path):
        """`x_1` cannot belong to a depth-2 node named `x_1`.

        That node's columns always carry two trailing index tokens (`x_1_1_1`),
        so bare `x_1` is the depth-0 select_multiple's choice-1 binary. Taking the
        longest name match unconditionally got this wrong in both directions.
        """
        c = self._c(tmp_path, _XML_SUFFIX_NAME)
        m = c.map_column("x_1")
        assert m["node_path"] == "x"
        assert m["choice_code"] == 1

    def test_the_deep_node_still_wins_when_its_indices_are_present(self, tmp_path):
        c = self._c(tmp_path, _XML_SUFFIX_NAME)
        m = c.map_column("x_1_1_1")
        assert m["node_path"] == "H/J/x_1"
        assert m["repeat_iterations"] == [("H", 1), ("J", 1)]

    def test_underscore_only_token_is_not_a_choice(self, tmp_path):
        # `x___` was reporting a choice value of `__`. Sanitisation always leaves
        # at least one alphanumeric, so an all-underscore token is not a value.
        c = self._c(tmp_path, _XML_HOMONYM)
        assert c.map_column("langs___")["choice_code"] is None
        assert c.map_column("langs_")["choice_code"] is None
        # A negative sentinel keeps its digits and must be unaffected.
        assert c.map_column("langs__66")["choice_code"] == -66
