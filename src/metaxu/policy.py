"""Declarative clinical policy engine.

Policies state which checks must have occurred before an AI system is
allowed to make a class of recommendation — the clinical analogue of a
linter rule. They are data, not code, so institutions can share a policy
pack and locally extend it.

A policy is satisfied by *observed events*: a requirement matches when any
event in the session carries the requirement string as its ``name`` or as
one of its ``tags``. This keeps policies decoupled from any particular
agent framework — instrumented tools simply tag the checks they perform.

Policy documents are JSON natively; YAML is supported when ``pyyaml`` is
installed (``pip install metaxu[yaml]``).

Example policy document::

    {
      "policies": [
        {
          "name": "before_anticoagulation",
          "description": "Checks required before recommending anticoagulation.",
          "trigger": {"answer_mentions": ["warfarin", "heparin", "apixaban"]},
          "requires": [
            "allergy_check",
            "platelet_count",
            "pregnancy_status",
            "creatinine"
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .events import Event


@dataclass
class PolicyResult:
    """Outcome of evaluating one policy against a session.

    ``errored`` distinguishes "the check was attempted but failed" from
    "the check never happened" (``missing``): a requirement lands there
    when every event matching it carries an error. Neither satisfies the
    policy — a failed allergy check is not an allergy check.
    """

    policy: str
    triggered: bool
    passed: bool
    satisfied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errored: list[str] = field(default_factory=list)
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
        }


@dataclass
class Policy:
    """One declarative rule.

    Attributes:
        name: Stable identifier for the policy.
        requires: Requirement strings that must each match at least one
            observed event (by name or tag).
        trigger: When the policy applies. Supported keys:
            ``answer_mentions`` — list of substrings; the policy triggers
            when the final answer contains any of them (case-insensitive).
            ``always`` — boolean; the policy always applies.
            An empty trigger means ``always``.
        description: Human-readable intent.
    """

    name: str
    requires: list[str]
    trigger: dict[str, Any] = field(default_factory=dict)
    description: str | None = None

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
        observed: set[str] = set()
        observed_errored: set[str] = set()
        for event in events:
            # An event that recorded an error is an attempt, not a check:
            # it must never satisfy a requirement.
            target = observed_errored if event.payload.get("error") else observed
            target.add(event.name)
            target.update(event.tags)
        satisfied = [r for r in self.requires if r in observed]
        errored = [r for r in self.requires if r not in observed and r in observed_errored]
        missing = [r for r in self.requires if r not in observed and r not in observed_errored]
        return PolicyResult(
            policy=self.name,
            description=self.description,
            triggered=True,
            passed=not missing and not errored,
            satisfied=satisfied,
            missing=missing,
            errored=errored,
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
