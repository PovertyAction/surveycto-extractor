"""Long repeat tables: ``<stem>_long/<repeat>.parquet`` beside the ``.dta``.

The Stata export is wide: a field in a repeat has one column per instance
(``member_age_1``, ``member_age_2``; nested, ``job_wage_1_2``). Each repeat gets one
table with one row per instance that has any value, keyed by the submission ``KEY``
column (``key`` in a Stata export) and one ``<repeat>_index`` per repeat from the
outermost down
(``key, member_index, job_index``). A column keeps its wide name without the
instance suffix: ``member_age_2`` -> ``member_age``; a select_multiple binary keeps
its choice, ``tasks_3_2`` -> ``tasks_3``.

The instance numbers come from the column name, after the question's name (and a
select_multiple binary's choice), one per repeat the question sits in. When one of
a question's columns does not read that way (Stata truncated it at 32 characters, or
another form version wrote it with more or fewer numbers), all its columns stay wide
only, so a long column holds every instance or is not there.
"""

import re
from functools import reduce
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from surveycto_extractor.parsers.xml_contract import sanitize_choice_value
from surveycto_extractor.pii import key_column

_INDICES = re.compile(r"_?\d+(?:_\d+)*")


def long_dir(parquet: Path) -> Path:
    """Return the directory of ``parquet``'s long tables (``<stem>_long``)."""
    return parquet.with_name(f"{parquet.stem}_long")


def repeat_parents(questions: list[dict]) -> dict[str, str | None]:
    """Return each repeat's enclosing repeat (None at the top level).

    Every repeat has a ``repeat_count`` question at its parent's level, so the
    nearest repeat in that question's group path is the parent.
    """
    if any(
        q.get("type") == "repeat_count" and not q.get("repeat_group_name")
        for q in questions
    ):
        raise SystemExit(
            "questions.json predates repeat_group_name: rerun surveycto-extract"
        )
    counts = {
        q["repeat_group_name"]: q for q in questions if q.get("type") == "repeat_count"
    }
    return {
        name: next((g for g in reversed(q["group_path"]) if g in counts), None)
        for name, q in counts.items()
    }


def index_columns(repeat: str, parents: dict[str, str | None]) -> list[str]:
    """Return ``<repeat>_index`` for each repeat from the outermost to ``repeat``."""
    chain = []
    while repeat is not None:
        chain.insert(0, repeat)
        repeat = parents[repeat]
    return [f"{r}_index" for r in chain]


def _choice_suffix(entry: dict) -> str:
    """Return the part of a select_multiple binary's name that names its choice."""
    survey = entry.get("survey") or {}
    contract = entry.get("contract") or {}
    if contract.get("is_select_multiple") and contract.get("choice_code") is not None:
        code = str(contract["choice_code"])
    elif survey.get("type") == "select_multiple" and isinstance(
        survey.get("choices"), dict
    ):
        code = str(survey["choices"]["value"])
    else:
        return ""
    # SurveyCTO writes a character a Stata name cannot hold as "_": -99 -> __99.
    return "_" + sanitize_choice_value(code)


def placement(questions: list[dict], variables: dict) -> tuple[dict, list[str]]:
    """Place each repeat column in its repeat's long table.

    Returns ``{column: (repeat, long_name, instance_numbers)}`` and the repeat
    columns that stay wide only, because their names do not carry one instance
    number per enclosing repeat. A nested repeat's count sits in its parent's
    instances (``jobs_count_2``), so it goes in the parent's table.
    """
    parents = repeat_parents(questions)
    chain_of = {
        q["variable_name"]: [g for g in q["group_path"] if g in parents]
        for q in questions
    }
    placed, unread = {}, set()
    question_of = {}
    for column, entry in variables.items():
        question = (entry.get("survey") or {}).get("original_variable_name")
        chain = chain_of.get(question)
        if not chain:
            continue
        question_of[column] = question
        stem = question + _choice_suffix(entry)
        rest = column[len(stem) :] if column.startswith(stem) else ""
        numbers = rest.lstrip("_").split("_") if _INDICES.fullmatch(rest) else []
        if len(numbers) != len(chain):
            unread.add(question)
            continue
        placed[column] = (chain[-1], column[: len(stem)], [int(n) for n in numbers])
    # A long column holds every instance or is not there: one unread column keeps
    # all of its question's columns wide.
    wide = [c for c, q in question_of.items() if q in unread]
    return {c: p for c, p in placed.items() if question_of[c] not in unread}, wide


def _has_value(column: pa.ChunkedArray) -> pa.ChunkedArray:
    valid = pc.is_valid(column)
    if pa.types.is_string(column.type) or pa.types.is_large_string(column.type):
        # Stata stores a missing string as "".
        valid = pc.and_(valid, pc.fill_null(pc.not_equal(column, ""), False))
    return valid


