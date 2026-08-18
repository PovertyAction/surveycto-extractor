# Justfile -- task runner (https://just.systems). Run `just` to list recipes.
# Adapted from the ipa-python-template for this toolkit (no jupyter/quarto/sql).

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

venv_dir := ".venv"
python := venv_dir + if os_family() == "windows" { "/Scripts/python.exe" } else { "/bin/python3" }

# Directory of the shared `dep-accept` harness checkout, used by `accept-deps`.
# Override with DEP_ACCEPT pointing at the directory, not at the script inside it.
home_dir := env('USERPROFILE', env('HOME', '.'))
dep_accept_dir := env('DEP_ACCEPT', home_dir / ".claude/skills/dep-accept")

# List recipes (default)
default:
    @just --list

# Display system information
system-info:
    @echo "CPU architecture: {{ arch() }}"
    @echo "Operating system: {{ os() }}"

# Create/refresh the virtual environment + install the pre-commit hooks
venv:
    uv sync --all-extras
    uv run pre-commit install

# First-time setup (alias for venv)
get-started: venv

# Update the lockfile + pre-commit hook versions
update-reqs:
    uv lock
    uv run pre-commit autoupdate

# Lint (ruff)
lint-py:
    uv run ruff check

# Format (ruff)
fmt-python:
    uv run ruff format

# Format a single python file, "f"
fmt-py f:
    uv run ruff format {{ f }}

# Run all pre-commit hooks over all files
pre-commit-run:
    uv run pre-commit run --all-files

# Run the test suite (all extras, so the MCP tests run too)
test:
    uv run --all-extras pytest -q

# Each PR head gets its own worktree + venv and is diffed against main on tests,
# the sample end-to-end outputs (see .dep-accept.toml) and the MCP smoke test.
# Pass PR numbers to narrow it. The engine is the project-agnostic `dep-accept`
# harness, kept outside this repo so every project shares one copy -- set
# DEP_ACCEPT to that checkout's DIRECTORY if it is not in the default location.

# Verify the open Dependabot PRs before merging (see CONTRIBUTING.md)
accept-deps *ARGS:
    uv run --no-project python -u "{{ dep_accept_dir / 'scripts/accept_dep_prs.py' }}" {{ ARGS }}

# Remove the virtual environment
clean:
    rm -rf .venv
