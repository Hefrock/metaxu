"""Tests for the safety and trust engines."""

from metaxu import AssuranceSession, ProvenanceRecord
from metaxu.events import Event, EventType
from metaxu.safety import SafetyContext, SafetyEngine


def make_prov(resource_type="Observation", resource_id="obs-1"):
    return ProvenanceRecord.for_resource(
        source_system="https://fhir.example.org",
        resource_type=resource_type,
        resource_id=resource_id,
        content={"id": resource_id},
    )


def findings_by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f.check, []).append(f)
    return out


def test_unsupported_claim_is_critical():
    with AssuranceSession(question="Q?") as session:
        session.record_claim("Renal function is normal.")
        session.set_answer("A")
    checks = {f["check"] for f in session.artifact.safety_checks}
    assert "unsupported_claims" in checks
    assert session.artifact.trust_scores["safety"]["score"] == 0.0
    assert session.artifact.trust_scores["provenance_coverage"]["score"] == 0.0


def test_supported_claim_is_clean():
    with AssuranceSession(question="Q?") as session:
        prov = session.record_retrieval(make_prov())
        claim = session.record_claim("Platelets normal.")
        session.link_evidence(claim, [prov])
        session.set_answer("A")
    assert session.artifact.safety_checks == []
    assert session.artifact.trust_scores["provenance_coverage"]["score"] == 1.0
    assert session.artifact.trust_scores["safety"]["score"] == 1.0


def test_hallucinated_resource_detected():
    claim = Event(type=EventType.CLAIM, name="claim", payload={"text": "x"})
    link = Event(
        type=EventType.EVIDENCE_LINK,
        name="supports",
        payload={"claim_id": claim.id, "provenance_ids": ["prov-nonexistent"]},
    )
    ctx = SafetyContext(answer="A", events=[claim, link], provenance=[])
    findings = findings_by_check(SafetyEngine().evaluate(ctx))
    assert "hallucinated_resources" in findings
    assert findings["hallucinated_resources"][0].severity == "critical"


def test_ignored_allergies_flagged():
    with AssuranceSession(question="Q?") as session:
        session.record_retrieval(make_prov("AllergyIntolerance", "alg-1"))
        session.set_answer("A")
    checks = {f["check"] for f in session.artifact.safety_checks}
    assert "ignored_allergies" in checks


def test_cited_allergies_not_flagged():
    with AssuranceSession(question="Q?") as session:
        prov = session.record_retrieval(make_prov("AllergyIntolerance", "alg-1"))
        claim = session.record_claim("No relevant allergies.")
        session.link_evidence(claim, [prov])
        session.set_answer("A")
    checks = {f["check"] for f in session.artifact.safety_checks}
    assert "ignored_allergies" not in checks


def test_missing_answer_flagged():
    with AssuranceSession(question="Q?") as session:
        pass
    checks = {f["check"] for f in session.artifact.safety_checks}
    assert "missing_answer" in checks


def test_missing_data_degrades_completeness():
    with AssuranceSession(question="Q?") as session:
        session.record_missing_data("pregnancy_status")
        session.set_answer("A")
    dim = session.artifact.trust_scores["data_completeness"]
    assert dim["score"] == 0.75
    assert dim["inputs"]["missing_items"] == 1


def test_fresh_retrieval_scores_high():
    with AssuranceSession(question="Q?") as session:
        prov = session.record_retrieval(make_prov())
        claim = session.record_claim("c")
        session.link_evidence(claim, [prov])
        session.set_answer("A")
    assert session.artifact.trust_scores["data_freshness"]["score"] > 0.99


def test_no_single_aggregate_score():
    with AssuranceSession(question="Q?") as session:
        session.set_answer("A")
    scores = session.artifact.trust_scores
    # Multiple named dimensions, each with a rationale; no "overall" key.
    assert len(scores) >= 5
    assert "overall" not in scores
    assert all("rationale" in dim for dim in scores.values())
