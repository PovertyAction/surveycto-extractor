"""Long repeat tables: ``<stem>_long/<repeat>.parquet`` beside the ``.dta``.

The Stata export is wide: a field in a repeat has one column per instance
(``member_age_1``, ``member_age_2``; nested, ``job_wage_1_2``). Each repeat gets one
table with one row per instance that has any value, keyed by the submission ``KEY``
column and one ``<repeat>_index`` per repeat from the outermost down
(``key, member_index, job_index``). A column keeps its wide name without the
instance suffix: ``member_age_2`` -> ``member_age``; a select_multiple binary keeps
its choice, ``tasks_3_2`` -> ``tasks_3``.

The instance numbers come from the column name, after the question's name (and a
select_multiple binary's choice), one per repeat the question sits in. A column
whose name does not read that way (Stata truncated it at 32 characters, or another
form version wrote it with more or fewer numbers) stays wide only.
"""

import re
from functools import reduce
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

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
    # Stata names cannot hold "-": SurveyCTO writes choice -99 as __99.
    return f"__{code[1:]}" if code.startswith("-") else f"_{code}"


def placement(questions: list[dict], variables: dict) -> tuple[dict, list[str]]:
    """Place each repeat column in its repeat's long table.

    Returns ``{column: (repeat, long_name, instance_numbers)}`` and the repeat
    columns that stay wide only, because their names do not carry one instance
    number per enclosing repeat.
    """
    parents = repeat_parents(questions)
    chain_of = {
        q["variable_name"]: [g for g in q["group_path"] if g in parents]
        for q in questions
        if q.get("type") != "repeat_count"
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
        parts.append(
            pa.Table.from_arrays(
                [rows[key].combine_chunks(), *index]
                + [rows[c].combine_chunks() for c in wide],
                names=[*keys, *(columns[c][0] for c in wide)],
            )
        )
    if not parts:
        # No submission reached the repeat: its table is the keys, no rows.
        types = [table.schema.field(key).type] + [pa.int32()] * (len(keys) - 1)
        return pa.schema(list(zip(keys, types, strict=True))).empty_table()
    long = pa.concat_tables(parts, promote_options="permissive")
    return long.sort_by([(k, "ascending") for k in keys])


def write_long_tables(questions: list[dict], vardict: dict, parquet: Path) -> Path:
    """Write ``<stem>_long/<repeat>.parquet`` for every repeat; return the directory.

    Raises when two columns land on the same long column, or one is named like a key.
    """
    parents = repeat_parents(questions)
    placed, wide = placement(questions, vardict["variables"])
    out = long_dir(parquet)
    for old in out.glob("*.parquet"):
        old.unlink()
    if not parents:
        return out
    out.mkdir(exist_ok=True)
    table = pq.read_table(parquet)
    key = key_column(table.column_names)
    summary = []
    for repeat in sorted(parents):
        keys = [key, *index_columns(repeat, parents)]
        columns = {c: (name, n) for c, (r, name, n) in placed.items() if r == repeat}
        seen: dict[tuple, str] = {}
        for column, (name, numbers) in columns.items():
            if name in keys:
                raise ValueError(
                    f"{column}: its long name {name!r} is a key of {repeat}.parquet"
                )
            other = seen.setdefault((name, *numbers), column)
            if other != column:
                raise ValueError(
                    f"{other} and {column} both land on {name!r} of the same "
                    f"{repeat} instance"
                )
        long = long_table(table, key, columns, keys)
        pq.write_table(long, out / f"{repeat}.parquet")
        summary.append(f"{repeat} ({long.num_rows} rows)")
    print(f"  Long tables in {out.name}: {', '.join(summary)}")
    if wide:
        print(
            f"  {len(wide)} repeat columns stay wide only: their names do not carry "
            "one instance number per repeat"
        )
    return out
