# Output files, dictionary fields and the shell fallback

Read this when the MCP tools are not connected, or when you need a file or field the
tools do not show (a whole section, `structure.txt`, a dictionary field by name).

## Contents

- [The output files](#the-output-files)
- [Without the MCP: search_survey.py](#without-the-mcp-search_surveypy)
- [Reading the dictionary](#reading-the-dictionary)
- [Installing and updating the skill](#installing-and-updating-the-skill)

## The output files

Each survey has a key (the `[surveys.KEY]` table in `config.toml`) and a docs folder
(its `output_dir`); the data files sit next to the `.dta` (`[datasets.KEY] data`).

| File | Where | What it holds |
|---|---|---|
| `<key>_questions.json` | docs folder | One entry per question, keyed by its XLSForm `name`, with the Stata translation of its skip logic and constraint |
| `sections/` | docs folder | `questions.json` sliced per group |
| `<key>_structure.txt` | docs folder | The form's group and repeat hierarchy, for reading by eye |
| `<key>_variable_dictionary.json` (+ `.xlsx` with `--xlsx`) | docs folder | One entry per `.dta` variable, in form order: its question, Stata type, counts, sentinels, `pii`, and the range of a column whose values are shown |
| `<key>_variable_graph.json` | docs folder | Questions linked by calculation, relevance, group relevance, constraint and repeat, plus the repeat tree |
| `<stem>.parquet` | next to the `.dta` | The submissions, every column; a missing string is null here (`""` in the `.dta`), as in the long tables |
| `<stem>_long/<repeat>.parquet` | next to the `.dta` | One table per repeat: a row per instance that has any value, keyed by `KEY` and a `<repeat>_index` per enclosing repeat, outer first (`key, member_index, job_index`); columns lose the instance suffix (`member_age_2` -> `member_age`). A question whose columns do not all carry their instance numbers stays only in `<stem>.parquet` |
| `<stem>_pii_review.xlsx` | next to the `.dta` | Every value of a flagged column, for the team to verify the flags. **Never open it** |
| `<project>.duckdb` | `[project] output_dir` | Per survey a `submissions` view, a view per long table and the `dictionary` (with `pii_status`), `questions`, `choices` and `edges` tables; in `main`, the surveys, datasets, reads and joins from `flow.json`. Views read the parquets by full path, so the file holds no response values; rerun `surveycto-duckdb` after `surveycto-vardict` |
| `<project>_db.json` / `_db.sql` | `[project] output_dir` | The database's map: every table and view, what one row is, its key, joins and columns. Read it through `get_structure_db`, never `DESCRIBE` or `information_schema`: a withheld list's columns carry its codes in their names, which the map shows by position |
| `<key>_summary_stats.do` | next to the `.dta` | `tabstat` per numeric variable grouped by skip condition; flagged columns get no stats |

## Without the MCP: search_survey.py

The same answers as the MCP tools come from `search_survey.py` in the skill's folder:
standard-library Python, no package or MCP needed.

```sh
uv run python .claude/skills/survey-expert/search_survey.py [--dir DOCS] [--survey KEY] <command>
```

`--dir` defaults to the current directory, scanned for `*_variable_dictionary.json`;
pass the docs folder when the outputs live elsewhere. A miss exits 1. A dictionary
built before the PII pass is refused: re-run `surveycto-vardict`.

| MCP tool | Command |
|---|---|
| `lookup_variable(name, context=0)` | `var NAME [--context N]` |
| `lookup_variables(names)` | `var NAME [NAME ...]` |
| `search_questions(query, max_results=20)` | `search WORDS [--max N]` |
| `get_choice_list(list_name)` | `choices LIST` |
| `get_gate_chain(name)` | `gates NAME` |
| `get_variable_neighborhood(name, depth=1)` | `neighborhood NAME [--depth N]` |
| `get_group(group, detail)` | `group GROUP [--detail]` |
| `trace_lineage(name)` | `lineage NAME` |
| `get_repeat_structure()` | `repeats` |
| `get_survey_info()` | `info` |
| `get_project()` | `project` |
| `get_structure_db(schema, table)` | `db [SCHEMA [TABLE]]` |

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
  calculate with unexplained negatives).
- `data_min` / `data_max`: the observed range of an integer, decimal, calculate, date,
  datetime or time column, present only on columns whose values are shown.
- `pii`: `status`, `basis`, and for a scanned column `presidio` (`entities`,
  `distinct_scanned`, `hit_rows`, `hit_keys`).
- `contract`: present only when the compiled XForm XML was supplied; the column's node
  path, repeat coordinates, choice code and `data_source` (`search()` / `pulldata()`).

## Installing and updating the skill

`uv run surveycto-skill` writes the skill into `.claude/skills/survey-expert/` with its
project section, adds the `## Survey project` section to `CLAUDE.md` when missing,
adds the `survey-expert` server (`uv run surveycto-mcp`) to `.mcp.json`, and installs
the spaCy models the `[pii]` config names. Rerun it after upgrading the toolkit or
rebuilding the outputs.
