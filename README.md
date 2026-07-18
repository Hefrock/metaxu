# Metaxu

**μεταξύ** (*metaxu*, pronounced **meh-TAX-oo**) — Greek for "between."

Metaxu is an AI assurance and provenance layer for healthcare —
model-agnostic, agent-agnostic, EHR-agnostic. It's not another healthcare
AI agent; it's the trust infrastructure that sits *between* AI systems
and clinical users, the way HTTPS sits between browsers and servers, or
OpenTelemetry sits between services and observability tooling.

Healthcare AI today lacks a standardized assurance layer providing
provenance, transparency, auditability, and trust. Metaxu is an open
framework for exactly that.

Instead of an AI system returning:

```
Answer
```

an instrumented system returns:

```
Answer + Assurance Artifact
```

The **Assurance Artifact** is a machine-readable record that lets any
consumer — a clinician, a dashboard, a CI pipeline, an auditor — answer:

- Where did this answer come from?
- What evidence supports it?
- Which tools were called?
- Which clinical policies were verified?
- What information was missing?
- Can this result be reproduced?
- Should a clinician trust it?

## Status

`0.3.0` — a working reference SDK with every core component from the
founding vision implemented (provenance, policy, safety, trust,
terminology, evidence graph, correlation/merge, governance, drift,
replay). APIs and the artifact schema may still change before `1.0`.
Feedback and design discussion are the point of publishing this early.
See [CHANGELOG.md](CHANGELOG.md) for the release history and
[RELEASING.md](RELEASING.md) for how releases are cut.

## What's here

| Path | What it is |
|---|---|
| [`spec/ARTIFACT.md`](spec/ARTIFACT.md) | The Assurance Artifact specification (fields, versioning, extensibility, PHI notes) |
| [`spec/EVENT_MODEL.md`](spec/EVENT_MODEL.md) | The event model everything derives from |
| [`src/metaxu/spec/assurance-artifact.schema.json`](src/metaxu/spec/assurance-artifact.schema.json) | JSON Schema (draft 2020-12) for the artifact |
| [`src/metaxu/`](src/metaxu/) | Reference Python SDK — stdlib-only core, zero required dependencies |
| [`examples/anticoagulation/`](examples/anticoagulation/) | End-to-end demo: two agents (diligent vs. careless) over synthetic FHIR data |
| [`tests/`](tests/) | Test suite, including the demo as a benchmark scenario |
| [`docs/adr/`](docs/adr/) | Architecture decision records (terminology strategy, adapter roadmap) |
| [`docs/USE_CASES.md`](docs/USE_CASES.md) | Five hands-on use cases, each runnable from a fresh clone |

## Quick start

```bash
pip install metaxu           # stdlib-only core, zero required dependencies
# or, from a clone, for development:
pip install -e ".[dev]" && pytest

# Run the demo: same question, two agents, two very different artifacts
python examples/anticoagulation/run_demo.py
metaxu inspect examples/anticoagulation/out/careless-artifact.json
```

The careless agent recommends warfarin without checking allergies, renal
function, or pregnancy status, and asserts "renal function is normal"
without ever retrieving it. Its artifact says so:

```
Policy checks (2):
  - before_anticoagulation: FAIL (missing: allergy_check, pregnancy_status, creatinine)
  - answer_must_cite_patient_record: PASS

Safety findings (1):
  - [critical] unsupported_claims: Claim has no linked evidence: Renal function is normal.

Trust dimensions:
  - policy_compliance: 0.50  1 of 2 triggered policies passed.
  - provenance_coverage: 0.50  1 of 2 claims are linked to retrieved evidence.
  - safety: 0.00  1 critical and 0 warning safety findings.
  ...
```

## Instrumenting an agent

Adopting Metaxu should feel like adding logging — decorate the tools the
agent already has, record claims and evidence, get an artifact:

