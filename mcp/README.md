# Survey Expert MCP Server (optional add-on)

Keeps survey metadata in memory for instant variable lookups.
Complement to the `skill/search_survey.py` for high-volume query sessions
(10-50+ variable lookups per cleaning module).

## Prerequisites

- Python 3.10+
- `config.py` configured with `DATASETS` entries (Phase 4 output required)
- Variable dictionary JSON files generated (`create_variable_dictionaries.py`)

## Setup

1. Install the MCP dependency:

```bash
pip install "mcp[cli]"
```

2. Add to your project's `.mcp.json` (in the project root):

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "python",
      "args": ["path/to/surveycto_extractor/mcp/survey_server.py"]
    }
  }
}
```

Or in `.claude/settings.json`:

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "python",
      "args": ["path/to/surveycto_extractor/mcp/survey_server.py"]
    }
  }
}
```

The server finds `config.py` in its parent directory automatically.
To override, set `SURVEY_CONFIG` env var:

```json
{
  "mcpServers": {
    "survey-expert": {
      "command": "python",
      "args": ["path/to/surveycto_extractor/mcp/survey_server.py"],
      "env": {
        "SURVEY_CONFIG": "/absolute/path/to/config.py"
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
| `search_questions` | Keyword search across names and question text | Discovering related variables |
| `get_choice_list` | All choices + variables using a list | Understanding categorical domains, verifying sentinels |
| `get_gate_chain` | Full composed skip logic tree for a variable | Understanding why a variable has missing/zero values |
| `get_survey_info` | Dataset overview | Getting oriented before diving in |

## How it works

On startup the server loads all `questions.json` and `variable_dictionary.json`
files referenced in `config.DATASETS`. It builds in-memory indexes for O(1)
lookups and watches file modification times -- when you regenerate the variable
dictionary, the server reloads automatically on the next query.

## Verify it works

```bash
# Test the server loads correctly
python mcp/survey_server.py
# Should print: [survey-expert] Loaded N survey(s): X variables, Y questions
# Then wait for MCP stdio input (Ctrl+C to stop)
```

## Skill vs MCP

| | skill/search_survey.py | mcp/survey_server.py |
|---|---|---|
| Loads JSON | Every call | Once at startup |
| Dependencies | None (stdlib only) | `mcp[cli]` |
| Setup | Copy to .claude/skills/ | Add to .mcp.json |
| Best for | Occasional lookups | Cleaning sessions (10-50+ lookups) |
| Batch queries | Not supported | `lookup_variables` tool |
| Gate chain | `--gate-chain` flag | `get_gate_chain` tool |
| Data range | Shown in output (from vardict) | Shown in lookup tools (from vardict) |
| Survey filter | `--survey KEY` flag | `survey` parameter on every tool |

Both can coexist. The skill is the default; the MCP is the add-on for heavy use.
