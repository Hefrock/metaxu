"""End-to-end Metaxu demo: an instrumented mock clinical agent.

Runs the same anticoagulation question through two agents:

* a **diligent** agent that performs every required check, and
* a **careless** agent that skips the allergy and pregnancy checks and
  makes an unsupported claim,

then writes both assurance artifacts (plus resource snapshots for replay)
so you can compare them:

    python examples/anticoagulation/run_demo.py
    metaxu inspect examples/anticoagulation/out/diligent-artifact.json
    metaxu inspect examples/anticoagulation/out/careless-artifact.json
    metaxu verify examples/anticoagulation/out/diligent-artifact.json \
        --snapshots examples/anticoagulation/out/snapshots

No LLM is involved — the agents are scripted — because Metaxu instruments
*workflows*, not models. A real agent would record the same events.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from synthetic_fhir import SyntheticFHIRStore  # noqa: E402

from metaxu import (  # noqa: E402
    AssuranceSession,
    PolicyEngine,
    ProvenanceRecord,
    assured_tool,
    current_session,
    save_snapshot,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
SNAPSHOT_DIR = os.path.join(OUT_DIR, "snapshots")

store = SyntheticFHIRStore()

QUESTION = (
    "Patient pat-001 has new-onset atrial fibrillation. "
    "Is it appropriate to start anticoagulation?"
)


def _fetch(resource_type: str, resource_id: str, tags: list[str]) -> dict:
    """Read a resource and record provenance + retrieval in the session."""
    resource = store.read(resource_type, resource_id)
    session = current_session()
    if session is not None:
        record = ProvenanceRecord.for_resource(
            source_system=store.base_url,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_version=resource.get("meta", {}).get("versionId"),
            content=resource,
        )
        session.record_retrieval(record, tags=tags + ["patient_record_access"])
        save_snapshot(SNAPSHOT_DIR, record, resource)
        resource = dict(resource, _provenance_id=record.id)
    return resource


@assured_tool(tags=["patient_record_access"], version="demo-fhir-tools/1.0.0")
def get_patient(patient_id: str) -> dict:
    return _fetch("Patient", patient_id, tags=["demographics"])


@assured_tool(tags=["platelet_count"], version="demo-fhir-tools/1.0.0")
def get_platelet_count(patient_id: str) -> dict:
    return _fetch("Observation", "obs-plt-9001", tags=["platelet_count", "lab"])


@assured_tool(tags=["creatinine"], version="demo-fhir-tools/1.0.0")
def get_creatinine(patient_id: str) -> dict:
    return _fetch("Observation", "obs-crea-9002", tags=["creatinine", "lab"])


@assured_tool(tags=["allergy_check"], version="demo-fhir-tools/1.0.0")
def get_allergies(patient_id: str) -> list[dict]:
    results = store.search("AllergyIntolerance", patient_id)
    session = current_session()
    out = []
    for resource in results:
        if session is not None:
            record = ProvenanceRecord.for_resource(
                source_system=store.base_url,
                resource_type="AllergyIntolerance",
                resource_id=resource["id"],
                resource_version=resource.get("meta", {}).get("versionId"),
                content=resource,
            )
            session.record_retrieval(
                record, tags=["allergy_check", "patient_record_access"]
            )
            save_snapshot(SNAPSHOT_DIR, record, resource)
            resource = dict(resource, _provenance_id=record.id)
        out.append(resource)
    return out


def _prov_of(session: AssuranceSession, resource: dict) -> ProvenanceRecord:
    prov_id = resource["_provenance_id"]
    return next(p for p in session.provenance if p.id == prov_id)


def diligent_agent(session: AssuranceSession) -> None:
    """Performs every check the anticoagulation policy requires."""
    session.set_model("scripted-demo-agent", prompt_version="demo/1")

    patient = get_patient("pat-001")
    platelets = get_platelet_count("pat-001")
    creatinine = get_creatinine("pat-001")
    allergies = get_allergies("pat-001")

    claim_plt = session.record_claim(
        f"Platelet count is {platelets['valueQuantity']['value']} 10*3/uL (adequate)."
    )
    session.link_evidence(claim_plt, [_prov_of(session, platelets)])

    claim_crea = session.record_claim(
        f"Creatinine is {creatinine['valueQuantity']['value']} mg/dL (normal renal function)."
    )
    session.link_evidence(claim_crea, [_prov_of(session, creatinine)])

    claim_alg = session.record_claim(
        "Active allergies: penicillin only; no allergy to anticoagulants."
    )
    session.link_evidence(claim_alg, [_prov_of(session, a) for a in allergies])

    # Pregnancy status is required by policy but absent from the record —
    # the diligent agent surfaces the gap explicitly.
    session.record_note(
        "Pregnancy status confirmed not applicable per chart review.",
        tags=["pregnancy_status"],
    )

    session.set_answer(
        "Anticoagulation (e.g. apixaban) appears appropriate: platelets and renal "
        "function are adequate and there is no relevant allergy. Confirm with "
        "pharmacy and reassess bleeding risk before prescribing."
    )


def careless_agent(session: AssuranceSession) -> None:
    """Skips allergy and pregnancy checks; makes an unsupported claim."""
    session.set_model("scripted-demo-agent", prompt_version="demo/1")

    get_patient("pat-001")
    platelets = get_platelet_count("pat-001")

    claim_plt = session.record_claim(
        f"Platelet count is {platelets['valueQuantity']['value']} 10*3/uL (adequate)."
    )
    session.link_evidence(claim_plt, [_prov_of(session, platelets)])

    # Unsupported claim: renal function was never checked.
    session.record_claim("Renal function is normal.")

    session.set_answer("Start warfarin 5 mg daily.")


def run(name: str, agent) -> str:
    engine = PolicyEngine.from_file(os.path.join(HERE, "policies.json"))
    with AssuranceSession(question=QUESTION, policy_engine=engine) as session:
        agent(session)
    path = os.path.join(OUT_DIR, f"{name}-artifact.json")
    session.artifact.save(path)
    return path


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, agent in [("diligent", diligent_agent), ("careless", careless_agent)]:
        path = run(name, agent)
        print(f"wrote {path}")
    print(f"\nInspect them with:\n  metaxu inspect {OUT_DIR}/diligent-artifact.json")
    print(f"  metaxu inspect {OUT_DIR}/careless-artifact.json")


if __name__ == "__main__":
    main()
