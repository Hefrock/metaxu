"""Tests for policy engine v2: value (where) and temporal (within_hours)
conditions, and the unmet outcome bucket."""

from datetime import datetime, timedelta, timezone

from metaxu import AssuranceSession, Policy, PolicyEngine, assured_tool
from metaxu.events import Event, EventType

PLATELET_RESULT = {
    "resourceType": "Observation",
    "id": "obs-plt-9001",
    "valueQuantity": {"value": 232, "unit": "10*3/uL"},
}


def value_policy(threshold=50):
    return PolicyEngine.from_document(
        {
            "policies": [
                {
                    "name": "adequate_platelets",
                    "trigger": {"always": True},
                    "requires": [
                        {
                            "check": "platelet_count",
                            "where": {
                                "path": "result_summary.valueQuantity.value",
                                "gte": threshold,
                            },
                        }
                    ],
                }
            ]
        }
    )


def run_with_tool(engine, result=PLATELET_RESULT):
    @assured_tool(tags=["platelet_count"])
    def get_platelets(patient_id: str) -> dict:
        return result

    with AssuranceSession(question="Q?", policy_engine=engine) as session:
        get_platelets("pat-001")
        session.set_answer("A")
    return session.artifact.policy_checks[0]


def test_where_satisfied_by_structured_tool_result():
    check = run_with_tool(value_policy(threshold=50))
    assert check["passed"]
    assert check["satisfied"] == ["platelet_count"]


def test_where_failure_lands_in_unmet_not_missing():
    check = run_with_tool(value_policy(threshold=500))  # 232 < 500
    assert not check["passed"]
    assert check["unmet"] == ["platelet_count"]
    assert check["missing"] == []
    assert check["errored"] == []


def test_where_missing_path_is_conservative():
    check = run_with_tool(value_policy(), result={"no": "value here"})
    assert not check["passed"]
    assert check["unmet"] == ["platelet_count"]


def test_where_operators():
    policy = Policy(
        name="p",
        requires=[{"check": "c", "where": {"path": "data.status", "in": ["final", "amended"]}}],
    )
    ok = Event(type=EventType.NOTE, name="c", payload={"data": {"status": "final"}})
    bad = Event(type=EventType.NOTE, name="c", payload={"data": {"status": "preliminary"}})
    assert policy.evaluate("A", [ok]).passed
    assert policy.evaluate("A", [bad]).unmet == ["c"]


def test_where_type_mismatch_is_false_not_crash():
    policy = Policy(
        name="p", requires=[{"check": "c", "where": {"path": "data.value", "gt": 50}}]
    )
    event = Event(
        type=EventType.NOTE, name="c", payload={"data": {"value": "not a number"}}
    )
    result = policy.evaluate("A", [event])
    assert result.unmet == ["c"]


def test_within_hours_rejects_stale_check():
    policy = Policy(name="p", requires=[{"check": "lab", "within_hours": 24}])
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    stale = Event(type=EventType.NOTE, name="lab", timestamp=old.isoformat())
    answer = Event(type=EventType.ANSWER, name="answer")
    result = policy.evaluate("A", [stale, answer])
    assert not result.passed
    assert result.unmet == ["lab"]


def test_within_hours_accepts_fresh_check():
    policy = Policy(name="p", requires=[{"check": "lab", "within_hours": 24}])
    fresh = Event(type=EventType.NOTE, name="lab")
    answer = Event(type=EventType.ANSWER, name="answer")
    assert policy.evaluate("A", [fresh, answer]).passed


def test_within_hours_measured_at_answer_time_not_now():
    """A lab that was fresh when the answer was given stays satisfied even
    if the artifact is evaluated much later."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    lab = Event(type=EventType.NOTE, name="lab", timestamp=week_ago.isoformat())
    answer = Event(
        type=EventType.ANSWER,
        name="answer",
        timestamp=(week_ago + timedelta(hours=2)).isoformat(),
    )
    policy = Policy(name="p", requires=[{"check": "lab", "within_hours": 24}])
    assert policy.evaluate("A", [lab, answer]).passed


def test_string_and_object_requirements_mix():
    engine = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "name": "mixed",
                    "requires": [
                        "allergy_check",
                        {"check": "platelet_count", "where": {"path": "data.value", "gte": 50}},
                    ],
                }
            ]
        }
    )
    events = [
        Event(type=EventType.NOTE, name="x", tags=["allergy_check"]),
        Event(type=EventType.NOTE, name="platelet_count", payload={"data": {"value": 232}}),
    ]
    [result] = engine.evaluate("A", events)
    assert result.passed
    assert sorted(result.satisfied) == ["allergy_check", "platelet_count"]


def test_record_note_data_feeds_where_clauses():
    engine = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "name": "renal",
                    "requires": [
                        {"check": "creatinine", "where": {"path": "data.value", "lte": 1.2}}
                    ],
                }
            ]
        }
    )
    with AssuranceSession(question="Q?", policy_engine=engine) as session:
        session.record_note("Creatinine reviewed", tags=["creatinine"], data={"value": 0.9})
        session.set_answer("A")
    assert session.artifact.policy_checks[0]["passed"]


def test_errored_events_never_satisfy_conditions():
    policy = Policy(
        name="p", requires=[{"check": "c", "where": {"path": "data.value", "gte": 1}}]
    )
    errored = Event(
        type=EventType.TOOL_INVOCATION,
        name="c",
        payload={"error": "boom", "data": {"value": 5}},
    )
    result = policy.evaluate("A", [errored])
    assert result.errored == ["c"]
    assert not result.passed
