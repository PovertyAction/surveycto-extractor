"""``surveycto-skill``: install the survey-expert skill into an analysis project.

Run from the project's root, through uv so the models land in its lockfile::

    uv run surveycto-skill [TARGET]

It writes ``SKILL.md`` and ``references/`` to TARGET (default
``.claude/skills/survey-expert``), copies this package's ``survey.py`` there as
``search_survey.py`` so the shell fallback matches the installed toolkit, adds the
``survey-expert`` server to ``.mcp.json``, and installs the spaCy model of every
``[pii]`` language. Rerun it after upgrading the toolkit.
"""

import argparse
import json
import os
import re
import subprocess
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path

# tomllib, or tomli on Python 3.10, as the config loader resolved it.
from surveycto_extractor.config_loader import load_config, pii_config, tomllib
from surveycto_extractor.pii import MODEL_URL

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


def install_skill(target: Path) -> Path:
    """Write the skill's files to ``target`` and return it."""
    package = files("surveycto_extractor")
    references = target / "references"
    references.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_bytes((package / "skill" / "SKILL.md").read_bytes())
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

    target = install_skill(args.target)
    print(f"[skill] survey-expert -> {target}")
    if entry is None:
        print("[mcp] survey-expert already in .mcp.json")
    else:
        mcp_json.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
        print("[mcp] survey-expert written to .mcp.json")
    install_models(project, models)
    for model in models:
        print(f"[pii] installed spaCy model {model}")
    print("Reconnect the MCP server (or restart the session) to load it.")
