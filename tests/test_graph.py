"""Tests for the evidence graph."""

import pytest

from metaxu import (
    AssuranceSession,
    EvidenceGraph,
    MCPProxy,
    ProvenanceRecord,
)
from metaxu.cli import main as cli_main


def make_prov(resource_type="Observation", resource_id="obs-1", content=None):
    return ProvenanceRecord.for_resource(
        source_system="https://fhir.example.org",
        resource_type=resource_type,
        resource_id=resource_id,
        content=content if content is not None else {"id": resource_id},
    )


def build_chain_artifact():
    """question -> answer -> [eligibility -> data claim -> resource+coding,
    guideline claim -> guideline resource]."""
    with AssuranceSession(question="Start anticoagulation?") as session:
        obs = session.record_retrieval(make_prov("Observation", "obs-plt"))
        session.record_coding("http://loinc.org", "777-3", "Platelets", provenance=obs)
        guideline = session.record_retrieval(make_prov("PlanDefinition", "guide-1"))

        data_claim = session.record_claim("Platelets adequate.")
        session.link_evidence(data_claim, [obs])
        eligibility = session.record_claim("No contraindication.")
        session.link_evidence(eligibility, [data_claim])  # claim -> claim
        guideline_claim = session.record_claim("Guideline recommends anticoagulation.")
        session.link_evidence(guideline_claim, [guideline])

        session.set_answer("Proceed.", based_on=[eligibility, guideline_claim])
    return session.artifact


def test_nodes_and_edges_built():
    graph = EvidenceGraph.from_artifact(build_chain_artifact())
    types = sorted(n.type for n in graph.nodes.values())
    assert types.count("claim") == 3
    assert types.count("resource") == 2
    assert types.count("coding") == 1
    assert types.count("question") == 1
    assert types.count("answer") == 1
    relations = {e.relation for e in graph.edges}
    assert {"based_on", "supports", "has_coding", "answered_by"} <= relations


def test_explicit_basis_edges_are_not_implicit():
    graph = EvidenceGraph.from_artifact(build_chain_artifact())
    basis_edges = [e for e in graph.edges if e.relation == "based_on"]
    assert len(basis_edges) == 2
    assert not any(e.implicit for e in basis_edges)


def test_implicit_basis_when_based_on_omitted():
    with AssuranceSession(question="Q?") as session:
        session.record_claim("c1")
        session.record_claim("c2")
        session.set_answer("A")  # no based_on
    graph = EvidenceGraph.from_artifact(session.artifact)
    basis_edges = [e for e in graph.edges if e.relation == "based_on"]
    assert len(basis_edges) == 2
    assert all(e.implicit for e in basis_edges)


def test_support_chain_walks_multi_hop():
    graph = EvidenceGraph.from_artifact(build_chain_artifact())
    chain = graph.support_chain()
    assert chain["node"]["type"] == "answer"
    labels_by_depth = []

    def collect(entry, depth=0):
        labels_by_depth.append((depth, entry["node"]["type"]))
        for child in entry["supports"]:
            collect(child, depth + 1)

    collect(chain)
    # answer(0) -> claims(1) -> claim/resource(2) -> resource(3) -> coding(4)
    depths = {t: d for d, t in reversed(sorted(labels_by_depth))}
    assert max(d for d, t in labels_by_depth if t == "coding") == 4


def test_dependents_impact_analysis():
    artifact = build_chain_artifact()
    graph = EvidenceGraph.from_artifact(artifact)
    [obs_node] = [n for n in graph.nodes.values() if n.label == "Observation/obs-plt"]
    dependents = graph.dependents(obs_node.id)
    types = sorted(d.type for d in dependents)
    # The data claim, the eligibility claim above it, the answer, the question.
    assert types == ["answer", "claim", "claim", "question"]


def test_unsupported_claims_detected():
    with AssuranceSession(question="Q?") as session:
        supported = session.record_claim("supported")
        session.link_evidence(supported, [session.record_retrieval(make_prov())])
        session.record_claim("dangling")
        session.set_answer("A")
    graph = EvidenceGraph.from_artifact(session.artifact)
    assert [n.label for n in graph.unsupported_claims()] == ["dangling"]


