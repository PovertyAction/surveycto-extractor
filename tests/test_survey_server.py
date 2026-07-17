"""Tests for mcp_server/survey_server.py robustness fixes (#29, #24).

The server module lives outside the import path and depends on the `mcp`
package, so it is loaded by path and skipped when mcp is unavailable.
"""

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

_SERVER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "surveycto_extractor"
    / "mcp_server"
    / "survey_server.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("survey_server_under_test", _SERVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


srv = _load_module()


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
                "summary": summary,
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
