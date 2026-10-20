---
name: survey-expert
description: Read a SurveyCTO project's outputs from surveycto-extractor - questions.json, the variable dictionary, the variable graph, the structure overview - built from the XLSForm and the Stata .dta, to explain questions, skip logic, choices, repeat groups and Stata variables, translate SurveyCTO logic to Stata, and tell which columns may hold PII so they are handled carefully. Use when asked what a variable means, why it is missing, what gates or feeds it, how a repeat or select_multiple is laid out in the .dta, how to write or review Stata cleaning code for it, or how to handle a column that may hold PII.
---

# Survey expert

You explain a project's SurveyCTO forms and their Stata data from the outputs
`surveycto-extract` and `surveycto-vardict` write. Each survey has a key (the
`[surveys.KEY]` table in `config.toml`) and a docs folder (its `output_dir`); the data
files sit next to the `.dta` (`[datasets.KEY] data`):

| File | Where | What it holds |
|---|---|---|
| `<key>_questions.json` | docs folder | One entry per question, keyed by its XLSForm `name`, with the Stata translation of its skip logic and constraint |
| `sections/` | docs folder | `questions.json` sliced per group |
| `<key>_structure.txt` | docs folder | The form's group and repeat hierarchy, for reading by eye |
| `<key>_variable_dictionary.json` (+ `.xlsx` with `--xlsx`) | docs folder | One entry per `.dta` variable, in form order: its question, Stata type, counts, sentinels, `pii`, and the range of a column whose values are shown |
| `<key>_variable_graph.json` | docs folder | Questions linked by calculation, relevance, group relevance, constraint and repeat, plus the repeat tree |
| `<stem>.parquet` | next to the `.dta` | The submissions, every column; a missing string is null here (`""` in the `.dta`), as in the long tables. Scripts use it; flagged columns' values stay out of what you print (below) |
| `<stem>_long/<repeat>.parquet` | next to the `.dta` | One table per repeat: a row per instance that has any value, keyed by `KEY` and a `<repeat>_index` per enclosing repeat, outer first (`key, member_index, job_index`); columns lose the instance suffix (`member_age_2` -> `member_age`). A question whose columns do not all carry their instance numbers stays only in `<stem>.parquet` |
| `<stem>_pii_review.xlsx` | next to the `.dta` | Every value of a flagged column, for the people cleaning the data to verify the flags. **Never open it** |
| `<project>.duckdb` | `[project] output_dir` | One database for the project (`surveycto-duckdb`): per survey a `submissions` view, a view per long repeat table and the `dictionary` (with `pii_status`), `questions`, `choices` and `edges` tables; in `main`, the surveys, datasets, reads and joins from `flow.json`. Views read the parquets by their full path, so the file holds no response values and is rebuilt on each machine; rerun `surveycto-duckdb` after `surveycto-vardict` |
| `<project>_db.json` / `_db.sql` | `[project] output_dir` | The database's map: every table and view, what one row is, its key, joins and columns. Read it through `get_structure_db`, never `DESCRIBE` or `information_schema`: a withheld list's columns carry its codes in their names, which the map shows by position |
| `<key>_summary_stats.do` | next to the `.dta` | `tabstat` per numeric variable grouped by skip condition; flagged columns get no stats |

<!-- surveycto-skill writes this project's section here -->

## Project setup

The section above comes from the outputs. What the team knows that no output records
lives in the analysis project's `CLAUDE.md`, section `## Survey project`: the study,
which survey each pipeline reads, the team's conventions, and its rulings. Read it
before any task and follow it over the general advice in this skill.

When that section is still the template, or `get_project` says the project is not
described, and the user wants it set up, follow `references/SETUP.md`. A cleaning or
analysis task does not wait for setup.

## How to look things up

Use the `survey-expert` MCP tools when they are connected. Otherwise the same answers
come from `search_survey.py` in this skill's folder: standard-library Python, with no
package or MCP needed (`<S>` below):

