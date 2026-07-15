"""Tests for the declarative policy engine."""

from metaxu import AssuranceSession, Policy, PolicyEngine
from metaxu.events import Event, EventType

ANTICOAG_POLICY = {
    "policies": [
        {
            "name": "before_anticoagulation",
            "trigger": {"answer_mentions": ["warfarin", "heparin"]},
            "requires": ["allergy_check", "platelet_count"],
        }
    ]
}


def tool_event(name, tags=()):
    return Event(type=EventType.TOOL_INVOCATION, name=name, tags=list(tags))


def test_policy_not_triggered_passes():
    engine = PolicyEngine.from_document(ANTICOAG_POLICY)
    [result] = engine.evaluate("Order a chest x-ray.", [])
    assert not result.triggered
    assert result.passed


def test_policy_triggered_and_satisfied_by_tags():
    engine = PolicyEngine.from_document(ANTICOAG_POLICY)
    events = [
        tool_event("check_allergies", tags=["allergy_check"]),
        tool_event("platelet_count"),  # satisfied by event name
    ]
    [result] = engine.evaluate("Start warfarin 5 mg.", events)
    assert result.triggered
    assert result.passed
    assert sorted(result.satisfied) == ["allergy_check", "platelet_count"]


def test_policy_triggered_and_failing_reports_missing():
    engine = PolicyEngine.from_document(ANTICOAG_POLICY)
    events = [tool_event("platelet_count")]
    [result] = engine.evaluate("Start Heparin drip.", events)  # case-insensitive
    assert result.triggered
    assert not result.passed
    assert result.missing == ["allergy_check"]


def test_always_trigger():
    policy = Policy(name="grounding", requires=["patient_record_access"], trigger={"always": True})
    result = policy.evaluate("anything", [])
    assert result.triggered
    assert not result.passed


def test_empty_trigger_means_always():
    policy = Policy(name="grounding", requires=["x"])
    assert policy.is_triggered(None, [])


def test_session_runs_policies_at_finalize():
    engine = PolicyEngine.from_document(ANTICOAG_POLICY)
    with AssuranceSession(question="Q?", policy_engine=engine) as session:
        session.record_note("checked allergies", tags=["allergy_check"])
        session.set_answer("Start warfarin.")
    [check] = session.artifact.policy_checks
    assert check["triggered"]
    assert not check["passed"]
    assert check["missing"] == ["platelet_count"]
    # Policy failure is reflected in the trust dimension.
    assert session.artifact.trust_scores["policy_compliance"]["score"] == 0.0


def test_errored_check_does_not_satisfy_policy():
    """A tool call that failed must not count as a completed check."""
    from metaxu import AssuranceSession, assured_tool

    @assured_tool(tags=["allergy_check"])
    def check_allergies(patient_id: str):
        raise ConnectionError("EHR unreachable")

    engine = PolicyEngine.from_document(ANTICOAG_POLICY)
    with AssuranceSession(question="Q?", policy_engine=engine) as session:
        try:
            check_allergies("pat-001")
        except ConnectionError:
            pass
        session.record_note("platelets reviewed", tags=["platelet_count"])
        session.set_answer("Start warfarin.")

    [check] = session.artifact.policy_checks
    assert check["triggered"]
    assert not check["passed"]
    assert check["errored"] == ["allergy_check"]
    assert check["missing"] == []
    assert check["satisfied"] == ["platelet_count"]


def test_errored_then_successful_check_satisfies_policy():
    """A retry that succeeds satisfies the requirement despite the earlier failure."""
    events = [
        Event(
            type=EventType.TOOL_INVOCATION,
            name="check_allergies",
            tags=["allergy_check"],
            payload={"error": "ConnectionError: EHR unreachable"},
        ),
        tool_event("check_allergies", tags=["allergy_check"]),
        tool_event("platelet_count"),
    ]
    engine = PolicyEngine.from_document(ANTICOAG_POLICY)
    [result] = engine.evaluate("Start warfarin.", events)
    assert result.passed
    assert result.errored == []


def test_policy_engine_from_json_file(tmp_path):
    import json

    path = tmp_path / "policies.json"
    path.write_text(json.dumps(ANTICOAG_POLICY))
    engine = PolicyEngine.from_file(str(path))
    assert len(engine.policies) == 1
    assert engine.policies[0].name == "before_anticoagulation"
