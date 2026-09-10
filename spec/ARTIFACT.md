# Assurance Artifact Specification

**Version:** 2026.9.10 (draft)
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
| `schema_version` | yes | Version of this specification the document conforms to — see [Versioning](#versioning) below. |
| `id` | yes | Globally unique artifact identifier. |
| `created_at` | yes | ISO-8601 timestamp of artifact creation. |
| `question` | yes | The clinical question or task posed to the AI system. |
| `answer` | yes (nullable) | The final answer; `null` if the session ended without one. |
| `evidence` | yes | Evidence-link events: edges connecting claims to provenance records (the evidence graph). |
| `tool_trace` | yes | Tool-invocation events, in call order. |
| `provenance` | yes | One record per retrieved resource: source system, id, version, retrieval time, content hash, cache state. |
| `policy_checks` | yes | Result of each declarative policy: triggered, passed, satisfied/missing requirements. |
| `safety_checks` | yes | Findings from structural safety checks, each with a severity (`info`/`warning`/`critical`). |
| `terminology` | no | Validation results for clinical codes (SNOMED CT, LOINC, RxNorm, UCUM, ICD). Each carries `valid`, `status`, and the `terminology_version` it was checked against. See [terminology validation](#terminology-validation). |
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

The artifact encodes a traversable, multi-hop reasoning graph rather
than a text log:

```
question ──answered_by──> answer
answer   ──based_on────>  claim            (explicit, or implicit to all claims)
claim    ──supports────>  provenance record (resource)
claim    ──supports────>  claim             (intermediate reasoning steps)
resource ──retrieved_by─> tool call         (via retrieval parent_id)
resource ──has_coding──>  coding            (with its terminology validation)
```

The edges are carried by event payloads, so the graph is a **derived
view** — reconstructable by any consumer from `events` + `provenance`
alone (the reference implementation is `metaxu.graph.EvidenceGraph`;
`metaxu graph` renders it as a tree, JSON, Mermaid, or DOT):

- `evidence_link` events: `payload.claim_id` → `payload.provenance_ids`
  (resources) and `payload.claim_ids` (supporting claims), with a
  `relation` such as `supports` or `contradicts`.
- `answer` events: `payload.based_on_claim_ids` names the claims the
  answer explicitly rests on. When absent, consumers MAY infer edges
  from the answer to every claim but MUST mark them implicit —
  recorded reasoning is never conflated with inferred reasoning.
- `coding` events: `payload.provenance_id` links a code to the resource
  that carried it.
- `retrieval` events: `parent_id` links the retrieval to the tool call
  that performed it.

Claims without support edges are, by definition, unsupported — the
safety engine flags them. Resources retrieved but never cited by any
claim are reachable in the graph as orphans: data the AI looked at but
never used.

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
4. Merging requires identical `interaction_id`s and mutually compatible
   `schema_version`s (see [Versioning](#versioning) —
   `metaxu.artifact.SCHEMA_COMPATIBILITY`); anything else is an error,
   not a best effort.

## Terminology validation

Coded clinical values (a LOINC code for a lab, a SNOMED CT concept, an
RxNorm drug, a UCUM unit) are recorded as `coding` events and validated at
finalize. Each `terminology` entry carries:

- `system` (canonical: `LOINC`, `SNOMED-CT`, `RxNorm`, `UCUM`, `ICD-10-CM`),
  `code`, and the `display` the AI used;
- `valid` and `status` — `well-formed` (passed format/checksum but existence
  unverified), `malformed` (failed — likely hallucinated), `active`/`inactive`
  (a data-backed resolver confirmed the code exists / is retired), `unknown`,
  or `unvalidated`;
- **`terminology_version`** — the release the check consulted (e.g.
  `LOINC-2.78`), or `format-check` for the built-in algorithmic validator.

`terminology_version` is required because terminologies change: a code valid
when an artifact was produced may be retired later, so validating against
"whatever is current" would make a historical artifact look wrong and would
make re-validation non-deterministic — violating the reproducibility
guarantee. Recording the version consulted keeps terminology checks
auditable and replayable, exactly as `provenance.resource_version` does for
data. The design rationale (format-check vs. data-backed resolvers, and the
licensing constraints that keep SNOMED CT out of any bundled distribution)
is recorded in `docs/adr/0001-terminology-validation.md`.

Malformed codes produce a `critical` safety finding and lower the
`terminology_correctness` trust dimension (present only when codes were used).

## Versioning

- `schema_version` is **calendar-versioned** (`YYYY.M.D`, e.g. `2026.9.10`)
  starting with the release that introduced this section; earlier
  artifacts carry a semver value (`0.1.0`/`0.2.0`/`0.3.0`) from before the
  switch. See [ADR 0003](../docs/adr/0003-calendar-versioning.md) for why.
- Within a compatibility set, fields are only ever *added* (never removed
  or repurposed) — the same rule as before, just no longer expressed as a
  "major version" parsed out of the string, since a calendar date has no
  such structure. Compatibility is instead an explicit, code-maintained
  set of known-compatible version strings
  (`metaxu.artifact.SCHEMA_COMPATIBILITY`): every version in one set
  differs from every other only by additive change, spanning both the
  semver and CalVer eras until a genuinely breaking change ever requires
  starting a new set.
- Producers MUST set `schema_version`; consumers MUST reject an artifact
  whose `schema_version` isn't in a compatibility set they recognize,
  rather than guessing.

## Extensibility

- `metadata` is a free-form object for producer extensions. Namespaced
  keys (`"org.example/deployment-id"`) are recommended to avoid
  collisions.
- New event types, safety checks, policy trigger kinds, and trust
  dimensions may be introduced by producers; consumers MUST ignore ones
  they do not recognize rather than failing.
- The JSON Schema deliberately allows unknown top-level fields and
  unknown event types (they are documented, not enumerated), so schema
  validation is consistent with the tolerance rules above: a validator
  built against one `schema_version` accepts artifacts from any producer
  whose version falls in the same `SCHEMA_COMPATIBILITY` set (see
  [Versioning](#versioning)).

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