```python
from metaxu import AssuranceSession, PolicyEngine, ProvenanceRecord, assured_tool

@assured_tool(tags=["platelet_count"], version="fhir-tools/1.2.0")
def get_platelet_count(patient_id: str) -> dict:
    resource = fhir.read("Observation", ...)   # your existing code
    return resource

engine = PolicyEngine.from_file("policies.json")

with AssuranceSession(question=question, policy_engine=engine) as session:
    obs = get_platelet_count("pat-001")            # traced automatically
    prov = session.record_retrieval(
        ProvenanceRecord.for_resource(
            source_system=fhir.base_url,
            resource_type="Observation",
            resource_id=obs["id"],
            content=obs,
        )
    )
    claim = session.record_claim("Platelet count is adequate.")
    session.link_evidence(claim, [prov])
    session.set_answer(answer)

session.artifact.save("artifact.json")
```

## Zero-code instrumentation: the MCP proxy

For MCP-based workflows you don't need to touch agent code at all. Wrap
any MCP stdio server with the assurance proxy — a config change, not a
code change:

```jsonc
// in your MCP client configuration, instead of running the server directly:
{
  "command": "metaxu",
  "args": ["mcp-proxy", "--out", "artifacts/",
           "--tags", "tags.json", "--policies", "policies.json",
           "--", "my-fhir-mcp-server", "--their", "args"]
}
```

The proxy forwards JSON-RPC byte-for-byte (recording can never drop or
mutate a message) while capturing every `tools/call` — name, arguments,
result summary, errors, timing — plus content hashes and snapshots of
everything retrieved, and the client/server/protocol versions for
reproducibility. A `tags.json` file (`{"get_allergies": ["allergy_check"]}`)
maps tool names to policy tags so institutional policies evaluate against
any server's tool vocabulary. An artifact is written when the session ends.

A transparent proxy can't see claims, evidence links, or the final answer
— those never cross the MCP wire. The proxy is the zero-effort floor;
SDK instrumentation is the ceiling.

## OpenTelemetry export

Assurance doesn't have to live in its own silo. The OpenTelemetry
exporter turns an artifact into a span tree, so an assurance session shows
up wherever a team already sends traces (`pip install metaxu[otel]`):

```python
from metaxu.adapters.otel import export_artifact
export_artifact(artifact, tracer=my_tracer)     # into your existing TracerProvider
```
```bash
metaxu otel artifact.json                        # print spans to the console
metaxu otel artifact.json --endpoint http://localhost:4318/v1/traces
```

One root span per interaction carries the model, trust dimensions, and
policy/safety/terminology roll-ups; tool calls and retrievals become child
spans (timed by their recorded durations); claims, policy checks, and
findings become span events. The root span's **status is ERROR** when the
interaction has a critical safety finding, a failed policy, or a broken
integrity hash — so an assurance regression trips the same alerting a
latency spike would. Attributes use OpenTelemetry's `gen_ai.*` conventions
where they fit and a `metaxu.*` namespace otherwise.

**PHI note:** question, answer, and claim *text* are omitted by default —
observability backends are a different trust boundary than the artifact
store. Pass `capture_content=True` (or `--capture-content`) only when the
destination is authorized for PHI.

The exporter is the first of the adapter roadmap in
[ADR 0002](docs/adr/0002-adapter-strategy.md); the core stays stdlib-only,
so OpenTelemetry is an optional extra imported lazily.

## CDS Hooks: assurance at the EHR boundary

