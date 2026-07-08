"""Tests for the optional coverage-trace sidecar.

The trace records, per row, which cells the gate authority ASKED vs GATED (with
the failing clause + resolved operands) and where each answer came from. It is
strictly write-only -- enabling it must not change the CSV bytes.
"""
import json

from generators.synthetic_data import ScriptedProvider, generate_synthetic_csv


def _run_trace(tmp_path, form, *, n_rows=1, seed=1, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    qp = tmp_path / "q.json"
    qp.write_text(json.dumps(form), encoding="utf-8")
    out = tmp_path / "synth.csv"
    tp = tmp_path / "synth.coverage.json"
    generate_synthetic_csv(
        qp, out, pulldata_search_dirs=[], survey_name="t",
        n_rows=n_rows, seed=seed, coverage_trace_path=tp, **kwargs,
    )
    return out, json.loads(tp.read_text(encoding="utf-8"))


def test_trace_records_asked_gated_and_operands(tmp_path):
    form = [
        {"type": "integer", "variable_name": "age", "group_path": []},
        {"type": "text", "variable_name": "dep",
         "relevance": "${age} >= 18", "group_path": []},
    ]
    _, data = _run_trace(tmp_path, form, provider=ScriptedProvider({"age": 12}))
    cells = data["rows"][0]["cells"]
    assert cells["age"]["asked"] is True
    assert cells["age"]["source"] == "scripted"
    assert cells["dep"]["asked"] is False
    assert cells["dep"]["gate"]["failing_expr"] == "${age} >= 18"
    assert str(cells["dep"]["gate"]["failing_operands"]["age"]) == "12"


def test_trace_source_scripted_vs_invalid_fallback(tmp_path):
    form = [
        {"type": "select_one", "choice_list": "c", "variable_name": "color",
         "choices": [{"value": "1"}, {"value": "2"}], "group_path": []},
    ]
    _, ok = _run_trace(tmp_path / "ok", form, provider=ScriptedProvider({"color": "1"}))
    _, bad = _run_trace(tmp_path / "bad", form, provider=ScriptedProvider({"color": "99"}))
    assert ok["rows"][0]["cells"]["color"]["source"] == "scripted"
    bad_cell = bad["rows"][0]["cells"]["color"]
    assert bad_cell["source"] == "scripted_invalid_fallback"
    assert bad_cell["note"]


def test_trace_aggregate_never_asked(tmp_path):
    form = [
        {"type": "text", "variable_name": "shown", "group_path": []},
        {"type": "text", "variable_name": "dep", "relevance": "1 = 2", "group_path": []},
    ]
    _, data = _run_trace(tmp_path, form, n_rows=3)
    agg = data["aggregate"]
    assert agg["asked_count"].get("shown") == 3
    assert agg["gated_count"].get("dep") == 3
    assert "dep" in agg["never_asked"]
    assert "shown" not in agg["never_asked"]


def test_trace_records_unevaluable_gate(tmp_path):
    form = [
        {"type": "text", "variable_name": "q",
         "relevance": "never_a_real_fn() = 1", "group_path": []},
    ]
    _, data = _run_trace(tmp_path, form)
    row = data["rows"][0]
    assert row["cells"]["q"]["gate"]["unevaluable"] is True
    assert row["unevaluable_gates"]
    assert row["unevaluable_gates"][0]["key"] == "q"


def test_trace_records_fixpoint_passes_on_forward_ref(tmp_path):
    form = [
        {"type": "text", "variable_name": "early",
         "relevance": "${late} = 1", "group_path": []},
        {"type": "calculate", "variable_name": "late",
         "calculation": "1", "group_path": []},
    ]
    _, data = _run_trace(tmp_path, form)
    row = data["rows"][0]
    assert row["fixpoint_passes"] >= 1
    assert row["converged"] is True
    assert row["cells"]["early"]["asked"] is True


def test_trace_is_write_only_csv_unchanged(tmp_path):
    form = [
        {"type": "text", "variable_name": "name", "group_path": []},
        {"type": "integer", "variable_name": "n",
         "constraint": ". >= 0 and . <= 9", "group_path": []},
    ]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    qp_a = a / "q.json"; qp_a.write_text(json.dumps(form), encoding="utf-8")
    out_a = a / "s.csv"
    generate_synthetic_csv(qp_a, out_a, pulldata_search_dirs=[], survey_name="t",
                           n_rows=5, seed=4)
    with_trace, _ = _run_trace(b, form, n_rows=5, seed=4)
    assert out_a.read_bytes() == with_trace.read_bytes()
