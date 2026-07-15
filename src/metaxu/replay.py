"""Reproducibility engine: verify that an artifact still holds.

Two levels of verification are supported today:

1. **Integrity** — the artifact's self-hash matches its content
   (tamper/truncation detection, no external data needed).
2. **Provenance re-verification** — given access to the original resources
   (a snapshot directory or any callable resolver), recompute each
   resource's content hash and compare with what the artifact recorded.
   A mismatch means the source data changed since the AI saw it — exactly
   the drift a clinician reviewing the decision needs to know about.

Full replay (re-running the model and comparing answers) requires the
model runtime and is out of scope for the SDK core; the artifact carries
the metadata (`reproducibility`) needed for an external harness to do it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .artifact import AssuranceArtifact
from .provenance import ProvenanceRecord, content_hash

# Given a provenance record, return the resource content as currently
# available from the source, or None if it cannot be resolved.
ResourceResolver = Callable[[ProvenanceRecord], Any | None]


@dataclass
class VerificationReport:
    """Outcome of verifying one artifact."""

    artifact_id: str
    integrity_ok: bool
    provenance_checked: int = 0
    provenance_matched: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def provenance_ok(self) -> bool:
        return not self.mismatches

    @property
    def ok(self) -> bool:
        return self.integrity_ok and self.provenance_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "ok": self.ok,
            "integrity_ok": self.integrity_ok,
            "provenance_checked": self.provenance_checked,
            "provenance_matched": self.provenance_matched,
            "mismatches": self.mismatches,
            "unresolved": self.unresolved,
        }


def snapshot_resolver(snapshot_dir: str) -> ResourceResolver:
    """Resolver over a directory of ``<ResourceType>-<id>.json`` snapshots."""

    def resolve(record: ProvenanceRecord) -> Any | None:
        path = os.path.join(
            snapshot_dir, f"{record.resource_type}-{record.resource_id}.json"
        )
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return resolve


def save_snapshot(snapshot_dir: str, record: ProvenanceRecord, content: Any) -> str:
    """Persist the resource content an artifact was built from."""
    os.makedirs(snapshot_dir, exist_ok=True)
    path = os.path.join(
        snapshot_dir, f"{record.resource_type}-{record.resource_id}.json"
    )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2, sort_keys=True, default=str)
    return path


def verify(
    artifact: AssuranceArtifact,
    resolver: ResourceResolver | None = None,
) -> VerificationReport:
    """Verify artifact integrity and (optionally) provenance hashes."""
    report = VerificationReport(
        artifact_id=artifact.id,
        integrity_ok=artifact.verify_integrity(),
    )
    if resolver is None:
        return report
    for record in artifact.provenance:
        current = resolver(record)
        if current is None:
            report.unresolved.append(record.id)
            continue
        report.provenance_checked += 1
        current_hash = content_hash(current)
        if current_hash == record.hash:
            report.provenance_matched += 1
        else:
            report.mismatches.append(
                {
                    "provenance_id": record.id,
                    "resource": f"{record.resource_type}/{record.resource_id}",
                    "recorded_hash": record.hash,
                    "current_hash": current_hash,
                }
            )
    return report
