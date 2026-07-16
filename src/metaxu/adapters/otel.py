"""OpenTelemetry exporter: assurance traces in existing observability tooling.

Turns an :class:`~metaxu.artifact.AssuranceArtifact` into an OpenTelemetry
span tree so an assurance session shows up wherever a team already sends
traces — the founding vision's "OpenTelemetry for healthcare AI assurance"
analogy made literal. This is an *exporter* (Metaxu events → OTel spans);
the reverse importer (spans → assurance events) is planned separately
(see ``docs/adr/0002-adapter-strategy.md``).

The event model was shaped for this: ``parent_id`` ≈ parent span, tags ≈
attributes. The mapping:

* one **root span** per interaction (``metaxu.interaction``), timed from the
  question to the answer, carrying the interaction id, model, trust
  dimensions, and policy/safety/terminology roll-ups;
* one **child span** per tool call (kind ``CLIENT``, ``gen_ai.tool.name``)
  and per retrieval, timed by the recorded duration and parented by
  ``parent_id`` where it maps to a span;
* **span events** for claims, policy checks, safety findings, and
  terminology validations — point-in-time annotations on the root.

Attributes use the OpenTelemetry ``gen_ai.*`` semantic conventions where
they fit (model, tool name, operation) and a ``metaxu.*`` namespace for the
assurance-specific ones.

**PHI:** question and answer *text* is omitted by default — artifacts may
carry PHI, and observability backends are a different trust boundary than
the artifact store. Pass ``capture_content=True`` only when the destination
is authorized for PHI. Without it, spans carry presence flags and lengths,
never the clinical text.

Requires the optional dependency: ``pip install metaxu[otel]``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..artifact import AssuranceArtifact
from ..events import EventType

if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.trace import Tracer


def _require_otel():
    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.trace import SpanKind, Status, StatusCode
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "OpenTelemetry export requires the 'otel' extra: pip install metaxu[otel]"
        ) from exc
    return trace_api, SpanKind, Status, StatusCode


def _epoch_ns(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def build_tracer(exporter: Any, service_name: str = "metaxu") -> "Tracer":
    """Convenience: a Tracer wired to ``exporter`` via a SimpleSpanProcessor.

    ``exporter`` is any OTel SpanExporter (Console, OTLP, InMemory for
    tests). Callers with an existing TracerProvider should pass their own
    tracer to :func:`export_artifact` instead.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("metaxu")


def _root_attributes(artifact: AssuranceArtifact, capture_content: bool) -> dict[str, Any]:
    correlation = artifact.correlation or {}
    repro = artifact.reproducibility or {}
    attrs: dict[str, Any] = {
        "metaxu.artifact_id": artifact.id,
        "metaxu.schema_version": artifact.schema_version,
        "metaxu.interaction_id": correlation.get("interaction_id", ""),
        "metaxu.observer": correlation.get("observer", ""),
        "metaxu.role": correlation.get("role", "partial"),
        "metaxu.integrity_ok": artifact.verify_integrity(),
    }
    if repro.get("model"):
        attrs["gen_ai.request.model"] = str(repro["model"])
        attrs["gen_ai.system"] = str(repro["model"])
    if repro.get("prompt_version"):
        attrs["metaxu.prompt_version"] = str(repro["prompt_version"])

    # Trust dimensions -> metaxu.trust.<dim>
    for name, dim in (artifact.trust_scores or {}).items():
        score = dim.get("score")
        if isinstance(score, (int, float)):
            attrs[f"metaxu.trust.{name}"] = float(score)

    triggered = [p for p in artifact.policy_checks if p.get("triggered")]
    attrs["metaxu.policy.triggered"] = len(triggered)
    attrs["metaxu.policy.failed"] = sum(1 for p in triggered if not p.get("passed"))
    attrs["metaxu.safety.critical"] = sum(
        1 for f in artifact.safety_checks if f.get("severity") == "critical"
    )
    attrs["metaxu.safety.warning"] = sum(
        1 for f in artifact.safety_checks if f.get("severity") == "warning"
    )
    attrs["metaxu.terminology.malformed"] = sum(
        1 for t in artifact.terminology if not t.get("valid")
    )
    attrs["metaxu.question.present"] = bool(artifact.question)
    attrs["metaxu.answer.present"] = artifact.answer is not None
    if capture_content:
        attrs["metaxu.question"] = artifact.question or ""
        if artifact.answer is not None:
            attrs["metaxu.answer"] = artifact.answer
    return attrs


