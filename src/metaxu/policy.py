"""Declarative clinical policy engine.

Policies state which checks must have occurred before an AI system is
allowed to make a class of recommendation — the clinical analogue of a
linter rule. They are data, not code, so institutions can share a policy
pack and locally extend it.

A policy is satisfied by *observed events*. A requirement is either:

* a **string** — satisfied when any non-errored event carries it as its
  ``name`` or one of its ``tags`` ("an allergy check occurred"), or
* an **object** — the string match plus conditions on the matching event:

  .. code-block:: json

      {
        "check": "platelet_count",
        "where": {"path": "result_summary.valueQuantity.value", "gte": 50},
        "within_hours": 48
      }

  ``where`` evaluates a dotted path into the event payload against
  operators ``eq``/``ne``/``gt``/``gte``/``lt``/``lte``/``in``.
  ``within_hours`` requires the event to be no older than N hours at the
  time the answer was given ("used the *newest* labs, not just *some*
  labs").

Each requirement lands in exactly one bucket of the result: ``satisfied``,
``missing`` (never attempted), ``errored`` (attempted, but every attempt
failed), or ``unmet`` (performed, but the value or timing failed the
condition — a platelet check that came back too low is not a passed
platelet check). Events that recorded an error never satisfy anything.

This keeps policies decoupled from any particular agent framework —
instrumented tools simply tag the checks they perform, and structured
tool results (see ``metaxu.session``) expose their values to ``where``.

Policy documents are JSON natively; YAML is supported when ``pyyaml`` is
installed (``pip install metaxu[yaml]``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .events import Event, EventType

_MISSING = object()


@dataclass
class PolicyResult:
    """Outcome of evaluating one policy against a session.

    Every requirement lands in exactly one of ``satisfied``, ``missing``
    (never attempted), ``errored`` (attempted but every attempt failed),
    or ``unmet`` (performed, but its value/timing conditions failed).
    Only ``satisfied`` counts toward passing.
    """

    policy: str
    triggered: bool
    passed: bool
    satisfied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errored: list[str] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "description": self.description,
            "triggered": self.triggered,
            "passed": self.passed,
            "satisfied": self.satisfied,
            "missing": self.missing,
            "errored": self.errored,
            "unmet": self.unmet,
        }


@dataclass
class Requirement:
    """One requirement within a policy (see module docstring)."""

    check: str
    where: dict[str, Any] | None = None
    within_hours: float | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Requirement":
        if isinstance(raw, str):
            return cls(check=raw)
        return cls(
            check=raw["check"],
            where=raw.get("where"),
            within_hours=raw.get("within_hours"),
        )

    def matches_event(self, event: Event) -> bool:
        return event.name == self.check or self.check in event.tags

    def conditions_met(self, event: Event, reference_time: datetime | None) -> bool:
        if self.where is not None and not _evaluate_where(self.where, event.payload):
            return False
        if self.within_hours is not None:
            event_time = _parse_time(event.timestamp)
            if event_time is None or reference_time is None:
                return False  # unverifiable timing is treated as stale
            age_hours = (reference_time - event_time).total_seconds() / 3600.0
            if age_hours > self.within_hours:
                return False
        return True


@dataclass
class Policy:
    """One declarative rule.

    Attributes:
        name: Stable identifier for the policy.
        requires: Requirements (strings or condition objects) that must
            each be satisfied by at least one observed event.
        trigger: When the policy applies. Supported keys:
            ``answer_mentions`` — list of substrings; the policy triggers
            when the final answer contains any of them (case-insensitive).
            ``always`` — boolean; the policy always applies.
            An empty trigger means ``always``.
        description: Human-readable intent.
    """

    name: str
    requires: list[Requirement | str]
    trigger: dict[str, Any] = field(default_factory=dict)
    description: str | None = None

    def __post_init__(self) -> None:
        self.requires = [Requirement.parse(r) for r in self.requires]

    def is_triggered(self, answer: str | None, events: list[Event]) -> bool:
        if not self.trigger or self.trigger.get("always"):
            return True
        mentions = self.trigger.get("answer_mentions", [])
        if mentions and answer:
            lowered = answer.lower()
            if any(term.lower() in lowered for term in mentions):
                return True
        return False

    def evaluate(self, answer: str | None, events: list[Event]) -> PolicyResult:
        triggered = self.is_triggered(answer, events)
        if not triggered:
            return PolicyResult(
                policy=self.name,
                description=self.description,
                triggered=False,
                passed=True,
            )

        reference_time = _reference_time(events)
        satisfied: list[str] = []
        missing: list[str] = []
        errored: list[str] = []
        unmet: list[str] = []
        for req in self.requires:
            # An event that recorded an error is an attempt, not a check:
            # it must never satisfy a requirement.
            ok_events = [
                e for e in events if req.matches_event(e) and not e.payload.get("error")
            ]
            if any(req.conditions_met(e, reference_time) for e in ok_events):
                satisfied.append(req.check)
            elif ok_events:
                unmet.append(req.check)
            elif any(
                req.matches_event(e) for e in events if e.payload.get("error")
            ):
                errored.append(req.check)
            else:
                missing.append(req.check)

        return PolicyResult(
            policy=self.name,
            description=self.description,
            triggered=True,
            passed=not missing and not errored and not unmet,
            satisfied=satisfied,
            missing=missing,
            errored=errored,
            unmet=unmet,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        return cls(
            name=data["name"],
            requires=list(data.get("requires", [])),
            trigger=data.get("trigger", {}),
            description=data.get("description"),
        )


class PolicyEngine:
    """Evaluates a set of policies against an assurance session."""

    def __init__(self, policies: list[Policy] | None = None):
        self.policies: list[Policy] = policies or []

    def add(self, policy: Policy) -> None:
        self.policies.append(policy)

    def evaluate(self, answer: str | None, events: list[Event]) -> list[PolicyResult]:
        return [p.evaluate(answer, events) for p in self.policies]

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "PolicyEngine":
        return cls([Policy.from_dict(p) for p in document.get("policies", [])])

    @classmethod
    def from_file(cls, path: str) -> "PolicyEngine":
        """Load a policy pack from a JSON or YAML file."""
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if path.endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "YAML policy files require pyyaml: pip install metaxu[yaml]"
                ) from exc
            document = yaml.safe_load(text)
        else:
            document = json.loads(text)
        return cls.from_document(document)


def _reference_time(events: list[Event]) -> datetime | None:
    """The moment `within_hours` is measured against: when the answer was
    given, falling back to the newest observed event."""
    answer_events = [e for e in events if e.type == EventType.ANSWER]
    candidates = answer_events or events
    times = [t for e in candidates if (t := _parse_time(e.timestamp)) is not None]
    return max(times) if times else None


def _parse_time(timestamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lookup(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _evaluate_where(where: dict[str, Any], payload: dict[str, Any]) -> bool:
    """All operators in the clause must hold; missing paths and type
    mismatches evaluate to False (conservative, never permissive)."""
    value = _lookup(payload, where.get("path", ""))
    if value is _MISSING:
        return False
    try:
        for op, expected in where.items():
            if op == "path":
                continue
            if op == "eq" and not value == expected:
                return False
            if op == "ne" and not value != expected:
                return False
            if op == "gt" and not value > expected:
                return False
            if op == "gte" and not value >= expected:
                return False
            if op == "lt" and not value < expected:
                return False
            if op == "lte" and not value <= expected:
                return False
            if op == "in" and value not in expected:
                return False
    except TypeError:
        return False
    return True
