"""Tests for the config loader: config.toml (primary) + config.py (fallback).

Covers path resolution relative to the config dir, sentinel/geo_bbox coercion,
the empty-table-is-override rule, baked-in column defaults, the legacy .py
fallback, and the absent/broken cases.
"""
import sys
from pathlib import Path

import pytest

from surveycto_extractor import config_loader
from surveycto_extractor.core import sentinels

_TOML = """
[surveys.hh]
input_file = "forms/hh.xlsx"
output_dir = "out/hh"
sections_dir = "out/hh/sections"
name = "HH"
max_section_depth = 3
external_choices_csv = ""
pulldata_search_dirs = ["media", "forms"]
geo_bbox = [-1.5, 4.2, 29.5, 35.0]

[datasets.hh]
data = "data/hh.dta"
questions_json = "out/hh/hh_questions.json"
output_json = "out/hh/hh_vd.json"
output_xlsx = "out/hh/hh_vd.xlsx"
skip_ord_dta = true
sumstats_dir_stata = "${root}/out/hh"

[sentinels]
scan_only = [-98, -97]
[sentinels.meanings]
"-99" = ["Don't know", ".d"]
"-88" = ["Refused", ".r"]
"""


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # No injected stub + fresh cache, so the .toml/.py branch actually runs.
    monkeypatch.delitem(sys.modules, "config", raising=False)
    config_loader._cache.clear()
    yield
    config_loader._cache.clear()


def _write(tmp_path, text, name="config.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_paths_resolved_relative_to_config_dir(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, _TOML))
    base = tmp_path.resolve()
    s = cfg.SURVEYS["hh"]
    assert s["input_file"] == base / "forms" / "hh.xlsx"
    assert isinstance(s["input_file"], Path)
    assert s["output_dir"] == base / "out" / "hh"
    d = cfg.DATASETS["hh"]
    assert d["data"] == base / "data" / "hh.dta"
    assert d["output_json"] == base / "out" / "hh" / "hh_vd.json"


def test_absolute_paths_kept(tmp_path):
    abs_path = (tmp_path / "elsewhere" / "form.xlsx").resolve()
    toml = f'[surveys.s]\ninput_file = "{abs_path.as_posix()}"\nname = "S"\n'
    cfg = config_loader.load_config(path=_write(tmp_path, toml))
    assert cfg.SURVEYS["s"]["input_file"] == abs_path


def test_non_path_and_empty_string_fields(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, _TOML))
    s = cfg.SURVEYS["hh"]
    assert s["name"] == "HH"  # str untouched
    assert s["max_section_depth"] == 3  # int untouched
    assert s["external_choices_csv"] is None  # "" -> None
    assert s["pulldata_search_dirs"] == [tmp_path.resolve() / "media", tmp_path.resolve() / "forms"]
    d = cfg.DATASETS["hh"]
    assert d["skip_ord_dta"] is True
    assert d["sumstats_dir_stata"] == "${root}/out/hh"  # Stata literal, NOT resolved


def test_geo_bbox_is_a_tuple(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, _TOML))
    assert cfg.SURVEYS["hh"]["geo_bbox"] == (-1.5, 4.2, 29.5, 35.0)


def test_sentinels_parsed(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, _TOML))
    assert cfg.SENTINEL_MEANINGS == {-99: ("Don't know", ".d"), -88: ("Refused", ".r")}
    assert cfg.SENTINEL_SCAN_ONLY == [-98, -97]
    # And it flows through the shared resolver.
    assert sentinels.resolve_sentinel_meanings(cfg) == cfg.SENTINEL_MEANINGS


def test_empty_meanings_table_is_a_deliberate_override(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, "[sentinels.meanings]\n"))
    assert cfg.SENTINEL_MEANINGS == {}
    # Empty override disables recoding (does NOT fall back to the default table).
    assert sentinels.resolve_sentinel_meanings(cfg) == {}


def test_absent_sentinels_uses_default(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, '[surveys.s]\nname = "S"\n'))
    assert not hasattr(cfg, "SENTINEL_MEANINGS")
    assert sentinels.resolve_sentinel_meanings(cfg) == sentinels.DEFAULT_SENTINEL_MEANINGS


def test_column_defaults_and_override(tmp_path):
    cfg = config_loader.load_config(path=_write(tmp_path, '[surveys.s]\nname = "S"\n'))
    assert cfg.SURVEY_COLUMNS[0] == "type"  # IPA default applied
    assert cfg.SYSTEM_PREFIXES == ["instanceID", "instanceName", "KEY", "SET-OF-"]
    # Distinct path -> not served from the by-path cache of the first load.
    cfg2 = config_loader.load_config(
        path=_write(tmp_path, '[columns]\nsystem_prefixes = ["KEY"]\n', "override.toml")
    )
    assert cfg2.SYSTEM_PREFIXES == ["KEY"]


def test_py_fallback(tmp_path):
    py = tmp_path / "config.py"
    py.write_text(
        "from pathlib import Path\n"
        "SURVEYS = {'s': {'input_file': Path('/x/f.xlsx'), 'name': 'S'}}\n"
        "DATASETS = {}\n",
        encoding="utf-8",
    )
    cfg = config_loader.load_config(path=py)
    assert cfg.SURVEYS["s"]["name"] == "S"
    assert config_loader.get_datasets(cfg) == {}


def test_absent_config_returns_none(tmp_path):
    assert config_loader.load_config(path=tmp_path / "nope.toml") is None


def test_broken_toml_raises(tmp_path):
    with pytest.raises(Exception):
        config_loader.load_config(path=_write(tmp_path, "this is = = not toml\n"))
