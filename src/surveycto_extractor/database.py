"""``<project>.duckdb``: one database for the project, for people and agents.

``surveycto-duckdb`` writes it to ``[project] output_dir``, beside ``flow.json``. Views
read the files on disk; nothing is copied into the database, so it holds no response
values. One schema per survey (its ``[datasets]`` key) and the project tables in
``main``:

- ``<survey>.submissions``: one row per submission, from the parquet sidecar: every
  column outside the long repeat tables.
- ``<survey>.<repeat>``: each repeat long, from ``<stem>_long/<repeat>.parquet``
  (surveycto-vardict): one row per instance that has any value, keyed by ``KEY`` and
  one ``<repeat>_index`` per repeat from the outermost down.
- ``<survey>.dictionary`` (one row per column: its ``view_name`` and
  ``view_column``, ``pii_status``), ``questions``, ``choices`` and ``edges`` (the
  variable graph).
- ``main.surveys``, ``datasets``, ``reads`` and ``joins`` from ``flow.json``.

The finished file's own ``information_schema`` is written, with each object's grain,
key and joins, to ``<project>_db.json``, the map the MCP's ``get_structure_db``
reads, and rendered for people as ``<project>_db.sql``. Columns named by a withheld
list's codes are shown there by position (``staff_<1>``).
"""

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from surveycto_extractor.config_loader import load_config
from surveycto_extractor.long_tables import (
    index_columns,
    long_dir,
    placement,
    repeat_parents,
)
from surveycto_extractor.survey import coded_aliases, render_structure

# Schemas DuckDB keeps for itself.
_RESERVED = {"main", "temp", "information_schema", "pg_catalog"}

# Each survey's description tables: what one row is, the key, what each refers to.
# `{s}` is the survey's schema, quoted.
_DESCRIPTIONS = {
    "dictionary": {
        "grain": "one row per parquet column; view_name is the view it is in and "
        "view_column its name there (member_age_3 in the wide data is member_age in "
        "the member view)",
        "key": ["column_name"],
        "joins": ["{s}.dictionary.question = {s}.questions.question"],
    },
    "questions": {"grain": "one row per question", "key": ["question"], "joins": []},
    "choices": {
        "grain": "one row per choice questions.json lists (a withheld list keeps "
        "only the form's own items)",
        "key": ["question", "value"],
        "joins": ["{s}.choices.question = {s}.questions.question"],
    },
    "edges": {
        "grain": "one row per link in the variable graph",
        "key": ["source", "target", "type"],
        "joins": [
            "{s}.edges.source = {s}.questions.question",
            "{s}.edges.target = {s}.questions.question",
        ],
    },
}

# The project tables in main, from flow.json.
_PROJECT = {
    "surveys": {"grain": "one row per survey", "key": ["survey"], "joins": []},
    "datasets": {
        "grain": "one row per dataset a survey reads; kind attachment: its rows "
        "come from outside the project",
        "key": ["dataset"],
        "joins": ["datasets.published_by = surveys.survey"],
    },
    "reads": {
        "grain": "one row per pulldata() or search() field",
        "key": ["survey", "field", "dataset", "dataset_column"],
        "joins": ["reads.dataset = datasets.dataset", "reads.survey = surveys.survey"],
    },
    "joins": {
        "grain": "one row per field pulled from a published dataset, matched to the "
        "publisher's field it comes from",
        "key": ["reader", "reader_field", "dataset", "dataset_column"],
        "joins": [],
    },
}


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _q(schema: str, name: str) -> str:
    """Return ``schema.name`` quoted for SQL."""
    return f"{_ident(schema)}.{_ident(name)}"


def _load(path: Path, rerun: str):
    if not path.exists():
        raise SystemExit(f"{path} is missing: run {rerun}")
    return json.loads(path.read_text(encoding="utf-8"))


def _check_view(con, view: str, expected: list[str]) -> None:
    """Raise when ``view``'s columns are not ``expected``.

    DuckDB ignores case in names and renames the later of two that clash (``Age``,
    ``age`` -> ``Age``, ``Age_1``), so a query by the clashing name would read the
    other column.
    """
    got = con.sql(f"SELECT * FROM {view} LIMIT 0").columns
    if got != expected:
        renamed = [f"{e} to {g}" for e, g in zip(expected, got, strict=True) if e != g]
        raise ValueError(
            f"{view}: DuckDB ignores case in column names, so it renamed "
            f"{', '.join(renamed)} to keep it apart from a column of the same name"
        )


