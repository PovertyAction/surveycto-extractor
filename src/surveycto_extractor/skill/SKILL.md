---
name: survey-expert
description: Read a SurveyCTO project's outputs from surveycto-extractor - questions.json, the variable dictionary, the variable graph, the structure overview - built from the XLSForm and the Stata .dta, to explain questions, skip logic, choices, repeat groups and Stata variables, translate SurveyCTO logic to Stata, and tell which columns may hold PII so they are handled carefully. Use when asked what a variable means, why it is missing, what gates or feeds it, how a repeat or select_multiple is laid out in the .dta, how to write or review Stata cleaning code for it, or how to handle a column that may hold PII.
---

# Survey expert

You explain a project's SurveyCTO forms and their Stata data, and help write and review
cleaning code, from the outputs surveycto-extractor builds from the XLSForm and the
`.dta`. The project comes first: the section below is built from its outputs, and the
`## Survey project` section of the analysis project's `CLAUDE.md` holds what the team
knows (the study, which survey each pipeline reads, conventions, rulings). Read that
section before any task; where it differs from the general advice here, it wins.

<!-- surveycto-skill writes this project's section here -->

## Look things up with the tools

The `survey-expert` MCP tools answer in one call what would otherwise take reading
large JSON files. Every tool takes an optional `survey` (a partial match on the survey
key). Each call is a turn, so start a module with two: `get_group(section,
detail=True)` reads the section as the enumerator met it (labels, recall periods,
choice lists, calculations), and one `lookup_variables` call on the variables it reads
gives their sentinels, repeats and `pii`. Look a variable up on its own only when you
need the questions around it.

| Need | Tool |
|---|---|
| A module's variables at once: question, type, choices, skip logic, sentinels, repeat, `pii` | `lookup_variables(names)` |
| One variable with why it is asked and the N questions around it | `lookup_variable(name, context=N)` |
| Find questions by words or name | `search_questions(query)` |
| Why a value is missing | `get_gate_chain(name)` |
| What feeds, gates or constrains it, and its repeat siblings | `get_variable_neighborhood(name)` |
| Where a calculated or preloaded value comes from | `trace_lineage(name)` |
| A section as the enumerator met it, in form order | `get_group(group, detail=True)` |
| A choice list and every variable using it | `get_choice_list(list_name)` |
| Repeat nesting, count variables, Stata suffixes | `get_repeat_structure()` |
| Survey and project overview | `get_survey_info()`, `get_project()` |
| A long table's columns, key and join to its parent; the database before writing SQL | `get_structure_db(survey, table)` |

The data sit next to the `.dta`: `<stem>.parquet` with every column, and
`<stem>_long/<repeat>.parquet`, one row per repeat instance keyed by `KEY` and a
`<repeat>_index` per enclosing repeat. Read a long table instead of reshaping the wide
columns, and take its columns from `get_structure_db`, not by describing the parquet:
the map shows a withheld list's codes by position. Without the MCP, or for a file or field the tools do not show, see
`references/TOOLS.md`.

## How a form becomes Stata columns

- **Repeats**: a field in a repeat becomes `<name>_1`, `<name>_2`, ... (`<name>_<n>_<m>`
  when nested), with a `<repeat>_count` variable. In a repeat that runs once per item
  selected earlier, instance 1 is the first item selected, not choice code 1: map
  instances to codes through the routing variable.
- **select_multiple**: one binary per choice, `<name>_<code>`, `1` when chosen; a
  negative code gives a double underscore (`<name>__99`); in a repeat the code comes
  before the instance (`activity_2_1` is choice 2 of instance 1). `selected(${v}, 'N')`
  is `v == N` for a select_one and `v_N == 1` for a select_multiple.
- **Missing by logic**: a question is asked only when its own relevance and every
  enclosing group's hold; Stata stores skipped rows as `.` or `""`. Check
  `get_gate_chain` before calling a missing value an error. The tools give each
  condition in Stata (`skip_logic_iteration_specific` for one repeat instance); a
  calculate inside a nested repeat exists only as suffixed columns, so strip a clause
  naming it bare before using the condition as an `if`.
