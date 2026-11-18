# Justfile -- task runner (https://just.systems). Run `just` to list recipes.
# Adapted from the ipa-python-template for this toolkit (no jupyter/quarto/sql).

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

venv_dir := ".venv"
python := venv_dir + if os_family() == "windows" { "/Scripts/python.exe" } else { "/bin/python3" }

# Directory of the shared `dependabot-review` harness checkout, used by `accept-deps`.
# Override with DEPENDABOT_REVIEW pointing at the directory, not at the script inside it.
home_dir := env('USERPROFILE', env('HOME', '.'))
dependabot_review_dir := env('DEPENDABOT_REVIEW', home_dir / ".claude/plugins/marketplaces/ipa-skills-private/.claude/skills/dependabot-review")

[doc("List recipes (default)")]
default:
    @just --list

[doc("Display system information")]
system-info:
    @echo "CPU architecture: {{ arch() }}"
    @echo "Operating system: {{ os() }}"

[doc("Create/refresh the virtual environment + install the pre-commit hooks")]
venv:
    uv sync
    uv run pre-commit install

[doc("First-time setup (alias for venv)")]
get-started: venv

[doc("Update the lockfile + pre-commit hook versions")]
update-reqs:
    uv lock
    uv run pre-commit autoupdate

[doc("Lint (ruff)")]
lint-py:
    uv run ruff check

[doc("Format (ruff)")]
fmt-python:
    uv run ruff format

[doc("Format a single python file, \"f\"")]
fmt-py f:
    uv run ruff format {{ f }}

[doc("Run all pre-commit hooks over all files")]
pre-commit-run:
    uv run pre-commit run --all-files

[doc("Run the test suite")]
test:
    uv run pytest -q

# Each PR head gets its own worktree + venv and is diffed against main on tests,
# the sample end-to-end outputs (see .dependabot-review.toml) and the MCP smoke test.
# Pass PR numbers to narrow it. The engine is the project-agnostic `dependabot-review`
# harness, kept outside this repo so every project shares one copy -- set
# DEPENDABOT_REVIEW to that checkout's DIRECTORY if it is not in the default location.
#
# `just` interpolates *ARGS unquoted, so an argument containing a space is split
# into two. That bites on `--json` with a path under a home directory whose name
# has a space in it. For those, call the script directly:
#   uv run --no-project python -u "$DEPENDABOT_REVIEW/scripts/accept_dep_prs.py" \
#       --json "/some path/report.json"
[doc("Verify the open Dependabot PRs before merging (see CONTRIBUTING.md)")]
accept-deps *ARGS:
    uv run --no-project python -u "{{ dependabot_review_dir / 'scripts/accept_dep_prs.py' }}" {{ ARGS }}

[doc("Remove the virtual environment")]
clean:
    rm -rf .venv
