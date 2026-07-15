"""Metaxu: a Healthcare AI Assurance Layer.

μεταξύ (metaxu) — Greek for "between"; pronounced meh-TAX-oo.

Trust infrastructure that sits between AI systems and clinical users.
Instead of returning ``Answer``, an instrumented system returns
``Answer + Assurance Artifact`` — a machine-readable record of provenance,
evidence, policy compliance, safety findings, and multi-dimensional trust.

Model-agnostic, agent-agnostic, EHR-agnostic.
"""

from .artifact import ARTIFACT_SCHEMA_VERSION, AssuranceArtifact
from .drift import compare_cohorts
from .events import Event, EventType
from .governance import aggregate_artifacts, load_artifacts
from .adapters.mcp import MCPProxy
from .instrument import assured_tool
from .merge import merge_artifacts
from .policy import Policy, PolicyEngine, PolicyResult, Requirement
from .provenance import ProvenanceRecord, content_hash
from .replay import (
    VerificationReport,
    diff_artifacts,
    replay_with_runner,
    save_snapshot,
    snapshot_resolver,
    verify,
)
from .safety import SafetyContext, SafetyEngine, SafetyFinding
from .session import AssuranceSession, current_session
from .trust import TrustDimension, TrustEngine

__version__ = "0.2.0"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "AssuranceArtifact",
    "AssuranceSession",
    "Event",
    "EventType",
    "MCPProxy",
    "Policy",
    "PolicyEngine",
    "PolicyResult",
    "ProvenanceRecord",
    "Requirement",
    "SafetyContext",
    "SafetyEngine",
    "SafetyFinding",
    "TrustDimension",
    "TrustEngine",
    "VerificationReport",
    "aggregate_artifacts",
    "assured_tool",
    "compare_cohorts",
    "content_hash",
    "diff_artifacts",
    "current_session",
    "load_artifacts",
    "merge_artifacts",
    "replay_with_runner",
    "save_snapshot",
    "snapshot_resolver",
    "verify",
    "__version__",
]