[CDS Hooks](https://cds-hooks.hl7.org/) is how an EHR calls a
decision-support service at workflow moments (order-sign, patient-view)
and renders the returned cards. The adapter wraps one hook invocation in
an assurance session — stdlib-only, framework-agnostic (plain dicts, so
it drops into Flask/FastAPI/Functions):

```python
from metaxu.adapters.cdshooks import assured_cds_service

@assured_cds_service(policy_engine=engine, tag_map={"platelets": ["platelet_count"]},
                     artifact_dir="artifacts/", add_assurance_card=True)
def my_service(request, session):
    ...                       # your logic; record claims/evidence as usual
    return [{"summary": "No contraindication found", "indicator": "info"}]
```

`prefetch` resources become hashed provenance with their codings
validated; draft orders in `context` get their codes checked (a
hallucinated RxNorm code on a proposed order is exactly what terminology
validation exists to catch); `hookInstance` becomes the correlation
`interaction_id`; the cards become the recorded answer. The response
carries a `dev.metaxu` extension (artifact id + assurance summary), and
with `add_assurance_card=True` a visible warning card is appended when
checks fail — so the assurance verdict reaches the clinician in the EHR,
not just the audit log. The request's `fhirAuthorization` bearer token is
never recorded. See `examples/cdshooks/`.

## Composing observers: correlation and merge

No single interception point sees a whole interaction, so artifacts are
designed to be **assembled from multiple observers**. Every observer of
one interaction shares an `interaction_id` (set `METAXU_INTERACTION_ID`
in the environment, or pass `interaction_id=`/`--interaction-id`), each
produces a *partial* artifact, and:

```bash
metaxu merge sdk-artifact.json proxy-artifact.json -o merged.json --policies policies.json
```

produces one *merged* artifact. A merge is a **re-evaluation, not a
concatenation**: events and provenance are unioned, then the policy,
safety, and trust engines run again over the combined observations — so a
policy that failed on every partial view (the proxy never saw the
platelet check; the SDK session never saw the allergy tool) can rightly
pass on the merged view. Conflicting observations are never silently
resolved; they're preserved in `metadata["dev.metaxu/merge_conflicts"]`.

MCP is one adapter, not the interface: the core is transport-neutral, and
adapters (`metaxu.adapters`) attach it to specific boundaries — MCP
today; OpenTelemetry, CDS Hooks, and LLM gateways are the planned next
vantage points.

Policies are declarative data, shareable across institutions:

```json
{
  "policies": [{
    "name": "before_anticoagulation",
    "trigger": {"answer_mentions": ["warfarin", "heparin", "apixaban"]},
    "requires": [
      "allergy_check",
      "pregnancy_status",
      "creatinine",
      {
        "check": "platelet_count",
        "where": {"path": "result_summary.valueQuantity.value", "gte": 50},
        "within_hours": 48
      }
    ]
  }]
}
```

A requirement is a plain string ("this check occurred") or an object with
conditions: `where` evaluates a dotted path into the matching event's
payload (`eq`/`ne`/`gt`/`gte`/`lt`/`lte`/`in`), and `within_hours`
requires the check to be no older than N hours *at the time the answer
was given* — "used the newest labs", not just "used some labs". Results
distinguish four outcomes per requirement: `satisfied`, `missing` (never
attempted), `errored` (attempted, every attempt failed), and `unmet`
(performed, but the value or timing failed the condition) — a platelet
check that came back too low is not a passed platelet check.

## Architecture

```
Healthcare AI  (chatbot / agent / MCP workflow / RAG pipeline / rule engine)
      │
      ▼
AssuranceSession ──── events: tool calls, retrievals, claims, evidence links
      │
      ├── Provenance engine    every resource: source, version, time, hash
      ├── Policy engine        declarative "these checks must have occurred"
      ├── Safety engine        structural checks: unsupported claims,
      │                        hallucinated resources, ignored allergies, …
      ├── Terminology engine   SNOMED/LOINC/RxNorm/UCUM/ICD code validation
      └── Trust engine         multiple dimensions, never one score
      │
      ▼
Assurance Artifact  ──►  clinician / dashboard / CI pipeline / auditor
      │
      ▼
metaxu CLI: inspect · validate · verify (provenance re-hashing / drift)
```

Design commitments:

- **The artifact is the interface.** Everything downstream consumes the
  artifact, never the AI system.
- **Instrumentation is never load-bearing.** Decorated tools behave
  identically outside a session; adopting Metaxu cannot change clinical
  behavior.
- **No single trust score.** Trust is reported per-dimension with a
  rationale and the inputs it was computed from — auditable, not oracular.
- **Symbolic verification of neural output.** LLMs generate hypotheses;
  policies, terminologies, and hashes verify them. The novelty is
  assurance, not reasoning.
- **Stdlib-only core.** `pip install metaxu` drags in nothing; YAML
  policies and full JSON Schema validation are optional extras.

## CLI

```bash
metaxu inspect  artifact.json                     # human-readable summary
metaxu validate artifact.json                     # JSON Schema validation
metaxu verify   artifact.json --snapshots dir/    # integrity + provenance re-hashing
metaxu mcp-proxy --out dir/ -- <server cmd>       # wrap an MCP server transparently
metaxu merge a.json b.json -o merged.json         # combine observers of one interaction
metaxu report artifacts/ [--json | --html dash.html]  # governance metrics over a store
metaxu drift baseline/ current/ [--fail-on-drift] # what changed between two cohorts
metaxu diff original.json replay.json             # compare two runs of one interaction
metaxu replay artifact.json --runner mod:fn       # re-run the workflow and diff it
metaxu graph artifact.json [--format mermaid]     # render the reasoning chain
```

`verify` recomputes content hashes of the resources the AI saw. A
mismatch means the source data changed since the decision was made —
exactly the drift a reviewing clinician needs to know about.

`replay` re-runs the workflow for an artifact's question — the
`--runner module:function` entrypoint receives `(question, session)` and
should wire its data access to the recorded snapshots rather than live
sources — then diffs the result against the original. `reproduced: YES`
requires the answer, tool-call sequence (names *and* arguments), claim
set, policy outcomes, and evidence base (same resources, same content
hashes) to all match; a replay that saw different data is not a
reproduction even if the answer text came out the same. `diff` runs the
same comparison on any two existing artifacts. Exit codes make both
usable in CI.

## Governance: from artifacts to oversight

Individual artifacts answer "should a clinician trust *this* answer?".
`metaxu report` answers the institutional questions over a whole
artifact store: per-dimension trust (never collapsed into one number),
policy pass rates with the most-unsatisfied requirements, hallucination
and unsupported-claim rates, tool reliability, most-missed data, and a
triage list of artifacts needing human review (critical findings, failed
policies, integrity failures).

```bash
metaxu report artifacts/                    # terminal summary
metaxu report artifacts/ --json             # machine-readable, for pipelines
metaxu report artifacts/ --html dash.html   # self-contained HTML dashboard
metaxu report artifacts/ --fail-on-review   # CI gate: exit 1 if anything needs review
```

The input is just a directory of artifacts — any producer's artifacts
aggregate identically, because the artifact is the interoperability
boundary. Reports may quote clinical questions, so handle the output
under the same PHI controls as the artifacts themselves.

`metaxu drift` asks the longitudinal question — what changed between a
baseline cohort and the current one? It detects **environment drift**
(model/prompt/tool/MCP-server versions that appeared or disappeared),
**behavioral drift** (regressions in trust dimensions, policy pass
rates, tool error rates, hallucination rates — improvements are reported
but never flagged), **answer drift** (the same question answered
differently), and **source drift** (the same resource carrying a
different content hash — the record itself changed). Keep artifacts in
dated directories and compare month over month, or run a benchmark
before and after a deploy with `--fail-on-drift` as the release gate.

## The evidence graph

"Where did this answer come from?" has a structural answer, not just a
log. Every artifact encodes a traversable reasoning graph — question →
answer → claims (including claim-on-claim reasoning steps) → resources →
codings — and `metaxu graph` renders it:

```
? Patient pat-001 has new-onset atrial fibrillation. Is it appropriate …
│
★ Anticoagulation (e.g. apixaban) appears appropriate…
├─ • No contraindication to anticoagulation identified. [based_on]
│  ├─ • Platelet count is 232 10*3/uL (adequate). [supports]
│  │  └─ ▤ Observation/obs-plt-9001 [supports]
│  │     ├─ # LOINC 777-3 [has_coding]
│  │     └─ # UCUM 10*3/uL [has_coding]
│  └─ …creatinine, allergies…
└─ • Guideline recommends oral anticoagulation for nonvalvular AF… [based_on]
   └─ ▤ PlanDefinition/guideline-af-anticoag [supports]
      └─ # SNOMED-CT 49436004 [has_coding]

Evidence not connected to the answer:
  ▤ Patient/pat-001
```

Record the linkage with the same session API: `link_evidence` accepts
claims as well as resources (multi-hop chains), and
`set_answer(..., based_on=[claims])` names what the answer actually
rests on — omit it and the graph connects the answer to every claim but
marks those edges *implicit*, so recorded reasoning is never conflated
with inferred. The graph is a derived view over the event stream (no
schema change; any consumer can rebuild it), and it works in reverse
too: `metaxu graph artifact.json --dependents obs-plt-9001` answers
*"this lab was corrected — what rests on it?"* Output formats: text
tree, JSON, Mermaid, DOT.

## Terminology validation

Clinical codes the AI cites — a LOINC code for a lab, a SNOMED CT concept,
an RxNorm drug, a UCUM unit — are recorded as codings and validated at
finalize. The built-in `FormatResolver` needs no data and no license: it
checks shape and **check digits** (LOINC Luhn mod-10, SNOMED CT Verhoeff)
to catch **hallucinated or malformed** codes. A malformed code becomes a
`critical` safety finding and lowers the `terminology_correctness` trust
dimension.

```python
session.record_coding("http://loinc.org", "2160-0", "Creatinine")
session.record_codings_from(fhir_observation)   # or extract from a FHIR resource
```

Confirming a code is the *right, active* one for the claim needs the real
code tables, which institutions supply through the `TerminologyResolver`
interface (`resolve(system, code) -> CodeValidation`) — Metaxu ships the
check logic, never the data. Every result carries the
`terminology_version` it was checked against (`format-check`, or e.g.
`LOINC-2.78`), because terminologies change: validating a historical
artifact against "whatever's current" would make a since-retired code look
like a hallucination and make re-validation non-deterministic. The full
strategy — bundling constraints, why SNOMED CT is never redistributed, and
the versioning discipline — is recorded in
[ADR 0001](docs/adr/0001-terminology-validation.md).

## Roadmap

- [x] MCP proxy that instruments any MCP server transparently
- [x] Multi-observer correlation and artifact merging
- Adapter roadmap (priority order in [ADR 0002](docs/adr/0002-adapter-strategy.md)):
  - [x] OpenTelemetry **exporter** (artifact → spans, `gen_ai.*` conventions, PHI-safe by default)
  - [x] CDS Hooks adapter (assured decision-support services; prefetch → provenance, cards → answer)
  - [ ] OpenTelemetry importer (spans → assurance events)
  - [ ] CDS Hooks transparent proxy variant (evaluate third-party services you don't control)
  - [ ] LLM API gateway adapter (closes the answer/claims blind spot without SDK adoption)
- [ ] Detached-signature envelope for artifact authentication
- [x] Terminology validation — format/checksum (SNOMED / LOINC / RxNorm / UCUM / ICD) + pluggable resolver interface ([ADR 0001](docs/adr/0001-terminology-validation.md))
- [x] Evidence graph as a traversable structure (multi-hop chains, dependents tracing, Mermaid/DOT export)
- [ ] Terminology data backends: bundled LOINC/RxNorm, SNOMED local-build (see ADR 0001)
- [ ] Temporal-reasoning checks (newest labs, discontinued medications)
- [x] Governance reporting and dashboard over artifact collections
- [x] Drift detection between artifact cohorts (environment, behavior, answers, sources)
- [x] Replay harness: re-run a recorded interaction and diff it against the original
- [ ] Benchmark scenario pack with reference artifacts
- [ ] Policy pack sharing/extension model across institutions

## A note on the data

All patient data in this repository is synthetic. Real artifacts may
contain PHI and must be handled under the same controls as the clinical
record itself — see the PHI section of [`spec/ARTIFACT.md`](spec/ARTIFACT.md).

## License

[Apache License 2.0](LICENSE). Chosen to match how comparable open
standards are licensed (OpenTelemetry, CycloneDX, the OpenAPI
Specification): permissive enough for commercial EHR/AI vendors to
embed the SDK and adapters, with an explicit patent grant and
retaliation clause, and — per the [NOTICE](NOTICE) file — no grant of
rights to the "Metaxu" name itself.
