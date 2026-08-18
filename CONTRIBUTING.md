# Contributing

Contributions are welcome. Please read this before opening a PR.

## Ground rules

- **No project-specific data or paths.** All code must be project-agnostic.
  `config.template.toml` is the only place project-specific structure is documented,
  and it uses only placeholder values.
- **No `config.toml` / `config.py` commits.** Both are blocked by `.gitignore` for good reason.
- **No survey data.** Do not include `.dta`, instrument `.xlsx`, or generated
  output files (JSON, CSV, do-files) as examples or test fixtures. The one
  exception is the bundled `sample/` directory, which carries the public
  [IPA High Frequency Checks](https://github.com/PovertyAction/high-frequency-checks)
  training dataset (all names in it are fictional) and is explicitly
  whitelisted in `.gitignore`.

## Workflow

1. Fork the repo and create a feature branch.
2. Make your changes. Keep PRs focused — one concern per PR.
3. Test against a real (private) survey instrument before submitting.
4. Open a pull request with a clear description of what changed and why.

## Accepting dependency updates

Dependabot opens a batch of version bumps every week. Green CI is necessary but
not sufficient: it proves the suite still passes, not that the toolkit still
produces the *same* outputs, that the sample pipeline still runs end to end, that
the MCP server still imports, or that the bump has not narrowed the install range
downstream users depend on.

Run the acceptance harness before merging any of them:

```sh
just accept-deps              # baseline + every open Dependabot PR
just accept-deps 52 53        # only these PRs
```

Each candidate — `main` as the baseline, every PR head, and a "combined" branch
modelling the post-merge end state — gets its own throwaway git worktree and its
own venv synced from *that candidate's* lockfile, then runs the same battery:
lockfile resolves, lint, full test suite, the bundled sample end to end
(fixed seed), and the MCP smoke test. Every output is reduced to counts and
digests and diffed against the baseline, so a bump that changes nothing
observable reports `PASS` and one that shifts a single synthetic cell reports
`DRIFT`.

The engine is project-agnostic and deliberately lives **outside** this repo, so
one copy serves every project instead of drifting per repo. What is committed here
is only what is specific to this toolkit:

| File | Role |
|---|---|
| `.dep-accept.toml` | what to run and what to measure (must stay committed — Dependabot branches from `main`, so this is what makes future bump PRs measurable) |
| `scripts/dep_probe_mcp.py` | the MCP-server smoke test, which no file comparison covers |
| `tests/test_dep_acceptance.py` | the repo invariants the harness relies on |

`just accept-deps` finds the engine at `~/.claude/skills/dep-accept/`; set
`DEP_ACCEPT` to override the path.

Two verdicts need a human:

- `DRIFT` — the bump changed an output. Look at the diff before merging.
- `REVIEW` — the bump raised a declared floor in `pyproject.toml` (narrowing what
  downstream users can install), or broke the ruff pin parity between
  `.pre-commit-config.yaml` and the `dev` extra. Both are judgement calls.

Nothing runs in your working tree, and every subprocess has `SURVEYCTO_CONFIG`
pointed at the sample config inside its worktree, so no run can reach real
survey data.

## Code style

- Python 3.10+ compatible (3.10-3.13 are exercised in CI).
- No external dependencies beyond those declared in `pyproject.toml`.
- All new modules must be project-agnostic (no hardcoded paths or survey names).
