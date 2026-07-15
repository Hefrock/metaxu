"""Drift detection: compare two cohorts of assurance artifacts over time.

Individual artifacts freeze one decision; governance summarizes a store;
drift asks the longitudinal question — *has anything changed since the
baseline?* Four kinds of change are detected, all computable from the
artifacts alone:

* **Environment drift** — model, prompt, tool, and MCP-server versions
  that appeared, disappeared, or changed between cohorts (the
  reproducibility block makes this visible).
* **Behavioral drift** — deltas in trust dimensions, policy pass rates,
  tool error rates, and hallucination/unsupported-claim rates.
  Regressions beyond a threshold are flagged; improvements are reported
  but not flagged.
* **Answer drift** — the same question answered differently in the two
  cohorts. Comparison is exact after whitespace/case normalization:
  paraphrases are treated as change, which is the conservative direction
  for clinical review (a human decides whether the change is benign).
* **Source drift** — the same resource (source system + type + id)
  carrying a different content hash: the record itself changed.

Typical use: keep artifacts in dated directories and compare last
month's store against this month's, or a pre-deploy benchmark run
against a post-deploy one::

    metaxu drift artifacts/2026-06/ artifacts/2026-07/
    metaxu drift baseline/ current/ --fail-on-drift   # CI gate
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .artifact import AssuranceArtifact
from .governance import aggregate_artifacts

DEFAULT_THRESHOLD = 0.1  # minimum regression in a rate/score to flag


def compare_cohorts(
    baseline: list[AssuranceArtifact],
    current: list[AssuranceArtifact],
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Compute the drift report between two artifact cohorts.

    ``flags`` collects every actionable finding as human-readable
    strings; an empty ``flags`` list means no drift worth review.
    """
    base_report = aggregate_artifacts(baseline)
    curr_report = aggregate_artifacts(current)
    flags: list[str] = []

    report: dict[str, Any] = {
        "baseline": {
            "artifact_count": base_report["artifact_count"],
            "period": base_report["time_range"],
        },
        "current": {
            "artifact_count": curr_report["artifact_count"],
            "period": curr_report["time_range"],
        },
        "threshold": threshold,
        "environment": _environment_drift(baseline, current, flags),
        "behavior": _behavior_drift(base_report, curr_report, threshold, flags),
        "answers": _answer_drift(baseline, current, flags),
        "sources": _source_drift(baseline, current, flags),
        "flags": flags,
    }
    return report


# -- environment ------------------------------------------------------------


def _versions(artifacts: list[AssuranceArtifact]) -> dict[str, Counter]:
    seen: dict[str, Counter] = {
        "model": Counter(),
        "prompt_version": Counter(),
        "mcp_server": Counter(),
        "tool_versions": Counter(),
    }
    for artifact in artifacts:
        repro = artifact.reproducibility
        for key in ("model", "prompt_version"):
            if repro.get(key):
                seen[key][str(repro[key])] += 1
        server = repro.get("mcp_server") or {}
        if server.get("name"):
            seen["mcp_server"][f"{server['name']}@{server.get('version', '?')}"] += 1
        for tool, version in (repro.get("tool_versions") or {}).items():
            seen["tool_versions"][f"{tool}@{version}"] += 1
    return seen


def _environment_drift(
    baseline: list[AssuranceArtifact],
    current: list[AssuranceArtifact],
    flags: list[str],
) -> dict[str, Any]:
    base, curr = _versions(baseline), _versions(current)
    out: dict[str, Any] = {}
    labels = {
        "model": "model",
        "prompt_version": "prompt version",
        "mcp_server": "MCP server",
        "tool_versions": "tool version",
    }
    for key, label in labels.items():
        added = sorted(set(curr[key]) - set(base[key]))
        removed = sorted(set(base[key]) - set(curr[key]))
        out[key] = {
            "baseline": dict(base[key]),
            "current": dict(curr[key]),
            "added": added,
            "removed": removed,
        }
        for value in added:
            flags.append(f"environment: new {label} in current cohort: {value}")
        for value in removed:
            flags.append(f"environment: {label} no longer seen: {value}")
    return out


# -- behavior ----------------------------------------------------------------


def _rate_delta(
    name: str,
    baseline_value: float | None,
    current_value: float | None,
    threshold: float,
    flags: list[str],
    higher_is_worse: bool,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "baseline": baseline_value,
        "current": current_value,
        "delta": None,
        "flagged": False,
    }
    if baseline_value is None or current_value is None:
        return entry
    delta = round(current_value - baseline_value, 4)
    entry["delta"] = delta
    regression = delta > 0 if higher_is_worse else delta < 0
    if regression and abs(delta) >= threshold:
        entry["flagged"] = True
        direction = "rose" if delta > 0 else "fell"
        flags.append(
            f"behavior: {name} {direction} from {baseline_value:.2f} to {current_value:.2f}"
        )
    return entry


def _behavior_drift(
    base_report: dict[str, Any],
    curr_report: dict[str, Any],
    threshold: float,
    flags: list[str],
) -> dict[str, Any]:
    behavior: dict[str, Any] = {"trust": {}, "policies": {}, "tools": {}, "safety": {}}

    for dim in sorted(set(base_report["trust"]) | set(curr_report["trust"])):
        behavior["trust"][dim] = _rate_delta(
            f"trust dimension '{dim}' mean",
            base_report["trust"].get(dim, {}).get("mean"),
            curr_report["trust"].get(dim, {}).get("mean"),
            threshold,
            flags,
            higher_is_worse=False,
        )

    for name in sorted(set(base_report["policies"]) | set(curr_report["policies"])):
        behavior["policies"][name] = _rate_delta(
            f"policy '{name}' pass rate",
            base_report["policies"].get(name, {}).get("pass_rate"),
            curr_report["policies"].get(name, {}).get("pass_rate"),
            threshold,
            flags,
            higher_is_worse=False,
        )

    for name in sorted(set(base_report["tools"]) | set(curr_report["tools"])):
        behavior["tools"][name] = _rate_delta(
            f"tool '{name}' error rate",
            base_report["tools"].get(name, {}).get("error_rate"),
            curr_report["tools"].get(name, {}).get("error_rate"),
            threshold,
            flags,
            higher_is_worse=True,
        )

    for metric in ("hallucination_rate", "unsupported_claim_rate"):
        behavior["safety"][metric] = _rate_delta(
            f"safety {metric.replace('_', ' ')}",
            base_report["safety"][metric],
            curr_report["safety"][metric],
            threshold,
            flags,
            higher_is_worse=True,
        )
    return behavior


