"""How a project's surveys connect: candidate links and ``flow.json``.

A survey reads another's data through a server dataset: ``pulldata()`` takes one
column of the row its key matches, ``search()`` lists a dataset's rows as choices.
Which form publishes a dataset is a server setting no XLSForm records, so the user
declares it in ``config.toml``::

    [server_datasets.hh_roster]
    published_by = "baseline"  # a [datasets] key
    key = ["hhid"]  # the columns that identify a row

    [entities.household]
    key = "hhid"
    description = "A household listed at baseline"

``surveycto-links`` suggests those tables from the forms (every dataset a form
reads, the survey carrying its columns, the columns it is matched on) for the user
to confirm. ``surveycto-flow`` checks what was declared against the forms and the
data and writes ``<[project] output_dir>/flow.json``, which the survey-expert tools
read for ``get_project`` and lineage across forms. A dataset with no
``[server_datasets]`` entry is an attachment: its rows come from outside the
project.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from surveycto_extractor.config_loader import load_config
from surveycto_extractor.long_tables import long_dir
from surveycto_extractor.parsers.xml_contract import is_system_column, pulldata_calls

_REF = re.compile(r"\$\{([^}]+)\}")
# Expression columns a pulldata() can sit in.
_EXPRESSIONS = ("calculation", "relevance", "constraint", "choice_filter")
# Fields every form has that join nothing: device metadata, timestamps, notes.
_NOT_JOINS = {
    "start",
    "end",
    "today",
    "deviceid",
    "subscriberid",
    "simserial",
    "phonenumber",
    "username",
    "caseid",
    "note",
    "repeat_count",
    "comments",
}


def _questions(cfg) -> dict[str, list[dict]]:
    """Return each survey's questions.json, by survey key."""
    datasets = getattr(cfg, "DATASETS", {})
    if not datasets:
        raise SystemExit("config.toml has no [datasets.*]: nothing to connect")
    out = {}
    for survey, entry in datasets.items():
        path = Path(entry["questions_json"])
        if not path.exists():
            raise SystemExit(
                f"{path} is missing: run surveycto-extract --survey {survey}"
            )
        out[survey] = json.loads(path.read_text(encoding="utf-8"))
    return out


def reads_of(questions: dict[str, list[dict]]) -> list[dict]:
    """Return every dataset read: one entry per ``pulldata()`` call in any
    expression (the column it takes and the key it matches on) and per ``search()``
    select (its filter columns). A ``pulldata()`` without a key raises: the form
    cannot find a row with it.
    """
    reads = []
    seen = set()
    for survey, qs in questions.items():
        for q in qs:
            name = q["variable_name"]
            for expression in _EXPRESSIONS:
                for call in pulldata_calls(q.get(expression)):
                    if call["key"] is None:
                        raise ValueError(
                            f"{survey}.{name}: pulldata('{call['dataset']}', ...) "
                            "needs a dataset, a column and a key"
                        )
                    read = (survey, name, call["dataset"], call["column"], call["key"])
                    if read in seen:
                        continue
                    seen.add(read)
                    reads.append(
                        {
                            "survey": survey,
                            "dataset": call["dataset"],
                            "field": name,
                            "via": "pulldata",
                            "column": call["column"],
                            "match": {call["key"]: call["match"]},
                        }
                    )
            source = q["data_source"]
            if source and source["kind"] == "search":
                reads.append(
                    {
                        "survey": survey,
                        "dataset": source["dataset"],
                        "field": name,
                        "via": "search",
                        "column": None,
                        "match": source["match"],
                    }
                )
    return reads


def _names(questions: dict[str, list[dict]]) -> dict[str, set[str]]:
    return {s: {q["variable_name"] for q in qs} for s, qs in questions.items()}


def _publisher_guess(mine: list[dict], names: dict[str, set[str]]):
    """Return the survey carrying most of a dataset's columns (pulled columns and
    search() filter columns) and how many, or (None, 0). A survey reading the
    dataset counts too (a form pulling its own earlier rows), unless another
    carries the columns as well.
    """
    columns = {r["column"] for r in mine if r["via"] == "pulldata"} | {
        c for r in mine if r["via"] == "search" for c in r["match"]
    }
    cover = {s: len(columns & names[s]) for s in names}
    cover = {s: n for s, n in cover.items() if n}
    readers = {r["survey"] for r in mine}
    others = {s: n for s, n in cover.items() if s not in readers}
    if others:
        cover = others
    if not cover:
        return None, 0, len(columns)
    best = max(sorted(cover), key=cover.get)
    return best, cover[best], len(columns)


