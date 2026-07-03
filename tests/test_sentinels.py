"""Tests for the shared sentinel-code table (`sentinels.py`).

The table is the single source of truth both create_variable_dictionaries.py
and load_survey_metadata.py read, resolving the old cross-module disagreement
(#26.8) where -99 meant "Refused" in one place and "Don't know" in the other.
It is also config-overridable, since sentinel codes are a project convention.
"""
import types

from core import sentinels as s


class TestDefaults:
    def test_ipa_default_meanings(self):
        assert s.sentinel_meaning("-99") == "Don't know"
        assert s.sentinel_meaning("-88") == "Refused to answer"
        assert s.sentinel_meaning("-77") == "Not applicable"

    def test_non_sentinel_returns_none(self):
        assert s.sentinel_meaning("1") is None
        assert s.sentinel_meaning("abc") is None
        assert s.sentinel_meaning("") is None

    def test_scan_codes_include_scan_only(self):
        # -98 is scanned/counted but has no recode target.
        assert -98 in s.sentinel_scan_codes()
        assert -99 in s.sentinel_scan_codes()

    def test_mvdecode_spec_byte_identical_to_historical(self):
        # Must match the previously hand-written recode for the default table,
        # so the generated do-file stays byte-identical.
        assert s.mvdecode_spec() == "-99=.d \\ -88=.r \\ -77=.n \\ -66=.o \\ -55=.m"


class TestConfigOverride:
    def test_wholesale_override(self):
        cfg = types.SimpleNamespace(
            SENTINEL_MEANINGS={-1: ("Missing", ".m"), -2: ("Skipped", ".s")})
        meanings = s.resolve_sentinel_meanings(cfg)
        assert s.sentinel_meaning("-1", meanings) == "Missing"
        assert s.sentinel_meaning("-99", meanings) is None  # default is gone
        assert s.mvdecode_spec(meanings) == "-2=.s \\ -1=.m"

    def test_no_override_falls_back_to_default(self):
        cfg = types.SimpleNamespace()
        assert s.resolve_sentinel_meanings(cfg) == s.DEFAULT_SENTINEL_MEANINGS

    def test_none_config_is_default(self):
        assert s.resolve_sentinel_meanings(None) == s.DEFAULT_SENTINEL_MEANINGS
