"""caseid-pool filter: restrict a simulation to a chosen case context.

For case-managed forms (caller-ID / screening), the survey body is gated by
`pulldata('cases', ...)` values (wave, total_phones). Restricting the caseid
pool to the bench-test cases makes those gates resolve the way they did for the
tester, so the synth reproduces the bench-test context.
"""

import pandas as pd
import pytest

from surveycto_extractor.extractors.pulldata_loader import PullDataTable
from surveycto_extractor.generators.synthetic_data import _build_caseid_pool

# a caseid-keyed pulldata ref so the pool is built from the 'id' column
_FORM = [
    {
        "variable_name": "wave",
        "type": "calculate",
        "calculation": "pulldata('cases', 'wave', 'id', ${caseid})",
    }
]
_TABLES = {
    "cases": PullDataTable(
        "cases",
        pd.DataFrame(
            {"id": ["BT001", "BT002", "P100", "P101"], "wave": ["1", "2", "1", "3"]}
        ),
    )
}


def test_pool_unfiltered_has_all_cases():
    assert _build_caseid_pool(_FORM, _TABLES) == ["BT001", "BT002", "P100", "P101"]


def test_prefix_filter_keeps_only_matching():
    assert _build_caseid_pool(_FORM, _TABLES, {"prefix": "BT"}) == ["BT001", "BT002"]


def test_prefix_is_case_insensitive():
    assert _build_caseid_pool(_FORM, _TABLES, {"prefix": "bt"}) == ["BT001", "BT002"]


def test_ids_filter_keeps_listed_only():
    assert _build_caseid_pool(_FORM, _TABLES, {"ids": ["P100"]}) == ["P100"]


def test_prefix_and_ids_combine():
    got = _build_caseid_pool(_FORM, _TABLES, {"prefix": "BT", "ids": ["BT002", "P100"]})
    assert got == ["BT002"]


def test_empty_result_is_hard_error():
    with pytest.raises(SystemExit):
        _build_caseid_pool(_FORM, _TABLES, {"prefix": "ZZ"})
