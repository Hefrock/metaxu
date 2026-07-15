"""Tests for the transparent MCP assurance proxy.

Unit tests drive MCPProxy's observers with synthetic JSON-RPC messages;
the integration test runs the real proxy CLI as a subprocess around
fake_mcp_server.py and checks both transparency (client sees exactly the
server's replies) and the artifact written on exit.
"""

import json
import os
import subprocess
import sys

from metaxu import AssuranceArtifact, MCPProxy, PolicyEngine

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_SERVER = os.path.join(HERE, "fake_mcp_server.py")


def rpc(msg_id, method, params=None):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def tool_result(payload, is_error=False):
    return {
        "jsonrpc": "2.0",
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error,
        },
    }


class TestMCPProxyUnit:
    def test_tool_call_recorded_with_tags_and_provenance(self):
        proxy = MCPProxy(["srv"], tag_map={"get_allergies": ["allergy_check"]})
        proxy.observe_client_message(
            rpc(1, "tools/call", {"name": "get_allergies", "arguments": {"patient": "p1"}})
        )
        proxy.observe_server_message(dict(tool_result({"id": "alg-001"}), id=1))
        artifact = proxy.finalize()

        [call] = artifact.tool_trace
        assert call["name"] == "get_allergies"
        assert call["tags"] == ["allergy_check"]
        assert call["payload"]["arguments"] == {"patient": "p1"}
        assert call["payload"]["error"] is None
        assert isinstance(call["payload"]["duration_ms"], float)

        [prov] = artifact.provenance
        assert prov.resource_type == "MCPToolResult"
        assert prov.resource_id == "get_allergies#1"
        assert prov.hash.startswith("sha256:")

    def test_jsonrpc_error_recorded_no_provenance(self):
        proxy = MCPProxy(["srv"])
        proxy.observe_client_message(rpc(2, "tools/call", {"name": "broken_tool"}))
        proxy.observe_server_message(
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "boom"}}
        )
        artifact = proxy.finalize()
        [call] = artifact.tool_trace
        assert call["payload"]["error"] == "jsonrpc -32000: boom"
        assert artifact.provenance == []

    def test_tool_level_error_recorded(self):
        proxy = MCPProxy(["srv"])
        proxy.observe_client_message(rpc(3, "tools/call", {"name": "flaky_tool"}))
        proxy.observe_server_message(dict(tool_result("upstream 500", is_error=True), id=3))
        artifact = proxy.finalize()
        [call] = artifact.tool_trace
        assert call["payload"]["error"].startswith("tool error:")

    def test_initialize_captured_for_reproducibility(self):
        proxy = MCPProxy(["srv"])
        proxy.observe_client_message(
            rpc(
                0,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "test-host", "version": "9"},
                },
            )
        )
        proxy.observe_server_message(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "result": {"serverInfo": {"name": "fake-fhir-server", "version": "1.2.3"}},
            }
        )
        artifact = proxy.finalize()
        repro = artifact.reproducibility
        assert repro["mcp_client"]["name"] == "test-host"
        assert repro["mcp_server"]["version"] == "1.2.3"
        assert repro["mcp_protocol_version"] == "2025-06-18"
        assert proxy.server_label == "mcp:fake-fhir-server"

    def test_unanswered_call_marked_on_finalize(self):
        proxy = MCPProxy(["srv"])
        proxy.observe_client_message(rpc(9, "tools/call", {"name": "get_allergies"}))
        artifact = proxy.finalize()
        [call] = artifact.tool_trace
        assert call["payload"]["error"] == "no response before session ended"

    def test_policy_evaluated_via_tag_map(self):
        engine = PolicyEngine.from_document(
            {
                "policies": [
                    {
                        "name": "before_anticoagulation",
                        "trigger": {"always": True},
                        "requires": ["allergy_check", "platelet_count"],
                    }
                ]
            }
        )
        proxy = MCPProxy(
            ["srv"],
            policy_engine=engine,
            tag_map={"get_allergies": ["allergy_check"]},
        )
        proxy.observe_client_message(rpc(1, "tools/call", {"name": "get_allergies"}))
        proxy.observe_server_message(dict(tool_result({"id": "alg-001"}), id=1))
        artifact = proxy.finalize()
        [check] = artifact.policy_checks
        assert check["satisfied"] == ["allergy_check"]
        assert check["missing"] == ["platelet_count"]
        assert not check["passed"]

    def test_missing_answer_not_flagged_for_proxy_sessions(self):
        proxy = MCPProxy(["srv"])
        artifact = proxy.finalize()
        assert all(f["check"] != "missing_answer" for f in artifact.safety_checks)

    def test_malformed_and_batch_messages_are_tolerated(self):
        proxy = MCPProxy(["srv"])
        proxy.observe_client_message("not an object")
        proxy.observe_client_message([rpc(1, "tools/call", {"name": "get_platelets"}), 42])
        proxy.observe_server_message([dict(tool_result({"v": 1}), id=1)])
        artifact = proxy.finalize()
        assert len(artifact.tool_trace) == 1


