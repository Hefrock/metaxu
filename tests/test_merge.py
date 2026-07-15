"""Tests for multi-observer correlation and artifact merging."""

import json

import pytest

from metaxu import (
    AssuranceArtifact,
    AssuranceSession,
    MCPProxy,
    PolicyEngine,
    ProvenanceRecord,
    merge_artifacts,
)
from metaxu.cli import main as cli_main

POLICY_DOC = {
    "policies": [
        {
            "name": "before_anticoagulation",
            "trigger": {"answer_mentions": ["warfarin"]},
            "requires": ["allergy_check", "platelet_count"],
        }
    ]
}


def make_prov(resource_type="Observation", resource_id="obs-1"):
    return ProvenanceRecord.for_resource(
        source_system="https://fhir.example.org",
        resource_type=resource_type,
        resource_id=resource_id,
        content={"id": resource_id},
    )


def sdk_partial(ixn="ixn-1"):
    """SDK observer: sees claims, evidence, answer, and the platelet check
    — but not the allergy tool (it ran behind an MCP server)."""
    engine = PolicyEngine.from_document(POLICY_DOC)
    with AssuranceSession(
        question="Start warfarin?", policy_engine=engine, interaction_id=ixn
    ) as session:
        prov = session.record_retrieval(make_prov(), tags=["platelet_count"])
        claim = session.record_claim("Platelets adequate.")
        session.link_evidence(claim, [prov])
        session.set_answer("Warfarin appears appropriate.")
    return session.artifact


def proxy_partial(ixn="ixn-1"):
    """MCP proxy observer: sees the allergy tool call, nothing else."""
    engine = PolicyEngine.from_document(POLICY_DOC)
    proxy = MCPProxy(
        ["srv"],
        policy_engine=engine,
        tag_map={"get_allergies": ["allergy_check"]},
        interaction_id=ixn,
    )
    proxy.observe_client_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_allergies"}}
    )
    proxy.observe_server_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "{}"}], "isError": False},
        }
    )
    return proxy.finalize()


def test_correlation_present_on_every_artifact():
    artifact = sdk_partial()
    assert artifact.correlation["interaction_id"] == "ixn-1"
    assert artifact.correlation["observer"] == "sdk"
    assert artifact.correlation["role"] == "partial"


def test_interaction_id_from_environment(monkeypatch):
    monkeypatch.setenv("METAXU_INTERACTION_ID", "ixn-from-env")
    with AssuranceSession(question="Q?") as session:
        session.set_answer("A")
    assert session.artifact.correlation["interaction_id"] == "ixn-from-env"


def test_merge_re_evaluates_policies_across_observers():
    """The whole point: each partial fails the policy, the merge passes it."""
    sdk = sdk_partial()
    proxy = proxy_partial()

    # Each observer alone fails before_anticoagulation.
    sdk_check = next(p for p in sdk.policy_checks if p["policy"] == "before_anticoagulation")
    assert sdk_check["triggered"] and not sdk_check["passed"]
    assert sdk_check["missing"] == ["allergy_check"]
    # The proxy never saw the answer, so its policy wasn't even triggered.
    proxy_check = next(
        p for p in proxy.policy_checks if p["policy"] == "before_anticoagulation"
    )
    assert not proxy_check["triggered"]

    merged = merge_artifacts(
        [sdk, proxy], policy_engine=PolicyEngine.from_document(POLICY_DOC)
    )
    [check] = merged.policy_checks
    assert check["triggered"]
    assert check["passed"]
    assert sorted(check["satisfied"]) == ["allergy_check", "platelet_count"]

    assert merged.correlation["role"] == "merged"
    assert merged.correlation["merged_from"] == [sdk.id, proxy.id]
    assert merged.answer == "Warfarin appears appropriate."
    assert merged.question == "Start warfarin?"  # proxy placeholder never wins
    assert len(merged.tool_trace) == 1
    assert len(merged.provenance) == 2
    assert merged.verify_integrity()


def test_merge_rejects_mismatched_interactions():
    with pytest.raises(ValueError, match="different interactions"):
        merge_artifacts([sdk_partial("ixn-a"), proxy_partial("ixn-b")])


def test_merge_rejects_missing_correlation():
    a = sdk_partial()
    b = proxy_partial()
    b.correlation = {}
    with pytest.raises(ValueError, match="interaction_id"):
        merge_artifacts([a, b])


def test_merge_requires_two_artifacts():
    with pytest.raises(ValueError, match="at least two"):
        merge_artifacts([sdk_partial()])


def test_scalar_conflicts_are_preserved_not_silently_resolved():
    a = sdk_partial()
    b = sdk_partial()
    b.answer = "Do not start warfarin."
    merged = merge_artifacts([a, b])
    assert merged.answer == "Warfarin appears appropriate."  # first wins
    conflicts = merged.metadata["dev.metaxu/merge_conflicts"]
    assert any(
        c["field"] == "answer" and c["discarded"] == "Do not start warfarin."
        for c in conflicts
    )


def test_duplicate_events_deduplicated():
    a = sdk_partial()
    b = AssuranceArtifact.from_dict(a.to_dict())  # same events, same ids
    merged = merge_artifacts([a, b])
    observational = [e for e in merged.events if e.type not in ("policy_check", "safety_check")]
    original_observational = [
        e for e in a.events if e.type not in ("policy_check", "safety_check")
    ]
    assert len(observational) == len(original_observational)
    assert len(merged.provenance) == 1


def test_stale_partial_findings_do_not_leak_into_merged_results():
    """The SDK partial's FAIL policy event is history, not a merged result."""
    merged = merge_artifacts(
        [sdk_partial(), proxy_partial()],
        policy_engine=PolicyEngine.from_document(POLICY_DOC),
    )
    # Top-level checks come only from the re-evaluation.
    assert len(merged.policy_checks) == 1
    assert merged.policy_checks[0]["passed"]
    # But the partials' own check events remain in the stream as history.
    historical = [e for e in merged.events if e.type == "policy_check"]
    assert len(historical) > 1


def test_unknown_event_types_preserved_on_load(tmp_path):
    artifact = sdk_partial()
    data = artifact.to_dict()
    data["events"].append(
        {
            "id": "evt-future",
            "type": "quantum_check",  # from a future producer
            "name": "x",
            "timestamp": "2027-01-01T00:00:00+00:00",
        }
    )
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data))
    loaded = AssuranceArtifact.load(str(path))
    assert any(e.type == "quantum_check" for e in loaded.events)
    assert loaded.to_dict()["events"][-1]["type"] == "quantum_check"


def test_cli_merge(tmp_path, capsys):
    a_path, b_path = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    sdk_partial().save(a_path)
    proxy_partial().save(b_path)
    pol_path = tmp_path / "policies.json"
    pol_path.write_text(json.dumps(POLICY_DOC))
    out_path = str(tmp_path / "merged.json")

    assert cli_main(["merge", a_path, b_path, "-o", out_path, "--policies", str(pol_path)]) == 0
    assert "merged 2 artifacts" in capsys.readouterr().out

    merged = AssuranceArtifact.load(out_path)
    assert merged.correlation["role"] == "merged"
    assert merged.policy_checks[0]["passed"]
    # And it validates against the schema.
    assert cli_main(["validate", out_path]) == 0


def test_cli_merge_rejects_mismatch(tmp_path, capsys):
    a_path, b_path = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    sdk_partial("ixn-a").save(a_path)
    proxy_partial("ixn-b").save(b_path)
    assert cli_main(["merge", a_path, b_path, "-o", str(tmp_path / "m.json")]) == 2
