"""AssuranceSession: the core recorder.

A session wraps one AI-mediated clinical interaction. Instrumented code
records events into it; on close, the policy, safety, and trust engines run
and the finished :class:`~metaxu.artifact.AssuranceArtifact` is assembled.

Usage::

    with AssuranceSession(question="Can we start warfarin?") as session:
        ...  # instrumented tools record themselves automatically
        claim = session.record_claim("Platelets are within normal range.")
        session.link_evidence(claim, [prov_record])
        session.set_answer("Warfarin appears appropriate; verify with pharmacy.")

    artifact = session.artifact
"""

from __future__ import annotations

import contextvars
import platform
import sys
from typing import Any

from .artifact import AssuranceArtifact
from .events import Event, EventType
from .policy import PolicyEngine
from .provenance import ProvenanceRecord
from .safety import SafetyContext, SafetyEngine
from .trust import TrustEngine

_current_session: contextvars.ContextVar["AssuranceSession | None"] = contextvars.ContextVar(
    "metaxu_current_session", default=None
)


def current_session() -> "AssuranceSession | None":
    """The session active in this context, if any (see @assured_tool)."""
    return _current_session.get()


class AssuranceSession:
    """Records one interaction and produces its assurance artifact."""

    def __init__(
        self,
        question: str,
        policy_engine: PolicyEngine | None = None,
        safety_engine: SafetyEngine | None = None,
        trust_engine: TrustEngine | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.question = question
        self.answer: str | None = None
        self.events: list[Event] = []
        self.provenance: list[ProvenanceRecord] = []
        self.missing_data: list[dict[str, Any]] = []
        self.reproducibility: dict[str, Any] = {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "tool_versions": {},
        }
        self.metadata = metadata or {}
        self.policy_engine = policy_engine or PolicyEngine()
        self.safety_engine = safety_engine or SafetyEngine()
        self.trust_engine = trust_engine or TrustEngine()
        self.artifact: AssuranceArtifact | None = None
        self._token: contextvars.Token | None = None
        self._record(Event(type=EventType.QUESTION, name="question", payload={"text": question}))

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "AssuranceSession":
        self._token = _current_session.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _current_session.reset(self._token)
            self._token = None
        self.finalize()

    # -- recording API ----------------------------------------------------

    def _record(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def record_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any = None,
        tags: list[str] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
        version: str | None = None,
    ) -> Event:
        """Record one tool/function/MCP invocation."""
        if version is not None:
            self.reproducibility["tool_versions"][name] = version
        return self._record(
            Event(
                type=EventType.TOOL_INVOCATION,
                name=name,
                tags=tags or [],
                payload={
                    "arguments": arguments,
                    "result_summary": _summarize(result),
                    "error": error,
                    "duration_ms": duration_ms,
                },
            )
        )

    def record_retrieval(
        self,
        provenance: ProvenanceRecord,
        tags: list[str] | None = None,
        parent_id: str | None = None,
    ) -> ProvenanceRecord:
        """Record that a resource was retrieved, with full provenance."""
        self.provenance.append(provenance)
        self._record(
            Event(
                type=EventType.RETRIEVAL,
                name=f"{provenance.resource_type}/{provenance.resource_id}",
                tags=tags or [],
                parent_id=parent_id,
                payload={"provenance_id": provenance.id, "source_system": provenance.source_system},
            )
        )
        return provenance

    def record_claim(self, text: str, tags: list[str] | None = None) -> Event:
        """Record an intermediate factual claim made by the AI."""
        return self._record(
            Event(type=EventType.CLAIM, name="claim", tags=tags or [], payload={"text": text})
        )

    def link_evidence(
        self,
        claim: Event,
        sources: list[ProvenanceRecord],
        relation: str = "supports",
    ) -> Event:
        """Link a claim to the provenance records that support it."""
        return self._record(
            Event(
                type=EventType.EVIDENCE_LINK,
                name=relation,
                parent_id=claim.id,
                payload={
                    "claim_id": claim.id,
                    "provenance_ids": [s.id for s in sources],
                    "relation": relation,
                },
            )
        )

    def record_missing_data(self, item: str, reason: str | None = None) -> None:
        """Record that required information could not be obtained."""
        entry = {"item": item, "reason": reason}
        self.missing_data.append(entry)
        self._record(Event(type=EventType.MISSING_DATA, name=item, payload=entry))

    def record_note(self, text: str, tags: list[str] | None = None) -> Event:
        return self._record(
            Event(type=EventType.NOTE, name="note", tags=tags or [], payload={"text": text})
        )

    def set_model(self, model: str, prompt_version: str | None = None) -> None:
        """Record the model (and optionally prompt) version for replay."""
        self.reproducibility["model"] = model
        if prompt_version is not None:
            self.reproducibility["prompt_version"] = prompt_version

    def set_answer(self, answer: str) -> None:
        self.answer = answer
        self._record(Event(type=EventType.ANSWER, name="answer", payload={"text": answer}))

    # -- finalization -----------------------------------------------------

    def finalize(self) -> AssuranceArtifact:
        """Run the engines and assemble the artifact. Idempotent."""
        if self.artifact is not None:
            return self.artifact

        policy_results = [r.to_dict() for r in self.policy_engine.evaluate(self.answer, self.events)]
        for result in policy_results:
            self._record(
                Event(
                    type=EventType.POLICY_CHECK,
                    name=result["policy"],
                    payload=result,
                )
            )

        safety_ctx = SafetyContext(
            answer=self.answer, events=self.events, provenance=self.provenance
        )
        safety_findings = self.safety_engine.evaluate(safety_ctx)
        safety_dicts = [f.to_dict() for f in safety_findings]
        for finding in safety_dicts:
            self._record(
                Event(type=EventType.SAFETY_CHECK, name=finding["check"], payload=finding)
            )

        trust_scores = self.trust_engine.evaluate(
            events=self.events,
            provenance=self.provenance,
            policy_checks=policy_results,
            safety_findings=safety_findings,
            missing_data=self.missing_data,
        )

        self.artifact = AssuranceArtifact(
            question=self.question,
            answer=self.answer,
            evidence=[
                e.to_dict() for e in self.events if e.type == EventType.EVIDENCE_LINK
            ],
            tool_trace=[
                e.to_dict() for e in self.events if e.type == EventType.TOOL_INVOCATION
            ],
            provenance=list(self.provenance),
            policy_checks=policy_results,
            safety_checks=safety_dicts,
            missing_data=list(self.missing_data),
            trust_scores=trust_scores,
            reproducibility=dict(self.reproducibility),
            metadata=dict(self.metadata),
            events=list(self.events),
        )
        return self.artifact


def _summarize(result: Any, limit: int = 500) -> Any:
    """Keep tool results in the trace small; provenance holds full hashes."""
    if result is None:
        return None
    if isinstance(result, (int, float, bool)):
        return result
    text = str(result)
    return text if len(text) <= limit else text[:limit] + "…"
