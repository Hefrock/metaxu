"""Governance engine: aggregate metrics over a collection of artifacts.

Individual artifacts answer "should a clinician trust *this* answer?".
Governance answers the institutional questions: how is the AI system
doing *overall*, is it getting better or worse, which tools are flaky,
which policies keep failing, and which interactions need human review.

The input is simply a directory (or list) of assurance artifacts — the
artifact is the interoperability boundary, so any producer's artifacts
aggregate the same way. The output is a plain JSON-able report consumed
by the CLI (`metaxu report`), the HTML dashboard, or a CI gate.

Consistent with the trust engine, per-dimension scores are aggregated
per-dimension. There is deliberately no single institution-wide "AI
trust score".
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from typing import Any

from .artifact import AssuranceArtifact


def load_artifacts(directory: str) -> list[AssuranceArtifact]:
    """Load every ``*.json`` assurance artifact under ``directory``
    (recursively). Files that are not artifacts are skipped, not fatal —
    governance runs over real, messy artifact stores."""
    artifacts: list[AssuranceArtifact] = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict) or "schema_version" not in data:
                    continue  # not an assurance artifact (e.g. a snapshot)
                artifacts.append(AssuranceArtifact.from_dict(data))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
    return artifacts


def aggregate_artifacts(artifacts: list[AssuranceArtifact]) -> dict[str, Any]:
    """Compute the governance report for a collection of artifacts."""
    report: dict[str, Any] = {
        "artifact_count": len(artifacts),
        "time_range": None,
        "observers": {},
        "roles": {},
        "integrity": {"verified": 0, "failed": 0},
        "trust": {},
        "policies": {},
        "safety": {
            "findings_by_check": {},
            "findings_by_severity": {},
            "artifacts_with_critical": 0,
            "hallucination_rate": None,
            "unsupported_claim_rate": None,
        },
        "terminology": {
            "codes_checked": 0,
            "malformed": 0,
            "malformed_rate": None,
            "by_system": {},
        },
        "tools": {},
        "provenance": {"total_records": 0, "by_source_system": {}},
        "missing_data": {},
        "needs_review": [],
    }
    if not artifacts:
        return report

    created = sorted(a.created_at for a in artifacts)
    report["time_range"] = {"from": created[0], "to": created[-1]}
    report["observers"] = dict(
        Counter(a.correlation.get("observer", "unknown") for a in artifacts)
    )
    report["roles"] = dict(
        Counter(a.correlation.get("role", "unknown") for a in artifacts)
    )

    trust_scores: dict[str, list[float]] = defaultdict(list)
    policy_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"triggered": 0, "passed": 0, "unsatisfied_requirements": Counter()}
    )
    check_counter: Counter = Counter()
    severity_counter: Counter = Counter()
    hallucinating = 0
    unsupported = 0
    tool_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "durations_ms": []}
    )
    source_counter: Counter = Counter()
    missing_counter: Counter = Counter()
    codes_checked = 0
    codes_malformed = 0
    malformed_by_system: Counter = Counter()

    for artifact in artifacts:
        integrity_ok = artifact.verify_integrity()
        report["integrity"]["verified" if integrity_ok else "failed"] += 1

        for dimension, value in artifact.trust_scores.items():
            score = value.get("score")
            if isinstance(score, (int, float)):
                trust_scores[dimension].append(float(score))

        failed_policies = []
        for check in artifact.policy_checks:
            if not check.get("triggered"):
                continue
            stats = policy_stats[check["policy"]]
            stats["triggered"] += 1
            if check.get("passed"):
                stats["passed"] += 1
            else:
                failed_policies.append(check["policy"])
                for bucket in ("missing", "errored", "unmet"):
                    stats["unsatisfied_requirements"].update(check.get(bucket, []))

        artifact_checks = {f["check"] for f in artifact.safety_checks}
        critical = [f for f in artifact.safety_checks if f.get("severity") == "critical"]
        for finding in artifact.safety_checks:
            check_counter[finding["check"]] += 1
            severity_counter[finding.get("severity", "unknown")] += 1
        if "hallucinated_resources" in artifact_checks:
            hallucinating += 1
        if "unsupported_claims" in artifact_checks:
            unsupported += 1

        for result in artifact.terminology:
            codes_checked += 1
            if not result.get("valid"):
                codes_malformed += 1
                malformed_by_system[result.get("system", "unknown")] += 1

        for call in artifact.tool_trace:
            stats = tool_stats[call["name"]]
            stats["calls"] += 1
            payload = call.get("payload", {})
            if payload.get("error"):
                stats["errors"] += 1
            duration = payload.get("duration_ms")
            if isinstance(duration, (int, float)):
                stats["durations_ms"].append(float(duration))

        report["provenance"]["total_records"] += len(artifact.provenance)
        source_counter.update(p.source_system for p in artifact.provenance)
        missing_counter.update(
            item.get("item", "unknown") for item in artifact.missing_data
        )

        reasons = []
        if critical:
            reasons.append(f"{len(critical)} critical safety finding(s)")
        if failed_policies:
            reasons.append("failed policies: " + ", ".join(sorted(set(failed_policies))))
        if not integrity_ok:
            reasons.append("integrity hash mismatch")
        if reasons:
            report["needs_review"].append(
                {
                    "artifact_id": artifact.id,
                    "created_at": artifact.created_at,
                    "question": artifact.question,
                    "reasons": reasons,
                }
            )

    report["trust"] = {
        dimension: {
            "mean": round(sum(scores) / len(scores), 4),
            "min": round(min(scores), 4),
            "artifacts": len(scores),
        }
        for dimension, scores in sorted(trust_scores.items())
    }
    report["policies"] = {
        name: {
            "triggered": stats["triggered"],
            "passed": stats["passed"],
            "pass_rate": round(stats["passed"] / stats["triggered"], 4),
            "top_unsatisfied_requirements": dict(
                stats["unsatisfied_requirements"].most_common(5)
            ),
        }
        for name, stats in sorted(policy_stats.items())
    }
    report["safety"]["findings_by_check"] = dict(check_counter.most_common())
    report["safety"]["findings_by_severity"] = dict(severity_counter.most_common())
    report["safety"]["artifacts_with_critical"] = sum(
        1
        for entry in report["needs_review"]
        if any("critical" in reason for reason in entry["reasons"])
    )
    report["safety"]["hallucination_rate"] = round(hallucinating / len(artifacts), 4)
    report["safety"]["unsupported_claim_rate"] = round(unsupported / len(artifacts), 4)
    report["terminology"] = {
        "codes_checked": codes_checked,
        "malformed": codes_malformed,
        "malformed_rate": round(codes_malformed / codes_checked, 4) if codes_checked else None,
        "by_system": dict(malformed_by_system.most_common()),
    }
    report["tools"] = {
        name: {
            "calls": stats["calls"],
            "errors": stats["errors"],
            "error_rate": round(stats["errors"] / stats["calls"], 4),
            "mean_duration_ms": (
                round(sum(stats["durations_ms"]) / len(stats["durations_ms"]), 2)
                if stats["durations_ms"]
                else None
            ),
        }
        for name, stats in sorted(tool_stats.items())
    }
    report["provenance"]["by_source_system"] = dict(source_counter.most_common())
    report["missing_data"] = dict(missing_counter.most_common())
    # Newest problem artifacts first: triage order for a reviewer.
    report["needs_review"].sort(key=lambda e: e["created_at"], reverse=True)
    return report


def render_text(report: dict[str, Any]) -> str:
    """Terminal rendering of a governance report."""
    lines: list[str] = []
    count = report["artifact_count"]
    lines.append(f"Governance report over {count} artifact(s)")
    if count == 0:
        lines.append("  (no artifacts found)")
        return "\n".join(lines)

    time_range = report["time_range"]
    lines.append(f"  period:    {time_range['from']} .. {time_range['to']}")
    observers = ", ".join(f"{k}={v}" for k, v in sorted(report["observers"].items()))
    lines.append(f"  observers: {observers}")
    integrity = report["integrity"]
    lines.append(
        f"  integrity: {integrity['verified']}/{count} verified"
        + (f", {integrity['failed']} FAILED" if integrity["failed"] else "")
    )

    lines.append("\nTrust dimensions (mean [min] across artifacts):")
    for dimension, stats in report["trust"].items():
        lines.append(f"  {dimension:<22} {stats['mean']:.2f}  [{stats['min']:.2f}]")

    if report["policies"]:
        lines.append("\nPolicy pass rates:")
        for name, stats in report["policies"].items():
            lines.append(
                f"  {name:<32} {stats['passed']}/{stats['triggered']} "
                f"({stats['pass_rate']:.0%})"
            )
            if stats["top_unsatisfied_requirements"]:
                worst = ", ".join(
                    f"{req} ×{n}"
                    for req, n in stats["top_unsatisfied_requirements"].items()
                )
                lines.append(f"    most unsatisfied: {worst}")

    safety = report["safety"]
    lines.append("\nSafety:")
    lines.append(f"  hallucination rate:      {safety['hallucination_rate']:.0%}")
    lines.append(f"  unsupported-claim rate:  {safety['unsupported_claim_rate']:.0%}")
    if safety["findings_by_check"]:
        for check, n in safety["findings_by_check"].items():
            lines.append(f"  {check:<24} {n} finding(s)")
    else:
        lines.append("  no findings")

    terminology = report["terminology"]
    if terminology["codes_checked"]:
        lines.append("\nTerminology:")
        lines.append(
            f"  {terminology['codes_checked']} code(s) checked, "
            f"{terminology['malformed']} malformed "
            f"({terminology['malformed_rate']:.0%})"
        )
        for system, n in terminology["by_system"].items():
            lines.append(f"  malformed {system}: {n}")

    if report["tools"]:
        lines.append("\nTool reliability:")
        for name, stats in report["tools"].items():
            duration = (
                f", mean {stats['mean_duration_ms']:.1f}ms"
                if stats["mean_duration_ms"] is not None
                else ""
            )
            lines.append(
                f"  {name:<28} {stats['calls']} call(s), "
                f"{stats['error_rate']:.0%} errors{duration}"
            )

    if report["missing_data"]:
        lines.append("\nMost-missed data:")
        for item, n in report["missing_data"].items():
            lines.append(f"  {item:<28} ×{n}")

    lines.append(f"\nNeeds review: {len(report['needs_review'])} artifact(s)")
    for entry in report["needs_review"][:10]:
        lines.append(f"  - {entry['artifact_id']}  ({'; '.join(entry['reasons'])})")
    if len(report["needs_review"]) > 10:
        lines.append(f"  … and {len(report['needs_review']) - 10} more")
    return "\n".join(lines)
