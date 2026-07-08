"""Parse a deployed SurveyCTO form XML into the form<->database column contract.

The compiled XForm is the authoritative statement of what gets stored in the
database. This module reads it into a node model that is the *structural spine*
of the variable dictionary:

- `<instance>` (the primary one, whose root `id` == the form_id) gives the node
  hierarchy. An element carrying `jr:template=""` is a **repeat group**; its
  fields are suffixed `_N` per instance in the wide export (nested repeats -> one
  index per level, outer->inner, e.g. `..._113_1`).
- Each `<bind nodeset=... type=...>` gives a node's type plus
  calculate/relevant/constraint/required/readonly. `calculate="pulldata(...)"`
  records preload provenance (which attached CSV fills the field).
- `<h:body>` controls distinguish `select1` (select_one) from `select`
  (select_multiple); a select_multiple expands to per-choice binaries in wide,
  carrying one extra trailing index = the choice code.

This is deliberately the database-side contract. `questions.json` (built by the
extractor's Phase 2 from the XLSForm + external choices CSV) enriches each node
with question text / choice labels / skip logic; the wide dataset realizes the
nodes into actual columns. `enrich_with_contract.py` overlays this contract onto
the variable dictionary produced by `create_variable_dictionaries.py`.

Pure stdlib (`xml.etree.ElementTree`) -- no third-party XML dependency. The
parsing/resolution logic here is project-agnostic; the CLI takes explicit paths.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

# pulldata()/search() args are parsed by the balanced, quote-aware
# _split_call_args walker below (a node value at the group level is still a
# stored column, so we key the model by the leaf token name and keep the full
# path for disambiguation).
_SELECT_CONTROL = {"select": "select_multiple", "select1": "select_one"}

# Wide columns that are SurveyCTO transport/metadata, not form nodes.
_SYS_EXACT = {
    "KEY", "SubmissionDate", "CompletionDate", "starttime", "endtime", "deviceid",
    "subscriberid", "simid", "devicephonenum", "username", "duration", "caseid",
    "formdef_version", "review_status", "review_comments", "review_corrections",
    "review_quality", "instanceID", "instanceName", "SubmitterID", "SubmitterName",
    "X_searchText",
}
_SYS_PREFIX = ("meta-", "SET-OF-")


def _ln(tag) -> str:
    """Local-name of a possibly namespaced ElementTree tag."""
    return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag


def is_system_column(col: str) -> bool:
    """True if a wide column is SurveyCTO transport/metadata, not a form node."""
    return col in _SYS_EXACT or col.startswith(_SYS_PREFIX) or col.endswith("-Comment")


@dataclass
class Node:
    """One stored node in the compiled form (a field, or a bound group)."""

    path: str                       # path below the form root, slash-joined
    name: str                       # leaf token (last path segment)
    group_path: list[str]           # ancestor tokens (groups + repeats), outer->inner
    repeat_path: list[str]          # ancestor tokens that are repeats, outer->inner
    repeat_depth: int = 0
    is_leaf: bool = True            # False = value bound at a group node
    xml_type: str | None = None     # bind type (string/decimal/select/...)
    control: str | None = None      # body control: select_one/select_multiple/input/...
    is_select_multiple: bool = False
    calculate: str | None = None
    relevant: str | None = None
    constraint: str | None = None
    required: bool = False
    readonly: bool = False
    pulldata: dict | None = None  # {dataset, value, key} from pulldata() calc
    preload: str | None = None         # jr:preload (system source, e.g. property)
    preload_params: str | None = None  # jr:preloadParams (e.g. username, deviceid)
    # Where the field's content/options come from, parsed from search()/pulldata():
    #   {kind:"search", dataset, mode, filter} | {kind:"pulldata", dataset, value, key}
    data_source: dict | None = None


# search('dataset','mode','col','val'[,'col2','val2'...]) on a select's appearance.


def _split_call_args(s: str | None, func_name: str) -> list[str] | None:
    """Top-level argument strings of the first ``func_name(...)`` call in ``s``,
    respecting nested parens and quoted literals. XPath has no backslash escape,
    so a literal closes on the next matching quote. Returns None if no balanced
    call is present. Replaces the old bounded regexes, which mis-split a nested
    call or a quoted `)` in an argument. (#23.4)"""
    if not s:
        return None
    rx = re.compile(rf"(?<![\w-]){re.escape(func_name)}\s*\(", re.IGNORECASE)
    m = rx.search(s)
    if not m:
        return None
    i, n = m.end(), len(s)
    depth = 1
    in_str: str | None = None
    args: list[str] = []
    cur: list[str] = []
    while i < n:
        ch = s[i]
        if in_str:
            cur.append(ch)
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(cur))
                return [a.strip() for a in args]
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    return None  # unbalanced


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    return s