def suggest_links(cfg) -> str:
    """Return candidate ``config.toml`` tables for the user to confirm."""
    questions = _questions(cfg)
    names = _names(questions)
    reads = reads_of(questions)
    declared = getattr(cfg, "SERVER_DATASETS", {})
    lines = [
        "# Candidate links found in the forms. Confirm or correct each with the user,",
        "# add the confirmed tables to config.toml, then run surveycto-flow.",
        "",
    ]
    if not getattr(cfg, "PROJECT", {}).get("output_dir"):
        lines += [
            "[project]",
            'name = ""',
            'output_dir = ""          # where flow.json is written',
            'description = ""         # what the study is about, in the user\'s words',
            "",
        ]
    # The survey fields reads are matched on: what identifies an entity here.
    entity_fields: set[str] = set()
    for dataset in sorted({r["dataset"] for r in reads}):
        mine = [r for r in reads if r["dataset"] == dataset]
        pulls = [r for r in mine if r["via"] == "pulldata"]
        readers = sorted({r["survey"] for r in mine})
        pulled = sorted({r["column"] for r in pulls})
        entity_fields |= {
            ref
            for r in pulls
            for m in r["match"].values()
            if m
            for ref in _REF.findall(m)
        }
        via = []
        if pulled:
            via.append(f"pulldata of {', '.join(pulled)}")
        if len(pulls) < len(mine):
            via.append("search() choices")
        lines.append(f"# {dataset}: read by {', '.join(readers)} ({'; '.join(via)})")
        if dataset in declared:
            lines += ["# already in config.toml", ""]
            continue
        best, covered, total = _publisher_guess(mine, names)
        if best is None:
            lines += [
                "# No survey here carries its columns: a CSV attached to the form, or",
                "# a form outside this config. Leave it out unless one publishes it.",
                "",
            ]
            continue
        # The column most reads match on; a key is one row's identity, not every
        # column some read happens to match on.
        matched = Counter(c for r in pulls for c in r["match"])
        lines += [
            f"[server_datasets.{dataset}]",
            f'published_by = "{best}"   # carries {covered} of the {total} columns read',
        ]
        if matched:
            key, *others = [c for c, _ in matched.most_common()]
            lines.append(f'key = ["{key}"]')
            if others:
                lines.append(f"# also matched on: {', '.join(others)}")
        else:
            lines.append(
                'key = [""]                # read by search() only: ask the user'
            )
        lines.append("")
    entities = getattr(cfg, "ENTITIES", {})
    named = {c for e in entities.values() for c in e["key"]}
    used: set[str] = set(entities)
    for column in sorted(entity_fields - named):
        name = column.removesuffix("_id") if column.endswith("_id") else column
        name = column if name in used or not name else name
        used.add(name)
        lines += [
            f"[entities.{name}]",
            f'key = "{column}"',
            'description = ""         # what one row is, in the user\'s words',
            "",
        ]
    joinable = {
        s: {
            q["variable_name"]
            for q in qs
            if q["type"] not in _NOT_JOINS and not is_system_column(q["variable_name"])
        }
        for s, qs in questions.items()
    }
    shared = sorted(
        (n, sorted(s for s in joinable if n in joinable[s]))
        for n in set().union(*joinable.values())
        if sum(n in joinable[s] for s in joinable) > 1
    )
    if shared:
        lines.append("# Fields in more than one survey (possible join columns):")
        lines += [f"#   {n}: {', '.join(surveys)}" for n, surveys in shared]
    return "\n".join(lines).rstrip() + "\n"


def _repeats(questions: list[dict]) -> set[str]:
    return {q["repeat_group_name"] for q in questions if q.get("repeat_group_name")}


def _sidecar(cfg, survey: str) -> Path:
    path = Path(cfg.DATASETS[survey]["data"]).with_suffix(".parquet")
    if not path.exists():
        raise SystemExit(f"{path} is missing: run surveycto-vardict --survey {survey}")
    return path


