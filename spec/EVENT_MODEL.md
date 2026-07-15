# Event Model

**Version:** 0.2.0 (draft)

Everything Metaxu asserts about an interaction derives from an ordered
stream of **events**. The model is deliberately minimal so that any
process — an agent framework, an MCP proxy, a rule engine, a second agent
in a multi-agent workflow — can participate by appending JSON objects.

## Event shape

```json
{
  "id": "evt-…",
  "type": "tool_invocation",
  "name": "get_platelet_count",
  "timestamp": "2026-07-15T02:00:00.000000+00:00",
  "payload": { "arguments": {"patient_id": "pat-001"}, "result_summary": "…" },
  "tags": ["platelet_count", "lab"],
  "parent_id": null
}
```

- **`id`** — unique within the artifact; other events reference it.
- **`type`** — one of the vocabulary below.
- **`name`** — short stable identifier (tool name, policy name, resource
  reference). Matched by the policy engine.
- **`payload`** — type-specific detail (see below).
- **`tags`** — free-form labels, also matched by the policy engine. This
  is the hinge between instrumentation and policy: a tool tagged
  `allergy_check` satisfies a policy requirement `allergy_check` without
  the policy knowing the tool's name.
- **`parent_id`** — the event that caused this one. Enables trace trees
  and lets provenance survive multi-agent workflows: a sub-agent's events
  parent onto the delegation event that spawned it.

## Event vocabulary

| Type | Emitted when | Key payload fields |
|---|---|---|
| `question` | A session opens | `text` |
| `tool_invocation` | Any tool/function/MCP call completes | `arguments`, `result_summary`, `error`, `duration_ms` |
| `retrieval` | A resource is fetched from a source of record | `provenance_id`, `source_system` |
| `claim` | The AI asserts an intermediate fact | `text` |
| `evidence_link` | A claim is connected to retrieved resources | `claim_id`, `provenance_ids`, `relation` |
| `policy_check` | The policy engine evaluates a policy (at finalize) | `policy`, `triggered`, `passed`, `missing` |
| `safety_check` | The safety engine reports a finding (at finalize) | `check`, `severity`, `message` |
| `missing_data` | Required information could not be obtained | `item`, `reason` |
| `answer` | The final answer is recorded | `text` |
| `note` | Free-form annotation (also usable to satisfy policy tags) | `text` |

Unknown event types MUST be preserved and ignored by consumers, not
rejected — this is how the vocabulary grows without breaking readers.

## Ordering and time

Events appear in the artifact in emission order. `timestamp` is
informational (clock skew across agents is expected); ordering guarantees
come from list position within one artifact, and from `parent_id` links
across producers.

## Multiple observers

Event ids are globally unique, so streams recorded by different observers
of the same interaction (an MCP proxy, an SDK session, a gateway) can be
unioned without collision. Observers correlate via the artifact's
`correlation.interaction_id`; see the *Correlation and merging* section
of [ARTIFACT.md](ARTIFACT.md). When merged, streams are deduplicated by
event id and ordered by timestamp — which is why timestamps, though
informational, SHOULD be honest UTC.

## Relationship to OpenTelemetry

The model is intentionally close to a span tree (`parent_id` ≈ parent
span) so an OTel exporter can be written as a thin adapter: events become
spans/span-events, tags become attributes. The reverse mapping — deriving
assurance events from existing OTel instrumentation — is a planned
integration path.
