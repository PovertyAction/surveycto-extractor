#!/usr/bin/env python3
"""Deterministic helper for the bench-test skill.

The skill's *semantic* work (proposing scenarios, authoring in-character answers)
is done by Claude. This script does the *mechanical, reproducible* work so none
of the pipeline depends on model output:

  --validate   check a cases file's must-hit answers against questions.json
  --expand     materialise per-variation answer-sheet JSONs + a seed manifest
  --report     read the coverage-trace sidecars and produce a findings report

Stdlib only. Copy alongside SKILL.md into .claude/skills/bench-test/.

Cases file schema (both user-supplied and auto-derived scenarios use this):

  {
    "survey": "household_survey",
    "scenarios": [
      {
        "id": "eligible_consents",
        "title": "Eligible household that consents",
        "persona": "Rural 5-person household; head consents; 2 kids in school",
        "must_hit": {"c_consent_qs_ans": "1", "c_consent": "1"},
        "expect": {"reach": ["consented_grp"], "block_at": []},
        "n_rows": 5
      }
    ]
  }

An answer sheet (what --expand writes, one per variation) is the shape main.py
consumes via --answers-file:

  {"answers": {var-or-suffixed-key: value}, "directives": {"repeat_counts": {name: N}}}
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from pathlib import Path


# ── questions.json helpers ────────────────────────────────────────────────────

def _load_questions(path: Path) -> list:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _index_by_var(questions: list) -> dict:
    idx = {}
    for q in questions:
        vn = (q.get("variable_name") or "").strip()
        if vn and vn not in idx:
            idx[vn] = q
    return idx


_SUFFIX_RE = re.compile(r"_\d+$")


def _base_var(key: str) -> str:
    """Strip a trailing repeat-iteration suffix (member_age_2 -> member_age)."""
    return _SUFFIX_RE.sub("", key)


def _numeric_bounds(constraint):
    """Very small min/max extractor: '. >= LO' / '. <= HI' (and > / <)."""
    if not constraint:
        return None, None
    lo = hi = None
    for m in re.finditer(r"\.\s*(>=|>)\s*(-?\d+(?:\.\d+)?)", constraint):
        v = float(m.group(2)) + (0 if m.group(1) == ">=" else 1e-9)
        lo = v if lo is None else max(lo, v)
    for m in re.finditer(r"\.\s*(<=|<)\s*(-?\d+(?:\.\d+)?)", constraint):
        v = float(m.group(2)) - (0 if m.group(1) == "<=" else 1e-9)
        hi = v if hi is None else min(hi, v)
    return lo, hi


def _validate_answer(var, val, q):
    """Return an error string, or None if the answer looks legal for q."""
    qt = q.get("type", "")
    if qt in ("select_one", "select_multiple"):
        vals = {str(c.get("value", "")).strip() for c in (q.get("choices") or [])}
        vals.discard("")
        toks = str(val).split() if qt == "select_multiple" else [str(val).strip()]
        bad = [t for t in toks if vals and t not in vals]
        if bad:
            return f"{var}: value(s) {bad} not in choices {sorted(vals)}"
        return None
    if qt in ("integer", "decimal"):
        try:
            num = float(str(val))
        except (TypeError, ValueError):
            return f"{var}: {val!r} is not numeric ({qt})"
        lo, hi = _numeric_bounds(q.get("constraint"))
        if lo is not None and num < lo:
            return f"{var}: {val} below min {lo}"
        if hi is not None and num > hi:
            return f"{var}: {val} above max {hi}"
        return None
    return None  # text / date / other: accept


# ── subcommands ───────────────────────────────────────────────────────────────

def cmd_validate(args) -> int:
    questions = _load_questions(Path(args.questions))
    idx = _index_by_var(questions)
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    scenarios = cases.get("scenarios", [])
    n_err = 0
    for sc in scenarios:
        sid = sc.get("id", "?")
        errs = []
        for var, val in (sc.get("must_hit") or {}).items():
            q = idx.get(var) or idx.get(_base_var(var))
            if q is None:
                errs.append(f"{var}: unknown variable (not in questions.json)")
                continue
            e = _validate_answer(var, val, q)
            if e:
                errs.append(e)
        for target in (sc.get("expect", {}).get("reach", []) +
                       sc.get("expect", {}).get("block_at", [])):
            if target not in idx and _base_var(target) not in idx:
                errs.append(f"expect target {target!r}: unknown variable")
        status = "OK" if not errs else "FAIL"
        print(f"[{status}] {sid}")
        for e in errs:
            print(f"    - {e}")
        n_err += len(errs)
    print(f"\n{len(scenarios)} scenario(s), {n_err} problem(s).")
    return 1 if n_err else 0


def _variation_seed(base_seed: int, scenario_id: str, v: int) -> int:
    h = int(hashlib.sha256(scenario_id.encode("utf-8")).hexdigest(), 16) % 100000
    return base_seed + h * 100 + v


def cmd_expand(args) -> int:
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"survey": cases.get("survey"), "base_seed": args.seed, "runs": []}
    for sc in cases.get("scenarios", []):
        sid = sc["id"]
        n = int(sc.get("n_rows", args.variations))
        sheet = {"answers": dict(sc.get("must_hit") or {})}
        directives = sc.get("directives") or {}
        if directives:
            sheet["directives"] = directives
        for v in range(1, n + 1):
            seed = _variation_seed(args.seed, sid, v)
            sheet_path = out_dir / f"{sid}_v{v:02d}.answers.json"
            # The must-hit answers are identical across variations; Claude may
            # enrich each sheet in-character before the fill runs. Seeds differ
            # so the stochastic tail varies.
            sheet_path.write_text(json.dumps(sheet, indent=2), encoding="utf-8")
            manifest["runs"].append({
                "scenario": sid, "variation": v, "seed": seed,
                "answers_file": str(sheet_path),
                "expect": sc.get("expect", {}),
            })
    man_path = out_dir / "run_manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[OK] wrote {len(manifest['runs'])} answer sheet(s) + {man_path.name}")
    print(f"     to {out_dir}")
    return 0


def _reached_vars(trace: dict) -> set:
    """Base variable names asked in at least one row of a coverage trace."""
    reached = set()
    for row in trace.get("rows", []):
        for cell in row.get("cells", {}).values():
            if cell.get("asked"):
                reached.add(cell.get("var"))
    return reached


def _gate_reasons(trace: dict, target: str) -> list:
    """For a target var, collect (expr, operands) of the gate that blocked it."""
    out = []
    for row in trace.get("rows", []):
        for cell in row.get("cells", {}).values():
            if cell.get("var") == target and not cell.get("asked"):
                g = cell.get("gate") or {}
                out.append((g.get("failing_expr"), g.get("failing_operands"), g.get("error")))
    return out


def cmd_report(args) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    runs = manifest.get("runs", [])
    # Map each run to its coverage trace (sits next to the CSV it produced).
    lines = ["# Bench-test report", ""]
    lines.append(f"Survey: `{manifest.get('survey')}`  |  runs: {len(runs)}")
    lines.append("")
    # group runs by scenario
    by_scenario = {}
    for r in runs:
        by_scenario.setdefault(r["scenario"], []).append(r)

    coverage_rows = ["scenario,variation,target,kind,status,blocking_gate"]
    for sid, rs in by_scenario.items():
        lines.append(f"## {sid}")
        expect = rs[0].get("expect", {})
        reach = expect.get("reach", [])
        block = expect.get("block_at", [])
        for r in rs:
            tp = _find_trace(r, args.trace_dir)
            if tp is None:
                lines.append(f"- v{r['variation']:02d}: (no coverage trace found)")
                continue
            trace = json.loads(Path(tp).read_text(encoding="utf-8"))
            reached = _reached_vars(trace)
            verdicts = []
            for t in reach:
                ok = t in reached
                verdicts.append(("reach", t, ok))
                blk = ""
                if not ok:
                    reasons = _gate_reasons(trace, t)
                    blk = _fmt_reason(reasons)
                coverage_rows.append(
                    f"{sid},{r['variation']},{t},reach,"
                    f"{'PASS' if ok else 'FAIL'},{blk}")
            for t in block:
                gated = t not in reached
                verdicts.append(("block", t, gated))
                coverage_rows.append(
                    f"{sid},{r['variation']},{t},block,"
                    f"{'PASS' if gated else 'FAIL'},")
            fails = [(k, t) for (k, t, ok) in verdicts if not ok]
            status = "PASS" if not fails else "FAIL"
            detail = ""
            if fails:
                bits = []
                for k, t in fails:
                    if k == "reach":
                        rs_reason = _fmt_reason(_gate_reasons(trace, t))
                        bits.append(f"expected to reach {t} but blocked ({rs_reason})")
                    else:
                        bits.append(f"expected {t} blocked but it was reached")
                detail = "; ".join(bits)
            lines.append(f"- v{r['variation']:02d} seed={r['seed']}: **{status}** {detail}")
        lines.append("")

    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cov = out.with_suffix(".coverage.csv")
    cov.write_text("\n".join(coverage_rows) + "\n", encoding="utf-8")
    print(f"[OK] report -> {out}")
    print(f"[OK] coverage matrix -> {cov}")
    return 0


def _fmt_reason(reasons):
    for expr, operands, error in reasons:
        if error:
            return f"unevaluable: {error}"
        if expr:
            ops = ", ".join(f"{k}={v}" for k, v in (operands or {}).items())
            return f"gate {expr} false ({ops})" if ops else f"gate {expr} false"
    return "gate closed"


def cmd_rejections(args) -> int:
    """Surface everything in a coverage trace that a fill subagent should repair:
    scripted answers that were refused (invalid -> sampled fallback) and gates
    that could not be evaluated. Exit code 1 if anything was found, so a repair
    loop can gate on it."""
    trace = json.loads(Path(args.trace).read_text(encoding="utf-8"))
    rejected, unevaluable = [], []
    for row in trace.get("rows", []):
        rk = row.get("key")
        for key, cell in row.get("cells", {}).items():
            if cell.get("source") == "scripted_invalid_fallback":
                rejected.append((rk, key, cell.get("var"), cell.get("note")))
        for u in row.get("unevaluable_gates", []):
            unevaluable.append((rk, u.get("key"), u.get("expr"), u.get("error")))
    if rejected:
        print(f"REJECTED scripted answers ({len(rejected)}) -- value refused, sampled instead:")
        for rk, key, var, note in rejected:
            print(f"  [{rk}] {key} ({var}): {note}")
    if unevaluable:
        print(f"UN-EVALUABLE gates ({len(unevaluable)}) -- relevance could not be evaluated:")
        for rk, key, expr, err in unevaluable:
            print(f"  [{rk}] {key}: {expr}  -> {err}")
    if not rejected and not unevaluable:
        print("clean: no rejected answers, no un-evaluable gates.")
    return 1 if (rejected or unevaluable) else 0


def _find_trace(run: dict, trace_dir: str):
    """Locate the coverage trace for a run. Convention: the fill writes
    <scenario>_v<NN>.coverage.json into trace_dir."""
    sid, v = run["scenario"], run["variation"]
    cand = Path(trace_dir) / f"{sid}_v{v:02d}.coverage.json"
    if cand.exists():
        return cand
    hits = glob.glob(str(Path(trace_dir) / f"{sid}_v{v:02d}*coverage*.json"))
    return hits[0] if hits else None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Bench-test scenario helper (deterministic).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="Validate must-hit answers against questions.json")
    pv.add_argument("--cases", required=True)
    pv.add_argument("--questions", required=True)
    pv.set_defaults(func=cmd_validate)

    pe = sub.add_parser("expand", help="Write per-variation answer sheets + seed manifest")
    pe.add_argument("--cases", required=True)
    pe.add_argument("--variations", type=int, default=5)
    pe.add_argument("--seed", type=int, default=0)
    pe.add_argument("--out", required=True)
    pe.set_defaults(func=cmd_expand)

    pr = sub.add_parser("report", help="Join coverage traces to expectations")
    pr.add_argument("--manifest", required=True)
    pr.add_argument("--trace-dir", required=True)
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_report)

    pj = sub.add_parser("rejections",
                        help="List refused scripted answers + un-evaluable gates in a trace")
    pj.add_argument("--trace", required=True)
    pj.set_defaults(func=cmd_rejections)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