def _parse_pulldata(calc: str | None) -> dict | None:
    args = _split_call_args(calc, "pulldata")
    if not args or len(args) < 3:
        return None
    return {"dataset": _unquote(args[0]), "value": args[1].strip(),
            "key": _unquote(args[2])}


def _parse_search(appearance: str | None) -> dict | None:
    """Parse a select's `search(...)` appearance into a choice-source descriptor.

    `search('choices','matches','list_name','treatTarget')` ->
      {kind:"search", dataset:"choices", filter:{"list_name":"treatTarget"}} (+mode).
    A column whose value is a node-ref (not a quoted literal) maps to None
    (dynamic filter); the literal pairs resolve options from the CSV.
    """
    if not appearance or "search(" not in appearance:
        return None
    args = _split_call_args(appearance, "search")
    if not args:
        return None
    dataset = _unquote(args[0])
    rest = args[1:]
    mode = _unquote(rest[0]) if rest else None
    pairs = rest[1:]
    filt: dict[str, str | None] = {}
    for i in range(0, len(pairs) - 1, 2):
        col = _unquote(pairs[i])
        val_raw = pairs[i + 1].strip()
        # a quoted literal is a static filter value; an unquoted node-ref is
        # dynamic -> None
        filt[col] = _unquote(val_raw) if val_raw[:1] in ("'", '"') else None
    if len(pairs) % 2 == 1:  # trailing col with a dynamic (node-ref) value
        filt[_unquote(pairs[-1])] = None
    return {"kind": "search", "dataset": dataset, "mode": mode, "filter": filt}


def _parse_data_source(appearance: str | None, calc: str | None) -> dict | None:
    """Unify select-from-file (`search`) and preload (`pulldata`) provenance."""
    s = _parse_search(appearance)
    if s:
        return s
    pd = _parse_pulldata(calc)
    if pd:
        return {"kind": "pulldata", **pd}
    return None


