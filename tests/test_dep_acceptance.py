"""Invariants the dependency-acceptance workflow depends on.

The acceptance *engine* lives in the `dep-accept` skill, not in this repo, and
carries its own `--selftest` for its parsers. What belongs here is the handful of
repo-local facts that engine relies on, so a dependency PR that breaks one fails
CI instead of slipping through green:

- the hook-vs-lockfile pin parity that `.pre-commit-config.yaml` documents in a
  comment but nothing enforced (ruff, codespell);
- `.dep-accept.toml` staying valid and in step with the real project layout.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEP_ACCEPT_TOML = REPO_ROOT / ".dep-accept.toml"

try:
    import tomllib
except ModuleNotFoundError:  # 3.10
    import tomli as tomllib


def _load(path):
    return tomllib.loads(path.read_text(encoding="utf-8"))


# Console script -> the module whose `main()` builds its argparse parser. Read as
# source rather than invoked: every one of these CLIs exits on a missing
# config.toml *before* argparse sees `--help`, so a subprocess check would pass
# locally (config.py present) and fail on CI's clean checkout.
_CLI_SOURCES = {
    "surveycto-extract": "src/surveycto_extractor/cli/extract.py",
    "surveycto-vardict": "src/surveycto_extractor/cli/vardict.py",
    "surveycto-summary-stats": "src/surveycto_extractor/cli/summary_stats.py",
    "surveycto-enrich": "src/surveycto_extractor/cli/enrich.py",
}


def _declared_flags(script_name):
    """Return the `--flags` a CLI module passes to add_argument()."""
    src = (REPO_ROOT / _CLI_SOURCES[script_name]).read_text(encoding="utf-8")
    # Match the flag only in first-argument position, so a flag named inside
    # another option's help text does not count as declared.
    return set(re.findall(r"add_argument\(\s*[\"'](--[\w-]+)[\"']", src))


# Tools pinned TWICE -- once as a pre-commit hook `rev`, once as a locked Python
# dependency -- and moved by two different mechanisms (Dependabot moves the lock,
# `pre-commit autoupdate` moves the rev). Every such pair needs the parity check;
# a hook whose tool is not also a locked dependency (validate-pyproject,
# markdownlint-cli, pre-commit-hooks) cannot drift and is not listed.
DOUBLE_PINNED = [
    ("astral-sh/ruff-pre-commit", "ruff"),
    ("codespell-project/codespell", "codespell"),
]


def _precommit_rev(repo_slug):
    """The `rev` pinned for a pre-commit repo, or None if that repo is absent."""
    hook = re.search(
        rf"repo:\s*https://github\.com/{re.escape(repo_slug)}.*?rev:\s*v?([0-9][^\s]*)",
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"),
        re.DOTALL,
    )
    return hook.group(1) if hook else None


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
    @pytest.mark.parametrize(("repo_slug", "package"), DOUBLE_PINNED)
    def test_precommit_rev_matches_the_locked_version(self, repo_slug, package):
        """A pre-commit hook rev must match the version the lockfile resolves.

        `.pre-commit-config.yaml` pins these deliberately so `uv run <tool>` and
        the CI hook never disagree. A lock-only bump breaks it silently -- CI
        stays green while local runs start reformatting files -- which is exactly
        what a Dependabot PR does. Assert it so the drift cannot land unnoticed.

        Ruff is the one that actually drifted (PR #59, caught here on all 11
        matrix jobs); codespell is the same shape and was simply unguarded.
        """
        rev = _precommit_rev(repo_slug)
        assert rev, f"no {repo_slug} hook found in .pre-commit-config.yaml"
        locked = _locked_version(package)
        assert locked, f"{package} not found in uv.lock"
        assert rev == locked, (
            f"{package} pin drift: .pre-commit-config.yaml rev v{rev} vs locked "
            f"{package} {locked}. Bump the hook rev in the same commit as the lock, "
            "and commit whatever the new version reformats."
        )


class TestDepAcceptConfig:
    def test_config_exists_and_parses(self):
        # A bot branch is cut from main, so this file has to be committed for
        # future bump PRs to be measurable at all.
        assert DEP_ACCEPT_TOML.is_file(), (
            ".dep-accept.toml is missing from the repo root"
        )
        assert _load(DEP_ACCEPT_TOML)

    def test_e2e_file_sources_exist(self):
        config = _load(DEP_ACCEPT_TOML)
        for dest, source in (config.get("e2e", {}).get("files") or {}).items():
            assert (REPO_ROOT / source).is_file(), (
                f".dep-accept.toml copies {source} -> {dest}, but {source} is absent"
            )

    def test_e2e_commands_are_declared_entry_points(self):
        config = _load(DEP_ACCEPT_TOML)
        scripts = set(_load(REPO_ROOT / "pyproject.toml")["project"]["scripts"])
        for argv in config.get("e2e", {}).get("commands") or []:
            assert argv[0] in scripts, (
                f"{argv[0]} is not a console script in pyproject.toml -- the "
                "acceptance e2e would fail to launch it"
            )

    def test_e2e_command_flags_are_declared_by_their_cli(self):
        # Replaces the old `--seed` check. That flag guarded determinism, which
        # the synthetic generator's removal now gives for free -- but it left the
        # sharper invariant unguarded: the harness declares its commands in TOML,
        # so a flag later removed from a CLI turns every candidate into the same
        # FAIL, and the run then reports nothing at all about the bump.
        config = _load(DEP_ACCEPT_TOML)
        commands = config.get("e2e", {}).get("commands") or []
        assert commands, "no e2e commands declared"
        for argv in commands:
            declared = _declared_flags(argv[0])
            for flag in (a for a in argv[1:] if a.startswith("--")):
                assert flag in declared, (
                    f".dep-accept.toml passes {flag} to {argv[0]}, which does not "
                    f"declare it -- the acceptance e2e would fail for every "
                    f"candidate and measure nothing. Declared: {sorted(declared)}"
                )

    def test_e2e_flag_check_actually_fires(self):
        # Ablation: the check above is worthless unless a bad flag trips it.
        declared = _declared_flags("surveycto-extract")
        assert "--survey" in declared, "positive control: a real flag must be seen"
        assert "--seed" not in declared, (
            "negative control: --seed went with the synthetic generator, so the "
            "detector must not report it as declared"
        )

    def test_probe_script_exists(self):
        config = _load(DEP_ACCEPT_TOML)
        script = config.get("probe", {}).get("script")
        if script:
            assert (REPO_ROOT / script).is_file(), f"probe script {script} is absent"

    @pytest.mark.parametrize(
        "key", ["digest", "csv_shape", "json_len", "xlsx_shape", "dir_count"]
    )
    def test_metric_globs_target_the_configured_output_dir(self, key):
        # Every metric path must point under the sample output dir the e2e writes
        # to; a stale prefix would silently match nothing and measure nothing.
        config = _load(DEP_ACCEPT_TOML)
        for pattern in config.get("metrics", {}).get(key) or []:
            assert pattern.startswith("sample/output/"), (
                f"metrics.{key} entry {pattern!r} does not point at sample/output/"
            )
