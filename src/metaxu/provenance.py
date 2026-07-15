"""Provenance engine: trace every statement back to its origin.

A :class:`ProvenanceRecord` captures where a piece of clinical data came
from, when it was retrieved, which version was seen, and a content hash so
the exact bytes can later be re-verified (see :mod:`metaxu.replay`).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import utcnow


def content_hash(content: Any) -> str:
    """Deterministic sha256 over the canonical JSON form of ``content``."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ProvenanceRecord:
    """The origin of a single retrieved resource.

    Attributes:
        source_system: The system of record (e.g. an EHR base URL, an MCP
            server name, a guideline repository).
        resource_type: Type within that system (e.g. FHIR ``Observation``).
        resource_id: Identifier within that system.
        resource_version: Version identifier, if the source supports
            versioning (FHIR ``meta.versionId``, git SHA, etc.).
        retrieved_at: When the resource was fetched.
        hash: Content hash of the resource as retrieved.
        cache_state: ``fresh`` | ``cached`` | ``unknown``.
    """

    source_system: str
    resource_type: str
    resource_id: str
    resource_version: str | None = None
    retrieved_at: str = field(default_factory=utcnow)
    hash: str | None = None
    cache_state: str = "unknown"
    id: str = field(default_factory=lambda: f"prov-{uuid.uuid4()}")

    @classmethod
    def for_resource(
        cls,
        source_system: str,
        resource_type: str,
        resource_id: str,
        content: Any,
        resource_version: str | None = None,
        cache_state: str = "fresh",
    ) -> "ProvenanceRecord":
        """Build a record for ``content`` retrieved from a source system."""
        return cls(
            source_system=source_system,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_version=resource_version,
            hash=content_hash(content),
            cache_state=cache_state,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_system": self.source_system,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_version": self.resource_version,
            "retrieved_at": self.retrieved_at,
            "hash": self.hash,
            "cache_state": self.cache_state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvenanceRecord":
        return cls(
            source_system=data["source_system"],
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            resource_version=data.get("resource_version"),
            retrieved_at=data["retrieved_at"],
            hash=data.get("hash"),
            cache_state=data.get("cache_state", "unknown"),
            id=data["id"],
        )