def _check_key(cfg, dataset: str, publisher: str, key: list[str], qs: list[dict]):
    """Check ``key`` is a set of the publisher's fields at one level, each with a
    column, and return how many rows repeat a key and how many lack one.

    A top-level key is counted over the submissions, through the publisher's
    dictionary (a Stata column may not carry the field's name: case, 32-character
    truncation). A key inside a repeat, which publishes one dataset row per instance,
    is counted over that repeat's long table. Repeats are counted, not refused: the
    server dataset keeps one row per key (the latest submission), so a resurveyed
    household is normal.
    """
    import pyarrow.parquet as pq

    by_name = {q["variable_name"]: q for q in qs}
    repeats = _repeats(qs)
    levels = {}
    for field in key:
        q = by_name.get(field)
        if q is None:
            raise ValueError(
                f"[server_datasets.{dataset}] key {field!r} is not a field of "
                f"{publisher}"
            )
        inner = [g for g in q["group_path"] if g in repeats]
        levels[field] = inner[-1] if inner else None
    if len(set(levels.values())) > 1:
        where = ", ".join(f"{f} in {r or 'the submission'}" for f, r in levels.items())
        raise ValueError(
            f"[server_datasets.{dataset}] key fields sit at different levels of "
            f"{publisher}: {where}"
        )
    repeat = levels[key[0]]
    if repeat is None:
        path, columns = (
            _sidecar(cfg, publisher),
            _key_columns(cfg, dataset, publisher, key),
        )
    else:
        path = long_dir(_sidecar(cfg, publisher)) / f"{repeat}.parquet"
        if not path.exists():
            raise SystemExit(
                f"{path} is missing: run surveycto-vardict --survey {publisher}"
            )
        names = pq.read_schema(path).names
        for field in key:
            if field not in names:
                raise ValueError(
                    f"[server_datasets.{dataset}] key {field!r} has no column in "
                    f"{path.name}: its Stata columns do not carry the instance numbers"
                )
        columns = key
    rows = pq.read_table(path, columns=columns).to_pandas()
    # Stata stores a missing string as "".
    missing = rows.isna() | rows.eq("")
    lacking = int(missing.any(axis=1).sum())
    repeated = int(rows[~missing.any(axis=1)].duplicated().sum())
    return repeated, lacking


def _key_columns(cfg, dataset: str, publisher: str, key: list[str]) -> list[str]:
    """Return the publisher's column for each top-level key field."""
    vardict_path = Path(cfg.DATASETS[publisher]["output_json"])
    if not vardict_path.exists():
        raise SystemExit(
            f"{vardict_path} is missing: run surveycto-vardict --survey {publisher}"
        )
    variables = json.loads(vardict_path.read_text(encoding="utf-8"))["variables"]
    columns = []
    for field in key:
        found = [
            col
            for col, e in variables.items()
            if (e.get("survey") or {}).get("original_variable_name") == field
        ]
        if len(found) != 1:
            raise ValueError(
                f"[server_datasets.{dataset}] key {field!r} has "
                f"{'no' if not found else len(found)} columns in {publisher}'s "
                "dictionary; it needs one"
            )
        columns += found
    return columns


def _check_parents(entities: dict) -> None:
    for name in entities:
        chain, parent = [name], entities[name]["parent"]
        while parent is not None:
            if parent not in entities:
                raise ValueError(
                    f"[entities.{name}] parent {parent!r} is not an entity"
                )
            if parent in chain:
                raise ValueError(
                    f"[entities.{name}] parents loop: {' -> '.join([*chain, parent])}"
                )
            chain.append(parent)
            parent = entities[parent]["parent"]


