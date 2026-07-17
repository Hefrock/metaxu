"""CDS Hooks adapter: assurance for decision support at the EHR boundary.

`CDS Hooks <https://cds-hooks.hl7.org/>`_ is the HL7 standard by which an
EHR calls out to a decision-support service at workflow moments
(``patient-view``, ``order-select``, ``order-sign``, …) and gets back
*cards* to show the clinician. It is the healthcare-native surface from
ADR 0002 — the point where an AI-mediated recommendation actually enters
the clinical workflow.

This adapter is for people **building** a CDS service (a transparent HTTP
proxy variant for third-party services is future work). It wraps one hook
invocation in an assurance session:

* :func:`begin_hook` — turn the hook request into a session:
  the ``prefetch`` FHIR resources become provenance records (hashed,
  versioned) with their codings extracted and validated; draft orders in
  ``context`` get their codings validated too (a hallucinated RxNorm code
  on a proposed order is exactly what terminology validation exists to
  catch); ``hookInstance`` becomes the correlation ``interaction_id`` so
  observers of the same invocation merge naturally.
* your handler does its work inside the session — recording claims,
  linking evidence, exactly like any SDK-instrumented agent;
* :func:`finish_hook` — the cards become the recorded answer, the
  artifact is finalized (policy/safety/trust/terminology all run), and
  the CDS Hooks response is annotated with a ``dev.metaxu`` extension
  carrying the artifact id and assurance summary. Optionally a visible
  assurance card is appended when something needs clinician attention.

Or use the :func:`assured_cds_service` decorator to do all three around a
``handler(request, session) -> cards`` function.

**Security:** the request's ``fhirAuthorization`` (an OAuth bearer token)
is *never* recorded into the artifact — sessions and artifacts outlive
tokens and travel to different trust boundaries. Only the FHIR server URL
is kept.

Everything here is stdlib-only and framework-agnostic: it manipulates
dicts, so it drops into Flask, FastAPI, Azure Functions, or anything else
that gives you the request body as JSON.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ..artifact import AssuranceArtifact
from ..provenance import ProvenanceRecord
from ..session import AssuranceSession

# CDS Hooks card indicators, most severe last.
_INDICATORS = ("info", "warning", "critical")


def _clean_context(context: dict[str, Any]) -> dict[str, Any]:
    """Context minus bulky resource payloads (kept elsewhere) — for metadata."""
    return {
        k: v
        for k, v in context.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    }


def _iter_resources(value: Any):
    """Yield FHIR resources from a prefetch/context entry.

    Entries may be a single resource or a searchset Bundle; Bundles are
    unwrapped. Anything without a resourceType is ignored.
    """
    if not isinstance(value, dict):
        return
    if value.get("resourceType") == "Bundle":
        for entry in value.get("entry", []) or []:
            resource = (entry or {}).get("resource")
            if isinstance(resource, dict) and resource.get("resourceType"):
                yield resource
    elif value.get("resourceType"):
        yield value


def begin_hook(
    request: dict[str, Any],
    policy_engine: Any | None = None,
    safety_engine: Any | None = None,
    trust_engine: Any | None = None,
    terminology_resolver: Any | None = None,
    tag_map: dict[str, list[str]] | None = None,
) -> AssuranceSession:
    """Open an assurance session for one CDS Hooks invocation.

    ``tag_map`` maps prefetch keys to policy tags (e.g.
    ``{"platelets": ["platelet_count"]}``) so institutional policies match
    the service's prefetch vocabulary. Every prefetch retrieval is also
    tagged with its own prefetch key and ``patient_record_access``.
    """
    hook = request.get("hook", "unknown-hook")
    hook_instance = request.get("hookInstance")
    context = request.get("context", {}) or {}
    fhir_server = request.get("fhirServer") or "cds-prefetch"
    patient = context.get("patientId", "unknown")
    tag_map = tag_map or {}

    session = AssuranceSession(
        question=f"CDS hook '{hook}' for Patient/{patient}: "
        "should the proposed action proceed?",
        policy_engine=policy_engine,
        safety_engine=safety_engine,
        trust_engine=trust_engine,
        terminology_resolver=terminology_resolver,
        interaction_id=hook_instance,  # env/generated fallback handled by session
        observer="cds-hooks",
        metadata={
            "dev.metaxu/hook": hook,
            "dev.metaxu/hookInstance": hook_instance,
            "dev.metaxu/fhirServer": request.get("fhirServer"),
            "dev.metaxu/context": _clean_context(context),
            # Note: request['fhirAuthorization'] is deliberately never
            # recorded — bearer tokens must not outlive their scope.
        },
    )

    # Prefetch: what the EHR handed the service. Each resource becomes a
    # provenance record (hash, version) and its codings are validated.
    for key, value in (request.get("prefetch") or {}).items():
        tags = [key, "patient_record_access", *tag_map.get(key, [])]
        for resource in _iter_resources(value):
            record = ProvenanceRecord.for_resource(
                source_system=fhir_server,
                resource_type=resource.get("resourceType", "Resource"),
                resource_id=str(resource.get("id", "unknown")),
                resource_version=(resource.get("meta") or {}).get("versionId"),
                content=resource,
            )
            session.record_retrieval(record, tags=tags)
            session.record_codings_from(resource, tags=tags, provenance=record)

    # Draft orders / selections: the *proposed* action. Not retrieved from
    # a source of record, so no provenance — but their codes are exactly
    # where a hallucinated RxNorm/SNOMED code would do harm.
    for context_key in ("draftOrders", "selections", "medications"):
        for resource in _iter_resources(context.get(context_key)):
            session.record_note(
                f"draft {resource.get('resourceType', 'Resource')}"
                f"/{resource.get('id', '?')} proposed via context.{context_key}",
                tags=["draft_order", context_key],
            )
            session.record_codings_from(resource, tags=["draft_order", context_key])

    return session


def finish_hook(
    session: AssuranceSession,
    cards: list[dict[str, Any]],
    add_assurance_card: bool = False,
) -> tuple[AssuranceArtifact, dict[str, Any]]:
    """Record ``cards`` as the answer, finalize, and build the response.

    Returns ``(artifact, response)`` where ``response`` is the CDS Hooks
    response body: the cards plus a ``dev.metaxu`` entry in ``extension``
    carrying the artifact id, interaction id, and assurance summary — so
    the EHR side can associate the card it shows with the artifact that
    justifies it. With ``add_assurance_card=True``, a visible card is
    appended when the artifact has critical findings or failed policies.
    """
    session.metadata["dev.metaxu/cards"] = cards
    answer = (
        "; ".join(
            f"[{c.get('indicator', 'info')}] {c.get('summary', '')}" for c in cards
        )
        or "(no cards returned)"
    )
    session.set_answer(answer)
    artifact = session.finalize()

    critical = sum(
        1 for f in artifact.safety_checks if f.get("severity") == "critical"
    )
    failed_policies = [
        p["policy"]
        for p in artifact.policy_checks
        if p.get("triggered") and not p.get("passed")
    ]
    summary = {
        "artifact_id": artifact.id,
        "interaction_id": artifact.correlation.get("interaction_id"),
        "schema_version": artifact.schema_version,
        "critical_findings": critical,
        "failed_policies": failed_policies,
        "malformed_codes": sum(1 for t in artifact.terminology if not t.get("valid")),
    }

    out_cards = list(cards)
    if add_assurance_card and (critical or failed_policies):
        problems = []
        if critical:
            problems.append(f"{critical} critical safety finding(s)")
        if failed_policies:
            problems.append("unmet policy: " + ", ".join(failed_policies))
        out_cards.append(
            {
                "summary": "Assurance checks did not pass for this recommendation",
                "detail": (
                    "Metaxu assurance evaluation flagged: "
                    + "; ".join(problems)
                    + f". Assurance artifact: {artifact.id}."
                ),
                "indicator": "warning",
                "source": {"label": "Metaxu assurance layer"},
            }
        )

    response = {"cards": out_cards, "extension": {"dev.metaxu": summary}}
    return artifact, response


def assured_cds_service(
    policy_engine: Any | None = None,
    tag_map: dict[str, list[str]] | None = None,
    artifact_dir: str | None = None,
    add_assurance_card: bool = False,
    terminology_resolver: Any | None = None,
) -> Callable:
    """Decorator: wrap a ``handler(request, session) -> cards`` function
    into a CDS service callable ``service(request) -> response``.

    Each invocation runs begin_hook -> handler -> finish_hook; when
    ``artifact_dir`` is set, every artifact is saved there (named by its
    id), giving the service an audit trail with zero extra code.
    """

    def decorate(handler: Callable[[dict[str, Any], AssuranceSession], list[dict[str, Any]]]):
        def service(request: dict[str, Any]) -> dict[str, Any]:
            session = begin_hook(
                request,
                policy_engine=policy_engine,
                tag_map=tag_map,
                terminology_resolver=terminology_resolver,
            )
            with session:
                cards = handler(request, session)
                artifact, response = finish_hook(
                    session, cards, add_assurance_card=add_assurance_card
                )
            if artifact_dir:
                os.makedirs(artifact_dir, exist_ok=True)
                artifact.save(os.path.join(artifact_dir, f"{artifact.id}.json"))
            return response

        service.__name__ = getattr(handler, "__name__", "cds_service")
        service.__doc__ = handler.__doc__
        return service

    return decorate
