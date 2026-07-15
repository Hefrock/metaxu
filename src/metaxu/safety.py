"""Safety engine: structural checks over the assurance session.

These checks do not judge clinical correctness — they judge whether the
*shape* of the interaction is defensible: every claim is backed by
evidence, every cited resource was actually retrieved, allergies that were
fetched were not silently ignored, and so on.

Checks are pluggable: a check is any callable taking a
:class:`SafetyContext` and returning a list of :class:`SafetyFinding`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Event, EventType
from .provenance import ProvenanceRecord


@dataclass
class SafetyContext:
    """Everything a safety check may inspect."""

    answer: str | None
    events: list[Event]
    provenance: list[ProvenanceRecord]

    def events_of(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


@dataclass
class SafetyFinding:
    """One issue discovered by a safety check."""

    check: str
    severity: str  # "info" | "warning" | "critical"
    message: str
    subject: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
            "details": self.details,
        }


SafetyCheck = Callable[[SafetyContext], list[SafetyFinding]]


def check_unsupported_claims(ctx: SafetyContext) -> list[SafetyFinding]:
    """Every CLAIM event must be referenced by at least one EVIDENCE_LINK."""
    supported = {
        link.payload.get("claim_id")
        for link in ctx.events_of(EventType.EVIDENCE_LINK)
    }
    findings = []
    for claim in ctx.events_of(EventType.CLAIM):
        if claim.id not in supported:
            findings.append(
                SafetyFinding(
                    check="unsupported_claims",
                    severity="critical",
                    message=f"Claim has no linked evidence: {claim.payload.get('text', claim.name)}",
                    subject=claim.id,
                )
            )
    return findings


def check_hallucinated_resources(ctx: SafetyContext) -> list[SafetyFinding]:
    """Evidence links must cite provenance records that actually exist."""
    known = {p.id for p in ctx.provenance}
    findings = []
    for link in ctx.events_of(EventType.EVIDENCE_LINK):
        for source in link.payload.get("provenance_ids", []):
            if source not in known:
                findings.append(
                    SafetyFinding(
                        check="hallucinated_resources",
                        severity="critical",
                        message=f"Evidence cites a resource that was never retrieved: {source}",
                        subject=link.id,
                        details={"provenance_id": source},
                    )
                )
    return findings


def check_ignored_allergies(ctx: SafetyContext) -> list[SafetyFinding]:
    """Retrieved allergy data must be linked as evidence, not just fetched.

    Fetching an AllergyIntolerance resource and then never referencing it in
    the reasoning chain is a red flag for a recommendation that ignored it.
    """
    allergy_prov_ids = {
        p.id for p in ctx.provenance if "allergy" in p.resource_type.lower()
    }
    if not allergy_prov_ids:
        return []
    cited: set[str] = set()
    for link in ctx.events_of(EventType.EVIDENCE_LINK):
        cited.update(link.payload.get("provenance_ids", []))
    ignored = allergy_prov_ids - cited
    return [
        SafetyFinding(
            check="ignored_allergies",
            severity="warning",
            message="Allergy data was retrieved but never linked as evidence.",
            subject=prov_id,
        )
        for prov_id in sorted(ignored)
    ]


def check_missing_answer(ctx: SafetyContext) -> list[SafetyFinding]:
    """Sessions must terminate with an explicit answer (even 'I don't know')."""
    if ctx.answer is None:
        return [
            SafetyFinding(
                check="missing_answer",
                severity="warning",
                message="Session ended without a recorded answer.",
            )
        ]
    return []


DEFAULT_CHECKS: list[SafetyCheck] = [
    check_unsupported_claims,
    check_hallucinated_resources,
    check_ignored_allergies,
    check_missing_answer,
]


class SafetyEngine:
    """Runs a configurable set of safety checks over a session."""

    def __init__(self, checks: list[SafetyCheck] | None = None):
        self.checks: list[SafetyCheck] = list(DEFAULT_CHECKS if checks is None else checks)

    def add(self, check: SafetyCheck) -> None:
        self.checks.append(check)

    def evaluate(self, ctx: SafetyContext) -> list[SafetyFinding]:
        findings: list[SafetyFinding] = []
        for check in self.checks:
            findings.extend(check(ctx))
        return findings
