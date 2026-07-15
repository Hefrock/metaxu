"""A minimal MCP stdio server for proxy integration tests.

Speaks newline-delimited JSON-RPC 2.0. Tools:
  get_allergies  -> returns synthetic allergy data
  get_platelets  -> returns a synthetic platelet observation
  broken_tool    -> returns a JSON-RPC error
  flaky_tool     -> returns an MCP tool-level error (isError: true)
"""

import json
import sys

ALLERGY = {"resourceType": "AllergyIntolerance", "id": "alg-001", "substance": "penicillin"}
PLATELETS = {"resourceType": "Observation", "id": "obs-plt-9001", "value": 232}


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def text_result(payload, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": is_error,
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            reply(
                msg_id,
                result={
                    "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-fhir-server", "version": "1.2.3"},
                },
            )
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name == "get_allergies":
                reply(msg_id, result=text_result(ALLERGY))
            elif name == "get_platelets":
                reply(msg_id, result=text_result(PLATELETS))
            elif name == "broken_tool":
                reply(msg_id, error={"code": -32000, "message": "boom"})
            elif name == "flaky_tool":
                reply(msg_id, result=text_result("upstream 500", is_error=True))
            else:
                reply(msg_id, error={"code": -32601, "message": f"unknown tool {name}"})
        elif msg_id is not None:
            reply(msg_id, result={})
        # notifications (no id) get no reply


if __name__ == "__main__":
    main()
