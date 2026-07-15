"""Event model for the Metaxu assurance layer.

Every observable action in an AI-mediated clinical interaction is recorded
as an :class:`Event`. Events are the raw material from which the assurance
artifact is assembled: the tool trace, the evidence graph, policy and safety
check results all derive from the ordered event stream.

The event model is deliberately flat (a list of events with optional
``parent_id`` links) so that it can survive multi-agent workflows: any
process that can append a JSON object to a list can participate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """The vocabulary of things Metaxu knows how to observe."""

    QUESTION = "question"
    TOOL_INVOCATION = "tool_invocation"
    RETRIEVAL = "retrieval"
    CLAIM = "claim"
    EVIDENCE_LINK = "evidence_link"
    POLICY_CHECK = "policy_check"
    SAFETY_CHECK = "safety_check"
    MISSING_DATA = "missing_data"
    ANSWER = "answer"
    NOTE = "note"


def utcnow() -> str:
    """ISO-8601 UTC timestamp with second precision plus microseconds."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    """A single observed action within an assurance session.

    Attributes:
        type: What kind of action this is.
        name: A short, stable identifier for the action (e.g. the tool name,
            the policy requirement it satisfies, the claim id).
        payload: Arbitrary JSON-serializable detail about the action.
        tags: Free-form labels used by the policy engine to match
            requirements (e.g. ``allergy_check``).
        parent_id: Optional id of the event that caused this one, allowing
            trace trees to be reconstructed across nested or multi-agent
            workflows.
    """

    type: EventType
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    parent_id: str | None = None
    id: str = field(default_factory=lambda: f"evt-{uuid.uuid4()}")
    timestamp: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "name": self.name,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "tags": self.tags,
            "parent_id": self.parent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            type=EventType(data["type"]),
            name=data["name"],
            payload=data.get("payload", {}),
            tags=data.get("tags", []),
            parent_id=data.get("parent_id"),
            id=data["id"],
            timestamp=data["timestamp"],
        )
