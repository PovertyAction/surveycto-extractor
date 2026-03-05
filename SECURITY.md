# Security Notes

This tool reads SurveyCTO instrument files and Stata datasets that may contain
personally identifiable information (PII). Follow these rules when using it in
a project repository.

## What to never commit

| File / path | Why |
|---|---|
| `config.py` | Contains absolute paths to your data and instruments (blocked by `.gitignore`) |
| `*.dta` | Stata datasets — may contain survey respondent data (blocked by `.gitignore`) |
| Instrument `.xlsx` files | May contain question labels with PII examples or village lists |
| Generated `*_questions.json` | May reproduce sensitive question text or logic |
| Generated `*_variable_dictionary.*` | Derived from PII-carrying dataset |

The `.gitignore` in this repo blocks the most common cases. Verify with
`git status` and `git diff --staged` before every commit.

## Reporting a vulnerability

If you find a security issue in this codebase, please open a private GitHub
Security Advisory rather than a public issue.