def test_retrieved_by_edge_from_mcp_proxy():
    """The proxy records retrievals parented to the tool call, so the
    graph carries resource -> tool_call lineage."""
    proxy = MCPProxy(["srv"])
    proxy.observe_client_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "get_labs", "arguments": {}}}
    )
    proxy.observe_server_message(
        {"jsonrpc": "2.0", "id": 1,
         "result": {"content": [{"type": "text", "text": "{}"}], "isError": False}}
    )
    graph = EvidenceGraph.from_artifact(proxy.finalize())
    retrieved = [e for e in graph.edges if e.relation == "retrieved_by"]
    assert len(retrieved) == 1
    assert graph.nodes[retrieved[0].target].type == "tool_call"


def test_cycle_protection():
    with AssuranceSession(question="Q?") as session:
        a = session.record_claim("a")
        b = session.record_claim("b")
        session.link_evidence(a, [b])
        session.link_evidence(b, [a])  # cycle
        session.set_answer("A", based_on=[a])
    graph = EvidenceGraph.from_artifact(session.artifact)
    chain = graph.support_chain()  # must terminate
    assert chain["node"]["type"] == "answer"


def test_dangling_reference_dropped_not_fabricated():
    with AssuranceSession(question="Q?") as session:
        claim = session.record_claim("c")
        phantom = make_prov("Observation", "never-retrieved")
        session.link_evidence(claim, [phantom])  # provenance never recorded
        session.set_answer("A")
    graph = EvidenceGraph.from_artifact(session.artifact)
    assert not any(e.relation == "supports" for e in graph.edges)


def test_coding_carries_validation():
    graph = EvidenceGraph.from_artifact(build_chain_artifact())
    [coding] = [n for n in graph.nodes.values() if n.type == "coding"]
    assert coding.data["validation"]["valid"] is True
    assert coding.data["validation"]["terminology_version"] == "format-check"


def test_serializations():
    graph = EvidenceGraph.from_artifact(build_chain_artifact())
    d = graph.to_dict()
    assert len(d["nodes"]) == len(graph.nodes)
    mermaid = graph.to_mermaid()
    assert mermaid.startswith("flowchart TD")
    assert "based_on" in mermaid
    dot = graph.to_dot()
    assert dot.startswith("digraph evidence")
    text = graph.render_text()
    assert "★" in text and "▤" in text


def test_render_text_orphans_are_evidence_only():
    with AssuranceSession(question="Q?") as session:
        session.record_tool_call("unlinked_tool", {})
        session.record_retrieval(make_prov("Patient", "pat-1"))  # never cited
        claim = session.record_claim("c")
        session.link_evidence(claim, [session.record_retrieval(make_prov())])
        session.set_answer("A", based_on=[claim])
    text = EvidenceGraph.from_artifact(session.artifact).render_text()
    assert "Evidence not connected to the answer:" in text
    assert "Patient/pat-1" in text
    assert "unlinked_tool" not in text  # instrumentation is not evidence


def test_missing_node_raises():
    graph = EvidenceGraph.from_artifact(build_chain_artifact())
    with pytest.raises(KeyError):
        graph.support_chain("nope")
    with pytest.raises(KeyError):
        graph.dependents("nope")


def test_cli_graph_formats_and_dependents(tmp_path, capsys):
    path = str(tmp_path / "a.json")
    build_chain_artifact().save(path)

    assert cli_main(["graph", path]) == 0
    assert "★" in capsys.readouterr().out

    assert cli_main(["graph", path, "--format", "mermaid"]) == 0
    assert "flowchart TD" in capsys.readouterr().out

    assert cli_main(["graph", path, "--format", "json"]) == 0
    import json

    parsed = json.loads(capsys.readouterr().out)
    assert parsed["answer"] is not None

    assert cli_main(["graph", path, "--dependents", "obs-plt"]) == 0
    out = capsys.readouterr().out
    assert "Dependents of resource Observation/obs-plt" in out
    assert "answer:" in out

    assert cli_main(["graph", path, "--dependents", "zzz-no-match"]) == 2