# Columns whose type their values may not show: all null, or empty lists.
_TYPES = {
    "repeat_iteration": "BIGINT",
    "pii_basis": "VARCHAR[]",
    "group_path": "VARCHAR[]",
    "choices_withheld": "VARCHAR[]",
    "key": "VARCHAR[]",
    "read_by": "VARCHAR[]",
    "repeated_keys": "BIGINT",
    "missing_keys": "BIGINT",
}


def _table(con, schema: str, name: str, frame: pd.DataFrame) -> None:
    # Any other column with no values is text: DuckDB would call it INTEGER.
    frame = frame.astype(
        {
            c: "string"
            for c in frame.columns
            if c not in _TYPES and frame[c].isna().all()
        }
    )
    columns = ", ".join(
        f"CAST({_ident(c)} AS {_TYPES[c]}) AS {_ident(c)}" if c in _TYPES else _ident(c)
        for c in frame.columns
    )
    con.register("frame", frame)
    con.execute(f"CREATE TABLE {_q(schema, name)} AS SELECT {columns} FROM frame")
    con.unregister("frame")


def _inner_repeat(question: dict, parents: dict) -> str | None:
    inner = [g for g in question["group_path"] if g in parents]
    return inner[-1] if inner else None


def _write_survey(con, cfg, survey: str, structure: dict) -> dict:
    """Write one survey's schema; return where each question's column is."""
    if survey in _RESERVED:
        raise ValueError(f"[datasets.{survey}]: DuckDB keeps the schema {survey!r}")
    paths = cfg.DATASETS[survey]
    rerun = f"surveycto-vardict --survey {survey}"
    questions = _load(
        Path(paths["questions_json"]), f"surveycto-extract --survey {survey}"
    )
    dict_path = Path(paths["output_json"])
    vardict = _load(dict_path, rerun)
    if "pii" not in vardict.get("summary", {}):
        raise SystemExit(f"{dict_path} predates PII classification: run {rerun}")
    graph = _load(
        dict_path.with_name(
            dict_path.name.replace("_variable_dictionary", "_variable_graph")
        ),
        rerun,
    )
    sidecar = Path(paths["data"]).with_suffix(".parquet")
    if not sidecar.exists():
        raise SystemExit(f"{sidecar} is missing: run {rerun}")
    variables = vardict["variables"]
    parents = repeat_parents(questions)
    # A repeat view sits beside submissions and the description tables.
    for repeat in parents:
        if repeat == "submissions" or repeat in _DESCRIPTIONS:
            raise ValueError(
                f"{survey}: the repeat {repeat!r} has the name of the "
                f"{survey}.{repeat} table the database writes"
            )

    # The view each column is read from, and its name there: its repeat's long
    # view when the long table holds it, else submissions.
    long = long_dir(sidecar)
    long_columns = {
        p.stem: pq.read_schema(p).names for p in sorted(long.glob("*.parquet"))
    }
    placed, _ = placement(questions, variables)
    view_of = {
        c: (r, name)
        for c, (r, name, _) in placed.items()
        if name in long_columns.get(r, [])
    }
    wide = [c for c in pq.read_schema(sidecar).names if c not in view_of]
    aliases = coded_aliases(variables, questions)

    con.execute(f"CREATE SCHEMA {_ident(survey)}")
    source = _literal(sidecar.resolve().as_posix())
    con.execute(
        f"CREATE VIEW {_q(survey, 'submissions')} AS SELECT "
        + ", ".join(_ident(c) for c in wide)
        + f" FROM read_parquet({source})"
    )
    _check_view(con, _q(survey, "submissions"), wide)
    structure[f"{survey}.submissions"] = {
        "grain": "one row per submission",
        "key": ["KEY"],
        "joins": [],
        "shown_as": {c: aliases[c] for c in wide if c in aliases},
    }
    for repeat, names in long_columns.items():
        keys = names[: 1 + len(index_columns(repeat, parents))]
        this = _q(survey, repeat)
        source = _literal((long / f"{repeat}.parquet").resolve().as_posix())
        con.execute(f"CREATE VIEW {this} AS SELECT * FROM read_parquet({source})")
        _check_view(con, this, names)
        # An instance joins its parent on the parent's keys, named alike in both.
        parent = _q(survey, parents[repeat] or "submissions")
        on = " AND ".join(
            f"{this}.{_ident(k)} = {parent}.{_ident(k)}" for k in keys[:-1]
        )
        structure[f"{survey}.{repeat}"] = {
            "grain": f"one row per instance of the repeat {repeat} that has any value",
            "key": keys,
            "joins": [f"{on}  (many to one)"],
            # A long column drops the instance numbers, and so does its alias.
            "shown_as": {
                name: aliases[c].split(">", 1)[0] + ">"
                for c, (r, name) in view_of.items()
                if r == repeat and c in aliases
            },
        }

    by_name = {q["variable_name"]: q for q in questions}
    tables = {
        "dictionary": pd.DataFrame(
            [
                {
                    "column_name": c,
                    "view_name": view_of.get(c, ("submissions",))[0],
                    "view_column": view_of.get(c, (None, c))[1],
                    "variable_order": v.get("variable_order"),
                    "question": v["survey"].get("original_variable_name"),
                    "question_type": v["survey"].get("type"),
                    "repeat_group": _inner_repeat(
                        by_name[v["survey"]["original_variable_name"]], parents
                    )
                    if v["survey"].get("original_variable_name") in by_name
                    else None,
                    "repeat_iteration": v.get("repeat_iteration"),
                    "stata_type": v["stata"]["type"],
                    "non_null_count": v.get("non_null_count"),
                    "missing_rate": v.get("missing_rate"),
                    "pii_status": v["pii"]["status"],
                    "pii_basis": v["pii"]["basis"],
                }
                for c, v in variables.items()
            ]
        ),
        "questions": pd.DataFrame(
            [
                {
                    "question": q["variable_name"],
                    "question_order": q.get("question_order"),
                    "type": q["type"],
                    "question_text": q.get("question_text"),
                    "group_path": q["group_path"],
                    "repeat_group": _inner_repeat(q, parents),
                    "relevance": q.get("relevance"),
                    "constraint_expr": q.get("constraint"),
                    "calculation": q.get("calculation"),
                    "choice_list": q.get("choice_list"),
                    "choice_filter": q.get("choice_filter"),
                    "appearance": q.get("appearance"),
                    "choices_withheld": q.get("choices_withheld"),
                    "data_source": json.dumps(q["data_source"])
                    if q.get("data_source")
                    else None,
                }
                for q in questions
            ]
        ),
        "choices": pd.DataFrame(
            [
                {
                    "question": q["variable_name"],
                    "value": str(c.get("value")),
                    "label": c.get("label"),
                }
                for q in questions
                for c in q.get("choices") or []
            ],
            columns=["question", "value", "label"],
        ),
        "edges": pd.DataFrame(
            [
                {"source": e["source"], "target": e["target"], "type": e["type"]}
                # networkx 3.4 (py3.10) writes "links"; 3.6 writes "edges".
                for e in (graph["edges"] if "edges" in graph else graph["links"])
            ],
            columns=["source", "target", "type"],
        ),
    }
    for name, frame in tables.items():
        _table(con, survey, name, frame)
    for name, described in _DESCRIPTIONS.items():
        structure[f"{survey}.{name}"] = {
            **described,
            "joins": [j.format(s=_ident(survey)) for j in described["joins"]],
        }

    def where(field: str) -> tuple[str, str]:
        """Return the view and column a top-level or repeat field is read from."""
        columns = [
            c
            for c, v in variables.items()
            if v["survey"].get("original_variable_name") == field
        ]
        if len(columns) == 1 and columns[0] in wide:
            return "submissions", columns[0]
        long = {view_of[c] for c in columns if c in view_of}
        return next(iter(long)) if len(long) == 1 else ("submissions", field)

    return {"where": where}


