"""Tests for _scan_and_clean_extended_missings (create_variable_dictionaries).

pyreadstat returns Stata extended-missings (.d, .r, ...) as single-letter
strings mixed into numeric object columns. The scan NaNs and counts them --
but it must NOT do so to a genuine STRING column, where "n"/"y"/"d" are real
answers. The old code blanked the letters before checking whether the column
was numeric, destroying real data (#26.6)."""
import numpy as np
import pandas as pd

from create_variable_dictionaries import _scan_and_clean_extended_missings


def test_numeric_column_letters_cleaned_and_counted():
    df = pd.DataFrame({"age": ["25", "30", "d", "r", "d"]})
    out, counts = _scan_and_clean_extended_missings(df)
    # Letters removed, column coerced to numeric, values preserved.
    assert out["age"].tolist()[:2] == [25.0, 30.0]
    assert out["age"].isna().sum() == 3
    assert counts["age"] == {"d": 2, "r": 1}


def test_genuine_string_column_preserved():
    # A text question answered with single letters -- these are REAL answers,
    # not extended missings, and must survive untouched and uncounted.
    df = pd.DataFrame({"initial": ["n", "y", "d", "maria", "n"]})
    out, counts = _scan_and_clean_extended_missings(df)
    assert out["initial"].tolist() == ["n", "y", "d", "maria", "n"]
    assert "initial" not in counts


def test_no_object_columns_is_noop():
    df = pd.DataFrame({"x": [1, 2, 3]})
    out, counts = _scan_and_clean_extended_missings(df)
    assert counts == {}
    assert out["x"].tolist() == [1, 2, 3]


def test_mixed_frame_only_numeric_col_cleaned():
    df = pd.DataFrame({
        "num": ["1", "d", "2"],       # numeric-with-tags -> cleaned
        "txt": ["a", "b", "hello"],   # genuine string -> preserved
    })
    out, counts = _scan_and_clean_extended_missings(df)
    assert "num" in counts and "txt" not in counts
    assert out["txt"].tolist() == ["a", "b", "hello"]
