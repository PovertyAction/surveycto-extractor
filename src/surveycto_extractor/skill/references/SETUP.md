# Setting up a project

Follow this when the user asks to set the project up, or when the `## Survey project`
section of the analysis project's `CLAUDE.md` is still the template `surveycto-skill`
wrote. A cleaning or analysis task does not wait for it.

## The `CLAUDE.md` section

`## Survey project` in the analysis project's `CLAUDE.md` is the one home for what the
team knows that no output records. Fill it with the user, in their words:

- **The study**: what the project is about. Ask for what describes it (a pre-analysis
  plan, a protocol, a codebook) and remind them to share only documents they are
  allowed to share. Ask the user to say in a few sentences what it is about; do not
  write it from the questions. The same words go in `config.toml [project]
  description`.
- **Pipelines**: which survey each cleaning or analysis pipeline reads, by path
  (`scripts/cleaning/ltfu/` reads `ltfu_hh` and `ltfu_adult`).
- **Conventions**: how the team cleans: missing and don't-know codes, outliers and
  where they are treated (in cleaning, or at analysis), units and currency, file
  layout, naming.
- **Rulings**: decisions on specific fields, written as they are made and never
  reopened.

## How the surveys connect

1. Run `uv run surveycto-links`: it lists every dataset a form reads, the survey that
   carries its columns, the columns it is matched on, and fields shared across
   surveys, as candidate `[server_datasets.*]` and `[entities.*]` tables. Which form
   publishes a dataset is a server setting no form records, so show each candidate to
   the user and ask them to confirm or correct it; give each entity a one-line
   description from the documents.
2. Write the confirmed tables into `config.toml`, run `uv run surveycto-flow` (it
   checks each key against the forms and counts repeated and blank keys), then
   `uv run surveycto-duckdb` for the project database.
3. Rerun `uv run surveycto-skill` so this skill's `## This project` section shows the
   new outputs, and ask the user to reconnect the MCP so `get_project` and
   `get_structure_db` serve them.
