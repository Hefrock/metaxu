"""Reproducibility engine: verify, replay, and diff.

Three levels of verification are supported:

1. **Integrity** — the artifact's self-hash matches its content
   (tamper/truncation detection, no external data needed).
2. **Provenance re-verification** — given access to the original resources
   (a snapshot directory or any callable resolver), recompute each
   resource's content hash and compare with what the artifact recorded.
   A mismatch means the source data changed since the AI saw it — exactly
   the drift a clinician reviewing the decision needs to know about.
3. **Replay** — re-run the workflow (a caller-supplied runner, typically
   wired to the recorded snapshots instead of live sources) in a fresh
   session with the original question, then :func:`diff_artifacts` the
   replay against the original: same tool calls, same claims, same
   answer, same policy outcomes, same data hashes? The diff is usable on
   its own (``metaxu diff``) for any two artifacts claiming to describe
   the same interaction.

Re-running the *model* itself remains the caller's responsibility — the
runner is any callable, so it may invoke a live model, a pinned model
version, or a fully scripted workflow; the artifact's ``reproducibility``
block carries the versions needed to reconstruct that environment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .artifact import AssuranceArtifact
from .events import EventType
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


# -- replay and diff ----------------------------------------------------------

# A runner re-executes the workflow for a question inside a session. It is
# the same shape as the example agents: read data (ideally from recorded
# snapshots), record retrievals/claims/evidence, set an answer.
Runner = Callable[[str, "Any"], None]


def _normalize(text: str | None) -> str | None:
    return " ".join(text.split()).casefold() if text is not None else None


def diff_artifacts(
    original: AssuranceArtifact, replay: AssuranceArtifact
) -> dict[str, Any]:
    """Compare two artifacts that claim to describe the same interaction.

    ``reproduced`` is True only when the answer, the tool-call sequence
    (names and arguments), the claim set, every policy outcome, and the
    evidence base all match — a replay that saw different data (hash
    mismatch) or a different set of resources is not a reproduction even
    if the answer text came out the same. Hash mismatches on shared
    resources are usually the *explanation* for any behavioral
    difference above.
    """
    differences: list[str] = []

    answer_match = _normalize(original.answer) == _normalize(replay.answer)
    if not answer_match:
        differences.append("answer differs")

    def call_sig(call: dict[str, Any]) -> tuple[str, str]:
        arguments = call.get("payload", {}).get("arguments", {})
        return call["name"], json.dumps(arguments, sort_keys=True, default=str)

    original_calls = [call_sig(c) for c in original.tool_trace]
    replay_calls = [call_sig(c) for c in replay.tool_trace]
    tools_match = original_calls == replay_calls
    if not tools_match:
        original_names = [name for name, _ in original_calls]
        replay_names = [name for name, _ in replay_calls]
        if original_names != replay_names:
            differences.append(
                f"tool sequence differs: {original_names} vs {replay_names}"
            )
        else:
            differences.append("tool arguments differ")

    def claim_set(artifact: AssuranceArtifact) -> set[str]:
        return {
            _normalize(e.payload.get("text", ""))
            for e in artifact.events
            if e.type == EventType.CLAIM
        }

    claims_match = claim_set(original) == claim_set(replay)
    if not claims_match:
        differences.append("claim set differs")

    def policy_outcomes(artifact: AssuranceArtifact) -> dict[str, bool]:
        return {
            p["policy"]: bool(p.get("passed"))
            for p in artifact.policy_checks
            if p.get("triggered")
        }

    original_policies = policy_outcomes(original)
    replay_policies = policy_outcomes(replay)
    policies_match = original_policies == replay_policies
    if not policies_match:
        for name in sorted(set(original_policies) | set(replay_policies)):
            before = original_policies.get(name)
            after = replay_policies.get(name)
            if before != after:
                differences.append(
                    f"policy '{name}' outcome differs: {before} vs {after}"
                )

    def hashes(artifact: AssuranceArtifact) -> dict[tuple[str, str, str], str]:
        return {
            (r.source_system, r.resource_type, r.resource_id): r.hash
            for r in artifact.provenance
            if r.hash
        }

    original_hashes, replay_hashes = hashes(original), hashes(replay)
    shared = set(original_hashes) & set(replay_hashes)
    hash_mismatches = [
        {
            "resource": f"{key[1]}/{key[2]}",
            "source_system": key[0],
            "original_hash": original_hashes[key],
            "replay_hash": replay_hashes[key],
        }
        for key in sorted(shared)
        if original_hashes[key] != replay_hashes[key]
    ]
    only_original = sorted(f"{k[1]}/{k[2]}" for k in set(original_hashes) - shared)
    only_replay = sorted(f"{k[1]}/{k[2]}" for k in set(replay_hashes) - shared)
    for entry in hash_mismatches:
        differences.append(
            f"resource {entry['resource']} content differs between runs"
        )
    if only_original:
        differences.append(f"resources only in original: {only_original}")
    if only_replay:
        differences.append(f"resources only in replay: {only_replay}")

    # A replay that saw different data — or didn't retrieve data the
    # original did — is not a faithful reproduction, even if the words of
    # the answer came out the same.
    provenance_match = not hash_mismatches and not only_original and not only_replay
    reproduced = (
        answer_match
        and tools_match
        and claims_match
        and policies_match
        and provenance_match
    )
    return {
        "original_artifact": original.id,
        "replay_artifact": replay.id,
        "reproduced": reproduced,
        "answer": {
            "match": answer_match,
            "original": original.answer,
            "replay": replay.answer,
        },
        "tool_calls": {
            "match": tools_match,
            "original_count": len(original_calls),
            "replay_count": len(replay_calls),
        },
        "claims": {"match": claims_match},
        "policies": {
            "match": policies_match,
            "original": original_policies,
            "replay": replay_policies,
        },
        "provenance": {
            "match": provenance_match,
            "shared_resources": len(shared),
            "hash_mismatches": hash_mismatches,
            "only_in_original": only_original,
            "only_in_replay": only_replay,
        },
        "differences": differences,
    }


def replay_with_runner(
    artifact: AssuranceArtifact,
    runner: Runner,
    policy_engine: Any | None = None,
    safety_engine: Any | None = None,
    trust_engine: Any | None = None,
) -> tuple[AssuranceArtifact, dict[str, Any]]:
    """Re-run the workflow for ``artifact``'s question and diff the result.

    The runner receives ``(question, session)`` and should execute the
    workflow inside the session — for a faithful replay, wire its data
    access to the recorded snapshots (:func:`snapshot_resolver`) rather
    than live sources, so behavioral differences can't be explained away
    by data changes. Returns ``(replay_artifact, diff)``.
    """
    from .session import AssuranceSession

    session = AssuranceSession(
        question=artifact.question,
        policy_engine=policy_engine,
        safety_engine=safety_engine,
        trust_engine=trust_engine,
        interaction_id=artifact.correlation.get("interaction_id"),
        observer="replay",
        metadata={"dev.metaxu/replay_of": artifact.id},
    )
    with session:
        runner(artifact.question, session)
    replay_artifact = session.artifact
    assert replay_artifact is not None
    return replay_artifact, diff_artifacts(artifact, replay_artifact)
