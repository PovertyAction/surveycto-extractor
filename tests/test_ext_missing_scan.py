"""Tests for _scan_and_clean_extended_missings (create_variable_dictionaries).

pyreadstat returns Stata extended-missings (.d, .r, ...) as single-letter
strings mixed into numeric object columns. The scan NaNs and counts them --
but it must NOT do so to a genuine STRING column, where "n"/"y"/"d" are real
answers. The old code blanked the letters before checking whether the column
was numeric, destroying real data (#26.6).
"""

import pandas as pd

from surveycto_extractor.cli.vardict import _scan_and_clean_extended_missings


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


def test_all_single_letter_column_preserved():
    # Every value is a single letter (sex m/f, plus a 'd'), so masking them
    # leaves an all-NaN column that pd.to_numeric coerces without error. This
    # is a categorical column, NOT numeric-with-letter-missings -- it must
    # survive untouched and uncounted. (#26.6 -- the case the probe missed.)
    df = pd.DataFrame({"sex": ["m", "f", "m", "f", "d"]})
    out, counts = _scan_and_clean_extended_missings(df)
    assert out["sex"].tolist() == ["m", "f", "m", "f", "d"]
    assert "sex" not in counts


def test_yes_no_single_letter_column_preserved():
    df = pd.DataFrame({"consent": ["y", "n", "y", "n", "n"]})
    out, counts = _scan_and_clean_extended_missings(df)
    assert out["consent"].tolist() == ["y", "n", "y", "n", "n"]
    assert "consent" not in counts


def test_numeric_with_letters_still_cleaned_when_a_value_survives():
    # Guard must not over-fire: one surviving numeric value means the letters
    # really are extended-missing tags and should still be cleaned + counted.
    df = pd.DataFrame({"q": ["m", "f", "5", "d"]})
    out, counts = _scan_and_clean_extended_missings(df)
    assert out["q"].tolist()[2] == 5.0
    assert out["q"].isna().sum() == 3  # m, f, d -> NaN
    assert counts["q"] == {"m": 1, "f": 1, "d": 1}


def test_no_object_columns_is_noop():
    df = pd.DataFrame({"x": [1, 2, 3]})
    out, counts = _scan_and_clean_extended_missings(df)
    assert counts == {}
    assert out["x"].tolist() == [1, 2, 3]


def test_mixed_frame_only_numeric_col_cleaned():
    df = pd.DataFrame(
        {
            "num": ["1", "d", "2"],  # numeric-with-tags -> cleaned
            "txt": ["a", "b", "hello"],  # genuine string -> preserved
        }
    )
    out, counts = _scan_and_clean_extended_missings(df)
    assert "num" in counts and "txt" not in counts
    assert out["txt"].tolist() == ["a", "b", "hello"]
