"""Tests for ironclad gating in the synthetic generator.

The deterministic evaluator is the single gating authority. A relevance clause
that *raises* (unsupported function / parse error) is un-evaluable: in strict
mode (the default) it fails CLOSED -- the cell is blanked and recorded, rather
than silently shown -- so we never fabricate a cell we cannot prove SurveyCTO
would display. ``--legacy-fail-open-relevance`` (strict=False) restores the old
show-on-error behaviour.
"""
import csv
import json

from transformers.expression_evaluator import EvalContext
from generators.synthetic_data import (
    StripLog,
    _is_relevant,
    generate_synthetic_csv,
)

_META = {
    "CompletionDate", "SubmissionDate", "starttime", "endtime", "deviceid",
    "subscriberid", "simid", "devicephonenum", "username", "duration",
    "caseid", "KEY", "formdef_version",
}


def _run(tmp_path, form, *, n_rows=1, seed=1, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    qp = tmp_path / "q.json"
    qp.write_text(json.dumps(form), encoding="utf-8")
    out = tmp_path / "synth.csv"
    generate_synthetic_csv(
        qp, out, pulldata_search_dirs=[], survey_name="t",
        n_rows=n_rows, seed=seed, **kwargs,
    )
    with out.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


# ── CSV-level behaviour ───────────────────────────────────────────────────────

def test_strict_unevaluable_relevance_fails_closed(tmp_path):
    form = [
        {"type": "text", "variable_name": "q",
         "relevance": "never_a_real_fn() = 1", "group_path": []},
    ]
    _, strict_rows = _run(tmp_path / "strict", form)          # default strict=True
    _, legacy_rows = _run(tmp_path / "legacy", form, strict=False)
    assert strict_rows[0]["q"] == ""            # un-evaluable -> hidden
    assert legacy_rows[0]["q"] != ""            # fail-open -> shown


def test_validly_false_relevance_blanks_in_both_modes(tmp_path):
    """A defined-falsy gate is legitimate gating, unchanged by strict mode."""
    form = [
        {"type": "text", "variable_name": "x", "group_path": []},
        {"type": "text", "variable_name": "dep",
         "relevance": "${x} = 999999", "group_path": []},
    ]
    _, strict_rows = _run(tmp_path / "s", form)
    _, legacy_rows = _run(tmp_path / "l", form, strict=False)
    assert strict_rows[0]["dep"] == ""
    assert legacy_rows[0]["dep"] == ""


# ── GateDecision unit behaviour ───────────────────────────────────────────────

def test_gate_decision_captures_operands_on_validly_false():
    sl = StripLog()
    ctx = EvalContext(row={"age": "12"})
    d = _is_relevant({"relevance": "${age} >= 18"}, ctx, sl, "dep", strict=True)
    assert d.relevant is False
    assert d.unevaluable is False
    assert d.failing_kind == "relevance"
    assert d.failing_expr == "${age} >= 18"
    assert "age" in (d.failing_operands or {})
    assert d.failing_operands["age"] == ctx.get_var("age")


def test_gate_decision_unevaluable_strict_vs_legacy():
    q = {"relevance": "never_a_real_fn() = 1"}
    strict = _is_relevant(q, EvalContext(row={}), StripLog(), "q", strict=True)
    legacy = _is_relevant(q, EvalContext(row={}), StripLog(), "q", strict=False)
    assert strict.relevant is False and strict.unevaluable is True and strict.error
    assert legacy.relevant is True and legacy.unevaluable is True


def test_forward_referenced_gate_resolves(tmp_path):
    """A gate referencing a variable defined LATER resolves via the fixpoint:
    ``early`` is gated on ``${late}=1``; ``late`` (a calc = 1) is defined after
    it, so a single pass would blank ``early``. Phase B re-gates and shows it."""
    form = [
        {"type": "text", "variable_name": "early",
         "relevance": "${late} = 1", "group_path": []},
        {"type": "calculate", "variable_name": "late",
         "calculation": "1", "group_path": []},
    ]
    _, rows = _run(tmp_path / "fwd", form)
    assert rows[0]["late"] == "1"
    assert rows[0]["early"] != ""


def test_forward_referenced_gate_stays_closed_when_false(tmp_path):
    form = [
        {"type": "text", "variable_name": "early",
         "relevance": "${late} = 1", "group_path": []},
        {"type": "calculate", "variable_name": "late",
         "calculation": "0", "group_path": []},
    ]
    _, rows = _run(tmp_path / "fwd0", form)
    assert rows[0]["early"] == ""       # gate genuinely false after resolution


def test_forward_ref_run_is_deterministic(tmp_path):
    form = [
        {"type": "text", "variable_name": "early",
         "relevance": "${late} = 1", "group_path": []},
        {"type": "integer", "variable_name": "n", "constraint": ". >= 0 and . <= 50",
         "relevance": "${late} = 1", "group_path": []},
        {"type": "calculate", "variable_name": "late",
         "calculation": "1", "group_path": []},
    ]
    _, r1 = _run(tmp_path / "d1", form, n_rows=5, seed=9)
    _, r2 = _run(tmp_path / "d2", form, n_rows=5, seed=9)
    assert r1 == r2


def test_gate_decision_group_relevance_short_circuits():
    q = {"group_relevances": ["1 = 2"], "relevance": "1 = 1"}
    d = _is_relevant(q, EvalContext(row={}), StripLog(), "q", strict=True)
    assert d.relevant is False
    assert d.failing_kind == "group_relevance"
    assert d.failing_expr == "1 = 2"