def _write_project(con, flow: dict, surveys: dict, structure: dict) -> None:
    """Write the project tables in ``main`` from ``flow.json``."""
    joins = []
    for j in flow["joins"]:
        reader_table, reader_column = surveys[j["reader"]]["where"](j["field"])
        publisher_table, publisher_column = surveys[j["publisher"]]["where"](
            j["column"]
        )
        joins.append(
            {
                "reader": j["reader"],
                "reader_table": reader_table,
                "reader_field": reader_column,
                "dataset": j["dataset"],
                "dataset_column": j["column"],
                "publisher": j["publisher"],
                "publisher_table": publisher_table,
                "publisher_field": publisher_column,
            }
        )
    tables = {
        "surveys": pd.DataFrame(
            [{"survey": s["key"], "label": s["label"]} for s in flow["surveys"]],
            columns=["survey", "label"],
        ),
        "datasets": pd.DataFrame(
            [
                {
                    "dataset": d["id"],
                    "kind": d["kind"],
                    "published_by": d["published_by"],
                    "key": d["key"],
                    "read_by": d["read_by"],
                    "repeated_keys": d.get("repeated_keys"),
                    "missing_keys": d.get("missing_keys"),
                }
                for d in flow["datasets"]
            ],
            columns=[
                "dataset",
                "kind",
                "published_by",
                "key",
                "read_by",
                "repeated_keys",
                "missing_keys",
            ],
        ),
        "reads": pd.DataFrame(
            [
                {
                    "survey": r["survey"],
                    "field": r["field"],
                    "via": r["via"],
                    "dataset": r["dataset"],
                    "dataset_column": r.get("column"),
                    "match": json.dumps(r["match"]),
                }
                for r in flow["reads"]
            ],
            columns=["survey", "field", "via", "dataset", "dataset_column", "match"],
        ),
        "joins": pd.DataFrame(
            joins,
            columns=[
                "reader",
                "reader_table",
                "reader_field",
                "dataset",
                "dataset_column",
                "publisher",
                "publisher_table",
                "publisher_field",
            ],
        ),
    }
    for name, frame in tables.items():
        _table(con, "main", name, frame)
    structure.update(_PROJECT)
    structure["joins"] = {
        **_PROJECT["joins"],
        "joins": [
            f"{_q(j['reader'], j['reader_table'])}.{_ident(j['reader_field'])} = "
            f"{_q(j['publisher'], j['publisher_table'])}."
            f"{_ident(j['publisher_field'])}  (pulldata() through dataset "
            f"{j['dataset']}, column {j['dataset_column']})"
            for j in joins
        ],
    }


