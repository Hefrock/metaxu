"""CDS Hooks demo: an assured order-sign service.

Simulates what an EHR does at order signing: it POSTs a CDS Hooks request
(hook context + prefetched FHIR resources) to a decision-support service
and renders the returned cards. The service here is wrapped with
``assured_cds_service``, so every invocation produces an assurance
artifact alongside its cards.

Two invocations run:

* **complete** — full prefetch, a well-coded warfarin draft order: the
  anticoagulation policy passes and the response carries a clean
  assurance extension;
* **careless** — allergy prefetch missing and the draft order carries a
  malformed RxNorm code (the "hallucinated code" case): the policy fails,
  terminology validation raises a critical finding, and a visible
  assurance card is appended to the response.

Run it, then inspect:

    python examples/cdshooks/run_demo.py
    metaxu inspect examples/cdshooks/out/<artifact-id>.json
    metaxu report examples/cdshooks/out
"""

from __future__ import annotations

import json
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
sys.path.insert(0, os.path.join(HERE, "..", "anticoagulation"))

from synthetic_fhir import SYNTHETIC_BUNDLE  # noqa: E402

from metaxu import PolicyEngine  # noqa: E402
from metaxu.adapters.cdshooks import assured_cds_service  # noqa: E402

POLICY_FILE = os.path.join(HERE, "..", "anticoagulation", "policies.json")

# Maps this service's prefetch keys onto the institutional policy tags.
TAG_MAP = {
    "platelets": ["platelet_count"],
    "creatinine": ["creatinine"],
    "allergies": ["allergy_check"],
}


def warfarin_order(rxnorm_code: str = "11289") -> dict:
    """A draft MedicationRequest as the EHR would put it in context."""
    return {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "MedicationRequest",
                    "id": "draft-001",
                    "status": "draft",
                    "medicationCodeableConcept": {
                        "coding": [
                            {
                                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                "code": rxnorm_code,
                                "display": "warfarin",
                            }
                        ]
                    },
                    "subject": {"reference": "Patient/pat-001"},
                }
            }
        ],
    }


def hook_request(prefetch_keys: list[str], rxnorm_code: str) -> dict:
    """Build an order-sign CDS Hooks request from the synthetic store."""
    resources = {
        "patient": SYNTHETIC_BUNDLE["Patient"][0],
        "platelets": SYNTHETIC_BUNDLE["Observation"][0],
        "creatinine": SYNTHETIC_BUNDLE["Observation"][1],
        "allergies": SYNTHETIC_BUNDLE["AllergyIntolerance"][0],
    }
    return {
        "hook": "order-sign",
        "hookInstance": f"demo-{uuid.uuid4()}",
        "fhirServer": "https://fhir.example.org/synthetic",
        # A real request carries fhirAuthorization; the adapter must never
        # record it, which the demo asserts below.
        "fhirAuthorization": {"access_token": "SECRET-TOKEN-DO-NOT-RECORD"},
        "context": {
            "userId": "Practitioner/demo",
            "patientId": "pat-001",
            "draftOrders": warfarin_order(rxnorm_code),
        },
        "prefetch": {key: resources[key] for key in prefetch_keys},
    }


@assured_cds_service(
    policy_engine=PolicyEngine.from_file(POLICY_FILE),
    tag_map=TAG_MAP,
    artifact_dir=OUT_DIR,
    add_assurance_card=True,
)
def warfarin_service(request: dict, session) -> list[dict]:
    """The decision-support logic. A real service might call a model here;
    the assurance recording is identical either way."""
    prefetch = request.get("prefetch", {})

    claims = []
    platelets = prefetch.get("platelets")
    if platelets:
        claim = session.record_claim(
            f"Platelet count {platelets['valueQuantity']['value']} "
            f"{platelets['valueQuantity']['unit']} is adequate for anticoagulation."
        )
        claims.append(claim)
    creatinine = prefetch.get("creatinine")
    if creatinine:
        claims.append(
            session.record_claim(
                f"Creatinine {creatinine['valueQuantity']['value']} mg/dL: "
                "no renal contraindication."
            )
        )
    if prefetch.get("allergies"):
        claims.append(
            session.record_claim("Documented allergy (penicillin) does not interact.")
        )
    session.record_note(
        "Pregnancy status not applicable per chart review.", tags=["pregnancy_status"]
    )

    # Evidence: link each claim to the prefetch resource it rests on.
    for claim, record in zip(claims, session.provenance[1:]):  # skip patient
        session.link_evidence(claim, [record])

    if len(claims) == 3:
        return [
            {
                "summary": "Warfarin order reviewed: no contraindication found",
                "detail": "Platelets, renal function, and allergies were checked.",
                "indicator": "info",
                "source": {"label": "Demo anticoagulation service"},
            }
        ]
    return [
        {
            "summary": "Warfarin order could not be fully verified",
            "detail": "One or more required checks lacked data in prefetch.",
            "indicator": "warning",
            "source": {"label": "Demo anticoagulation service"},
        }
    ]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    for name, keys, code in [
        ("complete", ["patient", "platelets", "creatinine", "allergies"], "11289"),
        ("careless", ["patient", "platelets"], "WARF-99"),  # malformed RxNorm
    ]:
        request = hook_request(keys, code)
        response = warfarin_service(request)
        extension = response["extension"]["dev.metaxu"]
        print(f"{name:>9}: {len(response['cards'])} card(s)")
        for card in response["cards"]:
            print(f"           [{card.get('indicator')}] {card['summary']}")
        print(
            f"           artifact {extension['artifact_id']} | "
            f"critical={extension['critical_findings']} "
            f"failed_policies={extension['failed_policies']} "
            f"malformed_codes={extension['malformed_codes']}"
        )
        # The bearer token must never reach the artifact.
        artifact_path = os.path.join(OUT_DIR, f"{extension['artifact_id']}.json")
        assert "SECRET-TOKEN" not in open(artifact_path).read(), "token leaked!"

    print(f"\nArtifacts in {OUT_DIR}; try: metaxu report {OUT_DIR}")


if __name__ == "__main__":
    main()
