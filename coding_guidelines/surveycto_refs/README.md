# SurveyCTO Reference Primers

This directory contains two reference primers we maintain as the authoritative source for `transformers/logic_converter.py` and the seed-generator type catalog. They are **derivative summaries** distilled from the SurveyCTO documentation site, not verbatim mirrors of any upstream file.

## Files

| File | Purpose |
|---|---|
| `xlsform.md` | XLSForm worksheet structure, column reference, field type catalog, choice filters, settings, groups and repeats. |
| `expressions.md` | Expression language: references, operators, function catalog, ODK/XPath divergences, worked patterns, common pitfalls. |

Each file's header records the regeneration date and points back at the canonical SurveyCTO docs pages as the source of truth.

## Source and attribution

These primers are written and maintained in-house from the public SurveyCTO documentation at <https://docs.surveycto.com> — primarily the *Designing forms: core concepts* section, with the specific pages cited in each primer's "Canonical docs" header line.

SurveyCTO's documentation is © SurveyCTO. We treat these primers as derivative summaries for the limited internal purpose of grounding our XLSForm-to-Stata converter; they are not redistributed as a SurveyCTO product. If `docs.surveycto.com` semantics change, the primers can drift — review the linked pages whenever the converter behaves unexpectedly on a new form.

## Rules

1. Edits are allowed and expected. These are living primers — refine them when the converter hits an XLSForm pattern they don't cover.
2. When you edit, bump the `STATUS: regenerated <YYYY-MM-DD>` line at the top of the file so reviewers can see how fresh the content is.
3. If `xlsform.md` changes in a way that affects the field-type table, rerun `python generators/build_type_catalog.py` to regenerate `_type_catalog.json`.

## Refresh procedure

There is no automated refresh — these are written by hand against the current SurveyCTO docs. To resync:

1. Read the relevant pages on <https://docs.surveycto.com/02-designing-forms/01-core-concepts/> (XLSForm columns, field types, expressions, constraints, relevance, groups/repeats).
2. Update the primer text to match.
3. Bump the `STATUS: regenerated <YYYY-MM-DD>` line.
4. Rerun `python generators/build_type_catalog.py` if `xlsform.md`'s field-type table changed.
