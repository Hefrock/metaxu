"""The Assurance Artifact: the primary deliverable of Metaxu.

Every AI interaction produces one machine-readable artifact. Everything
else in the ecosystem — dashboards, CI pipelines, replay tools, governance
reports — consumes this artifact rather than the AI system directly.

The canonical schema lives in ``src/metaxu/spec/assurance-artifact.schema.json``
and is documented in ``spec/ARTIFACT.md``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import Event, utcnow
from .provenance import ProvenanceRecord, content_hash

ARTIFACT_SCHEMA_VERSION = "0.2.0"


@dataclass
class AssuranceArtifact:
    """Machine-readable record of one AI-mediated clinical interaction.

    ``correlation`` ties together multiple observers of the same
    interaction: every artifact carries an ``interaction_id`` shared
    across observers, the ``observer`` that produced this view, and a
    ``role`` — ``partial`` (one observer's vantage point; every
    single-observer artifact is by definition partial) or ``merged``
    (assembled from several partials by :func:`metaxu.merge_artifacts`,
    which also records ``merged_from``).
    """

    question: str
    answer: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[ProvenanceRecord] = field(default_factory=list)
    policy_checks: list[dict[str, Any]] = field(default_factory=list)
    safety_checks: list[dict[str, Any]] = field(default_factory=list)
    missing_data: list[dict[str, Any]] = field(default_factory=list)
    trust_scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    reproducibility: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    correlation: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"axa-{uuid.uuid4()}")
    created_at: str = field(default_factory=utcnow)
    schema_version: str = ARTIFACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "question": self.question,
            "answer": self.answer,
            "evidence": self.evidence,
            "tool_trace": self.tool_trace,
            "provenance": [p.to_dict() for p in self.provenance],
            "policy_checks": self.policy_checks,
            "safety_checks": self.safety_checks,
            "missing_data": self.missing_data,
            "trust_scores": self.trust_scores,
            "reproducibility": self.reproducibility,
            "metadata": self.metadata,
            "correlation": self.correlation,
            "events": [e.to_dict() for e in self.events],
        }
        # Integrity hash covers every field above; consumers can detect
        # tampering or truncation without any external key material.
        body["artifact_hash"] = content_hash(body)
        return body

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssuranceArtifact":
        return cls(
            question=data["question"],
            answer=data.get("answer"),
            evidence=data.get("evidence", []),
            tool_trace=data.get("tool_trace", []),
            provenance=[ProvenanceRecord.from_dict(p) for p in data.get("provenance", [])],
            policy_checks=data.get("policy_checks", []),
            safety_checks=data.get("safety_checks", []),
            missing_data=data.get("missing_data", []),
            trust_scores=data.get("trust_scores", {}),
            reproducibility=data.get("reproducibility", {}),
            metadata=data.get("metadata", {}),
            correlation=data.get("correlation", {}),
            events=[Event.from_dict(e) for e in data.get("events", [])],
            id=data["id"],
            created_at=data["created_at"],
            schema_version=data.get("schema_version", ARTIFACT_SCHEMA_VERSION),
        )

    @classmethod
    def load(cls, path: str) -> "AssuranceArtifact":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def verify_integrity(self) -> bool:
        """Recompute the artifact hash and compare with the stored one.

        Returns True when the artifact was produced by :meth:`to_dict` and
        has not been modified since.
        """
        current = self.to_dict()
        stored_hash = current.pop("artifact_hash")
        return stored_hash == content_hash(current)

    @classmethod
    def verify_file(cls, path: str) -> bool:
        """Check the integrity hash of a serialized artifact on disk."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        stored_hash = data.pop("artifact_hash", None)
        if stored_hash is None:
            return False
        return stored_hash == content_hash(data)
