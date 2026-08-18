"""Invariants the dependency-acceptance workflow depends on.

The acceptance *engine* lives in the `dep-accept` skill, not in this repo, and
carries its own `--selftest` for its parsers. What belongs here is the handful of
repo-local facts that engine relies on, so a dependency PR that breaks one fails
CI instead of slipping through green:

- the ruff pin parity that `.pre-commit-config.yaml` documents in a comment but
  nothing enforced;
- `.dep-accept.toml` staying valid and in step with the real project layout.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEP_ACCEPT = REPO_ROOT / ".dep-accept.toml"

try:
    import tomllib
except ModuleNotFoundError:  # 3.10
    import tomli as tomllib


def _load(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


PRECOMMIT_RUFF_RX = re.compile(
    r"repo:\s*https://github\.com/astral-sh/ruff-pre-commit.*?rev:\s*v?([0-9][^\s]*)",
    re.DOTALL,
)


def _locked_version(package):
    """Return the version uv.lock resolves for a package, or None."""
    name = None
    for line in (REPO_ROOT / "uv.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith('name = "'):
            name = line.split('"')[1].lower()
        elif line.startswith('version = "') and name == package:
            return line.split('"')[1]
        elif line.startswith('version = "'):
            name = None
    return None


class TestRuffPinParity:
    def test_precommit_rev_matches_the_locked_ruff(self):
        """The pre-commit ruff rev must match the ruff the lockfile resolves.

        `.pre-commit-config.yaml` pins this deliberately so `uv run ruff` and the
        CI hook never disagree. A lock-only ruff bump breaks it silently -- CI
        stays green while local runs start reformatting files -- which is exactly
        what a Dependabot PR does. Assert it so the drift cannot land unnoticed.
        """
        hook = PRECOMMIT_RUFF_RX.search(
            (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )
        assert hook, "no ruff-pre-commit hook found in .pre-commit-config.yaml"
        locked = _locked_version("ruff")
        assert locked, "ruff not found in uv.lock"
        assert hook.group(1) == locked, (
            f"ruff pin drift: .pre-commit-config.yaml rev v{hook.group(1)} vs locked "
            f"ruff {locked}. Bump the hook rev in the same commit as the lock, and "
            "commit whatever the new version reformats."
        )


class TestDepAcceptConfig:
    def test_config_exists_and_parses(self):
        # A bot branch is cut from main, so this file has to be committed for
        # future bump PRs to be measurable at all.
        assert DEP_ACCEPT.is_file(), ".dep-accept.toml is missing from the repo root"
        assert _load(DEP_ACCEPT)

    def test_e2e_file_sources_exist(self):
        config = _load(DEP_ACCEPT)
        for dest, source in (config.get("e2e", {}).get("files") or {}).items():
            assert (REPO_ROOT / source).is_file(), (
                f".dep-accept.toml copies {source} -> {dest}, but {source} is absent"
            )

    def test_e2e_commands_are_declared_entry_points(self):
        config = _load(DEP_ACCEPT)
        scripts = set(_load(REPO_ROOT / "pyproject.toml")["project"]["scripts"])
        for argv in config.get("e2e", {}).get("commands") or []:
            assert argv[0] in scripts, (
                f"{argv[0]} is not a console script in pyproject.toml -- the "
                "acceptance e2e would fail to launch it"
            )

    def test_synthetic_run_is_seeded(self):
        # Digest comparison is only meaningful if the stochastic phase is pinned.
        config = _load(DEP_ACCEPT)
        extract = [
            argv
            for argv in config.get("e2e", {}).get("commands") or []
            if argv[0] == "surveycto-extract"
        ]
        assert extract, "no surveycto-extract command declared"
        for argv in extract:
            assert "--seed" in argv, f"{argv} must pass --seed for reproducibility"

    def test_probe_script_exists(self):
        config = _load(DEP_ACCEPT)
        script = config.get("probe", {}).get("script")
        if script:
            assert (REPO_ROOT / script).is_file(), f"probe script {script} is absent"

    @pytest.mark.parametrize(
        "key", ["digest", "csv_shape", "json_len", "xlsx_shape", "dir_count"]
    )
    def test_metric_globs_target_the_configured_output_dir(self, key):
        # Every metric path must point under the sample output dir the e2e writes
        # to; a stale prefix would silently match nothing and measure nothing.
        config = _load(DEP_ACCEPT)
        for pattern in config.get("metrics", {}).get(key) or []:
            assert pattern.startswith("sample/output/"), (
                f"metrics.{key} entry {pattern!r} does not point at sample/output/"
            )
