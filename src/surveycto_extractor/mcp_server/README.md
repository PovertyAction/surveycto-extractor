# Survey Expert MCP Server (optional add-on)

Keeps survey metadata in memory for instant variable lookups.
Complement to `skill/search_survey.py` for high-volume query sessions
(10-50+ variable lookups per cleaning module).

## Prerequisites

- Python 3.10+
- The package installed with the `mcp` extra: `uv sync --extra mcp`
- `config.py` in your project directory with `DATASETS` entries (Phase 4 output)
- Variable dictionary JSON files generated (`uv run surveycto-vardict`)
- `networkx` for the variable relationship graph (installed by default with `uv sync`)

## Setup

1. Install the MCP dependency:

```bash
uv sync --extra mcp
```

2. Add to your project's `.mcp.json` (in the project root):

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "uv",
      "args": ["run", "--extra", "mcp", "python", "-m", "surveycto_extractor.mcp_server.survey_server"]
    }
  }
}
```

The same block works in `.claude/settings.json`. A ready-made
`.mcp.json.example` sits in the repo root (`cp .mcp.json.example .mcp.json`).

The server discovers `config.py` in the **current working directory** (launch it
from your project root). To point elsewhere, set the `SURVEYCTO_CONFIG` env var:

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "uv",
      "args": ["run", "--extra", "mcp", "python", "-m", "surveycto_extractor.mcp_server.survey_server"],
      "env": {
        "SURVEYCTO_CONFIG": "/absolute/path/to/config.py"
      }
    }
  }
}
```

## Tools

| Tool | Purpose | When to use |
|------|---------|-------------|
| `lookup_variable` | Full metadata for one variable (includes data range) | Deep investigation of a specific variable |
| `lookup_variables` | Compact metadata for many variables (includes data range) | Batch-checking a cleaning module (10-50 vars) |
| `search_questions` | TF-IDF ranked search across names and question text | Discovering variables by natural-language query (e.g. "crop sales quantity") |
| `get_choice_list` | All choices + variables using a list | Understanding categorical domains, verifying sentinels |
| `get_gate_chain` | Full composed skip logic tree for a variable | Understanding why a variable has missing/zero values |
| `get_variable_neighborhood` | Relationship graph around a variable | Before modifying/recoding a variable -- shows what depends on it, what gates it, repeat siblings |
| `get_repeat_structure` | Repeat group topology tree | Before writing reshape/merge code -- shows nesting, count variables, max iterations, join keys |
| `get_survey_info` | Dataset overview | Getting oriented before diving in |

## How it works

On startup the server loads all `questions.json` and `variable_dictionary.json`
files referenced in `config.DATASETS`. It builds in-memory indexes for O(1)
lookups and watches file modification times -- when you regenerate the variable
dictionary, the server reloads automatically on the next query.

## Verify it works

```bash
# Test the server loads correctly (run from your project root, where config.py lives)
uv run --extra mcp python -m surveycto_extractor.mcp_server.survey_server
# Should print: [survey-expert] Loaded N survey(s): X variables, Y questions
# Then wait for MCP stdio input (Ctrl+C to stop)
```

## Skill vs MCP

| | `skill/search_survey.py` | `surveycto_extractor.mcp_server` |
|---|---|---|
| Loads JSON | Every call | Once at startup |
| Dependencies | None (stdlib only) | `mcp[cli]` |
| Setup | Copy to .claude/skills/ | Add to .mcp.json |
| Best for | Occasional lookups | Cleaning sessions (10-50+ lookups) |
| Batch queries | Not supported | `lookup_variables` tool |
| Gate chain | `--gate-chain` flag | `get_gate_chain` tool |
| Neighborhood | `--neighborhood` flag | `get_variable_neighborhood` tool |
| Repeat tree | `--repeat-tree` flag | `get_repeat_structure` tool |
| Data range | Shown in output (from vardict) | Shown in lookup tools (from vardict) |
| Survey filter | `--survey KEY` flag | `survey` parameter on every tool |

Both can coexist. The skill is the default; the MCP is the add-on for heavy use.
