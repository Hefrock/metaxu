"""A tiny in-memory FHIR server with one synthetic patient.

All data is synthetic. The store mimics the minimal read/search surface an
MCP FHIR tool would expose, including ``meta.versionId`` so provenance can
record resource versions.
"""

from __future__ import annotations

from typing import Any

SYNTHETIC_BUNDLE: dict[str, list[dict[str, Any]]] = {
    "Patient": [
        {
            "resourceType": "Patient",
            "id": "pat-001",
            "meta": {"versionId": "3"},
            "name": [{"family": "Testcase", "given": ["Alex"]}],
            "gender": "female",
            "birthDate": "1962-04-18",
        }
    ],
    "Observation": [
        {
            "resourceType": "Observation",
            "id": "obs-plt-9001",
            "meta": {"versionId": "1"},
            "status": "final",
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "777-3",
                        "display": "Platelets [#/volume] in Blood by Automated count",
                    }
                ]
            },
            "subject": {"reference": "Patient/pat-001"},
            "effectiveDateTime": "2026-07-14T08:30:00Z",
            "valueQuantity": {
                "value": 232,
                "unit": "10*3/uL",
                "system": "http://unitsofmeasure.org",
                "code": "10*3/uL",
            },
        },
        {
            "resourceType": "Observation",
            "id": "obs-crea-9002",
            "meta": {"versionId": "1"},
            "status": "final",
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "2160-0",
                        "display": "Creatinine [Mass/volume] in Serum or Plasma",
                    }
                ]
            },
            "subject": {"reference": "Patient/pat-001"},
            "effectiveDateTime": "2026-07-14T08:30:00Z",
            "valueQuantity": {
                "value": 0.9,
                "unit": "mg/dL",
                "system": "http://unitsofmeasure.org",
                "code": "mg/dL",
            },
        },
    ],
    "PlanDefinition": [
        {
            "resourceType": "PlanDefinition",
            "id": "guideline-af-anticoag",
            "meta": {"versionId": "5"},
            "status": "active",
            "title": "Anticoagulation for nonvalvular atrial fibrillation",
            "description": (
                "For patients with nonvalvular atrial fibrillation and elevated "
                "stroke risk, oral anticoagulation is recommended when platelet "
                "count and renal function are adequate and no contraindicating "
                "allergy exists."
            ),
            "topic": [
                {
                    "coding": [
                        {
                            "system": "http://snomed.info/sct",
                            "code": "49436004",
                            "display": "Atrial fibrillation",
                        }
                    ]
                }
            ],
        }
    ],
    "AllergyIntolerance": [
        {
            "resourceType": "AllergyIntolerance",
            "id": "alg-001",
            "meta": {"versionId": "2"},
            "clinicalStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                        "code": "active",
                    }
                ]
            },
            "code": {
                "coding": [
                    {
                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                        "code": "7980",
                        "display": "Penicillin G",
                    }
                ]
            },
            "patient": {"reference": "Patient/pat-001"},
        }
    ],
}


class SyntheticFHIRStore:
    """Minimal read/search API over the synthetic bundle."""

    base_url = "https://fhir.example.org/synthetic"

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        for resource in SYNTHETIC_BUNDLE.get(resource_type, []):
            if resource["id"] == resource_id:
                return resource
        raise KeyError(f"{resource_type}/{resource_id} not found")

    def search(self, resource_type: str, patient_id: str) -> list[dict[str, Any]]:
        results = []
        for resource in SYNTHETIC_BUNDLE.get(resource_type, []):
            subject = resource.get("subject") or resource.get("patient") or {}
            if subject.get("reference") == f"Patient/{patient_id}":
                results.append(resource)
        return results