# -- answers -----------------------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _answers_by_question(
    artifacts: list[AssuranceArtifact],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.answer is None:
            continue
        key = _normalize(artifact.question)
        entry = grouped.setdefault(
            key, {"question": artifact.question, "answers": [], "artifact_ids": []}
        )
        normalized_answer = _normalize(artifact.answer)
        if normalized_answer not in (_normalize(a) for a in entry["answers"]):
            entry["answers"].append(artifact.answer)
        entry["artifact_ids"].append(artifact.id)
    return grouped


def _answer_drift(
    baseline: list[AssuranceArtifact],
    current: list[AssuranceArtifact],
    flags: list[str],
) -> dict[str, Any]:
    base, curr = _answers_by_question(baseline), _answers_by_question(current)
    repeated = sorted(set(base) & set(curr))
    changed = []
    for key in repeated:
        base_norm = {_normalize(a) for a in base[key]["answers"]}
        curr_norm = {_normalize(a) for a in curr[key]["answers"]}
        if base_norm != curr_norm:
            changed.append(
                {
                    "question": base[key]["question"],
                    "baseline_answers": base[key]["answers"],
                    "current_answers": curr[key]["answers"],
                    "baseline_artifacts": base[key]["artifact_ids"],
                    "current_artifacts": curr[key]["artifact_ids"],
                }
            )
            flags.append(
                "answers: same question, different answer: "
                f"{_truncate(base[key]['question'], 80)}"
            )
    return {
        "repeated_questions": len(repeated),
        "changed": changed,
    }


# -- sources -----------------------------------------------------------------


def _latest_hashes(
    artifacts: list[AssuranceArtifact],
) -> dict[tuple[str, str, str], dict[str, str]]:
    latest: dict[tuple[str, str, str], dict[str, str]] = {}
    for artifact in artifacts:
        for record in artifact.provenance:
            if not record.hash:
                continue
            key = (record.source_system, record.resource_type, record.resource_id)
            existing = latest.get(key)
            if existing is None or record.retrieved_at > existing["retrieved_at"]:
                latest[key] = {"hash": record.hash, "retrieved_at": record.retrieved_at}
    return latest


def _source_drift(
    baseline: list[AssuranceArtifact],
    current: list[AssuranceArtifact],
    flags: list[str],
) -> dict[str, Any]:
    base, curr = _latest_hashes(baseline), _latest_hashes(current)
    shared = sorted(set(base) & set(curr))
    changed = []
    for key in shared:
        if base[key]["hash"] != curr[key]["hash"]:
            source_system, resource_type, resource_id = key
            changed.append(
                {
                    "resource": f"{resource_type}/{resource_id}",
                    "source_system": source_system,
                    "baseline_hash": base[key]["hash"],
                    "current_hash": curr[key]["hash"],
                    "baseline_retrieved_at": base[key]["retrieved_at"],
                    "current_retrieved_at": curr[key]["retrieved_at"],
                }
            )
            flags.append(
                f"sources: {resource_type}/{resource_id} changed at the source "
                f"({source_system})"
            )
    return {"resources_compared": len(shared), "changed": changed}


# -- rendering ----------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Drift report",
        f"  baseline: {report['baseline']['artifact_count']} artifact(s)",
        f"  current:  {report['current']['artifact_count']} artifact(s)",
        f"  flag threshold: {report['threshold']}",
    ]

    lines.append("\nEnvironment:")
    any_env = False
    for key, data in report["environment"].items():
        for value in data["added"]:
            lines.append(f"  + {key}: {value}")
            any_env = True
        for value in data["removed"]:
            lines.append(f"  - {key}: {value}")
            any_env = True
    if not any_env:
        lines.append("  unchanged")

    lines.append("\nBehavior (baseline -> current):")
    behavior = report["behavior"]
    for group_name, entries in behavior.items():
        for name, entry in entries.items():
            if entry["delta"] is None:
                continue
            marker = "  ⚠" if entry["flagged"] else "   "
            lines.append(
                f"{marker} {group_name}/{name}: "
                f"{entry['baseline']:.2f} -> {entry['current']:.2f} "
                f"({entry['delta']:+.2f})"
            )

    answers = report["answers"]
    lines.append(
        f"\nAnswers: {answers['repeated_questions']} repeated question(s), "
        f"{len(answers['changed'])} changed"
    )
    for change in answers["changed"]:
        lines.append(f"  ⚠ {_truncate(change['question'], 90)}")

    sources = report["sources"]
    lines.append(
        f"\nSources: {sources['resources_compared']} shared resource(s), "
        f"{len(sources['changed'])} changed"
    )
    for change in sources["changed"]:
        lines.append(f"  ⚠ {change['resource']} @ {change['source_system']}")

    lines.append(f"\nDrift flags: {len(report['flags'])}")
    for flag in report["flags"]:
        lines.append(f"  - {flag}")
    return "\n".join(lines)
