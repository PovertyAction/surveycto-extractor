# Survey Expert MCP Server

`surveycto-mcp` serves the extractor's outputs to an agent over stdio. Every tool
is a thin call into `surveycto_extractor/survey.py`, the same module the skill
copies into a project as `search_survey.py`, so the shell fallback answers the
same way.

## Setup

From the analysis project's root:

```bash
uv run surveycto-skill
```

It installs the skill, adds this entry to `.mcp.json`, and installs the spaCy
models your `[pii]` config names:

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "uv",
      "args": ["run", "surveycto-mcp"]
    }
  }
}
```

The server reads `config.toml` (or a legacy `config.py`) in the **current working
directory**, so launch it from the project root, or set `SURVEYCTO_CONFIG` in the
entry's `env` to point at a config elsewhere. A missing or broken config starts it
empty, and the tools say what to run.

## Tools

All take an optional `survey` (partial match on the survey key).

| Tool | Purpose | Shell (`search_survey.py`) |
|------|---------|----------------------------|
| `lookup_variable` | One variable in full: question, logic, choices, sentinels, range, `pii` | `var NAME [--context N]` |
| `lookup_variables` | Many variables in brief | `var NAME [NAME ...]` |
| `search_questions` | TF-IDF ranked search over names and question text | `search WORDS [--max N]` |
| `get_choice_list` | A choice list and the variables using it | `choices LIST` |
| `get_gate_chain` | The composed skip logic behind a variable | `gates NAME` |
| `get_variable_neighborhood` | What feeds, gates, constrains or repeats with it | `neighborhood NAME [--depth N]` |
| `get_group` | A group's questions in order, with each condition | `group GROUP` |
| `trace_lineage` | Where a value comes from: calculates, pulldata/search, answered questions | `lineage NAME` |
| `get_repeat_structure` | Repeat nesting, counts, suffixes, join keys | `repeats` |
| `get_project` | What the project is about; datasets linking the surveys; entities and candidate keys | `project` |
| `get_structure_db` | Map of `<project>.duckdb`: schemas, a schema, or a table's columns | `db [SCHEMA [TABLE]]` |
| `get_survey_info` | Overview of the loaded surveys | `info` |

Flagged PII columns (`contains_pii`, `may_contain_pii`) show counts, never values,
in every answer.

## How it works

On startup the store loads each dataset's `variable_dictionary.json`, the
`questions.json` and `variable_graph.json` beside it, builds in-memory indexes,
and reloads when a file changes, so a rebuilt dictionary is served on the next
query.

## Verify it works

```bash
uv run surveycto-mcp
# Should print: [survey-expert] Loaded N survey(s): X variables, Y questions
# Then waits for MCP stdio input (Ctrl+C to stop)
```