def build_flow(cfg) -> dict:
    """Return ``flow.json``: surveys, datasets, reads, joins, keys and the project.

    Raises on anything declared that the forms or the data contradict: a publisher
    that is not a survey, a key that is not a top-level field with a column, a
    pulled column the publisher has no field for, a declared dataset no survey
    reads, an entity column no survey carries, an unknown parent or a loop.
    """
    questions = _questions(cfg)
    names = _names(questions)
    reads = reads_of(questions)
    declared = getattr(cfg, "SERVER_DATASETS", {})
    read_ids = {r["dataset"] for r in reads}
    unread = set(declared) - read_ids
    if unread:
        raise ValueError(
            f"[server_datasets.{sorted(unread)[0]}]: no survey reads this dataset"
        )
    datasets = []
    for dataset in sorted(read_ids):
        entry = declared.get(dataset)
        publisher = entry["published_by"] if entry else None
        record = {
            "id": dataset,
            "kind": "server" if entry else "attachment",
            "published_by": publisher,
            "key": entry["key"] if entry else [],
            "read_by": sorted({r["survey"] for r in reads if r["dataset"] == dataset}),
        }
        if publisher is not None:
            if publisher not in questions:
                raise ValueError(
                    f"[server_datasets.{dataset}] published_by {publisher!r} is not a "
                    f"[datasets] key in config.toml ({', '.join(sorted(questions))})"
                )
            record["repeated_keys"], record["missing_keys"] = _check_key(
                cfg, dataset, publisher, entry["key"], questions[publisher]
            )
        datasets.append(record)
    publishers = {d["id"]: d["published_by"] for d in datasets if d["published_by"]}
    joins = []
    for r in reads:
        publisher = publishers.get(r["dataset"])
        if publisher is None or r["via"] != "pulldata":
            continue
        # A published column carries the field's name.
        if r["column"] not in names[publisher]:
            raise ValueError(
                f"{r['survey']}.{r['field']} pulls column {r['column']!r} of "
                f"{r['dataset']}, but {publisher} has no field {r['column']!r}"
            )
        joins.append(
            {
                "reader": r["survey"],
                "field": r["field"],
                "dataset": r["dataset"],
                "column": r["column"],
                "publisher": publisher,
            }
        )

    def surveys_with(column: str) -> list[str]:
        return sorted(s for s in names if column in names[s])

    key_of: dict[str, list[str]] = {}
    for d in datasets:
        for column in d["key"]:
            key_of.setdefault(column, []).append(d["id"])
    matched = Counter(c for r in reads if r["via"] == "pulldata" for c in r["match"])
    keys = sorted(
        (
            {
                "column": c,
                "key_of": key_of.get(c, []),
                "matched_by": matched.get(c, 0),
                "surveys": surveys_with(c),
            }
            for c in set(key_of) | set(matched)
        ),
        key=lambda k: (-len(k["surveys"]), k["column"]),
    )

    entities = getattr(cfg, "ENTITIES", {})
    _check_parents(entities)
    described = []
    for name, entity in entities.items():
        together = None
        for column in entity["key"]:
            carried = set(surveys_with(column))
            if not carried:
                raise ValueError(f"[entities.{name}] no survey has a field {column!r}")
            together = carried if together is None else together & carried
        if not together:
            raise ValueError(
                f"[entities.{name}] no survey has all of {', '.join(entity['key'])}"
            )
        described.append(
            {
                "name": name,
                "key": entity["key"],
                "description": entity["description"],
                "parent": entity["parent"],
                "surveys": sorted(together),
            }
        )
    project = getattr(cfg, "PROJECT", {})
    return {
        "project": {
            "name": project.get("name"),
            "description": project.get("description"),
            "entities": described,
        },
        # Each survey's config key and the label of its dictionary file, which
        # the shell's discovery names it by.
        "surveys": [
            {
                "key": s,
                "label": Path(cfg.DATASETS[s]["output_json"]).name.removesuffix(
                    "_variable_dictionary.json"
                ),
            }
            for s in sorted(questions)
        ],
        "datasets": datasets,
        "reads": reads,
        "joins": joins,
        "keys": keys,
    }


def links_main(argv: list[str] | None = None) -> None:
    """Entry point for ``surveycto-links``: print candidate config tables."""
    argparse.ArgumentParser(prog="surveycto-links", description=__doc__).parse_args(
        argv
    )
    cfg = load_config()
    if cfg is None:
        raise SystemExit("no config.toml here: run surveycto-init first")
    sys.stdout.reconfigure(encoding="utf-8")
    print(suggest_links(cfg), end="")


def flow_main(argv: list[str] | None = None) -> None:
    """Entry point for ``surveycto-flow``: check the declared links, write flow.json."""
    argparse.ArgumentParser(prog="surveycto-flow", description=__doc__).parse_args(argv)
    cfg = load_config()
    if cfg is None:
        raise SystemExit("no config.toml here: run surveycto-init first")
    out_dir = getattr(cfg, "PROJECT", {}).get("output_dir")
    if not out_dir:
        raise SystemExit("[project] output_dir is not set: it is where flow.json goes")
    flow = build_flow(cfg)
    out = Path(out_dir) / "flow.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written whole, then moved in, so the MCP server never reads half a file.
    partial = out.with_name("flow.json.partial")
    partial.write_text(json.dumps(flow, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(partial, out)
    print(
        f"[flow] {out}: {len(flow['surveys'])} survey(s), {len(flow['datasets'])} "
        f"dataset(s), {len(flow['joins'])} join(s)"
    )
    for d in flow["datasets"]:
        if d.get("repeated_keys") or d.get("missing_keys"):
            print(
                f"  {d['id']}: {d['repeated_keys']} submission(s) of "
                f"{d['published_by']} repeat a key (the server keeps the latest) and "
                f"{d['missing_keys']} lack one"
            )
