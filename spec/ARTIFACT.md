# Assurance Artifact Specification

**Version:** 0.2.0 (draft)
**Schema:** [`src/metaxu/spec/assurance-artifact.schema.json`](../src/metaxu/spec/assurance-artifact.schema.json)

## Purpose

Every AI-mediated clinical interaction produces exactly one **Assurance
Artifact**: a machine-readable record that lets any downstream consumer —
a clinician, a dashboard, a CI pipeline, an auditor — answer:

- Where did this answer come from?
- What evidence supports it?
- Which tools were called, with which arguments?
- Which clinical policies were verified, and did they pass?
- What information was missing?
- Can this result be reproduced?
- Should a clinician trust it?

The artifact is the interoperability boundary of the whole ecosystem.
Producers (SDKs, agent frameworks, MCP proxies) emit it; consumers never
need to know how the underlying AI system works.

## Top-level fields

| Field | Required | Description |
|---|---|---|
| `schema_version` | yes | Semver of this specification the document conforms to. |
| `id` | yes | Globally unique artifact identifier. |
| `created_at` | yes | ISO-8601 timestamp of artifact creation. |
| `question` | yes | The clinical question or task posed to the AI system. |
| `answer` | yes (nullable) | The final answer; `null` if the session ended without one. |
| `evidence` | yes | Evidence-link events: edges connecting claims to provenance records (the evidence graph). |
| `tool_trace` | yes | Tool-invocation events, in call order. |
| `provenance` | yes | One record per retrieved resource: source system, id, version, retrieval time, content hash, cache state. |
| `policy_checks` | yes | Result of each declarative policy: triggered, passed, satisfied/missing requirements. |
| `safety_checks` | yes | Findings from structural safety checks, each with a severity (`info`/`warning`/`critical`). |
| `missing_data` | no | Required information that could not be obtained, with reasons. |
| `trust_scores` | yes | Named trust dimensions, each `{score, rationale, inputs}`. **Never aggregated into a single number.** |
| `reproducibility` | no | Model, prompt, tool, and runtime versions needed to attempt replay. |
| `metadata` | no | Producer-defined extension point (see Extensibility). |
| `correlation` | no | Ties observers of one interaction together: `interaction_id`, `observer`, `role`, `merged_from` (see Correlation and merging). |
| `events` | yes | The complete ordered event stream the artifact was derived from (see [EVENT_MODEL.md](EVENT_MODEL.md)). |
| `artifact_hash` | no | `sha256:` over the canonical JSON of every other field; enables tamper detection without key material. |

### Derived views

`evidence`, `tool_trace`, `policy_checks`, and `safety_checks` are
*projections* of `events` provided for consumer convenience. `events` is
the source of truth; a consumer that needs guarantees should recompute the
projections from it.

### The evidence graph

The artifact encodes a graph rather than a text log:

```
question ──> claim ──evidence_link──> provenance record ──> source system
                └──── (claims without an evidence_link are, by definition,
                       unsupported — the safety engine flags them)
```

Nodes are `claim` events and `provenance` records; edges are
`evidence_link` events (`payload.claim_id` → `payload.provenance_ids`,
with a `relation` such as `supports` or `contradicts`).

### Trust dimensions

`trust_scores` maps a dimension name to `{score ∈ [0,1], rationale,
inputs}`. Core structural dimensions produced by the reference SDK:

- `provenance_coverage` — fraction of claims linked to retrieved evidence
- `policy_compliance` — fraction of triggered policies that passed
- `safety` — degraded by warning/critical findings
- `data_completeness` — degraded by reported missing data
- `data_freshness` — retrieval age against a configurable horizon

Producers may add domain dimensions (e.g. `terminology_correctness`).
Consumers MUST tolerate unknown dimensions and MUST NOT synthesize a
single aggregate score when presenting to clinicians.

## Correlation and merging

No single interception point sees a whole interaction. An MCP proxy sees
tool calls but not claims or the answer; an SDK-instrumented agent sees
claims; an LLM gateway sees the answer. The artifact is therefore
designed to be assembled from **multiple observers**, not just produced
by one:

- Every observer of one interaction stamps its artifact with the same
  `correlation.interaction_id` (producers typically propagate it via the
  `METAXU_INTERACTION_ID` environment variable across process
  boundaries).
- `correlation.observer` names the vantage point (`sdk`, `mcp-proxy`,
  `metaxu.merge`, …). `correlation.role` is `partial` for every
  single-observer artifact — a single vantage point is by definition a
  partial view — and `merged` for artifacts assembled from partials,
  which also list their inputs in `correlation.merged_from`.

**Merge semantics.** A merge is a *re-evaluation, not a concatenation*:

1. Event streams are unioned (deduplicated by event id, ordered by
   timestamp); provenance and `missing_data` are unioned by identity.
2. Policy, safety, and trust engines run again over the combined
   observational events (each partial's own `policy_check`/`safety_check`
   events are kept as history but excluded as engine inputs). A policy
   that failed on every partial view may rightly pass on the merged view
   — that is the point of composing observers.
3. Scalar conflicts (two observers recording different answers) are
   never silently resolved: the first non-null value in merge order wins
   and every losing value is preserved under
   `metadata["dev.metaxu/merge_conflicts"]` with its source observer.
4. Merging requires identical `interaction_id`s and the same major
   schema version; anything else is an error, not a best effort.

## Versioning

- The spec follows **semver**. Within a major version, fields are only
  ever *added* (never removed or repurposed), so a `0.x`/`1.x` consumer
  can read any artifact of the same major version.
- Producers MUST set `schema_version`; consumers MUST reject artifacts
  with a higher major version than they understand.

## Extensibility

- `metadata` is a free-form object for producer extensions. Namespaced
  keys (`"org.example/deployment-id"`) are recommended to avoid
  collisions.
- New event types, safety checks, policy trigger kinds, and trust
  dimensions may be introduced by producers; consumers MUST ignore ones
  they do not recognize rather than failing.
- The JSON Schema deliberately allows unknown top-level fields and
  unknown event types (they are documented, not enumerated), so schema
  validation is consistent with the tolerance rules above: a 0.x
  validator accepts artifacts from any later 0.x producer.

## Integrity

`artifact_hash` is `sha256` over the canonical JSON serialization
(sorted keys, `,`/`:` separators) of the artifact with the hash field
removed. It detects tampering and truncation. It is **not** a signature —
a future revision will define an optional detached-signature envelope for
producer authentication.

## PHI considerations

Artifacts may contain PHI (the question, answer, and claims typically
reference patient data). Artifacts MUST be stored and transmitted under
the same controls as the clinical record itself. Producers SHOULD prefer
recording resource references + hashes over embedding full resource
contents; the snapshot mechanism (see `metaxu.replay`) keeps full contents
in a separately controlled store.
