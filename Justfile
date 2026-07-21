# Justfile -- task runner (https://just.systems). Run `just` to list recipes.
# Adapted from the ipa-python-template for this toolkit (no jupyter/quarto/sql).

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

venv_dir := ".venv"
python := venv_dir + if os_family() == "windows" { "/Scripts/python.exe" } else { "/bin/python3" }

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

# Remove the virtual environment
clean:
    rm -rf .venv
