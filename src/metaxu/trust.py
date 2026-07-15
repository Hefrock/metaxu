"""Trust engine: multi-dimensional trust assessment.

Deliberately, there is **no aggregate score**. Collapsing evidence quality,
freshness, and policy compliance into one number hides exactly the
information a clinician needs. Each dimension is reported separately with a
score in [0, 1], a rationale, and the inputs it was computed from — so the
number is auditable, not oracular.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .events import Event, EventType
from .provenance import ProvenanceRecord
from .safety import SafetyFinding


@dataclass
class TrustDimension:
    """One dimension of trust, with its justification."""

    score: float
    rationale: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "rationale": self.rationale,
            "inputs": self.inputs,
        }


class TrustEngine:
    """Computes trust dimensions from the assurance session state.

    The built-in dimensions are structural (computable from any session
    without clinical knowledge). Domain-specific dimensions — terminology
    correctness, guideline concordance — are expected to be contributed as
    additional evaluators over time.
    """

    def __init__(self, freshness_horizon_hours: float = 24.0):
        self.freshness_horizon_hours = freshness_horizon_hours

    def evaluate(
        self,
        events: list[Event],
        provenance: list[ProvenanceRecord],
        policy_checks: list[dict[str, Any]],
        safety_findings: list[SafetyFinding],
        missing_data: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        dims = {
            "provenance_coverage": self._provenance_coverage(events),
            "policy_compliance": self._policy_compliance(policy_checks),
            "safety": self._safety(safety_findings),
            "data_completeness": self._completeness(missing_data),
            "data_freshness": self._freshness(provenance),
        }
        return {name: dim.to_dict() for name, dim in dims.items()}

    def _provenance_coverage(self, events: list[Event]) -> TrustDimension:
        claims = [e for e in events if e.type == EventType.CLAIM]
        if not claims:
            return TrustDimension(
                score=0.0,
                rationale="No claims were recorded, so nothing can be traced.",
                inputs={"claims": 0, "supported_claims": 0},
            )
        supported_ids = {
            link.payload.get("claim_id")
            for link in events
            if link.type == EventType.EVIDENCE_LINK
        }
        supported = sum(1 for c in claims if c.id in supported_ids)
        return TrustDimension(
            score=supported / len(claims),
            rationale=f"{supported} of {len(claims)} claims are linked to retrieved evidence.",
            inputs={"claims": len(claims), "supported_claims": supported},
        )

    def _policy_compliance(self, policy_checks: list[dict[str, Any]]) -> TrustDimension:
        triggered = [p for p in policy_checks if p.get("triggered")]
        if not triggered:
            return TrustDimension(
                score=1.0,
                rationale="No clinical policies were triggered by this interaction.",
                inputs={"triggered": 0, "passed": 0},
            )
        passed = sum(1 for p in triggered if p.get("passed"))
        return TrustDimension(
            score=passed / len(triggered),
            rationale=f"{passed} of {len(triggered)} triggered policies passed.",
            inputs={"triggered": len(triggered), "passed": passed},
        )

    def _safety(self, findings: list[SafetyFinding]) -> TrustDimension:
        critical = sum(1 for f in findings if f.severity == "critical")
        warnings = sum(1 for f in findings if f.severity == "warning")
        if critical:
            score = 0.0
        elif warnings:
            score = max(0.0, 1.0 - 0.25 * warnings)
        else:
            score = 1.0
        return TrustDimension(
            score=score,
            rationale=(
                f"{critical} critical and {warnings} warning safety findings."
                if findings
                else "No safety findings."
            ),
            inputs={"critical": critical, "warnings": warnings},
        )

    def _completeness(self, missing_data: list[dict[str, Any]]) -> TrustDimension:
        if not missing_data:
            return TrustDimension(
                score=1.0,
                rationale="No required data was reported missing.",
                inputs={"missing_items": 0},
            )
        return TrustDimension(
            score=max(0.0, 1.0 - 0.25 * len(missing_data)),
            rationale=f"{len(missing_data)} required data element(s) were unavailable.",
            inputs={"missing_items": len(missing_data), "items": missing_data},
        )

    def _freshness(self, provenance: list[ProvenanceRecord]) -> TrustDimension:
        if not provenance:
            return TrustDimension(
                score=0.0,
                rationale="No provenance records; freshness cannot be assessed.",
                inputs={"resources": 0},
            )
        now = datetime.now(timezone.utc)
        horizon = self.freshness_horizon_hours
        ages = []
        for record in provenance:
            try:
                retrieved = datetime.fromisoformat(record.retrieved_at)
                if retrieved.tzinfo is None:
                    retrieved = retrieved.replace(tzinfo=timezone.utc)
                ages.append((now - retrieved).total_seconds() / 3600.0)
            except ValueError:
                ages.append(horizon)  # unparseable timestamp -> assume stale
        scores = [max(0.0, 1.0 - age / horizon) for age in ages]
        return TrustDimension(
            score=sum(scores) / len(scores),
            rationale=(
                f"Mean retrieval age {sum(ages) / len(ages):.2f}h against a "
                f"{horizon:.0f}h freshness horizon."
            ),
            inputs={
                "resources": len(provenance),
                "freshness_horizon_hours": horizon,
                "max_age_hours": round(max(ages), 4),
            },
        )