def export_artifact(
    artifact: AssuranceArtifact,
    tracer: "Tracer | None" = None,
    capture_content: bool = False,
) -> None:
    """Emit ``artifact`` as an OpenTelemetry span tree through ``tracer``.

    When ``tracer`` is None a Console-exporting tracer is used, so
    ``export_artifact(a)`` prints the spans for inspection. The root span's
    status is ERROR when the interaction has a critical safety finding, a
    failed policy, or a broken integrity hash.
    """
    trace_api, SpanKind, Status, StatusCode = _require_otel()
    if tracer is None:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        tracer = build_tracer(ConsoleSpanExporter())

    events = artifact.events
    start_ns = next((_epoch_ns(e.timestamp) for e in events), None)
    end_ns = None
    for e in reversed(events):
        end_ns = _epoch_ns(e.timestamp)
        if end_ns is not None:
            break

    root = tracer.start_span(
        "metaxu.interaction",
        kind=SpanKind.INTERNAL,
        start_time=start_ns,
        attributes=_root_attributes(artifact, capture_content),
    )

    # Map provenance id -> resource reference for tool/retrieval spans.
    prov_ref = {
        p.id: f"{p.resource_type}/{p.resource_id}" for p in artifact.provenance
    }
    span_by_event: dict[str, Any] = {}

    try:
        parent_ctx = trace_api.set_span_in_context(root)

        for event in events:
            if event.type == EventType.TOOL_INVOCATION:
                payload = event.payload or {}
                ts = _epoch_ns(event.timestamp)
                duration = payload.get("duration_ms")
                child_start = ts
                if ts is not None and isinstance(duration, (int, float)):
                    child_start = ts - int(duration * 1_000_000)
                span = tracer.start_span(
                    event.name,
                    context=parent_ctx,
                    kind=SpanKind.CLIENT,
                    start_time=child_start,
                    attributes={
                        "gen_ai.tool.name": event.name,
                        "gen_ai.operation.name": "execute_tool",
                        "metaxu.tool.arguments": json.dumps(
                            payload.get("arguments", {}), default=str
                        ),
                        "metaxu.tool.duration_ms": float(duration)
                        if isinstance(duration, (int, float))
                        else 0.0,
                    },
                )
                if payload.get("error"):
                    span.set_status(Status(StatusCode.ERROR, str(payload["error"])))
                    span.set_attribute("metaxu.tool.error", str(payload["error"]))
                span_by_event[event.id] = span
                span.end(end_time=ts)

            elif event.type == EventType.RETRIEVAL:
                payload = event.payload or {}
                ts = _epoch_ns(event.timestamp)
                ctx = parent_ctx
                if event.parent_id in span_by_event:
                    ctx = trace_api.set_span_in_context(span_by_event[event.parent_id])
                span = tracer.start_span(
                    f"retrieve {event.name}",
                    context=ctx,
                    kind=SpanKind.CLIENT,
                    start_time=ts,
                    attributes={
                        "metaxu.resource": event.name,
                        "metaxu.source_system": str(payload.get("source_system", "")),
                        "metaxu.provenance_id": str(payload.get("provenance_id", "")),
                    },
                )
                span.end(end_time=ts)

            elif event.type == EventType.CLAIM:
                root.add_event(
                    "claim",
                    timestamp=_epoch_ns(event.timestamp),
                    attributes={
                        "metaxu.claim.text": (event.payload or {}).get("text", "")
                        if capture_content
                        else "",
                        "metaxu.claim.id": event.id,
                    },
                )

            elif event.type == EventType.POLICY_CHECK:
                p = event.payload or {}
                if p.get("triggered"):
                    root.add_event(
                        "policy_check",
                        timestamp=_epoch_ns(event.timestamp),
                        attributes={
                            "metaxu.policy.name": p.get("policy", ""),
                            "metaxu.policy.passed": bool(p.get("passed")),
                            "metaxu.policy.missing": json.dumps(p.get("missing", [])),
                        },
                    )

            elif event.type == EventType.SAFETY_CHECK:
                p = event.payload or {}
                root.add_event(
                    "safety_finding",
                    timestamp=_epoch_ns(event.timestamp),
                    attributes={
                        "metaxu.safety.check": p.get("check", ""),
                        "metaxu.safety.severity": p.get("severity", ""),
                        "metaxu.safety.message": p.get("message", ""),
                    },
                )

            elif event.type == EventType.CODING:
                # Terminology validations live on the artifact; annotate the
                # coding reference, marking malformed ones.
                p = event.payload or {}
                validation = next(
                    (
                        t
                        for t in artifact.terminology
                        if str(t.get("code")) == str(p.get("code"))
                    ),
                    None,
                )
                root.add_event(
                    "coding",
                    timestamp=_epoch_ns(event.timestamp),
                    attributes={
                        "metaxu.coding.system": str(p.get("system", "")),
                        "metaxu.coding.code": str(p.get("code", "")),
                        "metaxu.coding.valid": bool(validation.get("valid"))
                        if validation
                        else True,
                        "metaxu.coding.terminology_version": (validation or {}).get(
                            "terminology_version", ""
                        ),
                    },
                )

        # Root status reflects the assurance verdict.
        critical = artifact.safety_checks and any(
            f.get("severity") == "critical" for f in artifact.safety_checks
        )
        failed_policy = any(
            p.get("triggered") and not p.get("passed") for p in artifact.policy_checks
        )
        if critical or failed_policy or not artifact.verify_integrity():
            reasons = []
            if critical:
                reasons.append("critical safety finding")
            if failed_policy:
                reasons.append("policy failure")
            if not artifact.verify_integrity():
                reasons.append("integrity mismatch")
            root.set_status(Status(StatusCode.ERROR, "; ".join(reasons)))
        else:
            root.set_status(Status(StatusCode.OK))
    finally:
        root.end(end_time=end_ns)


def export_file(path: str, tracer: "Tracer | None" = None, capture_content: bool = False) -> None:
    """Load an artifact from disk and export it."""
    export_artifact(AssuranceArtifact.load(path), tracer=tracer, capture_content=capture_content)
