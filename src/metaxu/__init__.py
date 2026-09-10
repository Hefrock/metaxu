"""Metaxu: a Healthcare AI Assurance Layer.

μεταξύ (metaxu) — Greek for "between"; pronounced meh-TAX-oo.

Trust infrastructure that sits between AI systems and clinical users.
Instead of returning ``Answer``, an instrumented system returns
``Answer + Assurance Artifact`` — a machine-readable record of provenance,
evidence, policy compliance, safety findings, and multi-dimensional trust.

Model-agnostic, agent-agnostic, EHR-agnostic.
"""

from .artifact import ARTIFACT_SCHEMA_VERSION, SCHEMA_COMPATIBILITY, AssuranceArtifact, schema_era
from .drift import compare_cohorts
from .events import Event, EventType
from .governance import aggregate_artifacts, load_artifacts
from .graph import EvidenceGraph
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
from .terminology import (
    CodeValidation,
    Coding,
    FormatResolver,
    TerminologyValidator,
    extract_codings,
    normalize_system,
)
from .trust import TrustDimension, TrustEngine

__version__ = "2026.9.10"

__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "SCHEMA_COMPATIBILITY",
    "AssuranceArtifact",
    "AssuranceSession",
    "CodeValidation",
    "Coding",
    "Event",
    "EventType",
    "EvidenceGraph",
    "FormatResolver",
    "MCPProxy",
    "Policy",
    "PolicyEngine",
    "PolicyResult",
    "ProvenanceRecord",
    "Requirement",
    "SafetyContext",
    "SafetyEngine",
    "SafetyFinding",
    "TerminologyValidator",
    "TrustDimension",
    "TrustEngine",
    "VerificationReport",
    "aggregate_artifacts",
    "assured_tool",
    "compare_cohorts",
    "content_hash",
    "diff_artifacts",
    "extract_codings",
    "current_session",
    "load_artifacts",
    "merge_artifacts",
    "normalize_system",
    "replay_with_runner",
    "save_snapshot",
    "schema_era",
    "snapshot_resolver",
    "verify",
    "__version__",
]
