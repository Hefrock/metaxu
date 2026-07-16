# ADR 0002: Adapter strategy — which boundaries to instrument next

**Status:** accepted (2026-07-16)
**Context owner:** durable record of which adapters Metaxu prioritizes and
why. Read before starting a new adapter under `src/metaxu/adapters/`.

## Context

Metaxu's core is transport-neutral: the event model and artifact are the
interoperability boundary, and an *adapter* attaches that core to one
interception point in an AI stack. The MCP proxy (`adapters/mcp.py`) is the
first, chosen because it proves the zero-code-change story. But MCP is one
vantage point, not the interface — and it is deliberately blind to the most
valuable half of an interaction: it sees tool calls and retrieved data, but
never the prompt, the model, the claims, or the final answer (those never
cross the MCP wire). Full assurance comes from *composing* observers
(`metaxu merge`), so the adapter roadmap should add vantage points that see
what MCP cannot, and that reach where healthcare AI actually runs.

## Interception boundaries considered

| Boundary | Sees | Blind to |
|---|---|---|
| MCP / tool layer *(shipped)* | tool calls, retrieved data | prompt, model, claims, answer |
| OpenTelemetry plane | whatever is instrumented; ties into existing traces | clinical semantics unless added |
| CDS Hooks / SMART on FHIR | the decision as it enters the EHR workflow | the model-internal reasoning |
| LLM API gateway | prompt, answer, model/version, tool-call intents | what tools returned from source systems |
| Agent-framework callbacks | everything, incl. claims | requires that framework; needs code |

## Decision — priority order

### 1. OpenTelemetry (build next)

Highest reach, lowest friction, strongest positioning. Hospitals and
enterprises already run OTel collectors, and the GenAI semantic conventions
(`gen_ai.*`) are emerging — Metaxu should *map onto* them, not compete.
Two directions:

- **Exporter** (Metaxu events → OTel spans): makes assurance visible in the
  observability tooling teams already have. The event model was shaped for
  this — `parent_id` ≈ parent span, tags ≈ attributes.
- **Importer** (OTel spans → assurance events): derives a partial artifact
  from existing instrumentation, adding a vantage point with no new
  recording code.

The founding vision names OpenTelemetry as the direct analogy for what
Metaxu is; shipping the bridge makes the analogy literal. Start with the
exporter (smaller, immediately useful), then the importer.

### 2. CDS Hooks / SMART on FHIR (highest clinical credibility)

The healthcare-native integration surface — what a CMIO recognizes and what
Epic/Cerner actually support. It sees the decision *at the point it enters
the clinical workflow*, the EHR-facing boundary no other adapter touches,
and it is the strongest validation of the "EHR-agnostic" claim. More work
than OTel (a real service implementing the hook request/prefetch/card
exchange, not a shim), and it needs a concrete integration target to
validate against — so it follows OTel rather than leading.

### 3. LLM API gateway (best composition proof)

A proxy in front of the model API that records the prompt, the answer, the
model and version, and tool-call intents — closing exactly the blind spot
the MCP proxy has. Pairing an MCP-proxy partial (tools) with a
gateway partial (answer) and merging them is the most concrete
demonstration of the composition thesis. Medium priority because provider
request/response schemas differ (OpenAI/Anthropic/…), so it carries
per-provider maintenance; scope the first version to one provider and the
`gen_ai` conventions.

### Deferred — agent-framework callbacks (LangChain / LlamaIndex / CrewAI)

Sees everything including claims, but requires the agent to use that
framework, needs code integration (registering a callback handler), and the
frameworks' APIs churn fast. The SDK decorators already serve the "willing
to touch code" case. Best handled opportunistically or as community
contributions, one framework at a time, not as core roadmap.

## Consequences

- Next build is the OTel exporter; it rides an existing standard and the
  codebase already leans toward it.
- Each new adapter should ship with a composition example (its partial
  merged with an MCP-proxy partial) so the multi-observer story is
  demonstrated, not just asserted.
- No adapter is privileged in the artifact: all produce partials that merge
  identically. This ordering is about sequencing effort, not architecture.