class FormContract:
    """The compiled-form node model + the wide-column -> node resolver."""

    def __init__(
        self, formid: str, formdef_version: str | None, nodes: dict[str, Node]
    ):
        self.formid = formid
        self.formdef_version = formdef_version
        self.nodes: dict[str, Node] = nodes
        # leaf token -> [paths], used to resolve a de-indexed wide base name.
        # Built from sorted paths so resolution is DETERMINISTIC: nodes is keyed
        # off a set union (hash-seed-dependent iteration), so without sorting the
        # candidate order — and thus which node a homonym column maps to — would
        # vary between processes. Sorted path order gives a stable tie-break.
        self._by_name: dict[str, list[str]] = {}
        for path in sorted(nodes):
            self._by_name.setdefault(nodes[path].name, []).append(path)

    @property
    def repeat_groups(self) -> list[str]:
        seen: list[str] = []
        for n in self.nodes.values():
            for r in n.repeat_path:
                if r not in seen:
                    seen.append(r)
        return seen

    def map_column(self, col: str) -> dict:
        """Resolve a wide column to its node + repeat iterations + choice code.

        Returns {kind, ...}. kind is one of: 'system', 'matched', 'unmapped'.
        For 'matched': node_path, repeat_iterations (list of (group, index)),
        choice_code (int|None), is_select_multiple.
        """
        if is_system_column(col):
            return {"kind": "system"}

        # Peel trailing _<int> groups right-to-left, then reverse to outer->inner.
        # Track the raw digit strings in parallel so a leading-zero choice code
        # (`field_01`) can be preserved as a string instead of collapsing to 1.
        idxs: list[int] = []
        raws: list[str] = []
        base = col
        while True:
            m = re.search(r"_(\d+)$", base)
            if not m:
                break
            raws.append(m.group(1))
            idxs.append(int(m.group(1)))
            base = base[: m.start()]
        idxs.reverse()
        raws.reverse()

        candidates = self._by_name.get(base) or self._by_name.get(col)

        # Negative select_multiple choice code: SurveyCTO writes `-66` as a
        # dash->underscore `field__66`, so the positive peel above leaves a base
        # ending in `_` (e.g. `field_`) with the code as idxs[0]. Re-interpret
        # against the real base with a negated leading code. (#23.1)
        neg_choice = False
        if not candidates and base.endswith("_") and idxs:
            real_base = base[:-1]
            real_cands = self._by_name.get(real_base)
            if real_cands and any(self.nodes[p].is_select_multiple for p in real_cands):
                candidates = real_cands
                base = real_base
                neg_choice = True

        if not candidates:
            # String-keyed runtime columns: `<node>_<studyID>` or a trailing `_`
            # (per-study dynamic calculates). Only attempted after exact + numeric
            # matching miss, so it cannot shadow a distinct node like `Pre_treatList`.
            m2 = re.search(r"_([^_]*)$", base)
            if m2 and self._by_name.get(base[: m2.start()]):
                node = self.nodes[self._by_name[base[: m2.start()]][0]]
                return {
                    "kind": "matched",
                    "node_path": node.path,
                    "name": node.name,
                    "repeat_iterations": list(
                        zip(node.repeat_path, idxs[: node.repeat_depth])
                    ),
                    "choice_code": None,
                    "is_select_multiple": node.is_select_multiple,
                    "string_key": m2.group(1),
                    "ambiguous": False,
                }
            return {"kind": "unmapped", "base": base, "indices": idxs}

        # SurveyCTO wide-column convention for a select_multiple inside repeat(s):
        #   base_<choice>_<outer>_..._<inner>   -- the choice code is the FIRST
        # suffix, then the repeat-iteration chain (outer->inner) follows. This
        # matches the production Phase-4 matcher (create_variable_dictionaries.py:
        # double_match groups choice=first/repeat=last) and the concordance-
        # validated synthetic generator (synthetic_data.py: `_<choice>{repeat
        # suffix}`). A plain repeat field carries only the iteration chain.
        # Prefer the candidate whose repeat depth (+1 for a select_multiple choice
        # binary) consumes exactly the indices we peeled.
        def _choice_value(i: int) -> int | str:
            """Choice code at position i: preserve a leading-zero raw string
            (`01`), else int; negate for the dash->underscore negative form."""
            raw = raws[i] if i < len(raws) else str(idxs[i])
            if len(raw) > 1 and raw[0] == "0":
                return ("-" + raw) if neg_choice else raw
            return (-idxs[i]) if neg_choice else idxs[i]

        chosen = None
        choice = None
        rep_idxs = idxs  # iteration indices left after a leading choice is removed
        for path in candidates:
            node = self.nodes[path]
            depth = node.repeat_depth
            if len(idxs) == depth and not neg_choice:
                chosen, choice, rep_idxs = node, None, idxs
                break
            if node.is_select_multiple and len(idxs) == depth + 1:
                chosen, choice, rep_idxs = node, _choice_value(0), idxs[1:]
                break
        if chosen is None:
            # Fall back to the first candidate; treat a leading surplus index as the
            # choice code if it is select_multiple, else leave iterations partial.
            chosen = self.nodes[candidates[0]]
            if chosen.is_select_multiple and len(idxs) == chosen.repeat_depth + 1:
                choice, rep_idxs = _choice_value(0), idxs[1:]
            else:
                choice, rep_idxs = (_choice_value(0) if neg_choice else None), idxs

        n_iter = chosen.repeat_depth
        iters = list(zip(chosen.repeat_path, rep_idxs[:n_iter]))
        result = {
            "kind": "matched",
            "node_path": chosen.path,
            "name": chosen.name,
            "repeat_iterations": iters,
            "choice_code": choice,
            "is_select_multiple": chosen.is_select_multiple,
            "ambiguous": len(candidates) > 1,
        }
        # Ragged: the number of repeat indices doesn't match the node's repeat
        # depth. SurveyCTO normally emits exactly one index per level, so flag a
        # mismatch instead of silently truncating (over-indexed: the zip above
        # drops surplus indices) or under-assigning levels. Was `< n_iter`, which
        # missed the over-indexed case (e.g. member_age_3_4 on a depth-1 field
        # dropped the trailing 4 with no flag). (#23.2 / review #11)
        if len(rep_idxs) != n_iter:
            result["ragged"] = True
        return result

    def to_dict(self) -> dict:
        return {
            "formid": self.formid,
            "formdef_version": self.formdef_version,
            "n_nodes": len(self.nodes),
            "n_repeat_groups": len(self.repeat_groups),
            "repeat_groups": self.repeat_groups,
            "nodes": {p: asdict(n) for p, n in self.nodes.items()},
        }


