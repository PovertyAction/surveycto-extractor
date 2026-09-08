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
nodes into actual columns. `surveycto-enrich` overlays this contract onto
the variable dictionary produced by `surveycto-vardict`.

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
    "KEY",
    "SubmissionDate",
    "CompletionDate",
    "starttime",
    "endtime",
    "deviceid",
    "subscriberid",
    "simid",
    "devicephonenum",
    "username",
    "duration",
    "caseid",
    "formdef_version",
    "review_status",
    "review_comments",
    "review_corrections",
    "review_quality",
    "instanceID",
    "instanceName",
    "SubmitterID",
    "SubmitterName",
    "X_searchText",
}
_SYS_PREFIX = ("meta-", "SET-OF-")


def _ln(tag) -> str:
    """Local-name of a possibly namespaced ElementTree tag."""
    return tag.split("}")[-1] if isinstance(tag, str) and "}" in tag else tag


def is_system_column(col: str) -> bool:
    """Return True if a wide column is SurveyCTO transport/metadata, not a form node."""
    return col in _SYS_EXACT or col.startswith(_SYS_PREFIX) or col.endswith("-Comment")


# Wide-column suffix grammar (see FormContract.map_column).
# Only a NEGATIVE number sanitises to a leading underscore, so `_66` is -66.
_NEG_TOKEN_RE = re.compile(r"_(\d+)")
# `1_1` -- all-integer tokens. On a non-select node this marks formdef index
# drift; on a select_multiple whose universe does not contain it, it marks an
# unknowable choice/index boundary, because a real numeric choice value
# sanitises to exactly ONE token.
_ALL_INT = re.compile(r"\d+(?:_\d+)*")
_COMPOSITE_INT = re.compile(r"\d+(?:_\d+)+")
_SANITISE_RE = re.compile(r"[^A-Za-z0-9_]")


def sanitize_choice_value(value: str) -> str:
    """Render a choice value the way SurveyCTO renders it into a wide column name.

    Every character outside `[A-Za-z0-9_]` becomes an underscore, which is why a
    sanitised token cannot be reversed without the form's own choice universe --
    `AB-12` and `AB_12` both arrive as `AB_12`. Callers building a choice index
    for `FormContract.choice_index` must key it through this function.
    """
    return _SANITISE_RE.sub("_", value)


