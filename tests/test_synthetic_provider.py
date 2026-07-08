"""Tests for the pluggable answer-provider seam in the synthetic generator.

The seam lives inside ``_compute_value``, which the walker only calls for a
question the deterministic gate authority has already marked as asked. So:

* with no provider, output is byte-identical to the historical sampler;
* a supplied provider is consulted only for relevant cells;
* a provider can NEVER populate a gated-out cell (ironclad skip logic).
"""
import csv
import json

from generators.sampling import sample_python_value
from generators.synthetic_data import (
    AnswerResult,
    DefaultStochasticProvider,
    ScriptedProvider,
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
    return out


def _read(path):
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


class _FixedProvider:
    """Returns a pinned value for keys it knows; stochastic fallback otherwise.
    Records every key it was asked to answer so tests can assert the gate never
    reached it for a hidden question."""

    def __init__(self, values):
        self.values = dict(values)
        self.seen = []

    def provide(self, req):
        self.seen.append(req.key)
        if req.key in self.values:
            return AnswerResult(self.values[req.key], "scripted")
        return AnswerResult(
            sample_python_value(
                req.q_type, req.choices, req.constraint, req.rng, **req.sampler_kwargs
            ),
            "stochastic",
        )


def test_default_provider_is_byte_identical(tmp_path):
    """provider=None (fast path) and an explicit DefaultStochasticProvider must
    consume the RNG stream identically -> identical bytes."""
    form = [
        {"type": "text", "variable_name": "name", "group_path": []},
        {"type": "integer", "variable_name": "age", "constraint": ". >= 0 and . <= 99",
         "group_path": []},
        {"type": "select_one", "choice_list": "yn", "variable_name": "owns",
         "choices": [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}],
         "group_path": []},
    ]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    none_out = _run(a, form, n_rows=5, seed=7)
    prov_out = _run(b, form, n_rows=5, seed=7, provider=DefaultStochasticProvider())
    assert none_out.read_bytes() == prov_out.read_bytes()


def test_provider_consulted_for_relevant_cell(tmp_path):
    form = [
        {"type": "text", "variable_name": "name", "relevance": "1 = 1",
         "group_path": []},
    ]
    prov = _FixedProvider({"name": "ALICE"})
    _, rows = _read(_run(tmp_path, form, provider=prov))
    assert rows[0]["name"] == "ALICE"
    assert "name" in prov.seen


def test_provider_never_populates_gated_cell(tmp_path):
    """A pinned answer for a gated-out question must be ignored -- the cell stays
    blank and the provider is never even consulted (seam is below the gate)."""
    form = [
        {"type": "text", "variable_name": "shown", "relevance": "1 = 1",
         "group_path": []},
        {"type": "text", "variable_name": "hidden", "relevance": "1 = 2",
         "group_path": []},
    ]
    prov = _FixedProvider({"shown": "YES", "hidden": "SHOULD_NOT_APPEAR"})
    _, rows = _read(_run(tmp_path, form, provider=prov))
    assert rows[0]["shown"] == "YES"
    assert rows[0]["hidden"] == ""
    assert "shown" in prov.seen
    assert "hidden" not in prov.seen


# ── ScriptedProvider ──────────────────────────────────────────────────────────

_YESNO = [{"value": "1", "label": "Yes"}, {"value": "0", "label": "No"}]


def test_scripted_consent_cascade_populates_through_the_gate(tmp_path):
    """A scripted consent=1 opens the consent-gated section via the REAL gate
    (not a bypass). consent=0 leaves it blank."""
    form = [
        {"type": "select_one", "choice_list": "yn", "variable_name": "consent",
         "choices": _YESNO, "group_path": []},
        {"type": "text", "variable_name": "followup",
         "relevance": "${consent} = 1", "group_path": []},
    ]
    _, yes = _read(_run(tmp_path / "yes", form,
                        provider=ScriptedProvider({"consent": "1"})))
    _, no = _read(_run(tmp_path / "no", form,
                       provider=ScriptedProvider({"consent": "0"})))
    assert yes[0]["consent"] == "1" and yes[0]["followup"] != ""
    assert no[0]["consent"] == "0" and no[0]["followup"] == ""


def test_scripted_invalid_choice_falls_back_to_valid_sample(tmp_path):
    form = [
        {"type": "select_one", "choice_list": "c", "variable_name": "color",
         "choices": [{"value": "1"}, {"value": "2"}, {"value": "3"}],
         "group_path": []},
    ]
    _, rows = _read(_run(tmp_path, form, provider=ScriptedProvider({"color": "99"})))
    assert rows[0]["color"] in {"1", "2", "3"}      # sampled fallback, never "99"


def test_scripted_out_of_bounds_integer_falls_back(tmp_path):
    form = [
        {"type": "integer", "variable_name": "age",
         "constraint": ". >= 0 and . <= 5", "group_path": []},
    ]
    _, rows = _read(_run(tmp_path, form, provider=ScriptedProvider({"age": 999})))
    assert 0 <= int(rows[0]["age"]) <= 5            # sampled fallback, never 999


def test_scripted_answer_for_gated_question_is_ignored(tmp_path):
    form = [
        {"type": "text", "variable_name": "hidden", "relevance": "1 = 2",
         "group_path": []},
    ]
    _, rows = _read(_run(tmp_path, form, provider=ScriptedProvider({"hidden": "X"})))
    assert rows[0]["hidden"] == ""


def test_repeat_count_directive_pins_roster_size(tmp_path):
    form = [
        {"type": "repeat_count", "variable_name": "m_count",
         "repeat_group_name": "m", "group_path": []},
        {"type": "text", "variable_name": "mname", "group_path": ["m"]},
    ]
    header, _ = _read(_run(tmp_path, form, repeat_count_overrides={"m": 3}))
    content = [c for c in header if c not in _META]
    assert "mname_1" in content and "mname_3" in content
    assert "mname_4" not in content


def test_repeat_count_directive_still_gated(tmp_path):
    """A pinned roster size on a gated-out repeat still yields zero iterations."""
    form = [
        {"type": "repeat_count", "variable_name": "m_count",
         "repeat_group_name": "m", "relevance": "1 = 2", "group_path": []},
        {"type": "text", "variable_name": "mname", "group_path": ["m"]},
    ]
    header, _ = _read(_run(tmp_path, form, repeat_count_overrides={"m": 3}))
    content = [c for c in header if c not in _META]
    assert not any(c.startswith("mname") for c in content)


def test_scripted_run_is_deterministic(tmp_path):
    form = [
        {"type": "select_one", "choice_list": "yn", "variable_name": "consent",
         "choices": _YESNO, "group_path": []},
        {"type": "text", "variable_name": "followup",
         "relevance": "${consent} = 1", "group_path": []},
        {"type": "integer", "variable_name": "n",
         "constraint": ". >= 0 and . <= 9", "group_path": []},
    ]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    o1 = _run(a, form, n_rows=4, seed=5, provider=ScriptedProvider({"consent": "1"}))
    o2 = _run(b, form, n_rows=4, seed=5, provider=ScriptedProvider({"consent": "1"}))
    assert o1.read_bytes() == o2.read_bytes()
