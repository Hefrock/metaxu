# Five use cases you can test today

Each of these runs entirely from a fresh clone with `pip install -e ".[dev]"`
— synthetic data only, no external services, no credentials. Every command
shown is real; the *"what to look for"* notes tell you whether Metaxu did
its job.

---

## 1. Catch a hallucinated drug code at order signing (CDS Hooks)

**The scenario:** an AI-backed decision-support service reviews a warfarin
order inside the EHR's order-sign workflow. One invocation is complete;
the other is missing the allergy prefetch **and** carries a malformed
RxNorm code — the classic "the model invented a code" failure.

```bash
python examples/cdshooks/run_demo.py
metaxu report examples/cdshooks/out
```

**What to look for:** the `careless` invocation gets a second, visible
card — *"Assurance checks did not pass for this recommendation"* — plus
`critical=1`, `failed_policies=['before_anticoagulation']`, and
`malformed_codes=1` in the response extension. Then `metaxu inspect` the
flagged artifact: the malformed `WARF-99` code appears as a critical
`malformed_terminology` finding. The bearer token from the request is
nowhere in the artifact (the demo asserts this).

---

## 2. Prove — or disprove — that an AI recommendation followed policy

**The scenario:** two agents answer the same anticoagulation question.
One performs every required check; one skips allergies/renal function and
asserts "renal function is normal" without ever retrieving it.

```bash
python examples/anticoagulation/run_demo.py
metaxu inspect examples/anticoagulation/out/diligent-artifact.json
metaxu inspect examples/anticoagulation/out/careless-artifact.json
metaxu diff examples/anticoagulation/out/diligent-artifact.json \
            examples/anticoagulation/out/careless-artifact.json
```

**What to look for:** the careless artifact shows
`before_anticoagulation: FAIL (missing: allergy_check, pregnancy_status,
creatinine)` and a critical `unsupported_claims` finding, with trust
dimensions degraded — while the diligent one is clean. `metaxu diff`
localizes exactly which tools were skipped and which policy outcome
flipped.

---

## 3. Answer "where did this answer come from?" — and "this lab changed, what's affected?"

**The scenario:** a clinician (or auditor) wants the reasoning chain
behind a recommendation, and separately, a lab result was corrected after
the fact — what decisions rested on it?

```bash
metaxu graph examples/anticoagulation/out/diligent-artifact.json
metaxu graph examples/anticoagulation/out/diligent-artifact.json --dependents obs-plt-9001
metaxu graph examples/anticoagulation/out/diligent-artifact.json --format mermaid
```

**What to look for:** a tree from the answer down through claims →
FHIR resources → validated LOINC/SNOMED codes, including the guideline
(`PlanDefinition`) the recommendation cites — the multi-hop chain, not a
flat log. The `--dependents` query lists every claim and the answer that
transitively rest on the platelet observation. Note `Patient/pat-001`
listed as *retrieved but never cited* — data the AI looked at but never
used, surfaced honestly.

---

## 4. Detect a silent change — model swap, data edit, flipped answer

**The scenario:** artifacts are kept in dated directories; you want to
know if anything changed between last month's cohort and this month's —
a vendor silently upgraded the model, source records were edited, the
same question now gets a different answer.

```bash
# Sanity: a store compared to itself must be drift-free
metaxu drift examples examples --fail-on-drift && echo "clean"

# Real usage: two dated stores
#   metaxu drift artifacts/2026-06/ artifacts/2026-07/ --fail-on-drift
```

To see drift *fire*, run the demo, copy `examples/anticoagulation/out` to
a baseline directory, change the model name in `run_demo.py`
(`set_model("scripted-demo-agent-v2", ...)`), re-run, and compare — the
report flags the environment change, and would separately flag changed
answers and changed source hashes. Exit code 1 makes it a release gate.

---

## 5. Wrap an existing MCP tool server with zero code changes, then gate on governance

**The scenario:** an agent uses tools over MCP (a FHIR server, a
terminology service). You want assurance artifacts without touching the
agent or the server — then an institutional dashboard over everything
collected.

In your MCP client config, replace the server command with:

```jsonc
{ "command": "metaxu",
  "args": ["mcp-proxy", "--out", "artifacts/", "--tags", "tags.json",
           "--policies", "policies.json", "--", "your-fhir-mcp-server"] }
```

Every `tools/call` gets traced, hashed, and policy-checked; an artifact is
written per session. Then:

```bash
metaxu report artifacts/ --html dashboard.html   # self-contained governance dashboard
metaxu report artifacts/ --fail-on-review        # CI gate: exit 1 if anything needs review
```

**What to look for (runnable today without your own MCP server):**
`metaxu report examples --html governance.html` builds the dashboard over
all demo artifacts — policy pass rates, hallucination rate, tool
reliability, and a needs-review triage list with the careless artifacts
at the top. `--fail-on-review` exits 1 on that store, which is the same
gate you'd put in a deployment pipeline.

---

## Where to go after these

- Point use case 1's pattern at a **real CDS sandbox** (the public CDS
  Hooks sandbox, or a SMART-on-FHIR test EHR) — the adapter is
  framework-agnostic dicts-in/dicts-out.
- Wrap a **real agent** with the SDK (or its MCP tools with the proxy)
  and let `metaxu report` accumulate a store over a week of use.
- Evaluate a **vendor agent you don't control** (e.g. Copilot Studio) via
  the black-box harness pattern: call it, record what it cited into a
  session, and let the engines judge it.
