"""Tests for AssuranceSession recording and artifact assembly."""

from metaxu import (
    AssuranceArtifact,
    AssuranceSession,
    EventType,
    ProvenanceRecord,
    assured_tool,
    current_session,
)


def make_prov(resource_id="obs-1", resource_type="Observation"):
    return ProvenanceRecord.for_resource(
        source_system="https://fhir.example.org",
        resource_type=resource_type,
        resource_id=resource_id,
        content={"resourceType": resource_type, "id": resource_id},
    )


def test_session_records_full_flow():
    with AssuranceSession(question="Q?") as session:
        prov = session.record_retrieval(make_prov(), tags=["lab"])
        claim = session.record_claim("Platelets normal.")
        session.link_evidence(claim, [prov])
        session.set_answer("Yes.")

    artifact = session.artifact
    assert artifact is not None
    assert artifact.question == "Q?"
    assert artifact.answer == "Yes."
    assert len(artifact.provenance) == 1
    assert len(artifact.evidence) == 1
    assert artifact.evidence[0]["payload"]["claim_id"] == claim.id
    types = {e.type for e in artifact.events}
    assert EventType.QUESTION in types
    assert EventType.ANSWER in types


def test_finalize_is_idempotent():
    session = AssuranceSession(question="Q?")
    session.set_answer("A")
    first = session.finalize()
    second = session.finalize()
    assert first is second


def test_current_session_context_var():
    assert current_session() is None
    with AssuranceSession(question="Q?") as session:
        assert current_session() is session
    assert current_session() is None


def test_assured_tool_records_call_and_result():
    @assured_tool(tags=["platelet_count"], version="1.0.0")
    def get_platelets(patient_id: str) -> int:
        return 232

    with AssuranceSession(question="Q?") as session:
        assert get_platelets("pat-001") == 232
        session.set_answer("A")

    trace = session.artifact.tool_trace
    assert len(trace) == 1
    assert trace[0]["name"] == "get_platelets"
    assert trace[0]["tags"] == ["platelet_count"]
    assert trace[0]["payload"]["arguments"] == {"patient_id": "pat-001"}
    assert trace[0]["payload"]["result_summary"] == 232
    assert session.artifact.reproducibility["tool_versions"]["get_platelets"] == "1.0.0"


def test_assured_tool_records_errors_and_reraises():
    @assured_tool()
    def broken() -> None:
        raise ValueError("boom")

    with AssuranceSession(question="Q?") as session:
        try:
            broken()
        except ValueError:
            pass
        session.set_answer("A")

    [call] = session.artifact.tool_trace
    assert "ValueError: boom" in call["payload"]["error"]


def test_assured_tool_is_transparent_outside_session():
    @assured_tool()
    def plain(x: int) -> int:
        return x + 1

    assert plain(1) == 2


def test_artifact_roundtrip_and_integrity(tmp_path):
    with AssuranceSession(question="Q?") as session:
        prov = session.record_retrieval(make_prov())
        claim = session.record_claim("c")
        session.link_evidence(claim, [prov])
        session.set_answer("A")

    path = str(tmp_path / "artifact.json")
    session.artifact.save(path)

    loaded = AssuranceArtifact.load(path)
    assert loaded.id == session.artifact.id
    assert loaded.verify_integrity()
    assert AssuranceArtifact.verify_file(path)


def test_tampered_artifact_fails_integrity(tmp_path):
    import json

    with AssuranceSession(question="Q?") as session:
        session.set_answer("A")
    path = str(tmp_path / "artifact.json")
    session.artifact.save(path)

    with open(path) as f:
        data = json.load(f)
    data["answer"] = "Tampered answer"
    with open(path, "w") as f:
        json.dump(data, f)

    assert not AssuranceArtifact.verify_file(path)


def test_loaded_artifact_mutated_in_memory_fails_integrity(tmp_path):
    """Regression: verify_integrity() used to recompute the hash from
    current content and compare it with itself — a tautology that let
    post-load tampering pass. It must compare against the hash captured
    at load/save time."""
    with AssuranceSession(question="Q?") as session:
        session.set_answer("A")
    path = str(tmp_path / "artifact.json")
    session.artifact.save(path)

    loaded = AssuranceArtifact.load(path)
    assert loaded.verify_integrity()
    loaded.answer = "Tampered in memory"
    assert not loaded.verify_integrity()

    # Post-save mutation of the original object is detectable too.
    session.artifact.answer = "Tampered after save"
    assert not session.artifact.verify_integrity()


def test_missing_data_recorded():
    with AssuranceSession(question="Q?") as session:
        session.record_missing_data("pregnancy_status", reason="not in chart")
        session.set_answer("A")
    assert session.artifact.missing_data == [
        {"item": "pregnancy_status", "reason": "not in chart"}
    ]