def write_database(cfg) -> Path:
    """Write ``<project>.duckdb`` and its map in ``[project] output_dir``, replacing
    any earlier one; return the database's path.
    """
    project = cfg.PROJECT
    if not project.get("name") or not project.get("output_dir"):
        raise SystemExit(
            "config.toml [project] needs name and output_dir: the database is "
            "<output_dir>/<name>.duckdb, beside flow.json"
        )
    out = Path(project["output_dir"])
    flow = _load(out / "flow.json", "surveycto-flow")
    name = project["name"]
    path = out / f"{name}.duckdb"
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    # Each object's grain, key and joins, recorded as it is created.
    structure: dict[str, dict] = {}
    try:
        surveys = {
            s["key"]: _write_survey(con, cfg, s["key"], structure)
            for s in flow["surveys"]
        }
        _write_project(con, flow, surveys, structure)
        # The finished file's own schema. One query for every column: each query
        # binds every view again.
        columns_of: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for schema, table, column, data_type in con.sql(
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns ORDER BY table_schema, table_name, "
            "ordinal_position"
        ).fetchall():
            columns_of.setdefault((schema, table), []).append((column, data_type))
        objects = []
        for schema, table, kind in con.sql(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables"
        ).fetchall():
            key = table if schema == "main" else f"{schema}.{table}"
            shown = structure[key].get("shown_as", {})
            objects.append(
                {
                    "name": key,
                    "kind": "view" if kind == "VIEW" else "table",
                    **{k: v for k, v in structure[key].items() if k != "shown_as"},
                    "columns": [
                        {"name": c, "type": t}
                        | ({"shown_as": shown[c]} if c in shown else {})
                        for c, t in columns_of[schema, table]
                    ],
                }
            )
    finally:
        con.close()
    order = {key: i for i, key in enumerate(structure)}
    objects.sort(key=lambda o: order[o["name"]])
    (out / f"{name}_db.json").write_text(
        json.dumps(objects, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out / f"{name}_db.sql").write_text(
        render_structure(name, objects, all_columns=True) + "\n", encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``surveycto-duckdb``."""
    argparse.ArgumentParser(prog="surveycto-duckdb", description=__doc__).parse_args(
        argv
    )
    cfg = load_config()
    path = write_database(cfg)
    print(f"[duckdb] {path} and {path.stem}_db.json (the map get_structure_db reads)")
