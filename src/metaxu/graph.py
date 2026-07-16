"""Evidence graph: the reasoning chain as a traversable structure.

The original vision called for a *knowledge graph of reasoning* —

    Question -> FHIR Observation -> LOINC validation
                                 -> Clinical Guideline -> Recommendation

— rather than a flat log. This module derives exactly that graph from an
artifact's event stream. The artifact stays the single source of truth
(the graph adds no artifact fields and needs no schema change); the graph
is a *view* any consumer can rebuild from any 0.3.0 artifact.

Node types: ``question``, ``answer``, ``claim``, ``resource`` (a
provenance record), ``coding`` (a terminology reference with its
validation), ``tool_call``.

Edges point **from what is supported toward what supports it**, so
walking out-edges from the answer descends to its sources:

    question --answered_by--> answer
    answer   --based_on-->    claim      (explicit, or implicit to all claims)
    claim    --supports-->    resource   (evidence_link; relation preserved)
    claim    --supports-->    claim      (evidence_link between claims)
    resource --retrieved_by-> tool_call  (retrieval parent_id)
    resource --has_coding-->  coding     (coding recorded with provenance)

Two questions become graph traversals:

* :meth:`EvidenceGraph.support_chain` — *where did this answer come
  from?* The full tree beneath the answer, down to resources, codings,
  and the tool calls that fetched them.
* :meth:`EvidenceGraph.dependents` — *this lab was corrected; what is
  affected?* Everything that transitively rests on a node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .artifact import AssuranceArtifact
from .events import EventType


@dataclass
class Node:
    id: str
    type: str  # question | answer | claim | resource | coding | tool_call
    label: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "label": self.label, "data": self.data}


@dataclass
class Edge:
    source: str
    target: str
    relation: str
    implicit: bool = False  # inferred (e.g. answer->all claims), not recorded

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
        }
        if self.implicit:
            out["implicit"] = True
        return out


class EvidenceGraph:
    """A traversable evidence graph derived from one artifact."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        self.answer_id: str | None = None
        self.question_id: str | None = None

    # -- construction ------------------------------------------------------

    def _add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def _add_edge(self, edge: Edge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            return  # never fabricate endpoints; dangling refs are dropped
        self.edges.append(edge)
        self._out.setdefault(edge.source, []).append(edge)
        self._in.setdefault(edge.target, []).append(edge)

    @classmethod
    def from_artifact(cls, artifact: AssuranceArtifact) -> "EvidenceGraph":
        graph = cls()

        # Validation results keyed by (system, code) to attach onto codings.
        validations = {
            (t.get("system"), t.get("code")): t for t in artifact.terminology
        }
        from .terminology import normalize_system

        # -- nodes ----------------------------------------------------------
        for record in artifact.provenance:
            graph._add_node(
                Node(
                    id=record.id,
                    type="resource",
                    label=f"{record.resource_type}/{record.resource_id}",
                    data={
                        "source_system": record.source_system,
                        "resource_version": record.resource_version,
                        "retrieved_at": record.retrieved_at,
                        "hash": record.hash,
                    },
                )
            )
        claim_ids: list[str] = []
        for event in artifact.events:
            if event.type == EventType.QUESTION:
                graph.question_id = event.id
                graph._add_node(
                    Node(event.id, "question", _clip(event.payload.get("text", "")), {})
                )
            elif event.type == EventType.ANSWER:
                graph.answer_id = event.id
                graph._add_node(
                    Node(event.id, "answer", _clip(event.payload.get("text", "")), {})
                )
            elif event.type == EventType.CLAIM:
                claim_ids.append(event.id)
                graph._add_node(
                    Node(event.id, "claim", _clip(event.payload.get("text", "")), {})
                )
            elif event.type == EventType.TOOL_INVOCATION:
                graph._add_node(
                    Node(
                        event.id,
                        "tool_call",
                        f"{event.name}()",
                        {"arguments": event.payload.get("arguments", {})},
                    )
                )
            elif event.type == EventType.CODING:
                system = event.payload.get("system")
                code = event.payload.get("code")
                validation = validations.get(
                    (normalize_system(system), str(code))
                ) or validations.get((system, str(code)))
                graph._add_node(
                    Node(
                        event.id,
                        "coding",
                        f"{normalize_system(system)} {code}",
                        {
                            "display": event.payload.get("display"),
                            "validation": validation,
                        },
                    )
                )

        # -- edges ----------------------------------------------------------
        explicit_basis = False
        for event in artifact.events:
            if event.type == EventType.EVIDENCE_LINK:
                claim = event.payload.get("claim_id")
                relation = event.payload.get("relation", "supports")
                for prov_id in event.payload.get("provenance_ids", []):
                    graph._add_edge(Edge(claim, prov_id, relation))
                for supporting in event.payload.get("claim_ids", []):
                    graph._add_edge(Edge(claim, supporting, relation))
            elif event.type == EventType.RETRIEVAL and event.parent_id:
                prov_id = event.payload.get("provenance_id")
                if prov_id:
                    graph._add_edge(Edge(prov_id, event.parent_id, "retrieved_by"))
            elif event.type == EventType.CODING:
                prov_id = event.payload.get("provenance_id")
                if prov_id:
                    graph._add_edge(Edge(prov_id, event.id, "has_coding"))
            elif event.type == EventType.ANSWER:
                basis = event.payload.get("based_on_claim_ids") or []
                if basis:
                    explicit_basis = True
                    for claim_id in basis:
                        graph._add_edge(Edge(event.id, claim_id, "based_on"))

        if graph.answer_id and not explicit_basis:
            # Without a recorded basis, the honest default is "the answer
            # rests on every claim" — but each edge is marked implicit so
            # a consumer can tell recorded reasoning from inferred.
            for claim_id in claim_ids:
                graph._add_edge(
                    Edge(graph.answer_id, claim_id, "based_on", implicit=True)
                )
        if graph.question_id and graph.answer_id:
            graph._add_edge(Edge(graph.question_id, graph.answer_id, "answered_by"))

        return graph

    # -- traversal ----------------------------------------------------------

    def support_chain(self, node_id: str | None = None) -> dict[str, Any]:
        """The tree of everything supporting ``node_id`` (default: the
        answer), walking out-edges with cycle protection."""
        root = node_id or self.answer_id
        if root is None or root not in self.nodes:
            raise KeyError(f"no such node: {root!r} (and no answer recorded)")

        def descend(current: str, seen: frozenset[str]) -> dict[str, Any]:
            node = self.nodes[current]
            entry: dict[str, Any] = {"node": node.to_dict(), "supports": []}
            for edge in self._out.get(current, []):
                if edge.target in seen:
                    continue  # cycle guard
                child = descend(edge.target, seen | {edge.target})
                child["relation"] = edge.relation
                if edge.implicit:
                    child["implicit"] = True
                entry["supports"].append(child)
            return entry

        return descend(root, frozenset({root}))

    def dependents(self, node_id: str) -> list[Node]:
        """Everything that transitively rests on ``node_id`` — impact
        analysis for "this resource changed, what is affected?"."""
        if node_id not in self.nodes:
            raise KeyError(f"no such node: {node_id!r}")
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            for edge in self._in.get(current, []):
                if edge.source not in seen and edge.source != node_id:
                    seen.add(edge.source)
                    stack.append(edge.source)
        return [self.nodes[i] for i in seen]

    def unsupported_claims(self) -> list[Node]:
        """Claims with no outgoing support edges (mirrors the safety check)."""
        return [
            node
            for node in self.nodes.values()
            if node.type == "claim"
            and not any(
                e.relation not in ("based_on", "answered_by")
                for e in self._out.get(node.id, [])
            )
        ]

    def find(self, fragment: str) -> list[Node]:
        """Nodes whose id or label contains ``fragment`` (case-insensitive)."""
        needle = fragment.lower()
        return [
            n
            for n in self.nodes.values()
            if needle in n.id.lower() or needle in n.label.lower()
        ]

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "question": self.question_id,
            "answer": self.answer_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_mermaid(self) -> str:
        """Mermaid flowchart (renders on GitHub and in Metaxu artifacts)."""
        shapes = {
            "question": ('(["', '"])'),
            "answer": ('[["', '"]]'),
            "claim": ('("', '")'),
            "resource": ('["', '"]'),
            "coding": ('{{"', '"}}'),
            "tool_call": ('[/"', '"/]'),
        }
        ids = {node_id: f"n{i}" for i, node_id in enumerate(self.nodes)}
        lines = ["flowchart TD"]
        for node_id, node in self.nodes.items():
            open_s, close_s = shapes[node.type]
            label = node.label.replace('"', "'")
            lines.append(f"    {ids[node_id]}{open_s}{label}{close_s}")
        for edge in self.edges:
            arrow = "-.->" if edge.implicit else "-->"
            lines.append(
                f"    {ids[edge.source]} {arrow}|{edge.relation}| {ids[edge.target]}"
            )
        return "\n".join(lines)

    def to_dot(self) -> str:
        """Graphviz DOT."""
        shapes = {
            "question": "oval",
            "answer": "doubleoctagon",
            "claim": "box",
            "resource": "folder",
            "coding": "hexagon",
            "tool_call": "cds",
        }
        lines = ["digraph evidence {", "    rankdir=TB;"]
        for node in self.nodes.values():
            label = node.label.replace('"', "'")
            lines.append(
                f'    "{node.id}" [label="{label}" shape={shapes[node.type]}];'
            )
        for edge in self.edges:
            style = " style=dashed" if edge.implicit else ""
            lines.append(
                f'    "{edge.source}" -> "{edge.target}" '
                f'[label="{edge.relation}"{style}];'
            )
        lines.append("}")
        return "\n".join(lines)

    def render_text(self) -> str:
        """Terminal tree of the answer's support chain, plus orphans."""
        lines: list[str] = []
        root_id = self.answer_id or self.question_id
        if root_id is None:
            return "(empty graph)"

        marks = {
            "question": "?",
            "answer": "★",
            "claim": "•",
            "resource": "▤",
            "coding": "#",
            "tool_call": "⚙",
        }

        def walk(entry: dict[str, Any], prefix: str, is_last: bool, is_root: bool) -> None:
            node = entry["node"]
            mark = marks[node["type"]]
            relation = f" [{entry.get('relation')}]" if entry.get("relation") else ""
            implicit = " (implicit)" if entry.get("implicit") else ""
            connector = "" if is_root else ("└─ " if is_last else "├─ ")
            lines.append(f"{prefix}{connector}{mark} {node['label']}{relation}{implicit}")
            child_prefix = prefix if is_root else prefix + ("   " if is_last else "│  ")
            children = entry["supports"]
            for i, child in enumerate(children):
                walk(child, child_prefix, i == len(children) - 1, False)

        if self.question_id:
            lines.append(f"? {self.nodes[self.question_id].label}")
            lines.append("│")
        walk(self.support_chain(root_id), "", True, True)

        reachable = self._collect_reachable(root_id)
        if self.question_id:
            reachable.add(self.question_id)
        # Only evidence-bearing nodes matter as orphans: a retrieved-but-
        # never-cited resource is a finding; an unlinked tool call is just
        # instrumentation (SDK flows record retrievals without tool parents).
        orphans = [
            n
            for i, n in self.nodes.items()
            if i not in reachable and n.type in ("resource", "claim", "coding")
        ]
        if orphans:
            lines.append("")
            lines.append("Evidence not connected to the answer:")
            for node in orphans:
                lines.append(f"  {marks[node.type]} {node.label}")
        return "\n".join(lines)

    def _collect_reachable(self, root: str) -> set[str]:
        seen = {root}
        stack = [root]
        while stack:
            current = stack.pop()
            for edge in self._out.get(current, []):
                if edge.target not in seen:
                    seen.add(edge.target)
                    stack.append(edge.target)
        return seen


def _clip(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
