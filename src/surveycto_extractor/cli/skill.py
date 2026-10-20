"""``surveycto-skill``: install the survey-expert skill into an analysis project.

Run from the project's root, through uv so the models land in its lockfile::

    uv run surveycto-skill [TARGET]

It writes ``SKILL.md`` and ``references/`` to TARGET (default
``.claude/skills/survey-expert``), with a ``## This project`` section built from the
config and the outputs (surveys, repeat groups, missing-value codes, where the files
are); copies this package's ``survey.py`` there as ``search_survey.py`` so the shell
fallback matches the installed toolkit; adds a ``## Survey project`` section to the
project's ``CLAUDE.md`` when it has none (the study, pipelines, conventions and
rulings, filled in with the team); adds the ``survey-expert`` server to
``.mcp.json``; and installs the spaCy model of every ``[pii]`` language. Rerun it
after upgrading the toolkit or rebuilding the outputs.
"""

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

# tomllib, or tomli on Python 3.10, as the config loader resolved it.
import pyarrow.parquet as pq

from surveycto_extractor.config_loader import load_config, pii_config, tomllib
from surveycto_extractor.long_tables import long_dir, repeat_parents
from surveycto_extractor.pii import FLAGGED, MODEL_URL

SERVER = {"command": "uv", "args": ["run", "surveycto-mcp"]}
# The entry this toolkit documented before surveycto-mcp existed: replaced, since
# its `--extra mcp` no longer resolves.
_OLD_ARGS = [
    "run",
    "--extra",
    "mcp",
    "python",
    "-m",
    "surveycto_extractor.mcp_server.survey_server",
]


# Where SKILL.md takes the project's section.
MARKER = "<!-- surveycto-skill writes this project's section here -->"
_NO_PROJECT = (
    "## This project\n\nRun `uv run surveycto-skill` from the analysis project to "
    "describe its surveys here."
)
CLAUDE_SECTION = """
## Survey project

<!-- Written by surveycto-skill. Fill it in with the team, in their words; the
survey-expert skill reads it before any task (references/SETUP.md says how). -->

### The study

What the project is about, and the documents that describe it.

### Pipelines

Which survey each cleaning or analysis pipeline reads, by path.

### Conventions

How the team cleans: missing and don't-know codes, outliers and where they are
treated, units and currency, file layout.

### Rulings

Decisions on specific fields, written as they are made and never reopened.
"""


def project_section(config) -> str:
    """Return SKILL.md's ``## This project`` section, built from the config and the
    outputs: a row per survey, its missing-value codes and repeat groups, and where
    the files are. A survey not built yet says what to run.
    """
    project = getattr(config, "PROJECT", None) or {}
    surveys = getattr(config, "SURVEYS", {})
    lines = [f"## This project: {project.get('name') or Path.cwd().name}", ""]
    if project.get("description"):
        lines += [project["description"], ""]
    lines += [
        "| Survey | Title | Submissions | Columns | Questions | Flagged PII columns | Data | Docs |",
        "|---|---|---|---|---|---|---|---|",
    ]
    built = []
    for key, paths in config.DATASETS.items():
        title = surveys.get(key, {}).get("name", "")
        docs = surveys.get(key, {}).get("output_dir", "")
        dict_path = Path(paths["output_json"])
        if not dict_path.exists():
            lines.append(
                f"| `{key}` | {title} | not built: run `surveycto-vardict --survey "
                f"{key}` | | | | `{paths['data']}` | `{docs}` |"
            )
            continue
        vardict = json.loads(dict_path.read_text(encoding="utf-8"))
        questions = json.loads(
            Path(paths["questions_json"]).read_text(encoding="utf-8")
        )
        pii = vardict["summary"]["pii"]
        flagged = sum(n for status, n in pii.items() if status in FLAGGED)
        lines.append(
            f"| `{key}` | {title} | {vardict['dataset']['n_observations']:,} | "
            f"{vardict['dataset']['n_variables']:,} | {len(questions):,} | "
            f"{flagged:,} | `{paths['data']}` | `{docs}` |"
        )
        graph = dict_path.with_name(
            dict_path.name.replace("_variable_dictionary", "_variable_graph")
        )
        repeats = (
            json.loads(graph.read_text(encoding="utf-8"))["repeat_groups"]
            if graph.exists()
            else {}
        )
        codes = Counter()
        for entry in vardict["variables"].values():
            found = entry.get("sentinels") or {}
            for kind in ("raw_int_detail", "string_detail"):
                codes.update(found.get(kind) or {})
            # Stata's extended missings, .d and .r.
            codes.update(
                {f".{c}": n for c, n in (found.get("ext_missing_detail") or {}).items()}
            )
        built.append(
            (key, Path(paths["data"]), repeats, codes, repeat_parents(questions))
        )
    for key, data, repeats, codes, parents in built:
        lines += ["", f"### `{key}`", ""]
        if codes:
            lines.append(
                "Missing-value codes in the data (cells holding each): "
                + ", ".join(f"`{c}` {n:,}" for c, n in codes.most_common())
                + "."
            )
        if repeats:
            long = long_dir(data.with_suffix(".parquet"))
            lines += [
                "",
                f"Repeat groups; long tables in `{long}`:",
                "",
                "| Repeat | Inside | Max instances | Count variable | Long table rows |",
                "|---|---|---|---|---|",
            ]
            for name, group in repeats.items():
                table = long / f"{name}.parquet"
                rows = (
                    f"{pq.read_metadata(table).num_rows:,}"
                    if table.exists()
                    else "none"
                )
                lines.append(
                    f"| `{name}` | {parents.get(name) or 'submission'} | "
                    f"{group['max_iterations']} | `{group['count_var']}` | {rows} |"
                )
    if project.get("output_dir"):
        out = Path(project["output_dir"])
        db = out / f"{project.get('name')}.duckdb"
        lines += [
            "",
            f"Project files in `{out}`: "
            + ("`flow.json`" if (out / "flow.json").exists() else "no `flow.json` yet")
            + "; "
            + (
                f"`{db.name}` (map: `get_structure_db`)"
                if db.exists()
                else "no database yet"
            )
            + ".",
        ]
    return "\n".join(lines)


