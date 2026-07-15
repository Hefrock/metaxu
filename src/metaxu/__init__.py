"""Metaxu: a Healthcare AI Assurance Layer.

μεταξύ (metaxu) — Greek for "between"; pronounced meh-TAX-oo.

Trust infrastructure that sits between AI systems and clinical users.
Instead of returning ``Answer``, an instrumented system returns
``Answer + Assurance Artifact`` — a machine-readable record of provenance,
evidence, policy compliance, safety findings, and multi-dimensional trust.

Model-agnostic, agent-agnostic, EHR-agnostic.
"""

from .artifact import ARTIFACT_SCHEMA_VERSION, AssuranceArtifact
from .events import Event, EventType
from .instrument import assured_tool
from .mcp_proxy import MCPProxy
from .policy import Policy, PolicyEngine, PolicyResult
from .provenance import ProvenanceRecord, content_hash
from .replay import VerificationReport, save_snapshot, snapshot_resolver, verify
from .safety import SafetyContext, SafetyEngine, SafetyFinding
from .session import AssuranceSession, current_session
from .trust import TrustDimension, TrustEngine

__version__ = "0.1.0"

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
    "SafetyContext",
    "SafetyEngine",
    "SafetyFinding",
    "TrustDimension",
    "TrustEngine",
    "VerificationReport",
    "assured_tool",
    "content_hash",
    "current_session",
    "save_snapshot",
    "snapshot_resolver",
    "verify",
    "__version__",
]
