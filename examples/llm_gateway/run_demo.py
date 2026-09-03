"""Composition demo: an LLM gateway partial + an MCP-proxy partial.

Splits the anticoagulation workflow across two observers that see
different halves of the same interaction and share an ``interaction_id``:

* the **LLM gateway** sees the model's own request/response pair — the
  prompt, the labs tool call it issued (and, once the caller echoed the
  result back on the next turn, the lab values themselves), and the final
  answer. It never sees the allergy or pregnancy checks, which ran behind
  an EHR-side MCP server the model called separately.
* the **MCP proxy** sees exactly those two tool calls on the wire — but
  never a prompt, a model, or an answer to trigger a policy against.

Neither partial alone satisfies ``before_anticoagulation``: the gateway
is missing ``allergy_check`` and ``pregnancy_status``; the policy never
even triggers on the proxy side, because triggering requires an answer
that mentions an anticoagulant and the proxy structurally never sees one.
Merging re-evaluates the policy over the union and it passes.

Run it, then compare all three artifacts:

    python examples/llm_gateway/run_demo.py
    metaxu inspect examples/llm_gateway/out/gateway-partial.json
    metaxu inspect examples/llm_gateway/out/proxy-partial.json
    metaxu inspect examples/llm_gateway/out/merged.json
"""

from __future__ import annotations

import os

from metaxu import MCPProxy, PolicyEngine, merge_artifacts
from metaxu.adapters.llm_gateway import record_exchange

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
POLICY_FILE = os.path.join(HERE, "..", "anticoagulation", "policies.json")
INTERACTION_ID = "ixn-llm-gateway-demo"
QUESTION = (
    "Patient pat-001 has new-onset atrial fibrillation. "
    "Is it appropriate to start anticoagulation?"
)


def gateway_observer():
    """What the LLM gateway sees: the model's own tool call and answer.

    Two request/response pairs, as a real tool-use loop would produce:
    the model asks for labs, the caller runs the tool and sends the
    result back, and the model's next turn is the final answer. Feeding
    ``record_exchange`` the *final* pair reconstructs the whole trace from
    ``request["messages"]``, not just this last turn.
    """
    engine = PolicyEngine.from_file(POLICY_FILE)
    first_request = {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": QUESTION}],
    }
    first_response = {
        "model": "claude-opus-5",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_labs",
                "name": "get_labs",
                "input": {"patient_id": "pat-001"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 340, "output_tokens": 45},
    }
    second_request = {
        "model": "claude-opus-5",
        "messages": first_request["messages"]
        + [
            {"role": "assistant", "content": first_response["content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_labs",
                        "content": (
                            "Platelet count 232 10*3/uL (adequate); "
                            "Creatinine 0.9 mg/dL (normal renal function)."
                        ),
                    }
                ],
            },
        ],
    }
    second_response = {
        "model": "claude-opus-5",
        "content": [
            {
                "type": "text",
                "text": (
                    "Anticoagulation (e.g. apixaban) appears appropriate "
                    "pending allergy review."
                ),
            }
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 410, "output_tokens": 60},
    }
    return record_exchange(
        second_request,
        second_response,
        policy_engine=engine,
        interaction_id=INTERACTION_ID,
        tag_map={"get_labs": ["platelet_count", "creatinine", "patient_record_access"]},
    )


def proxy_observer():
    """What the MCP proxy sees: the allergy and pregnancy-status tool calls
    that ran behind the EHR-side MCP server, invisible to the gateway."""
    proxy = MCPProxy(
        ["fake-fhir-mcp-server"],
        policy_engine=PolicyEngine.from_file(POLICY_FILE),
        tag_map={
            "get_allergies": ["allergy_check", "patient_record_access"],
            "check_pregnancy_status": ["pregnancy_status", "patient_record_access"],
        },
        interaction_id=INTERACTION_ID,
    )
    for msg_id, name, arguments, result_text in [
        (1, "get_allergies", {"patient_id": "pat-001"}, "[]"),
        (2, "check_pregnancy_status", {"patient_id": "pat-001"}, "not applicable"),
    ]:
        proxy.observe_client_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        proxy.observe_server_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            }
        )
    return proxy.finalize()


def _verdict(artifact) -> str:
    anticoag = next(
        p for p in artifact.policy_checks if p["policy"] == "before_anticoagulation"
    )
    if not anticoag["triggered"]:
        return "not triggered (no answer to check)"
    if anticoag["passed"]:
        return "PASS"
    return f"FAIL (missing: {', '.join(anticoag['missing'])})"


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    gateway = gateway_observer()
    proxy = proxy_observer()
    merged = merge_artifacts(
        [gateway, proxy], policy_engine=PolicyEngine.from_file(POLICY_FILE)
    )

    for name, artifact in [
        ("gateway-partial", gateway),
        ("proxy-partial", proxy),
        ("merged", merged),
    ]:
        path = os.path.join(OUT_DIR, f"{name}.json")
        artifact.save(path)
        print(f"{name:>16}: before_anticoagulation {_verdict(artifact)}  -> {path}")

    print(
        "\nThe gateway sees the answer but not the allergy/pregnancy checks; the "
        "proxy sees those checks but never an answer to trigger the policy at "
        "all. The merged view has both.\n"
        f"Inspect with: metaxu inspect {os.path.join(OUT_DIR, 'merged.json')}"
    )


if __name__ == "__main__":
    main()