class TestMCPProxyIntegration:
    def run_proxy(self, tmp_path, requests, tags=None, policies=None):
        out_dir = str(tmp_path / "artifacts")
        cmd = [sys.executable, "-m", "metaxu.cli", "mcp-proxy", "--out", out_dir]
        if tags:
            tags_path = tmp_path / "tags.json"
            tags_path.write_text(json.dumps(tags))
            cmd += ["--tags", str(tags_path)]
        if policies:
            pol_path = tmp_path / "policies.json"
            pol_path.write_text(json.dumps(policies))
            cmd += ["--policies", str(pol_path)]
        cmd += ["--", sys.executable, FAKE_SERVER]

        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run(
            cmd,
            input=stdin.encode(),
            capture_output=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": os.path.join(HERE, "..", "src")},
        )
        responses = [
            json.loads(line) for line in proc.stdout.decode().splitlines() if line.strip()
        ]
        artifacts = [f for f in os.listdir(out_dir) if f.endswith(".json")]
        assert len(artifacts) == 1, proc.stderr.decode()
        artifact = AssuranceArtifact.load(os.path.join(out_dir, artifacts[0]))
        return responses, artifact, out_dir

    def test_end_to_end_transparency_and_artifact(self, tmp_path):
        requests = [
            rpc(0, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "t"}}),
            rpc(1, "tools/call", {"name": "get_allergies", "arguments": {"patient": "p1"}}),
            rpc(2, "tools/call", {"name": "get_platelets", "arguments": {"patient": "p1"}}),
            rpc(3, "tools/call", {"name": "broken_tool", "arguments": {}}),
        ]
        responses, artifact, out_dir = self.run_proxy(
            tmp_path,
            requests,
            tags={"get_allergies": ["allergy_check"], "get_platelets": ["platelet_count"]},
            policies={
                "policies": [
                    {
                        "name": "before_anticoagulation",
                        "trigger": {"always": True},
                        "requires": ["allergy_check", "platelet_count"],
                    }
                ]
            },
        )

        # Transparency: the client received all four replies, unmodified.
        assert {r["id"] for r in responses} == {0, 1, 2, 3}
        allergy_reply = next(r for r in responses if r["id"] == 1)
        assert "alg-001" in allergy_reply["result"]["content"][0]["text"]
        assert next(r for r in responses if r["id"] == 3)["error"]["message"] == "boom"

        # Assurance: the artifact saw everything.
        assert len(artifact.tool_trace) == 3
        assert artifact.verify_integrity()
        [check] = artifact.policy_checks
        assert check["passed"]
        assert artifact.reproducibility["mcp_server"]["name"] == "fake-fhir-server"

        # Snapshots enable replay verification of what the AI saw.
        from metaxu import snapshot_resolver, verify

        report = verify(artifact, snapshot_resolver(os.path.join(out_dir, "snapshots")))
        assert report.ok
        assert report.provenance_matched == 2
