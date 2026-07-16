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
import json
import os
import platform
import sys
import uuid
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
        interaction_id: str | None = None,
        observer: str = "sdk",
        terminology_resolver: Any | None = None,
    ):
        """``interaction_id`` correlates this session with other observers
        of the same interaction (an MCP proxy, an LLM gateway, …) so their
        partial artifacts can later be combined by ``metaxu merge``. When
        not passed explicitly it is taken from the ``METAXU_INTERACTION_ID``
        environment variable — letting observers in different processes
        share one id with no code changes — and generated otherwise.
        """
        self.question = question
        self.correlation: dict[str, Any] = {
            "interaction_id": interaction_id
            or os.environ.get("METAXU_INTERACTION_ID")
            or f"ixn-{uuid.uuid4()}",
            "observer": observer,
            "role": "partial",
        }
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
        # Terminology validation always runs (format-checking is free and
        # data-free); a caller may supply a data-backed resolver instead.
        from .terminology import TerminologyValidator

        self.terminology_validator = TerminologyValidator(terminology_resolver)
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
        sources: list[ProvenanceRecord | Event],
        relation: str = "supports",
    ) -> Event:
        """Link a claim to what supports it.

        ``sources`` may mix provenance records (retrieved resources) and
        other claim events — intermediate reasoning steps — so multi-hop
        chains (claim resting on claim resting on data) become edges in
        the evidence graph rather than flat co-citations.
        """
        provenance_ids = [s.id for s in sources if isinstance(s, ProvenanceRecord)]
        claim_ids = [s.id for s in sources if isinstance(s, Event)]
        return self._record(
            Event(
                type=EventType.EVIDENCE_LINK,
                name=relation,
                parent_id=claim.id,
                payload={
                    "claim_id": claim.id,
                    "provenance_ids": provenance_ids,
                    "claim_ids": claim_ids,
                    "relation": relation,
                },
            )
        )

    def record_coding(
        self,
        system: str,
        code: str,
        display: str | None = None,
        tags: list[str] | None = None,
        provenance: ProvenanceRecord | None = None,
    ) -> Event:
        """Record a clinical terminology reference (SNOMED/LOINC/RxNorm/…).

        Recorded codings are validated at finalize; malformed codes become
        critical safety findings and lower the terminology_correctness trust
        dimension. See ``docs/adr/0001-terminology-validation.md``.

        ``provenance`` links the coding to the resource that carried it, so
        the evidence graph gets a resource → coding edge.
        """
        payload: dict[str, Any] = {"system": system, "code": code, "display": display}
        if provenance is not None:
            payload["provenance_id"] = provenance.id
        return self._record(
            Event(
                type=EventType.CODING,
                name=f"{system}|{code}",
                tags=tags or [],
                payload=payload,
            )
        )

    def record_codings_from(
        self,
        content: Any,
        tags: list[str] | None = None,
        provenance: ProvenanceRecord | None = None,
    ) -> list[Event]:
        """Extract codings from a FHIR-shaped object and record each one,
        optionally linked to the provenance record the object came from."""
        from .terminology import extract_codings

        return [
            self.record_coding(c.system, c.code, c.display, tags=tags, provenance=provenance)
            for c in extract_codings(content)
        ]

    def record_missing_data(self, item: str, reason: str | None = None) -> None:
        """Record that required information could not be obtained."""
        entry = {"item": item, "reason": reason}
        self.missing_data.append(entry)
        self._record(Event(type=EventType.MISSING_DATA, name=item, payload=entry))

    def record_note(
        self,
        text: str,
        tags: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """Free-form annotation. ``data`` holds structured values that
        policy ``where`` clauses can address (path prefix ``data.``)."""
        payload: dict[str, Any] = {"text": text}
        if data is not None:
            payload["data"] = data
        return self._record(
            Event(type=EventType.NOTE, name="note", tags=tags or [], payload=payload)
        )

    def set_model(self, model: str, prompt_version: str | None = None) -> None:
        """Record the model (and optionally prompt) version for replay."""
        self.reproducibility["model"] = model
        if prompt_version is not None:
            self.reproducibility["prompt_version"] = prompt_version

    def set_answer(self, answer: str, based_on: list[Event] | None = None) -> None:
        """Record the final answer.

        ``based_on`` names the claims the answer actually rests on, giving
        the evidence graph explicit answer → claim edges. When omitted,
        the graph falls back to connecting the answer to every claim,
        marking those edges implicit — recorded reasoning always beats
        inferred reasoning.
        """
        self.answer = answer
        payload: dict[str, Any] = {"text": answer}
        if based_on:
            payload["based_on_claim_ids"] = [c.id for c in based_on]
        self._record(Event(type=EventType.ANSWER, name="answer", payload=payload))

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

        # Terminology validation runs before safety so malformed codes can
        # surface as safety findings, and before trust for its dimension.
        from .terminology import Coding

        codings = [
            Coding(
                system=e.payload["system"],
                code=e.payload["code"],
                display=e.payload.get("display"),
            )
            for e in self.events
            if e.type == EventType.CODING
        ]
        terminology_results = [
            v.to_dict() for v in self.terminology_validator.validate(codings)
        ]

        safety_ctx = SafetyContext(
            answer=self.answer,
            events=self.events,
            provenance=self.provenance,
            terminology=terminology_results,
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
            terminology=terminology_results,
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
            terminology=terminology_results,
            missing_data=list(self.missing_data),
            trust_scores=trust_scores,
            reproducibility=dict(self.reproducibility),
            metadata=dict(self.metadata),
            correlation=dict(self.correlation),
            events=list(self.events),
        )
        return self.artifact


def _summarize(result: Any, limit: int = 500) -> Any:
    """Keep tool results in the trace small; provenance holds full hashes.

    Small JSON-native results are kept structured so policy ``where``
    clauses can address their values by path
    (e.g. ``result_summary.valueQuantity.value``).
    """
    if result is None:
        return None
    if isinstance(result, (int, float, bool)):
        return result
    if isinstance(result, (dict, list)):
        try:
            if len(json.dumps(result, default=str)) <= limit:
                return result
        except (TypeError, ValueError):
            pass
    text = str(result)
    return text if len(text) <= limit else text[:limit] + "…"
