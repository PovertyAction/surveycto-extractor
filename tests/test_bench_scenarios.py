"""Offline tests for the bench-test skill helper (skill/bench-test/bench_scenarios.py).

The helper is a standalone stdlib CLI (not a package), so we load it by path.
"""
import importlib.util
import json
from pathlib import Path

_HELPER = Path(__file__).resolve().parents[1] / "skill" / "bench-test" / "bench_scenarios.py"
_spec = importlib.util.spec_from_file_location("bench_scenarios", _HELPER)
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


_QUESTIONS = [
    {"variable_name": "consent", "type": "select_one",
     "choices": [{"value": "1"}, {"value": "0"}]},
    {"variable_name": "age", "type": "integer", "constraint": ". >= 0 and . <= 120"},
    {"variable_name": "name", "type": "text"},
]


def _write(p, obj):
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_validate_passes_and_fails(tmp_path):
    q = _write(tmp_path / "q.json", _QUESTIONS)
    good = _write(tmp_path / "good.json", {"survey": "s", "scenarios": [
        {"id": "ok", "must_hit": {"consent": "1", "age": 30},
         "expect": {"reach": ["name"], "block_at": []}},
    ]})
    bad = _write(tmp_path / "bad.json", {"survey": "s", "scenarios": [
        {"id": "bad", "must_hit": {"consent": "9", "age": 999, "nope": "x"},
         "expect": {"reach": ["ghost"]}},
    ]})
    assert bs.main(["validate", "--cases", str(good), "--questions", str(q)]) == 0
    assert bs.main(["validate", "--cases", str(bad), "--questions", str(q)]) == 1


def test_validate_accepts_suffixed_key(tmp_path):
    q = _write(tmp_path / "q.json", [
        {"variable_name": "member_age", "type": "integer", "constraint": ". >= 0 and . <= 120"},
    ])
    cases = _write(tmp_path / "c.json", {"scenarios": [
        {"id": "s", "must_hit": {"member_age_2": 40}, "expect": {}},
    ]})
    assert bs.main(["validate", "--cases", str(cases), "--questions", str(q)]) == 0


def test_expand_writes_sheets_and_manifest_deterministically(tmp_path):
    cases = _write(tmp_path / "c.json", {"survey": "s", "scenarios": [
        {"id": "alpha", "must_hit": {"consent": "1"},
         "expect": {"reach": ["name"]}, "n_rows": 3},
    ]})
    out = tmp_path / "run"
    assert bs.main(["expand", "--cases", str(cases), "--variations", "3",
                    "--seed", "10", "--out", str(out)]) == 0
    sheets = sorted(p.name for p in out.glob("alpha_v*.answers.json"))
    assert sheets == ["alpha_v01.answers.json", "alpha_v02.answers.json",
                      "alpha_v03.answers.json"]
    sheet = json.loads((out / "alpha_v01.answers.json").read_text(encoding="utf-8"))
    assert sheet["answers"] == {"consent": "1"}
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["runs"]) == 3
    seeds = [r["seed"] for r in manifest["runs"]]
    assert len(set(seeds)) == 3                         # distinct per variation
    assert seeds == [bs._variation_seed(10, "alpha", v) for v in (1, 2, 3)]


def _trace(cells):
    return {"n_rows": 1, "aggregate": {}, "rows": [{"key": "uuid:1", "cells": cells}]}


def test_report_pass_and_fail(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    # scenario alpha (reach consented) -> trace has it asked -> PASS
    _write(run / "alpha_v01.coverage.json",
           _trace({"consented": {"var": "consented", "asked": True}}))
    # scenario beta (reach youth) -> trace has it gated -> FAIL with reason
    _write(run / "beta_v01.coverage.json",
           _trace({"youth": {"var": "youth", "asked": False,
                             "gate": {"failing_expr": "${age} >= 15",
                                      "failing_operands": {"age": 12}, "error": None}}}))
    manifest = _write(run / "run_manifest.json", {"survey": "s", "runs": [
        {"scenario": "alpha", "variation": 1, "seed": 1,
         "expect": {"reach": ["consented"], "block_at": []}},
        {"scenario": "beta", "variation": 1, "seed": 2,
         "expect": {"reach": ["youth"], "block_at": []}},
    ]})
    report = run / "report.md"
    assert bs.main(["report", "--manifest", str(manifest),
                    "--trace-dir", str(run), "--out", str(report)]) == 0
    text = report.read_text(encoding="utf-8")
    assert "## alpha" in text and "## beta" in text
    assert "**PASS**" in text
    assert "**FAIL**" in text
    assert "${age} >= 15" in text and "age=12" in text
    cov = (run / "report.coverage.csv").read_text(encoding="utf-8")
    assert "alpha,1,consented,reach,PASS" in cov
    assert "beta,1,youth,reach,FAIL" in cov
