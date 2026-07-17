"""Tests for the CDS Hooks adapter."""

import json

from metaxu import PolicyEngine
from metaxu.adapters.cdshooks import assured_cds_service, begin_hook, finish_hook

PATIENT = {"resourceType": "Patient", "id": "pat-1", "meta": {"versionId": "2"}}
PLATELETS = {
    "resourceType": "Observation",
    "id": "obs-plt",
    "code": {"coding": [{"system": "http://loinc.org", "code": "777-3"}]},
    "valueQuantity": {"value": 232, "unit": "10*3/uL"},
}


def make_request(prefetch=None, draft_code="11289", hook="order-sign"):
    return {
        "hook": hook,
        "hookInstance": "hook-abc-123",
        "fhirServer": "https://ehr.example.org/fhir",
        "fhirAuthorization": {"access_token": "SECRET-TOKEN", "token_type": "Bearer"},
        "context": {
            "userId": "Practitioner/u1",
            "patientId": "pat-1",
            "draftOrders": {
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "draft-1",
                            "medicationCodeableConcept": {
                                "coding": [
                                    {
                                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                        "code": draft_code,
                                        "display": "warfarin",
                                    }
                                ]
                            },
                        }
                    }
                ],
            },
        },
        "prefetch": prefetch if prefetch is not None else {
            "patient": PATIENT,
            "platelets": PLATELETS,
        },
    }


def test_begin_hook_builds_session_from_request():
    session = begin_hook(make_request(), tag_map={"platelets": ["platelet_count"]})
    assert session.correlation["interaction_id"] == "hook-abc-123"
    assert session.correlation["observer"] == "cds-hooks"
    assert "order-sign" in session.question and "Patient/pat-1" in session.question

    # Prefetch became provenance with hashes and versions.
    types = {(p.resource_type, p.resource_id) for p in session.provenance}
    assert ("Patient", "pat-1") in types and ("Observation", "obs-plt") in types
    plt = next(p for p in session.provenance if p.resource_id == "obs-plt")
    assert plt.hash.startswith("sha256:")
    assert plt.source_system == "https://ehr.example.org/fhir"

    # tag_map + prefetch key + patient_record_access all applied.
    retrievals = [e for e in session.events if e.type == "retrieval"]
    plt_event = next(e for e in retrievals if "obs-plt" in e.name)
    assert {"platelets", "patient_record_access", "platelet_count"} <= set(plt_event.tags)


def test_draft_order_codings_are_validated():
    session = begin_hook(make_request(draft_code="WARF-99"))  # malformed RxNorm
    with session:
        artifact, _ = finish_hook(session, [{"summary": "ok", "indicator": "info"}])
    malformed = [t for t in artifact.terminology if not t.get("valid")]
    assert any(t["code"] == "WARF-99" for t in malformed)
    assert any(
        f["check"] == "malformed_terminology" and f["severity"] == "critical"
        for f in artifact.safety_checks
    )


def test_fhir_authorization_never_recorded():
    session = begin_hook(make_request())
    with session:
        artifact, response = finish_hook(session, [{"summary": "ok"}])
    serialized = artifact.to_json() + json.dumps(response)
    assert "SECRET-TOKEN" not in serialized
    assert "fhirAuthorization" not in serialized


def test_finish_hook_answer_and_extension():
    session = begin_hook(make_request())
    with session:
        artifact, response = finish_hook(
            session,
            [
                {"summary": "No contraindication", "indicator": "info"},
                {"summary": "Renal dose check advised", "indicator": "warning"},
            ],
        )
    assert artifact.answer == "[info] No contraindication; [warning] Renal dose check advised"
    assert artifact.metadata["dev.metaxu/cards"][1]["indicator"] == "warning"
    ext = response["extension"]["dev.metaxu"]
    assert ext["artifact_id"] == artifact.id
    assert ext["interaction_id"] == "hook-abc-123"
    assert ext["critical_findings"] == 0 or ext["critical_findings"] > 0  # present
    assert len(response["cards"]) == 2  # no assurance card by default


def test_assurance_card_appended_only_when_problems():
    # Clean session: no card appended even with the flag on.
    clean = begin_hook(make_request())
    with clean:
        c_claim = clean.record_claim("Checked.")
        clean.link_evidence(c_claim, [clean.provenance[0]])
        _, clean_resp = finish_hook(
            clean, [{"summary": "fine", "indicator": "info"}], add_assurance_card=True
        )
    assert len(clean_resp["cards"]) == 1

    # Malformed draft code -> critical finding -> card appended.
    bad = begin_hook(make_request(draft_code="WARF-99"))
    with bad:
        b_claim = bad.record_claim("Checked.")
        bad.link_evidence(b_claim, [bad.provenance[0]])
        bad_artifact, bad_resp = finish_hook(
            bad, [{"summary": "fine", "indicator": "info"}], add_assurance_card=True
        )
    assert len(bad_resp["cards"]) == 2
    assurance = bad_resp["cards"][-1]
    assert assurance["source"]["label"] == "Metaxu assurance layer"
    assert bad_artifact.id in assurance["detail"]


def test_policy_engine_runs_against_prefetch_tags():
    engine = PolicyEngine.from_document(
        {
            "policies": [
                {
                    "name": "before_anticoagulation",
                    "trigger": {"answer_mentions": ["warfarin"]},
                    "requires": ["platelet_count", "allergy_check"],
                }
            ]
        }
    )
    session = begin_hook(
        make_request(),  # prefetch has platelets but no allergies
        policy_engine=engine,
        tag_map={"platelets": ["platelet_count"]},
    )
    with session:
        artifact, response = finish_hook(
            session, [{"summary": "warfarin order ok", "indicator": "info"}]
        )
    [check] = artifact.policy_checks
    assert check["triggered"] and not check["passed"]
    assert check["missing"] == ["allergy_check"]
    assert response["extension"]["dev.metaxu"]["failed_policies"] == [
        "before_anticoagulation"
    ]


def test_bundle_prefetch_unwrapped():
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "AllergyIntolerance", "id": "a1"}},
            {"resource": {"resourceType": "AllergyIntolerance", "id": "a2"}},
        ],
    }
    session = begin_hook(make_request(prefetch={"allergies": bundle}))
    ids = {p.resource_id for p in session.provenance}
    assert {"a1", "a2"} <= ids


def test_decorator_saves_artifacts(tmp_path):
    out = str(tmp_path / "artifacts")

    @assured_cds_service(artifact_dir=out)
    def service(request, session):
        claim = session.record_claim("Reviewed.")
        session.link_evidence(claim, [session.provenance[0]])
        return [{"summary": "ok", "indicator": "info"}]

    response = service(make_request())
    ext = response["extension"]["dev.metaxu"]
    import os

    from metaxu import AssuranceArtifact

    path = os.path.join(out, f"{ext['artifact_id']}.json")
    loaded = AssuranceArtifact.load(path)
    assert loaded.verify_integrity()
    assert loaded.correlation["observer"] == "cds-hooks"


def test_missing_hook_fields_are_tolerated():
    session = begin_hook({})  # empty request
    with session:
        artifact, response = finish_hook(session, [])
    assert artifact.answer == "(no cards returned)"
    assert response["cards"] == []
