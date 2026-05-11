# SurveyCTO Reference Docs (vendored)

This directory contains read-only copies of two reference files from SurveyCTO's official agent-skill. Our SurveyCTO-to-Stata converter uses them as the authoritative source for XLSForm column semantics and expression syntax.

## Files

| File | Purpose |
|---|---|
| `xlsform.md` | XLSForm worksheet structure, column reference, field type catalog, choice filters, settings, groups and repeats. |
| `expressions.md` | Expression language: references, operators, function catalog, ODK/XPath divergences, worked patterns, common pitfalls. |

## Source

- Upstream repo: https://github.com/surveycto/surveycto-agent-skill (Apache-2.0)
- Branch: `develop`
- Pinned commit: `6d2e6d37bc26abd4a67e1517f43a12f93c5f241d`
- Upstream paths: `references/xlsform.md`, `references/expressions.md`

## Rules

1. Do not edit these files in place. They are a mirror.
2. To refresh: rerun the download against a newer commit on `develop`, update the pinned SHA above, and review the diff for changes that affect `transformers/logic_converter.py` coverage or the type catalog at `_type_catalog.json`.
3. The vendored content is Apache-2.0 licensed by SurveyCTO. Attribution is preserved by this README and the source-comment at the top of each file.

## What was NOT vendored and why

The upstream skill bundles other content that we deliberately exclude:

- `SKILL.md` — SurveyCTO's authoring/debugging orchestrator. We consume forms; we do not author them. Their orchestrator targets form creation from templates, plug-in development, dataset XML, and Data Explorer dashboards, all of which are out of scope here.
- `references/field-plugins.md` — Plug-ins generate values that fall back into the standard XLSForm type system anyway, so the plug-in surface adds nothing to the variable dictionary.
- `references/data-explorer.md`, `references/datasets-xml.md`, `references/mcp.md`, `references/overview.md` — Server-side, dashboard, and tooling concerns that are not part of our extractor pipeline.

## Refresh procedure

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/surveycto/surveycto-agent-skill/develop/references/xlsform.md" -OutFile "coding_guidelines\surveycto_refs\xlsform.md"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/surveycto/surveycto-agent-skill/develop/references/expressions.md" -OutFile "coding_guidelines\surveycto_refs\expressions.md"
```

After refresh, also rerun `python generators/build_type_catalog.py` to regenerate `_type_catalog.json` from the updated `xlsform.md`.
