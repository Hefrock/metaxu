"""Merge partial assurance artifacts from multiple observers.

No single interception point sees a whole interaction: an MCP proxy sees
tool calls but not claims or answers; an SDK-instrumented agent sees
claims but maybe not every tool hop; a gateway sees the answer. Observers
that share an ``interaction_id`` (see ``AssuranceSession`` and the
``METAXU_INTERACTION_ID`` environment variable) each produce a *partial*
artifact; this module combines them into one *merged* artifact.

A merge is a **re-evaluation, not a concatenation**: event streams and
provenance are unioned, then the policy, safety, and trust engines run
again over the combined observations. A policy that failed on every
partial view (the proxy never saw the platelet check; the SDK session
never saw the allergy tool) can rightly pass on the merged view — that is
the point of composing observers.

Scalar conflicts (two observers recording different answers) are never
silently resolved: the first non-null value in the order given wins, and
every losing value is preserved under ``metadata["dev.metaxu/merge_conflicts"]``.
Pass the most authoritative observer first.
"""

from __future__ import annotations

from typing import Any

from .artifact import AssuranceArtifact
from .events import Event, EventType
from .policy import PolicyEngine
from .safety import SafetyContext, SafetyEngine
from .trust import TrustEngine

# Engine-produced events are excluded when re-evaluating: a policy_check
# event named "before_anticoagulation" must not satisfy a requirement of
# the same name, and stale findings from a partial view are history, not
# input.
_DERIVED_TYPES = {EventType.POLICY_CHECK, EventType.SAFETY_CHECK}