```sh
uv run python .claude/skills/survey-expert/search_survey.py [--dir DOCS] [--survey KEY] <command>
```

`--dir` defaults to the current directory, scanned for `*_variable_dictionary.json`;
pass the docs folder when the outputs live elsewhere (a synced drive). A miss exits 1.
A dictionary built before the PII pass is refused: re-run `surveycto-vardict`.
Every MCP tool takes an optional `survey` (a partial match on the survey key, as
`--survey` is for the script): `survey='endline'` matches `endline_hh` and
`endline_adult`.

| Need | MCP tool | Shell command (`<S> <command>`) |
|---|---|---|
| What a variable or question is: type, text, choices, skip logic, constraint, sentinels, repeat | `lookup_variable(name, context=0)` | `var NAME [--context N]` |
| Several variables at once (a cleaning module's) | `lookup_variables(names)` | `var NAME [NAME ...]` |
| Find questions by words or name | `search_questions(query, max_results=20)` | `search WORDS [--max N]` |
| A choice list and the variables that use it | `get_choice_list(list_name)` | `choices LIST` |
| Why a question is asked | `get_gate_chain(name)` | `gates NAME` |
| What feeds, gates or constrains it, and its repeat siblings | `get_variable_neighborhood(name, depth=1)` | `neighborhood NAME [--depth N]` |
| A group's questions in order, with each condition | `get_group(group)` | `group GROUP` |
| Where a value comes from, back to the questions someone answered | `trace_lineage(name)` | `lineage NAME` |
| Repeat groups: nesting, count variables, max iterations, Stata suffixes | `get_repeat_structure()` | `repeats` |
| Survey overview | `get_survey_info()` | `info` |
| What the project is about: description, entities, datasets linking the surveys, candidate keys | `get_project()` | `project` |
| The project database before writing SQL: its schemas, one schema, or one table's columns | `get_structure_db(schema, table)` | `db [SCHEMA [TABLE]]` |

`context=N` adds why the question is asked and the N questions around it in form
order. Read the JSON files directly only for what the tools do not show, such as a
whole section or `structure.txt`.

`uv run surveycto-skill` writes this skill into `.claude/skills/survey-expert/` with
its project section, adds the `## Survey project` section to `CLAUDE.md` when missing,
adds the `survey-expert` server (`uv run surveycto-mcp`) to `.mcp.json`, and installs
the spaCy models the `[pii]` config names. Rerun it after upgrading the toolkit or
rebuilding the outputs.

## How a form works

- Why a value is missing: `get_gate_chain`. It walks from the outermost group's
  relevance to the question's own, resolves each `${ref}` to its question, choices and
  own gate, and ends with how many rows reached the question. Use it before calling a
  missing value an error.
- What a change breaks, before recoding: `get_variable_neighborhood`. Calculates-from
  inputs carry sentinels into the calculate; calculated-by outputs change with it; a
  gate makes its dependants disappear; repeat siblings belong to the same instance and
  are handled together.
- Where a value comes from: `trace_lineage`. It follows a calculate to the fields its
  formula reads and a `pulldata()` or `search()` to the dataset column and the field
  it is matched on, down to answered questions. When the dataset is published by
  another survey of the project (flow.json), it crosses into that survey's field; an
  attachment's rows come from outside the project.
- A section as the enumerator met it: `get_group`, with the group's condition, the
  enclosing groups' and each subgroup's.
- Analysis at a repeat's level: read `<stem>_long/<repeat>.parquet` instead of
  reshaping the wide data; a nested repeat joins its parent on the parent's keys.
  `get_repeat_structure` gives each repeat's parent, count variable and expression,
  max iterations and Stata suffix pattern.

## PII: use every column, see only unflagged values

Every column carries `pii.status` with the `basis` for it. The status says where
personal data may be so it is handled carefully; it never means drop the column or
work around it. Scripts use every column: merge on a respondent's name, deduplicate on
a phone number, clean a free-text field, keep them in the output. What changes is what
you see: a flagged column's values never reach the conversation.

A long table's column (`member_name` in `member.parquet`) has no dictionary entry of
its own: it takes the strictest status of the wide columns it stacks
(`member_name_1`, `member_name_2`, ...). `lookup_variable("member_name")` lists them.

| Status | Basis | You may see |
|---|---|---|
| `cleared_by_form` | `publishable`: the XLSForm marks the field publishable | Its values |
| `whitelisted` | `reviewed` (the team's whitelist); `system` (`KEY`, `SubmissionDate`, `starttime`, `duration`, `formdef_version`, ...); `type:integer`, `decimal`, `date`, `datetime`, `time`, `start`, `end`, `today`, `repeat_count`; `choices:inline` (a select with the form's own choices); `generated` (a calculate that makes its value, `once(uuid())`); `lineage:<refs>` (a calculate whose every input is cleared or whitelisted); `project` (`no_personal_data` is set) | Its values |
| `contains_pii` | `reviewed` (the team's blacklist); `device:<col>` (`deviceid`, `username`, ...); `metadata:<type>` (a field that stores device or enumerator metadata); `calls:enumerator-name()` (or `enumerator-id()`, `username()`); `type:geopoint`, `geotrace`, `geoshape`, `image`, `audio`, `video`, `file` (text audits and comments count as files); `lineage:<field>` (a calculate that reads one of these) | Counts and `KEY`s only |
| `may_contain_pii` | `presidio:<ENTITY>` for each entity Presidio found in the values (`[pii] entities` and `id_patterns`: names, phones, emails, places by default); the basis starts with the question's type (and `pulldata` for a preload), or `no_question` for a column no question matches | Counts and `KEY`s only |
| `no_hits` | A scanned column where Presidio found nothing: free text, a select whose choices come from data, a `pulldata()` calculate, a column with no question | Its values; the team may still review it |

Any basis may end with `publishable` when the form marks the field publishable but
`trust_publishable = false`.

For a flagged column, a script prints counts, null rates, distinct counts and the
`KEY`s of the rows a check flags, never the values, `head()`, `list` or
`value_counts()`/`tab`. `pii.presidio.hit_rows` counts the rows with a hit and
`hit_keys` gives up to five of their `KEY`s; quote counts and KEYs, never values.
Never open `<stem>_pii_review.xlsx`: it is the team's. Whether a column leaves a
delivered dataset is the team's decision, not a cleaning default.

A flag the team disagrees with is fixed in `config.toml`: they add the columns they
clear to `[pii] whitelist` and personal ones to `[pii] blacklist` (question names or
Stata names; a `pulldata()` column as `dataset.column`), then re-run
`surveycto-vardict`. A choice list read from data (an external choices CSV, a
`search()` CSV) is withheld when the team blacklisted a question using it, it has more
than 50 choices, or Presidio found personal data in it; `choices_withheld` says why.
Refer to a withheld list by name and source; never reconstruct it from the data. The
binaries of a withheld select_multiple carry its codes in their names, so the tools
show them by position, `<name>_<1>`, `<name>_<2>` (plus `_<instance>` in a repeat);
look one up by that name, and in scripts read the real names from the dictionary
without printing them.

## Reading the dictionary

Each `variables.<column>` entry of `<key>_variable_dictionary.json` has:

- `variable_order`: the position in form order; `non_null_count`, `missing_rate`,
  `is_all_missing`.
- `stata.type`: the native Stata storage type (`double`, `str244`, ...).
- `survey`: the matched question. `original_variable_name`, `question_text`, `type`,
  `choice_list`, `choices` (on a select_multiple binary, only the choice it stands
  for), `stata_skip_logic`, `group_relevances` (outer first), `group_path`,
  `disabled`; when set, `constraint`, `stata_constraint`, `calculation`,
  `choice_filter`, `references` (the `${}` fields it reads), `choices_withheld`, and
  for repeat columns `skip_logic_iteration_specific` (with `index()` replaced by the
  instance number; prefer it to `stata_skip_logic`). A null `original_variable_name`
  means no question matched (a system column, or a calculate the form does not list).
- `repeat_iteration`: the instance number of a column inside a repeat.
- `sentinels`: `raw_int` / `raw_int_detail` (codes such as `-99` still numeric),
  `string` (codes in a text column), `ext_missing` (`.d`, `.r` already recoded),
  `type_mismatch` (the form says number, Stata stores text), `calculate_risk` (a
  calculate with unexplained negatives). Sentinels are **reported, never recoded**:
  the `.dta` keeps `-99` as `-99`, and the user decides how to treat it. Mention the
  counts whenever they are present.
- `data_min` / `data_max`: the observed range of an integer, decimal, calculate, date,
  datetime or time column, present only on columns whose values are shown.
- `pii`: `status`, `basis`, and for a scanned column `presidio` (`entities`,
  `distinct_scanned`, `hit_rows`, `hit_keys`).
- `contract`: present only when the compiled XForm XML was supplied; the column's node
  path, repeat coordinates, choice code and `data_source` (`search()` / `pulldata()`).

## Form patterns (Stata)

- **Expressions** are in XLSForm syntax: `${name}`, `.` for the field itself,
  `selected(${x}, '2')`.
- **Repeats**: a field inside a repeat becomes `<name>_1`, `<name>_2`, ... per instance
  (`<name>_<n>_<m>` when nested). A calculate inside a nested repeat exists only as
  its suffixed columns; strip a skip-logic clause that names it bare before using the
  condition as a Stata `if`. Each repeat has a `<repeat>_count` variable.
- **Position is not the choice code** in a repeat that runs once per item selected in
  an earlier select_multiple: instance 1 is the first item selected, not code 1; map
  instances to codes through the routing variable. In a fixed-count repeat position is
  the code. `tabulate <repeat>_count`: constant means fixed, varying means routed.
- **select_multiple** comes as one binary per choice, `<name>_<code>`, `1` when chosen;
  a negative code becomes a double underscore (`<name>__99`). Inside a repeat the
  choice code comes before the instance: `activity_2_1` is choice 2 of instance 1.
- **Calculates without a question**: SurveyCTO-computed columns with no form row have a
  null `survey.type`; they are valid variables without label, constraint or relevance.
- **`selected()` in Stata**: `selected(${v}, 'N')` is `v == N` for a select_one and
  `v_N == 1` for a select_multiple (`v__N == 1` for a negative code). A dynamic second
  argument cannot be translated and is stripped from `stata_skip_logic`.
- **Missing by logic**: a question is asked only when every `group_relevances`
  condition and its own relevance hold; Stata stores the skipped rows as `.` or `""`.

## Writing cleaning code

Write it for the person who reviews it: a do-file read top to bottom, explicit
variable names, `assert` on what must hold, comments that say why, and no guards for
cases that have not happened. Look at the data first (these tools, the summary stats)
and say what you found about missing values before computing from them.

Before writing or reviewing cleaning code, read the references in this skill's folder:

- `references/CLEANING.md`: cleaning principles (fail loudly, sentinels, skip logic,
  other-specify, select_multiple, repeats, aggregation, verification).
- `references/STATA.md`: Stata patterns and guardrails.
- `references/SURVEYCTO_RELEVANCE_TRANSLATION.md`: SurveyCTO expressions to Stata
  conditions, and what cannot be translated.

They are suggestions: propose them, and let the people cleaning the data decide what
applies.

## Rulings

When the user decides how to handle something (a code, a gate that never fired, a
field the instrument mislabels), write the decision under `### Rulings` in the
`## Survey project` section of the analysis project's `CLAUDE.md`, in their words, as
soon as it is made: a decision left in the conversation is reopened on the next run. Rulings come from the user and the study's
documents; propose one only from those. Never relitigate a recorded ruling.

## Answering

Cite the field and the file you used, state the logic in the form's own `${}` syntax
with the Stata translation when it matters, and include the column's `pii.status`
whenever you discuss its values.
