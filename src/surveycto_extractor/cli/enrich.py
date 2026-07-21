"""Enrich the extractor's variable_dictionary.json with the compiled-form XML contract.

`surveycto-vardict` (Phase 4) maps dataset columns to form questions
with heuristic/fuzzy name matching, which leaves some columns unmatched (dotted
group nodes, `pulldata` preloads, select_multiple within repeats, ...) and can
occasionally mismatch. When the **compiled XForm XML** is available it is the
authoritative, deterministic `column -> node` map. This pass overlays it:

- every mapped column gets a `contract` block (sibling of `survey`): node_path,
  repeat coordinates, select_multiple choice code, `data_source`
  (`search()`/`pulldata()` provenance), `calculate`, and `preload` for system
  fields -- so an MCP-querying agent learns exactly *where each variable comes
  from*;
- columns the fuzzy matcher left unmatched but the XML resolves get their
  `survey.original_variable_name` (and question text / choices) backfilled from
  `questions.json`, marked `resolved_by: "xml"`; a fuzzy false-positive the XML
  contradicts is overridden and marked `corrected_from`;
- select-from-file choice options are resolved from the form's attached CSVs via
  the `search()` appearance;
- a best-effort `formdef_version` distribution + legacy-column (collected-then-
  removed) audit is recorded at the top level.

**Optional and additive.** A dataset is enriched only when its `config.DATASETS`
entry carries an `xml_path` pointing at an existing XForm; otherwise this is a
no-op and the dictionary is left exactly as Phase 4 wrote it. Fuzzy matching
stays the default for the (common) case where no compiled XML was saved.

Usage:
    uv run surveycto-enrich --survey KEY     # one dataset
    uv run surveycto-enrich --all            # every dataset with an xml_path

Config (per-dataset, in config.DATASETS):
    "xml_path":            Path("forms/my_form.xml"),   # compiled XForm (enables this)
    "xml_attachments_dirs": [Path("forms")],            # optional: extra dirs for search() CSVs
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from surveycto_extractor.parsers.xml_contract import parse_contract

_CSV_CACHE: dict[str, list] = {}


def _load_csv_rows(path) -> list:
    key = str(path)
    if key not in _CSV_CACHE:
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            _CSV_CACHE[key] = list(csv.DictReader(fh))
    return _CSV_CACHE[key]


def _resolve_search_choices(attachment_dirs, data_source: dict | None) -> list | None:
    """Resolve a select-from-file's real value/label options from its `search()`.

    The XML data_source gives the dataset (a folder CSV) + a literal filter
    (e.g. {list_name: 'distUnit'}); we read that CSV (searched across
    `attachment_dirs`) and return the matching value/label pairs. A purely
    dynamic filter (node-ref values -> None) can't be enumerated statically, so we
    return None.
    """
    if not data_source or data_source.get("kind") != "search":
        return None
    filt = {k: v for k, v in (data_source.get("filter") or {}).items() if v is not None}
    if not filt:
        return None
    cands: list[Path] = []
    for d in attachment_dirs:
        cands.extend(sorted(Path(d).glob(f"{data_source['dataset']}*.csv")))
    if not cands:
        return None
    out = []
    for row in _load_csv_rows(cands[0]):
        if all((row.get(k) or "").strip() == v for k, v in filt.items()):
            val = (row.get("value") or "").strip()
            if val != "":
                out.append({"value": val, "label": (row.get("label") or "").strip()})
    return out or None


def _load_questions_index(questions_json_path) -> dict:
    idx: dict[str, dict] = {}
    qpath = Path(questions_json_path)
    if qpath.exists():
        for q in json.loads(qpath.read_text(encoding="utf-8")):
            name = q.get("variable_name")
            if name and name not in idx:
                idx[name] = q
    return idx


def _formdef_version_counts(data_path) -> Counter:
    """Best-effort `formdef_version` distribution for drift detection.

    Reads the column from the parquet sidecar (written by Phase 4's load_data)
    if present, else from the `.dta`. Any failure (no file, no column, no
    reader) yields an empty Counter -- drift detection is purely additive.
    """
    versions: Counter = Counter()
    if not data_path:
        return versions
    data_path = Path(data_path)
    pq = data_path.with_suffix(".parquet")
    try:
        if pq.exists():
            import pyarrow.parquet as papq

            schema = papq.read_schema(pq)
            if "formdef_version" in schema.names:
                col = papq.read_table(pq, columns=["formdef_version"]).column(0)
                for v in col.to_pylist():
                    versions[str(v)] += 1
            return versions
        if data_path.exists():
            try:
                import pyreadstat

                df, _ = pyreadstat.read_dta(str(data_path), usecols=["formdef_version"])
            except Exception:
                import pandas as pd

                df = pd.read_stata(str(data_path), columns=["formdef_version"])
            for v in df["formdef_version"].tolist():
                versions[str(v)] += 1
    except Exception as exc:  # noqa: BLE001 — drift is best-effort
        print(f"  (formdef_version drift skipped: {exc})")
    return versions


def enrich_contract(
    output_json,
    questions_json,
    xml_path,
    attachment_dirs=None,
    data_path=None,
) -> dict:
    """Overlay the XML contract onto a variable_dictionary.json, in place.

    Operates on explicit paths so it is decoupled from config; `enrich_dataset`
    wraps this with config.DATASETS lookups.
    """
    dict_path = Path(output_json)
    if not dict_path.exists():
        raise FileNotFoundError(
            f"No variable dictionary at {dict_path}. Run Phase 4 "
            f"(surveycto-vardict) first."
        )
    if attachment_dirs is None:
        attachment_dirs = [Path(xml_path).parent]

    d = json.loads(dict_path.read_text(encoding="utf-8"))
    contract = parse_contract(xml_path)
    qidx = _load_questions_index(questions_json)
    versions = _formdef_version_counts(data_path)

    node_choices: dict[str, list | None] = {}  # resolved search choices, per node
    variables = d.get("variables", {})
    # n_xml_resolved counts only DETERMINISTIC (exact node) resolutions; the
    # heuristic string-key fallback is counted separately so it never inflates the
    # "XML resolved" headline with guesses.
    n_system = n_xml_resolved = n_xml_heuristic = n_corrected = n_choice_lists = 0
    legacy: list[str] = []

    for col, entry in variables.items():
        m = contract.map_column(col)
        if m["kind"] == "system":
            entry["contract"] = {"kind": "system"}
            n_system += 1
            continue
        if m["kind"] == "unmapped":
            entry["contract"] = {
                "kind": "legacy",
                "note": "collected but absent from the deployed-XML contract",
            }
            legacy.append(col)
            continue

        node = contract.nodes[m["node_path"]]
        prior = entry.get("contract") or {}  # a previous run's contract, if any
        # A string-key fallback match is a heuristic, NOT a deterministic node
        # match -- label it so a consumer never reads a guess as authoritative.
        heuristic = m.get("string_key") is not None
        # Surface the useful XML provenance only: where it comes from
        # (data_source = search/pulldata), the derivation (calculate), repeat
        # coords, and preload for SurveyCTO system fields. Constraint/appearance/
        # readonly/required are data-entry concerns, not useful at data landing.
        entry["contract"] = {
            "kind": "matched",
            "node_path": node.path,
            "match": "string_key" if heuristic else "exact",
            "xml_type": node.xml_type,
            "control": node.control,
            "repeat_path": node.repeat_path,
            "repeat_iterations": m["repeat_iterations"],
            "is_select_multiple": m["is_select_multiple"],
            "choice_code": m["choice_code"],
            "string_key": m.get("string_key"),
            "data_source": node.data_source,
            "calculate": node.calculate,
        }
        if m.get("ragged"):
            # Column carried fewer repeat indices than the node's depth (#23.2).
            entry["contract"]["ragged"] = True
        if node.preload:
            entry["contract"]["preload"] = {
                "type": node.preload,
                "params": node.preload_params,
            }

        # XML is authoritative: the contract's column->node mapping wins. Where it
        # disagrees with the fuzzy match, override (correcting a false-positive or
        # filling a miss) and attach the question metadata for the XML-resolved
        # node. Where they already agree, keep the richer existing survey block.
        survey = entry.get("survey") or {}
        prev = survey.get("original_variable_name")
        resolved_label = "xml-heuristic" if heuristic else "xml"
        if prev != node.name:
            q = qidx.get(node.name)
            # prev is None -> XML resolved a fuzzy MISS: merge so we never discard
            # anything the column already carried, then fill from the node's question.
            # Otherwise XML CORRECTED a fuzzy false-positive: the old block described
            # the wrong question, so start clean from the correct node.
            new_survey = dict(survey) if prev is None else {}
            new_survey["original_variable_name"] = node.name
            if not new_survey.get("group_path"):
                new_survey["group_path"] = "/".join(node.group_path)
            if q:
                # Copy the full set of question-derived fields the Phase-4 survey
                # block carries, so a resolved/corrected variable gets a complete
                # block from the *correct* node, not a stub.
                for k in (
                    "question_text",
                    "type",
                    "choice_list",
                    "choices",
                    "stata_skip_logic",
                    "group_relevances",
                    "calculation",
                    "references",
                    "disabled",
                    "constraint",
                    "stata_constraint",
                    "choice_filter",
                ):
                    if q.get(k) not in (None, "", []):
                        new_survey[k] = q.get(k)
            entry["survey"] = new_survey
            if prev:
                entry["contract"]["corrected_from"] = prev
                n_corrected += 1
            else:
                entry["contract"]["resolved_by"] = resolved_label
                if heuristic:
                    n_xml_heuristic += 1
                else:
                    n_xml_resolved += 1
        else:
            # Names already agree. Carry forward a prior run's resolution markers so
            # re-running enrichment on an ALREADY-ENRICHED dictionary is idempotent
            # (markers + counters). Note: the Phase-4 hook writes a fresh dictionary
            # each build before enriching, so `prior` is empty there -- this branch
            # only fires on a standalone `surveycto-enrich` re-run.
            if prior.get("resolved_by") == "xml-heuristic":
                entry["contract"]["resolved_by"] = "xml-heuristic"
                n_xml_heuristic += 1
            elif prior.get("resolved_by"):
                entry["contract"]["resolved_by"] = prior["resolved_by"]
                n_xml_resolved += 1
            elif prior.get("corrected_from"):
                entry["contract"]["corrected_from"] = prior["corrected_from"]
                n_corrected += 1

        # Resolve real value/label options for select-from-file fields from the
        # XML data_source (the from-file choice labels the fuzzy path left as a
        # template placeholder). Cached per node; reused across repeat instances.
        if node.data_source and node.data_source.get("kind") == "search":
            if node.path not in node_choices:
                resolved = _resolve_search_choices(attachment_dirs, node.data_source)
                node_choices[node.path] = resolved
                if resolved:
                    n_choice_lists += 1
            resolved = node_choices[node.path]
            if resolved:
                entry["survey"]["choices"] = resolved
                entry["survey"]["choice_list"] = node.data_source["dataset"]
                code = m["choice_code"]
                if code is not None:
                    entry["contract"]["choice_label"] = next(
                        (c["label"] for c in resolved if c["value"] == str(code)), None
                    )

    matched_total = sum(
        1
        for e in variables.values()
        if (e.get("survey") or {}).get("original_variable_name")
    )

    d["formid"] = contract.formid
    d["contract_source_version"] = contract.formdef_version
    if versions:
        d["formdef_versions_in_data"] = dict(versions)
    d.setdefault("sources", {})["xml"] = str(xml_path)
    d.setdefault("summary", {})  # Phase 4 always writes it; guard hand-edited dicts
    d["summary"]["matched_after_xml"] = matched_total
    d["summary"]["resolved_by_xml"] = n_xml_resolved
    d["summary"]["resolved_by_xml_heuristic"] = n_xml_heuristic
    d["summary"]["corrected_by_xml"] = n_corrected
    d["summary"]["system_columns"] = n_system
    d["summary"]["legacy_columns"] = len(legacy)
    d["summary"]["choice_lists_resolved_from_xml"] = n_choice_lists
    d["audit"] = {
        "system_columns": n_system,
        "resolved_by_xml": n_xml_resolved,
        "resolved_by_xml_heuristic": n_xml_heuristic,
        "corrected_by_xml": n_corrected,
        "choice_lists_resolved_from_xml": n_choice_lists,
        "legacy_columns": legacy,
        "formdef_version_drift": len(versions) > 1,
    }

    dict_path.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    drift = len(versions) > 1
    print(
        f"[{dict_path.name}] enriched: "
        f"matched_after_xml={matched_total}/{len(variables)} "
        f"(+xml_resolved={n_xml_resolved}, +xml_heuristic={n_xml_heuristic}, "
        f"corrected_fuzzy={n_corrected}) "
        f"system={n_system} legacy={len(legacy)} "
        f"choice_lists_from_xml={n_choice_lists} "
        f"formdef_versions={len(versions)} drift={drift}"
    )
    return d


def _dataset_xml_path(cfg: dict):
    """Return the configured compiled-XForm path for a dataset, or None."""
    xp = cfg.get("xml_path")
    if not xp:
        return None
    xp = Path(xp)
    if not xp.exists():
        print(f"  (xml_path configured but missing: {xp} -- skipping enrichment)")
        return None
    return xp


def enrich_dataset(dataset_name: str, cfg: dict | None = None) -> dict | None:
    """Enrich one dataset's dictionary from its config entry. No-op without xml_path.

    `cfg` may be passed by the Phase-4 caller (which already has it); otherwise it
    is loaded from config.DATASETS.
    """
    if cfg is None:
        from surveycto_extractor.config_loader import get_datasets

        cfg = get_datasets()[dataset_name]

    xml_path = _dataset_xml_path(cfg)
    if xml_path is None:
        return None

    json_key = "questions_json" if "questions_json" in cfg else "json"
    attachment_dirs = [Path(p) for p in cfg.get("xml_attachments_dirs", [])]
    attachment_dirs.append(xml_path.parent)

    print(f"\n[{dataset_name}] overlaying XML contract from {xml_path.name}")
    return enrich_contract(
        output_json=cfg["output_json"],
        questions_json=cfg[json_key],
        xml_path=xml_path,
        attachment_dirs=attachment_dirs,
        data_path=cfg.get("data"),
    )


def main() -> None:
    """Overlay the compiled-XForm contract onto one or all variable dictionaries."""
    parser = argparse.ArgumentParser(
        description="Overlay the compiled-XForm contract onto a variable dictionary."
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--survey", metavar="KEY", help="one dataset key from config.DATASETS"
    )
    g.add_argument(
        "--all", action="store_true", help="every dataset that has an xml_path"
    )
    args = parser.parse_args()

    from surveycto_extractor.config_loader import get_datasets

    DATASETS = get_datasets()

    if not DATASETS:
        print("ERROR: config.DATASETS is empty. Add survey entries to config.toml.")
        sys.exit(1)

    if args.survey:
        if args.survey not in DATASETS:
            print(
                f"ERROR: '{args.survey}' not in config.DATASETS. "
                f"Available: {', '.join(DATASETS.keys())}"
            )
            sys.exit(1)
        keys = [args.survey]
    else:
        keys = list(DATASETS.keys())

    any_enriched = False
    for key in keys:
        result = enrich_dataset(key, DATASETS[key])
        any_enriched = any_enriched or (result is not None)
    if not any_enriched:
        print(
            "\nNo datasets enriched (none have a usable 'xml_path' in config.DATASETS)."
        )


if __name__ == "__main__":
    main()