def install_skill(target: Path, section: str | None = None) -> Path:
    """Write the skill's files to ``target``, with ``section`` as SKILL.md's project
    section, and return it.
    """
    package = files("surveycto_extractor")
    references = target / "references"
    references.mkdir(parents=True, exist_ok=True)
    text = (package / "skill" / "SKILL.md").read_text(encoding="utf-8")
    (target / "SKILL.md").write_text(
        text.replace(MARKER, section or _NO_PROJECT), encoding="utf-8"
    )
    for guide in (package / "skill" / "references").iterdir():
        if guide.name.endswith(".md"):
            (references / guide.name).write_bytes(guide.read_bytes())
    (target / "search_survey.py").write_bytes((package / "survey.py").read_bytes())
    return target


def server_entry(mcp_json: Path) -> dict | None:
    """Return ``mcp_json``'s content with the survey-expert server set, or None
    when it is already set. The old documented entry is replaced (its ``env``
    kept); any other ``survey-expert`` entry raises: it is someone's edit.
    """
    data = json.loads(mcp_json.read_text(encoding="utf-8")) if mcp_json.exists() else {}
    servers = data.setdefault("mcpServers", {})
    current = servers.get("survey-expert")
    if current == SERVER:
        return None
    if current is None:
        servers["survey-expert"] = SERVER
    elif current.get("command") == "uv" and current.get("args") == _OLD_ARGS:
        servers["survey-expert"] = {**current, **SERVER}
    else:
        raise SystemExit(
            f"{mcp_json} already registers a different survey-expert server "
            f"({json.dumps(current)}); edit or remove it, then rerun"
        )
    return data


def claude_md(project: Path) -> bool:
    """Append the ``## Survey project`` section to ``project``'s CLAUDE.md when it has
    none; return whether it wrote. An existing section is the team's: never touched.
    """
    path = project / "CLAUDE.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r"^## Survey project\s*$", text, re.M):
        return False
    path.write_text(
        text.rstrip() + ("\n" if text else "") + CLAUDE_SECTION, encoding="utf-8"
    )
    return True


def missing_models(languages: dict[str, str]) -> list[str]:
    """Return the configured spaCy models not installed (by distribution, which
    is all ``spacy.util.is_package`` checks, without loading spaCy).
    """
    missing = []
    for model in languages.values():
        try:
            distribution(model)
        except PackageNotFoundError:
            missing.append(model)
    return missing


def install_models(project: Path, models: list[str]) -> None:
    """``uv add`` each model's wheel, so it is locked and survives ``uv sync``
    (``spacy download`` needs pip, which a uv environment lacks).
    """
    for model in models:
        subprocess.run(
            [os.environ["UV"], "add", MODEL_URL.format(model=model)],
            cwd=project,
            check=True,
        )


def _refuse_outside_a_project(project: Path) -> None:
    pyproject = project / "pyproject.toml"
    if "UV" not in os.environ:
        raise SystemExit("run it through uv: `uv run surveycto-skill`")
    if not pyproject.exists():
        raise SystemExit(
            f"no pyproject.toml in {project}: run it from the project root"
        )
    meta = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    if re.sub(r"[-_.]+", "-", meta.get("name", "")).lower() == "surveycto-extractor":
        raise SystemExit(
            "this is the toolkit's own repository; run surveycto-skill in the "
            "analysis project that uses it"
        )


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``surveycto-skill``.

    Everything that can refuse runs before anything is written: the project,
    the config, a clashing ``.mcp.json`` entry.
    """
    parser = argparse.ArgumentParser(prog="surveycto-skill", description=__doc__)
    parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        default=Path(".claude/skills/survey-expert"),
        help="where to write the skill (default: .claude/skills/survey-expert)",
    )
    args = parser.parse_args(argv)
    project = Path.cwd()
    _refuse_outside_a_project(project)
    config = load_config()
    if config is None:
        raise SystemExit(
            f"no config.toml in {project}: run `surveycto-init` and fill it in first "
            "(its [pii] languages say which spaCy models to install)"
        )
    settings = pii_config(config)
    mcp_json = project / ".mcp.json"
    entry = server_entry(mcp_json)
    models = [] if settings.no_personal_data else missing_models(settings.languages)

    section = project_section(config)

    target = install_skill(args.target, section)
    print(f"[skill] survey-expert -> {target}")
    if claude_md(project):
        print("[claude] ## Survey project added to CLAUDE.md: fill it in with the team")
    if entry is None:
        print("[mcp] survey-expert already in .mcp.json")
    else:
        mcp_json.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
        print("[mcp] survey-expert written to .mcp.json")
    install_models(project, models)
    for model in models:
        print(f"[pii] installed spaCy model {model}")
    print("Reconnect the MCP server (or restart the session) to load it.")
