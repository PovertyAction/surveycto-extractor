# Contributing

Contributions are welcome. Please read this before opening a PR.

## Ground rules

- **No project-specific data or paths.** All code must be project-agnostic.
  `config.template.py` is the only place project-specific structure is documented,
  and it uses only placeholder values.
- **No `config.py` commits.** It is blocked by `.gitignore` for good reason.
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

## Code style

- Python 3.10+ compatible (3.10-3.13 are exercised in CI).
- No external dependencies beyond those declared in `pyproject.toml`.
- All new modules must be project-agnostic (no hardcoded paths or survey names).
