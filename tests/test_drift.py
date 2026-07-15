"""Tests for drift detection between artifact cohorts."""

import json

from metaxu import (
    AssuranceSession,
    PolicyEngine,
    ProvenanceRecord,
    compare_cohorts,
)
from metaxu.cli import main as cli_main
from metaxu.drift import render_text

GROUNDING = {
    "policies": [{"name": "grounding", "requires": ["patient_record_access"]}]
}


def make_artifact(
    question="Start warfarin?",
    answer="Yes, appropriate.",
    model="model-v1",
    resource_content=None,
    grounded=True,
):
    """One artifact with controllable drift-relevant properties."""
    engine = PolicyEngine.from_document(GROUNDING)
    with AssuranceSession(question=question, policy_engine=engine) as session:
        session.set_model(model, prompt_version="p1")
        prov = session.record_retrieval(
            ProvenanceRecord.for_resource(
                source_system="https://fhir.example.org",
                resource_type="Observation",
                resource_id="obs-1",
                content=resource_content if resource_content is not None else {"value": 232},
            ),
            tags=["patient_record_access"] if grounded else ["lab"],
        )
        claim = session.record_claim("Value reviewed.")
        session.link_evidence(claim, [prov])
        session.set_answer(answer)
    return session.artifact


def test_identical_cohorts_have_no_flags():
    baseline = [make_artifact(), make_artifact()]
    current = [make_artifact(), make_artifact()]
    report = compare_cohorts(baseline, current)
    assert report["flags"] == []
    assert report["answers"]["repeated_questions"] == 1
    assert report["answers"]["changed"] == []
    assert report["sources"]["resources_compared"] == 1
    assert report["sources"]["changed"] == []


def test_model_change_is_environment_drift():
    report = compare_cohorts(
        [make_artifact(model="model-v1")], [make_artifact(model="model-v2")]
    )
    env = report["environment"]["model"]
    assert env["added"] == ["model-v2"]
    assert env["removed"] == ["model-v1"]
    assert any("new model" in f for f in report["flags"])
    assert any("no longer seen" in f for f in report["flags"])


def test_answer_change_is_flagged():
    report = compare_cohorts(
        [make_artifact(answer="Yes, appropriate.")],
        [make_artifact(answer="No — contraindicated.")],
    )
    [change] = report["answers"]["changed"]
    assert change["baseline_answers"] == ["Yes, appropriate."]
    assert change["current_answers"] == ["No — contraindicated."]
    assert any("different answer" in f for f in report["flags"])


def test_answer_whitespace_and_case_are_not_drift():
    report = compare_cohorts(
        [make_artifact(answer="Yes, appropriate.")],
        [make_artifact(answer="  yes,   APPROPRIATE.  ")],
    )
    assert report["answers"]["changed"] == []


def test_source_hash_change_is_flagged():
    report = compare_cohorts(
        [make_artifact(resource_content={"value": 232})],
        [make_artifact(resource_content={"value": 90})],
    )
    [change] = report["sources"]["changed"]
    assert change["resource"] == "Observation/obs-1"
    assert change["baseline_hash"] != change["current_hash"]
    assert any("changed at the source" in f for f in report["flags"])


def test_policy_regression_is_flagged_improvement_is_not():
    good = make_artifact(grounded=True)
    bad = make_artifact(grounded=False)  # grounding policy fails

    regression = compare_cohorts([good, good], [good, bad])
    assert any("policy 'grounding' pass rate fell" in f for f in regression["flags"])
    entry = regression["behavior"]["policies"]["grounding"]
    assert entry["flagged"]
    assert entry["delta"] == -0.5

    improvement = compare_cohorts([good, bad], [good, good])
    assert not improvement["behavior"]["policies"]["grounding"]["flagged"]
    assert not any("pass rate" in f for f in improvement["flags"])


def test_threshold_suppresses_small_regressions():
    good = make_artifact(grounded=True)
    bad = make_artifact(grounded=False)
    # Pass rate falls 1.0 -> 0.9: below a 0.2 threshold, above 0.05.
    baseline = [good] * 10
    current = [good] * 9 + [bad]
    loose = compare_cohorts(baseline, current, threshold=0.2)
    strict = compare_cohorts(baseline, current, threshold=0.05)
    assert not loose["behavior"]["policies"]["grounding"]["flagged"]
    assert strict["behavior"]["policies"]["grounding"]["flagged"]


def test_new_question_is_not_answer_drift():
    report = compare_cohorts(
        [make_artifact(question="Q one?")], [make_artifact(question="Q two?")]
    )
    assert report["answers"]["repeated_questions"] == 0
    assert report["answers"]["changed"] == []


def test_render_text_smoke():
    text = render_text(
        compare_cohorts(
            [make_artifact(model="model-v1")],
            [make_artifact(model="model-v2", answer="Different.")],
        )
    )
    assert "Drift report" in text
    assert "+ model: model-v2" in text
    assert "Drift flags:" in text


def test_cli_drift_text_json_and_gate(tmp_path, capsys):
    base_dir, curr_dir = tmp_path / "base", tmp_path / "curr"
    base_dir.mkdir()
    curr_dir.mkdir()
    make_artifact().save(str(base_dir / "a.json"))
    make_artifact().save(str(curr_dir / "a.json"))

    # Identical cohorts: gate passes.
    assert cli_main(["drift", str(base_dir), str(curr_dir), "--fail-on-drift"]) == 0
    capsys.readouterr()

    # Introduce answer drift: gate fails, JSON carries the flag.
    make_artifact(answer="Changed answer.").save(str(curr_dir / "b.json"))
    assert cli_main(["drift", str(base_dir), str(curr_dir), "--fail-on-drift"]) == 1
    capsys.readouterr()

    assert cli_main(["drift", str(base_dir), str(curr_dir), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["flags"]
    assert len(report["answers"]["changed"]) == 1