def parse_contract(xml_path) -> FormContract:
    """Parse a compiled XForm XML into a FormContract."""
    import xml.etree.ElementTree as ET

    root = ET.parse(str(xml_path)).getroot()

    # 1) Primary instance. SurveyCTO compiles select_from_file / pulldata / search
    #    datasets into SECONDARY <instance id="..."> blocks; the PRIMARY model
    #    instance is the <instance> with no `id` attribute, and its first child
    #    element is the form root (whose id/tag is the form_id). Prefer that so a
    #    secondary instance can't hijack detection; fall back to the old "first
    #    element whose id == its own tag" scan for unconventional shapes.
    primary = None
    model = next((e for e in root.iter() if _ln(e.tag) == "model"), None)
    if model is not None:
        instances = [e for e in model if _ln(e.tag) == "instance"]
        prim_inst = next((i for i in instances if not i.get("id")), None)
        if prim_inst is not None:
            primary = next((c for c in prim_inst if isinstance(c.tag, str)), None)
    if primary is None:
        for el in root.iter():
            if el.get("id") and _ln(el.tag) == el.get("id"):
                primary = el
                break
    if primary is None:
        raise RuntimeError(f"Could not find primary instance in {xml_path}")
    formid = primary.get("id") or _ln(primary.tag)
    formdef_version = primary.get("version")

    # 2) Walk the instance: record every node with its group/repeat ancestry.
    instance_nodes: dict[str, dict] = {}
    repeat_tokens: set[str] = set()

    def walk(el, ancestors: list[str], repeats: list[str]):
        name = _ln(el.tag)
        is_rep = any(_ln(k) == "template" for k in el.attrib)
        if is_rep:
            repeat_tokens.add(name)
        anc = ancestors + [name]
        rep = repeats + ([name] if is_rep else [])
        kids = [c for c in el if isinstance(c.tag, str)]
        path = "/".join(anc)
        instance_nodes[path] = {
            "group_path": anc[:-1],
            "repeat_path": list(rep[:-1]) if is_rep else list(rep),
            "is_leaf": not kids,
        }
        for c in kids:
            walk(c, anc, rep)

    for c in primary:
        if isinstance(c.tag, str):
            walk(c, [], [])

    # 3) Binds: nodeset (full /formid/...) -> attribs. Binds are the authoritative
    #    typed-node set and may target group nodes too.
    prefix = f"/{formid}/"
    binds: dict[str, dict] = {}
    for el in root.iter():
        if _ln(el.tag) != "bind":
            continue
        ns = el.get("nodeset")
        if not ns or not ns.startswith(prefix):
            continue
        rel = ns[len(prefix):]
        attrs = {_ln(k): v for k, v in el.attrib.items()}
        binds[rel] = attrs

    # 4) Body controls: ref (full path) -> control local-name + appearance.
    #    The appearance carries `search(...)` for select-from-file fields, which is
    #    the deterministic choice-source (dataset + filter) -> data_source.
    controls: dict[str, str] = {}
    appearances: dict[str, str] = {}
    body = next((c for c in root if _ln(c.tag) == "body"), None)
    if body is not None:
        for el in body.iter():
            ref = el.get("ref") or el.get("nodeset")
            if not ref or not ref.startswith(prefix):
                continue
            rel = ref[len(prefix):]
            ctl = _ln(el.tag)
            if ctl in ("select", "select1", "input", "upload", "trigger", "range"):
                controls[rel] = ctl
            ap = el.get("appearance")
            if ap:
                appearances[rel] = ap

    # 5) Merge into Node objects, keyed by the union of instance paths + bind paths.
    def repeats_for(path: str) -> list[str]:
        toks = path.split("/")[:-1]
        return [t for t in toks if t in repeat_tokens]

    nodes: dict[str, Node] = {}
    for path in set(instance_nodes) | set(binds):
        inst = instance_nodes.get(path, {})
        toks = path.split("/")
        rep = inst.get("repeat_path", repeats_for(path))
        b = binds.get(path, {})
        ctl = controls.get(path)
        ap = appearances.get(path)
        select_multiple = ctl == "select" or b.get("type") == "select"
        nodes[path] = Node(
            path=path,
            name=toks[-1],
            group_path=inst.get("group_path", toks[:-1]),
            repeat_path=rep,
            repeat_depth=len(rep),
            is_leaf=inst.get("is_leaf", True),
            xml_type=b.get("type"),
            control=_SELECT_CONTROL.get(ctl, ctl),
            is_select_multiple=select_multiple,
            calculate=b.get("calculate"),
            relevant=b.get("relevant"),
            constraint=b.get("constraint"),
            required=str(b.get("required", "")).strip() in ("true()", "true", "1"),
            readonly=str(b.get("readonly", "")).strip() in ("true()", "true", "1"),
            pulldata=_parse_pulldata(b.get("calculate")),
            preload=b.get("preload"),
            preload_params=b.get("preloadParams"),
            data_source=_parse_data_source(ap, b.get("calculate")),
        )
    return FormContract(formid, formdef_version, nodes)


def build_contract(xml_path, out_path=None) -> FormContract:
    """Parse a form's XML; optionally write `<out_path>` as the contract JSON."""
    contract = parse_contract(xml_path)
    npull = sum(1 for n in contract.nodes.values() if n.pulldata)
    nsel = sum(1 for n in contract.nodes.values() if n.is_select_multiple)
    print(
        f"[{Path(xml_path).name}] form_id={contract.formid} "
        f"version={contract.formdef_version} nodes={len(contract.nodes)} "
        f"repeat_groups={len(contract.repeat_groups)} "
        f"select_multiple={nsel} pulldata={npull}"
    )
    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(contract.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  wrote {out}")
    return contract


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a compiled SurveyCTO form XML into the DB contract."
    )
    parser.add_argument("xml_path", help="path to the compiled XForm XML")
    parser.add_argument(
        "-o", "--out", default=None,
        help="write the contract JSON to this path (default: print summary only)",
    )
    args = parser.parse_args()
    build_contract(args.xml_path, args.out)


if __name__ == "__main__":
    main()
