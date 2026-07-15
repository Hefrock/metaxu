"""Composition demo: two observers, one interaction, one merged artifact.

No single interception point sees a whole clinical interaction. This demo
splits the anticoagulation workflow across two observers that share an
``interaction_id``:

* an **MCP proxy** observes the allergy lookup (a tool call on the wire —
  it never sees claims or the final answer), and
* an **SDK-instrumented agent** performs the platelet/creatinine checks,
  records claims and evidence, and produces the answer — but never sees
  the allergy tool, which ran behind the MCP server.

Each partial artifact *fails* the ``before_anticoagulation`` policy on
its own. Merging them re-evaluates the policy over the union of
observations, and it passes — composition, not concatenation.

Run it, then compare all three artifacts:

    python examples/composition/run_demo.py
    metaxu inspect examples/composition/out/sdk-partial.json
    metaxu inspect examples/composition/out/proxy-partial.json
    metaxu inspect examples/composition/out/merged.json
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
sys.path.insert(0, os.path.join(HERE, "..", "anticoagulation"))

from synthetic_fhir import SyntheticFHIRStore  # noqa: E402

from metaxu import (  # noqa: E402
    AssuranceSession,
    MCPProxy,
    PolicyEngine,
    ProvenanceRecord,
    merge_artifacts,
)

INTERACTION_ID = "ixn-composition-demo"
QUESTION = (
    "Patient pat-001 has new-onset atrial fibrillation. "
    "Is it appropriate to start anticoagulation?"
)
POLICY_FILE = os.path.join(HERE, "..", "anticoagulation", "policies.json")

store = SyntheticFHIRStore()


def proxy_observer():
    """What the MCP proxy sees: the allergy tool call crossing the wire.

    Driven with synthetic JSON-RPC here so the demo has no subprocesses;
    `metaxu mcp-proxy` records exactly the same events for a real server.
    """
    proxy = MCPProxy(
        ["fake-fhir-mcp-server"],
        policy_engine=PolicyEngine.from_file(POLICY_FILE),
        tag_map={"get_allergies": ["allergy_check", "patient_record_access"]},
        interaction_id=INTERACTION_ID,
    )
    allergies = store.search("AllergyIntolerance", "pat-001")
    proxy.observe_client_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_allergies", "arguments": {"patient_id": "pat-001"}},
        }
    )
    proxy.observe_server_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(allergies)}],
                "isError": False,
            },
        }
    )
    return proxy.finalize()


def sdk_observer():
    """What the instrumented agent sees: labs, claims, evidence, answer."""
    engine = PolicyEngine.from_file(POLICY_FILE)
    with AssuranceSession(
        question=QUESTION, policy_engine=engine, interaction_id=INTERACTION_ID
    ) as session:
        for resource_id, tag, text in [
            ("obs-plt-9001", "platelet_count", "Platelet count is 232 10*3/uL (adequate)."),
            ("obs-crea-9002", "creatinine", "Creatinine is 0.9 mg/dL (normal renal function)."),
        ]:
            resource = store.read("Observation", resource_id)
            prov = session.record_retrieval(
                ProvenanceRecord.for_resource(
                    source_system=store.base_url,
                    resource_type="Observation",
                    resource_id=resource_id,
                    resource_version=resource.get("meta", {}).get("versionId"),
                    content=resource,
                ),
                tags=[tag, "patient_record_access"],
            )
            claim = session.record_claim(text)
            session.link_evidence(claim, [prov])
        session.record_note(
            "Pregnancy status confirmed not applicable per chart review.",
            tags=["pregnancy_status"],
        )
        session.set_answer(
            "Anticoagulation (e.g. apixaban) appears appropriate pending allergy review."
        )
    return session.artifact


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    sdk = sdk_observer()
    proxy = proxy_observer()
    merged = merge_artifacts(
        [sdk, proxy], policy_engine=PolicyEngine.from_file(POLICY_FILE)
    )

    for name, artifact in [("sdk-partial", sdk), ("proxy-partial", proxy), ("merged", merged)]:
        path = os.path.join(OUT_DIR, f"{name}.json")
        artifact.save(path)
        anticoag = next(
            p for p in artifact.policy_checks if p["policy"] == "before_anticoagulation"
        )
        verdict = (
            "PASS" if anticoag["passed"] and anticoag["triggered"]
            else "not triggered" if not anticoag["triggered"]
            else f"FAIL (missing: {', '.join(anticoag['missing'])})"
        )
        print(f"{name:>13}: before_anticoagulation {verdict}  -> {path}")

    print(
        "\nEach observer alone cannot satisfy the policy; the merged view can."
        f"\nInspect with: metaxu inspect {os.path.join(OUT_DIR, 'merged.json')}"
    )


if __name__ == "__main__":
    main()
