"""Dependency-acceptance probe: the MCP server smoke test.

Run by the `dep-accept` harness inside each candidate worktree (see
`.dep-accept.toml`), which invokes it as `python scripts/dep_probe_mcp.py --out
PATH` with that candidate's dependency set installed and `SURVEYCTO_CONFIG`
pointed at the sample config. It writes a JSON object the harness diffs against
the `main` baseline.

The optional `mcp` extra is the part of this toolkit no file comparison can
cover: the server has to *import* and answer. Importing is the check that matters
most for an `mcp` bump, because the SDK has already relocated its server class
once -- 1.x's `mcp.server.fastmcp.FastMCP` became 2.x's
`mcp.server.mcpserver.MCPServer` -- so a version range that looks harmless in a
lockfile can break the server outright.

Keys ending in `_raw` or starting with `info_` are reported by the harness but
never counted as drift, so the resolved mcp version is recorded without making
every bump look like a regression.
"""

import argparse
import hashlib
import json
from pathlib import Path


def digest(text: str) -> str:
    """Return the sha256 of a string, for comparing tool output across candidates."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def probe() -> dict:
    """Import the MCP server, build its store from config, and exercise two tools."""
    result: dict = {"import_ok": False}

    try:
        from surveycto_extractor.mcp_server import survey_server as srv
    except Exception as exc:
        result["import_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["import_ok"] = True
    result["server_type"] = type(srv.server).__name__

    try:
        from importlib.metadata import version

        result["info_mcp_version"] = version("mcp")
    except Exception:
        result["info_mcp_version"] = None

    try:
        store = srv._get_store()
    except Exception as exc:
        result["store_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["surveys_loaded"] = sorted(store._vardicts)
    info = store.get_info()
    result["get_info_sha256"] = digest(info)
    result["get_info_len"] = len(info)

    # Look up a variable this run itself produced, so the probe never hardcodes a
    # name that a form edit could invalidate.
    name = None
    for survey_vars in store._vardicts.values():
        if survey_vars:
            name = sorted(survey_vars)[0]
            break
    result["lookup_name"] = name
    if name:
        text = store.lookup_var(name, context=3)
        result["lookup_sha256"] = digest(text)
        result["lookup_len"] = len(text)
        result["lookup_found"] = "not found" not in text.lower()

    # Tool registration goes through MCPServer itself, so listing the tools
    # proves the decorator side of the API still works.
    try:
        import asyncio

        tools = asyncio.run(srv.server.list_tools())
        result["tools"] = len(tools)
        result["tool_names"] = sorted(getattr(t, "name", str(t)) for t in tools)
    except Exception as exc:
        result["tools_error"] = f"{type(exc).__name__}: {exc}"

    return result


def main() -> int:
    """Write the probe result as JSON to the path given by ``--out``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Path to write the JSON result to")
    args = parser.parse_args()

    Path(args.out).write_text(
        json.dumps(probe(), indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"[dep-probe:mcp] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