def _empty(column: pa.ChunkedArray) -> bool:
    return pc.any(_has_value(column)).as_py() is not True


def long_table(
    table: pa.Table, key: str, columns: dict[str, tuple[str, list[int]]], keys: list
) -> pa.Table:
    """Stack wide ``columns`` into one row per instance that has any value.

    ``columns`` maps each wide column to its long name and instance numbers, outer
    first; ``keys`` is ``key`` then one index column per instance number.
    """
    by_instance: dict[tuple[int, ...], list[str]] = {}
    for column, (_, numbers) in columns.items():
        by_instance.setdefault(tuple(numbers), []).append(column)
    parts = []
    for numbers, wide in sorted(by_instance.items()):
        rows = table.select([key, *wide])
        rows = rows.filter(reduce(pc.or_, [_has_value(rows[c]) for c in wide]))
        index = [pa.array([n] * rows.num_rows, pa.int32()) for n in numbers]
        # An instance column with no value (Stata types an empty column on its
        # own, "" or NaN) takes the type of the instances that have values.
        values = [
            pa.nulls(rows.num_rows) if _empty(rows[c]) else rows[c].combine_chunks()
            for c in wide
        ]
        parts.append(
            pa.Table.from_arrays(
                [rows[key].combine_chunks(), *index, *values],
                names=[*keys, *(columns[c][0] for c in wide)],
            )
        )
    long = pa.concat_tables(parts, promote_options="permissive")
    return long.sort_by([(k, "ascending") for k in keys])


def _kind(dtype: pa.DataType) -> str | None:
    if pa.types.is_null(dtype):
        return None
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        return "text"
    if pa.types.is_integer(dtype) or pa.types.is_floating(dtype):
        return "number"
    return str(dtype)


def _mixed(placed: dict, table: pa.Table) -> list[str]:
    """Return the placed columns whose long column would mix text and numbers.

    SurveyCTO's do-file destrings each wide column alone, so one instance of a field
    can be numeric and another text; neither type holds both. A column with no value
    has no kind.
    """
    kinds: dict[tuple[str, str], set] = {}
    for column, (repeat, name, _) in placed.items():
        kind = None if _empty(table[column]) else _kind(table[column].type)
        kinds.setdefault((repeat, name), set()).add(kind)
    mixed = {k for k, found in kinds.items() if len(found - {None}) > 1}
    return [c for c, (r, name, _) in placed.items() if (r, name) in mixed]


def write_long_tables(questions: list[dict], vardict: dict, parquet: Path) -> Path:
    """Write ``<stem>_long/<repeat>.parquet`` for every repeat; return the directory.

    A repeat none of whose columns could be placed gets no table. Raises when two
    columns land on the same long column, or one is named like a key.
    """
    parents = repeat_parents(questions)
    placed, wide = placement(questions, vardict["variables"])
    out = long_dir(parquet)
    for old in out.glob("*.parquet"):
        old.unlink()
    if not parents:
        if out.exists():
            out.rmdir()
        return out
    out.mkdir(exist_ok=True)
    table = pq.read_table(parquet)
    key = key_column(table.column_names)
    mixed = _mixed(placed, table)
    for column in mixed:
        del placed[column]
    summary, unplaced = [], []
    for repeat in sorted(parents):
        keys = [key, *index_columns(repeat, parents)]
        columns = {c: (name, n) for c, (r, name, n) in placed.items() if r == repeat}
        if not columns:
            unplaced.append(repeat)
            continue
        # Named by question and instance only: a withheld list's codes are in its
        # columns' names.
        seen: set[tuple] = set()
        for column, (name, numbers) in columns.items():
            question = vardict["variables"][column]["survey"]["original_variable_name"]
            if name in keys:
                raise ValueError(
                    f"{question}: its long column is named like a key of "
                    f"{repeat}.parquet"
                )
            if (name, *numbers) in seen:
                raise ValueError(
                    f"two columns of {question} land on the same long column of "
                    f"{repeat} instance {numbers}"
                )
            seen.add((name, *numbers))
        long = long_table(table, key, columns, keys)
        pq.write_table(long, out / f"{repeat}.parquet")
        summary.append(f"{repeat} ({long.num_rows} rows)")
    print(f"  Long tables in {out.name}: {', '.join(summary) or 'none'}")
    if wide:
        print(
            f"  {len(wide)} repeat columns stay wide only: their names do not carry "
            "one instance number per repeat"
        )
    if mixed:
        print(
            f"  {len(mixed)} repeat columns stay wide only: their instances mix text "
            "and numbers"
        )
    if unplaced:
        print(f"  No long table for {', '.join(unplaced)}: every column stays wide")
    return out
