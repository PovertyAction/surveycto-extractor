"""The preload layer is mandatory for a faithful simulation.

A form that pulls choices from a CSV (search() appearance) or reads pulldata()
cannot be simulated without that CSV -- the select's real options / the gate
inputs simply don't exist. Both are required; a missing source is a hard error,
not a silent fall-back to the static list.
"""

import pytest

from surveycto_extractor.extractors.pulldata_loader import (
    MissingPullDataError,
    load_pulldata_tables,
    scan_pulldata_refs,
)


def test_search_appearance_csv_is_scanned_as_mandatory():
    form = [
        {
            "variable_name": "town",
            "type": "select_one",
            "appearance": "search('towns', 'matches', 'region', ${region})",
        }
    ]
    assert "towns" in scan_pulldata_refs(form)["static"]


def test_missing_search_choice_csv_is_hard_error(tmp_path):
    form = [
        {
            "variable_name": "town",
            "type": "select_one",
            "appearance": "search('towns', 'matches', 'region', ${region})",
        }
    ]
    with pytest.raises(MissingPullDataError):
        load_pulldata_tables(form, [tmp_path])


def test_present_search_choice_csv_loads(tmp_path):
    (tmp_path / "towns.csv").write_text(
        "region,name\n1,Alpha\n2,Beta\n", encoding="utf-8"
    )
    form = [
        {
            "variable_name": "town",
            "type": "select_one",
            "appearance": "search('towns', 'matches', 'region', ${region})",
        }
    ]
    tables = load_pulldata_tables(form, [tmp_path])
    assert "towns" in tables


def test_missing_cases_pulldata_is_hard_error(tmp_path):
    form = [
        {
            "variable_name": "wave",
            "type": "calculate",
            "calculation": "pulldata('cases', 'wave', 'id', ${caseid})",
        }
    ]
    with pytest.raises(MissingPullDataError):
        load_pulldata_tables(form, [tmp_path])


def test_no_preload_refs_requires_nothing(tmp_path):
    form = [{"variable_name": "q1", "type": "integer", "constraint": ". >= 0"}]
    assert load_pulldata_tables(form, [tmp_path]) == {}
