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

`0.1.0` — early draft of the spec and a working reference SDK. APIs and
the artifact schema will change. Feedback and design discussion are the
point of publishing this early.

## What's here

| Path | What it is |
|---|---|
| [`spec/ARTIFACT.md`](spec/ARTIFACT.md) | The Assurance Artifact specification (fields, versioning, extensibility, PHI notes) |
| [`spec/EVENT_MODEL.md`](spec/EVENT_MODEL.md) | The event model everything derives from |
| [`src/metaxu/spec/assurance-artifact.schema.json`](src/metaxu/spec/assurance-artifact.schema.json) | JSON Schema (draft 2020-12) for the artifact |
| [`src/metaxu/`](src/metaxu/) | Reference Python SDK — stdlib-only core, zero required dependencies |
| [`examples/anticoagulation/`](examples/anticoagulation/) | End-to-end demo: two agents (diligent vs. careless) over synthetic FHIR data |
| [`tests/`](tests/) | Test suite, including the demo as a benchmark scenario |

## Quick start

```bash
pip install -e ".[dev]"
pytest

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
```

`verify` recomputes content hashes of the resources the AI saw. A
mismatch means the source data changed since the decision was made —
exactly the drift a reviewing clinician needs to know about.

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

## Roadmap

- [x] MCP proxy that instruments any MCP server transparently
- [x] Multi-observer correlation and artifact merging
- [ ] OpenTelemetry adapter (events ↔ spans; map to GenAI semantic conventions)
- [ ] CDS Hooks / SMART on FHIR adapter (the healthcare-native decision surface)
- [ ] LLM API gateway adapter (closes the answer/claims blind spot without SDK adoption)
- [ ] Detached-signature envelope for artifact authentication
- [ ] Terminology validation checks (SNOMED / LOINC / RxNorm / UCUM)
- [ ] Temporal-reasoning checks (newest labs, discontinued medications)
- [x] Governance reporting and dashboard over artifact collections
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