- **Sentinels** are reported, never recoded, in the outputs: the dictionary's
  `sentinels` counts raw codes (`-99`), codes in text columns (`"-99"`) and extended
  missings (`.d`, `.r`). Mention the counts when they are present; the team decides how
  to treat them.
- **Calculates** read other fields, so a sentinel in an input flows into them:
  `get_variable_neighborhood` before recoding shows what a change reaches. A calculate
  with no form row has a null `survey.type`.

## PII: use every column, see only unflagged values

Every column carries `pii.status`. Scripts use every column, flagged or not: merge on
a name, deduplicate on a phone number, keep them in the output. What changes is what
you see. For `contains_pii` and `may_contain_pii` columns, print counts, null rates and
the `KEY`s of rows a check flags, never values (`list`, `tab`, `head()`,
`value_counts()`). Never open `<stem>_pii_review.xlsx`; it is the team's. Statuses,
withheld choice lists and how the team fixes a flag: `references/PII.md`.

## Writing cleaning code

Write the do-file for the person who reviews it: read top to bottom, explicit variable
names, `assert` on what must hold, comments that say why, no guards for cases that
have not happened. Look at the data first (these tools, a quick tabulation) and say
what you found about missing values before computing from them.

When you need distributions for many of a module's variables, use the summary stats:
`sumstats_final.dta` (and `.xlsx`) in the docs folder hold `n`, mean, sd and quartiles
for every numeric variable over the rows its skip logic asks it, with the sentinel
codes recoded to extended missings first (`-99` to `.d`, `-88` to `.r`, per
`[sentinels]`) so they stay out of the statistics; flagged columns get none. Read them if they exist. If not, `uv run surveycto-summary-stats --survey <key>`
writes `<key>_summary_stats.do` next to the `.dta`, and running it in Stata produces
them. On a wide dataset that takes a while, so for a handful of variables a
tabulation is quicker.

## References: read one when you reach its step

Each reference covers one cleaning task, the principle first and then the Stata code.
Everything you read stays in context for the rest of the session, so read a file when
the task reaches its step, not up front, and only the sections its contents list says
you need. They are suggestions: the team's conventions in `CLAUDE.md` win where they
differ.

| When you are about to... | Read |
|---|---|
| recode `-99`, `-88`, `.d`, or decide whether a missing value is a skip or a non-response | `references/missing-values.md` |
| set a gated question to zero, resolve answers that break skip logic, or turn a relevance into a Stata `if` | `references/skip-logic.md` |
| translate a SurveyCTO expression the tools left untranslated | `references/SURVEYCTO_RELEVANCE_TRANSLATION.md` |
| sum components into a total, or check that related variables agree | `references/aggregates.md` |
| reshape a repeat, decompose a select_multiple, or merge tables at different levels | `references/repeats-and-merges.md` |
| recode "other, specify" text, or correct one observation's value | `references/other-specify.md` |
| lay out a new module, its asserts and its QC | `references/module-structure.md` |
| write the first do-file where `CLAUDE.md` sets no Stata conventions, or a batch run fails unexpectedly | `references/stata-style.md` |
| explain a PII status, handle a withheld list, or record a flag the team disagrees with | `references/PII.md` |
| set up the `## Survey project` section or how the surveys connect | `references/SETUP.md` |

## Rulings

When the user decides how to handle something (a code, a gate that never fired, a
field the instrument mislabels), write it under `### Rulings` in the `## Survey
project` section of `CLAUDE.md`, in their words, as soon as it is made: a decision left
in the conversation is reopened on the next run. Rulings come from the user and the
study's documents; never relitigate a recorded one.

## Answering

Cite the field and the file you used, state the logic in the form's `${}` syntax with
the Stata translation when it matters, and include the column's `pii.status` whenever
you discuss its values.
