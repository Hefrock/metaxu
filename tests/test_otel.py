"""Tests for the OpenTelemetry exporter adapter.

Skipped entirely when the optional 'otel' extra is not installed, so the
stdlib-only core is never coupled to opentelemetry.
"""

import json

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402

from metaxu import AssuranceSession, PolicyEngine, ProvenanceRecord  # noqa: E402
from metaxu.adapters.otel import build_tracer, export_artifact  # noqa: E402


def export(artifact, capture_content=False):
    exporter = InMemorySpanExporter()
    export_artifact(artifact, tracer=build_tracer(exporter), capture_content=capture_content)
    return exporter.get_finished_spans()


def root_of(spans):
    return next(s for s in spans if s.name == "metaxu.interaction")


def rich_artifact():
    engine = PolicyEngine.from_document(
        {"policies": [{"name": "grounding", "requires": ["patient_record_access"]}]}
    )
    with AssuranceSession(question="Start anticoagulation?", policy_engine=engine) as s:
        s.set_model("test-model", prompt_version="p1")
        prov = s.record_retrieval(
            ProvenanceRecord.for_resource(
                source_system="https://fhir.example.org",
                resource_type="Observation",
                resource_id="obs-1",
                content={"value": 232},
            ),
            tags=["patient_record_access"],
        )
        s.record_coding("http://loinc.org", "2160-0", "Creatinine", provenance=prov)
        s.record_tool_call("get_labs", {"patient": "p1"}, result={"v": 1}, duration_ms=12.5)
        claim = s.record_claim("Platelets adequate.")
        s.link_evidence(claim, [prov])
        s.set_answer("Proceed.", based_on=[claim])
    return s.artifact


def test_root_span_and_children():
    spans = export(rich_artifact())
    root = root_of(spans)
    assert root.attributes["gen_ai.request.model"] == "test-model"
    assert root.attributes["metaxu.prompt_version"] == "p1"
    assert root.attributes["metaxu.interaction_id"].startswith("ixn-")
    tool = next(s for s in spans if s.attributes.get("gen_ai.tool.name") == "get_labs")
    assert tool.attributes["metaxu.tool.duration_ms"] == 12.5
    assert json.loads(tool.attributes["metaxu.tool.arguments"]) == {"patient": "p1"}
    assert any(s.name == "retrieve Observation/obs-1" for s in spans)


def test_trust_dimensions_are_attributes():
    root = root_of(export(rich_artifact()))
    assert root.attributes["metaxu.trust.provenance_coverage"] == 1.0
    assert "metaxu.trust.terminology_correctness" in root.attributes


def test_span_events_for_claims_policies_codings():
    root = root_of(export(rich_artifact()))
    event_names = {e.name for e in root.events}
    assert {"claim", "policy_check", "coding"} <= event_names
    policy_event = next(e for e in root.events if e.name == "policy_check")
    assert policy_event.attributes["metaxu.policy.passed"] is True


def test_status_ok_when_clean():
    assert root_of(export(rich_artifact())).status.status_code is StatusCode.OK


def test_status_error_on_critical_finding():
    with AssuranceSession(question="Q?") as s:
        s.record_claim("Unsupported claim.")  # -> critical unsupported_claims
        s.set_answer("A")
    root = root_of(export(s.artifact))
    assert root.status.status_code is StatusCode.ERROR
    assert "critical safety finding" in root.status.description


def test_status_error_on_policy_failure():
    engine = PolicyEngine.from_document(
        {"policies": [{"name": "grounding", "requires": ["patient_record_access"]}]}
    )
    with AssuranceSession(question="Q?", policy_engine=engine) as s:
        s.set_answer("A")  # grounding never satisfied
    root = root_of(export(s.artifact))
    assert root.status.status_code is StatusCode.ERROR
    assert "policy failure" in root.status.description


def test_tool_error_marks_child_span():
    with AssuranceSession(question="Q?") as s:
        s.record_tool_call("broken", {}, error="ConnectionError: down", duration_ms=3.0)
        s.set_answer("A")
    spans = export(s.artifact)
    tool = next(s for s in spans if s.attributes.get("gen_ai.tool.name") == "broken")
    assert tool.status.status_code is StatusCode.ERROR
    assert "ConnectionError" in tool.attributes["metaxu.tool.error"]


def test_malformed_coding_flagged_in_event():
    with AssuranceSession(question="Q?") as s:
        s.record_coding("http://loinc.org", "9999-9")  # malformed
        s.set_answer("A")
    root = root_of(export(s.artifact))
    coding = next(e for e in root.events if e.name == "coding")
    assert coding.attributes["metaxu.coding.valid"] is False


def test_phi_omitted_by_default_included_on_optin():
    artifact = rich_artifact()
    default_root = root_of(export(artifact))
    assert "metaxu.question" not in default_root.attributes
    assert default_root.attributes["metaxu.question.present"] is True
    claim_event = next(e for e in default_root.events if e.name == "claim")
    assert claim_event.attributes["metaxu.claim.text"] == ""

    captured_root = root_of(export(artifact, capture_content=True))
    assert captured_root.attributes["metaxu.question"] == "Start anticoagulation?"
    assert captured_root.attributes["metaxu.answer"] == "Proceed."


def test_span_timing_reflects_recorded_durations():
    root = root_of(export(rich_artifact()))
    assert root.start_time is not None and root.end_time is not None
    assert root.end_time >= root.start_time


def test_default_console_tracer_does_not_crash():
    # tracer=None -> ConsoleSpanExporter path; must run without raising.
    from metaxu.adapters.otel import export_artifact as ea

    ea(rich_artifact())  # no exception == pass