@dataclass
class Node:
    """One stored node in the compiled form (a field, or a bound group)."""

    path: str  # path below the form root, slash-joined
    name: str  # leaf token (last path segment)
    group_path: list[str]  # ancestor tokens (groups + repeats), outer->inner
    repeat_path: list[str]  # ancestor tokens that are repeats, outer->inner
    repeat_depth: int = 0
    is_leaf: bool = True  # False = value bound at a group node
    xml_type: str | None = None  # bind type (string/decimal/select/...)
    control: str | None = None  # body control: select_one/select_multiple/input/...
    is_select_multiple: bool = False
    calculate: str | None = None
    relevant: str | None = None
    constraint: str | None = None
    required: bool = False
    readonly: bool = False
    pulldata: dict | None = None  # {dataset, value, key} from pulldata() calc
    preload: str | None = None  # jr:preload (system source, e.g. property)
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
    call or a quoted `)` in an argument. (#23.4).
    """
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
    return {
        "dataset": _unquote(args[0]),
        "value": args[1].strip(),
        "key": _unquote(args[2]),
    }


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
        self,
        formid: str,
        formdef_version: str | None,
        nodes: dict[str, Node],
        choice_index: dict | None = None,
    ):
        """Store the node model and build the deterministic leaf-name resolver index."""
        self.formid = formid
        self.formdef_version = formdef_version
        self.nodes: dict[str, Node] = nodes
        # {node_path: {sanitised token: (original value, label|None)}}, for
        # de-sanitising a string-valued select_multiple choice back to the value the
        # form actually declared. Optional and injected by the caller: resolving a
        # `search()` choice universe needs the form's attached CSVs, which this
        # parser deliberately knows nothing about (`cli/enrich.py` already resolves
        # them for labels via `_resolve_search_choices`). Absent, a sanitised token
        # is carried verbatim -- lossy but never wrong about which choice it is.
        self.choice_index: dict = choice_index or {}
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
        """Return the distinct repeat-group paths in first-seen order."""
        seen: list[str] = []
        for n in self.nodes.values():
            for r in n.repeat_path:
                if r not in seen:
                    seen.append(r)
        return seen

    def _longest_node_prefix(self, col: str) -> str | None:
        """Return the longest node NAME `col` starts with on an `_` boundary.

        Longest-first, so a node whose own name ends in digits or underscores
        (`q1_2`) wins over the shorter prefix that would strand its own tail.
        """
        toks = col.split("_")
        for k in range(len(toks), 0, -1):
            cand = "_".join(toks[:k])
            if cand in self._by_name:
                return cand
        return None

    def _pick_node(self, name: str, n_tokens: int) -> Node:
        """Disambiguate a repeated leaf token by how well its depth fits the suffix.

        Preference order mirrors the wide-column grammar: an exact-depth fit (the
        node's own column) beats a select_multiple choice binary, which beats a
        string-keyed runtime column, which beats a short/partial index run. Ties
        keep the first path, and `_by_name` is built from sorted paths, so the
        tie-break is deterministic across processes.
        """
        best, best_score = None, 99
        for path in self._by_name[name]:
            node = self.nodes[path]
            depth = node.repeat_depth
            if n_tokens == depth:
                score = 0
            elif n_tokens > depth:
                score = 1 if node.is_select_multiple else 2
            else:
                score = 3
            if score < best_score:
                best, best_score = node, score
        return best

    def _decode_choice(self, node: Node, token: str):
        """Sanitised token -> (choice value, label|None).

        Prefers the injected choice universe, the only thing that can undo
        sanitisation (`AB-12` and `AB_12` both render `AB_12`). Falls back to the
        two shapes recoverable from the token alone: a dash->underscore negative
        sentinel (`_66` -> -66) and a bare integer, both kept as the ints the
        dictionary has always carried. Anything else is carried verbatim -- lossy,
        but never wrong about WHICH choice it is.
        """
        hit = self.choice_index.get(node.path, {}).get(token)
        if hit is not None:
            return hit
        m = _NEG_TOKEN_RE.fullmatch(token)
        if m:  # only a negative number sanitises to a leading underscore
            raw = m.group(1)
            neg = ("-" + raw) if len(raw) > 1 and raw[0] == "0" else -int(raw)
            return neg, None
        if token.isdigit():
            # A leading zero is a distinct choice code, so keep it as a string.
            return (token if len(token) > 1 and token[0] == "0" else int(token)), None
        return token, None

    def map_column(self, col: str) -> dict:
        """Resolve a wide column to its node + repeat iterations + choice code.

        Returns {kind, ...}. kind is one of: 'system', 'matched', 'unmapped'.
        For 'matched': node_path, repeat_iterations (list of (group, index)),
        choice_code (int|str|None), is_select_multiple, and optionally
        choice_label, string_key, ambiguous_choice, ragged.

        Anchors on the NODE first, then on its repeat depth: exactly `depth`
        trailing integer tokens are iteration indices, and whatever precedes them
        is the (sanitised) choice value. The previous parser instead peeled EVERY
        trailing integer and required the remainder to be a node name, which holds
        only when the choice value is a bare positive integer -- so a string-valued
        choice (`langs_ES`, or a `search()`-sourced key whose own digits look like
        suffixes) fell into a fallback that hardcoded `choice_code = None` and,
        worse, reported the value's own digits as the repeat iteration. `vardict`
        gates per-choice resolution on `choice_code`, so those binaries were
        labelled with their question's whole choice list instead of their choice.
        """
        if is_system_column(col):
            return {"kind": "system"}

        name = self._longest_node_prefix(col)
        if name is None:
            return {"kind": "unmapped", "base": col, "indices": []}

        rest = col[len(name) :]
        rest = rest[1:] if rest.startswith("_") else rest
        toks = rest.split("_") if rest else []
        node = self._pick_node(name, len(toks))
        depth = node.repeat_depth

        # Consume trailing integer tokens as repeat indices, at most `depth` of them.
        n = 0
        while n < depth and n < len(toks) and toks[len(toks) - 1 - n].isdigit():
            n += 1
        head = toks[: len(toks) - n]
        # An EMPTY choice is impossible, so if the head vanished while the suffix
        # still carries more tokens than the node has depth, hand an index back: a
        # negative sentinel's own leading underscore produced that empty first
        # token (`x__77_2` is choice -77 at one index, not choice "" at iterations
        # 77 and 2). When len(toks) == depth there is no choice at all -- that is
        # the select_multiple's own parent column, which the export writes empty.
        while n > 0 and len(toks) > depth and not "_".join(head):
            n -= 1
            head = toks[: len(toks) - n]
        token = "_".join(head) if head else None

        # A node with no choices cannot have a numeric head: a purely numeric
        # surplus is formdef drift (the column was written while the node sat in
        # one more repeat) and the surplus index is the INNER one, so the node's
        # own coordinates are the LEADING tokens. Reading the trailing `depth`
        # instead would shift every repeat group by one.
        drift = bool(
            token is not None
            and not node.is_select_multiple
            and _ALL_INT.fullmatch(token)
        )
        if drift:
            n = min(depth, len(toks))
            token = None
        idx_toks = toks[:n] if drift else (toks[len(toks) - n :] if n else [])
        iters = list(zip(node.repeat_path, [int(t) for t in idx_toks]))

        choice = label = string_key = None
        ambiguous_choice = False
        if token is not None:
            if node.is_select_multiple:
                choice, label = self._decode_choice(node, token)
                # An unresolved MULTI-token all-integer choice is not a choice: a
                # real numeric value sanitises to exactly ONE token. This is the
                # same drift as above but on a select_multiple, where the
                # choice/index boundary is genuinely unknowable from the contract.
                # Abstain and say so, rather than emit a fabricated out-of-domain
                # code that downstream would then try to look up.
                if choice == token and _COMPOSITE_INT.fullmatch(token):
                    choice, label, ambiguous_choice = None, None, True
            else:
                # Per-study dynamic calculates: `<node>_<studyID>`, no choice.
                string_key = token

        result = {
            "kind": "matched",
            "node_path": node.path,
            "name": node.name,
            "repeat_iterations": iters,
            "choice_code": choice,
            "is_select_multiple": node.is_select_multiple,
            "ambiguous": len(self._by_name[name]) > 1,
        }
        if label is not None:
            result["choice_label"] = label
        if string_key is not None:
            result["string_key"] = string_key
        if ambiguous_choice:
            # Load-bearing, not decorative: without it a select_multiple with
            # choice_code None is indistinguishable from the parent column, which
            # is the confusion this whole change exists to remove.
            result["ambiguous_choice"] = True
        # Ragged: the column CARRIED a different number of repeat indices than the
        # node's depth. SurveyCTO normally emits exactly one index per level, so a
        # mismatch is flagged rather than silently truncated (over-indexed) or
        # under-assigned (#23.2 / review #11). Counted from what the column
        # supplied, not from what was consumed: the drift branch above absorbs the
        # surplus index deliberately, and counting consumed tokens there would
        # report a tidy match for the very shape the flag exists to surface.
        if (len(toks) if drift else n) != depth:
            result["ragged"] = True
        return result

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict of the contract (form id, version, nodes)."""
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
        rel = ns[len(prefix) :]
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
            rel = ref[len(prefix) :]
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
    """Parse a compiled XForm XML from the CLI and optionally write the contract JSON."""
    parser = argparse.ArgumentParser(
        description="Parse a compiled SurveyCTO form XML into the DB contract."
    )
    parser.add_argument("xml_path", help="path to the compiled XForm XML")
    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help="write the contract JSON to this path (default: print summary only)",
    )
    args = parser.parse_args()
    build_contract(args.xml_path, args.out)


if __name__ == "__main__":
    main()