def merge_artifacts(
    artifacts: list[AssuranceArtifact],
    policy_engine: PolicyEngine | None = None,
    safety_engine: SafetyEngine | None = None,
    trust_engine: TrustEngine | None = None,
) -> AssuranceArtifact:
    """Combine partial artifacts sharing one interaction_id and re-evaluate.

    Raises ``ValueError`` if fewer than two artifacts are given, any lacks
    a correlation interaction_id, the ids disagree, or major schema
    versions differ.
    """
    if len(artifacts) < 2:
        raise ValueError("merge requires at least two artifacts")

    interaction_ids = {a.correlation.get("interaction_id") for a in artifacts}
    if None in interaction_ids:
        raise ValueError(
            "every artifact must carry correlation.interaction_id to be merged"
        )
    if len(interaction_ids) != 1:
        raise ValueError(
            f"artifacts describe different interactions: {sorted(interaction_ids)}"
        )
    majors = {a.schema_version.split(".")[0] for a in artifacts}
    if len(majors) != 1:
        raise ValueError(f"artifacts have different major schema versions: {sorted(majors)}")

    conflicts: list[dict[str, Any]] = []

    # -- union event streams (dedupe by id, stable-sort by timestamp) -----
    seen_events: set[str] = set()
    combined_events: list[Event] = []
    for artifact in artifacts:
        for event in artifact.events:
            if event.id not in seen_events:
                seen_events.add(event.id)
                combined_events.append(event)
    combined_events.sort(key=lambda e: e.timestamp)
    observational = [e for e in combined_events if e.type not in _DERIVED_TYPES]

    # -- union provenance and missing_data (dedupe) ------------------------
    seen_prov: set[str] = set()
    provenance = []
    for artifact in artifacts:
        for record in artifact.provenance:
            if record.id not in seen_prov:
                seen_prov.add(record.id)
                provenance.append(record)

    missing_data: list[dict[str, Any]] = []
    for artifact in artifacts:
        for item in artifact.missing_data:
            if item not in missing_data:
                missing_data.append(item)

    # -- scalars: first non-null wins; losers preserved as conflicts ------
    question = _first_scalar(artifacts, "question", conflicts)
    answer = _first_scalar(artifacts, "answer", conflicts)

    # -- dicts: first-wins shallow merge; differing values are conflicts --
    reproducibility = _merge_dicts(artifacts, "reproducibility", conflicts)
    metadata = _merge_dicts(artifacts, "metadata", conflicts)
    if conflicts:
        metadata["dev.metaxu/merge_conflicts"] = conflicts

    # -- re-evaluate engines over the combined observations ----------------
    policy_engine = policy_engine or PolicyEngine()
    safety_engine = safety_engine or SafetyEngine()
    trust_engine = trust_engine or TrustEngine()

    # Re-validate terminology over the combined codings (format-check is
    # data-free; a merge cannot know which resolver a partial used).
    from .terminology import Coding, TerminologyValidator

    codings = [
        Coding(
            system=e.payload["system"],
            code=e.payload["code"],
            display=e.payload.get("display"),
        )
        for e in observational
        if e.type == EventType.CODING
    ]
    terminology_results = [
        v.to_dict() for v in TerminologyValidator().validate(codings)
    ]

    policy_results = [r.to_dict() for r in policy_engine.evaluate(answer, observational)]
    safety_findings = safety_engine.evaluate(
        SafetyContext(
            answer=answer,
            events=observational,
            provenance=provenance,
            terminology=terminology_results,
        )
    )
    safety_dicts = [f.to_dict() for f in safety_findings]
    trust_scores = trust_engine.evaluate(
        events=observational,
        provenance=provenance,
        policy_checks=policy_results,
        safety_findings=safety_findings,
        missing_data=missing_data,
        terminology=terminology_results,
    )

    # Append fresh check events so the "projections derive from events"
    # invariant holds on the merged artifact; each partial's own check
    # events remain earlier in the stream as history.
    for result in policy_results:
        combined_events.append(
            Event(type=EventType.POLICY_CHECK, name=result["policy"], payload=result)
        )
    for finding in safety_dicts:
        combined_events.append(
            Event(type=EventType.SAFETY_CHECK, name=finding["check"], payload=finding)
        )

    return AssuranceArtifact(
        question=question or "",
        answer=answer,
        evidence=[e.to_dict() for e in combined_events if e.type == EventType.EVIDENCE_LINK],
        tool_trace=[
            e.to_dict() for e in combined_events if e.type == EventType.TOOL_INVOCATION
        ],
        provenance=provenance,
        policy_checks=policy_results,
        safety_checks=safety_dicts,
        terminology=terminology_results,
        missing_data=missing_data,
        trust_scores=trust_scores,
        reproducibility=reproducibility,
        metadata=metadata,
        correlation={
            "interaction_id": interaction_ids.pop(),
            "observer": "metaxu.merge",
            "role": "merged",
            "merged_from": [a.id for a in artifacts],
        },
        events=combined_events,
    )


def _first_scalar(
    artifacts: list[AssuranceArtifact], field_name: str, conflicts: list[dict[str, Any]]
) -> Any:
    chosen = None
    chosen_observer = None
    for artifact in artifacts:
        value = getattr(artifact, field_name)
        if value is None:
            continue
        # Proxy sessions synthesize a placeholder question; never let it
        # win over, or conflict with, a real one.
        if field_name == "question" and value.startswith("MCP session:"):
            continue
        if chosen is None:
            chosen = value
            chosen_observer = artifact.correlation.get("observer")
        elif value != chosen:
            conflicts.append(
                {
                    "field": field_name,
                    "kept": chosen,
                    "kept_from": chosen_observer,
                    "discarded": value,
                    "discarded_from": artifact.correlation.get("observer"),
                }
            )
    return chosen


def _merge_dicts(
    artifacts: list[AssuranceArtifact], field_name: str, conflicts: list[dict[str, Any]]
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    origin: dict[str, str | None] = {}
    for artifact in artifacts:
        observer = artifact.correlation.get("observer")
        for key, value in getattr(artifact, field_name).items():
            if key not in merged:
                merged[key] = value
                origin[key] = observer
            elif merged[key] != value:
                conflicts.append(
                    {
                        "field": f"{field_name}.{key}",
                        "kept": merged[key],
                        "kept_from": origin[key],
                        "discarded": value,
                        "discarded_from": observer,
                    }
                )
    return merged
