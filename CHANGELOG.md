# Changelog

All notable changes to Metaxu are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
versions the [Assurance Artifact schema](spec/ARTIFACT.md) and the Python
package together, calendar-dated (`YYYY.M.D`) as of `2026.9.10` — see
[ADR 0003](docs/adr/0003-calendar-versioning.md) for why the scheme moved
off semantic versioning, and `metaxu.artifact.SCHEMA_COMPATIBILITY` for
how schema compatibility is decided now that the version string itself
carries no major/minor/patch structure to parse.

`0.3.0` was the first release published to PyPI — see
[RELEASING.md](RELEASING.md). The `0.1.0` and `0.2.0` entries record the
pre-publication development history (the schema versions artifacts were
produced under); all three predate the move to calendar versioning.

## [Unreleased]

### Changed
- **Versioning scheme: semver → CalVer (`YYYY.M.D`).** The package
  version, `ARTIFACT_SCHEMA_VERSION`, and git tags all move from
  `0.3.0`-style semver to a calendar date, starting at `2026.9.10` — see
  [ADR 0003](docs/adr/0003-calendar-versioning.md). Schema compatibility
  (previously "same major version") is now an explicit, code-maintained
  set of mutually-compatible version strings
  (`metaxu.artifact.SCHEMA_COMPATIBILITY`, checked via the new
  `schema_era()` helper) rather than something parsed out of the version
  string — `merge_artifacts()` uses it in place of the old
  major-version comparison. The pre-CalVer `0.1.0`/`0.2.0`/`0.3.0`
  versions and `2026.9.10` are all in the same compatibility set: the
  format change was not itself a breaking schema change.

### Added
- **LLM API gateway adapter** (`metaxu.adapters.llm_gateway`, Anthropic
  Messages API): assurance from the raw model call — the prompt, the
  answer, and tool-call intents, closing the blind spot the MCP proxy
  structurally has. `record_exchange(request, response)` builds one
  artifact from a `client.messages.create()` call; because the Messages
  API is stateless, walking `request["messages"]` recovers the whole
  tool-call trace (intent *and*, when echoed back, result) for the
  conversation so far, not just the latest turn. Stdlib-only — no
  dependency on the `anthropic` package. `examples/llm_gateway/`
  demonstrates the composition thesis: paired with an MCP-proxy partial
  and merged, per [ADR 0002](docs/adr/0002-adapter-strategy.md).

### Fixed
- LLM gateway question extraction no longer stops at the most recent
  `user`-role message when it's a content-less `tool_result` envelope
  (the normal shape of a tool-use loop's follow-up turn) — it now walks
  back to the actual question text instead of falling back to
  `"(no user message)"`.

## [0.3.0] — 2026-08-16

First release, published to PyPI via Trusted Publishing (OIDC, no stored
token) — see [RELEASING.md](RELEASING.md). Validated end-to-end against
[`docs/USE_CASES.md`](docs/USE_CASES.md) (all five use cases, including a
real run on NixOS / Python 3.14.6 outside the CI test matrix) before the
tag was pushed.

### Added
- **Terminology validation** ([ADR 0001](docs/adr/0001-terminology-validation.md)):
  format/checksum validation for LOINC (Luhn mod-10), SNOMED CT (Verhoeff),
  RxNorm, UCUM, and ICD-10-CM via the built-in data-free `FormatResolver`,
  plus a pluggable `TerminologyResolver` interface for data-backed
  resolvers. Every result carries the `terminology_version` it was checked
  against. Malformed codes become critical safety findings and lower a new
  `terminology_correctness` trust dimension.
- **Evidence graph**: the reasoning chain as a traversable structure
  (`metaxu graph`) — question → answer → claims (including multi-hop
  claim-on-claim reasoning) → resources → codings, with `dependents()`
  impact analysis and text/JSON/Mermaid/DOT rendering. A derived view over
  the event stream; no schema change.
- **OpenTelemetry exporter** (`metaxu.adapters.otel`, `metaxu otel`): turns
  an artifact into an OpenTelemetry span tree — one root span per
  interaction (model, trust dimensions, policy/safety/terminology
  roll-ups), child spans for tool calls and retrievals, and span events for
  claims, policy checks, and findings. Root span status is ERROR on a
  critical safety finding, failed policy, or integrity mismatch. Uses
  `gen_ai.*` semantic conventions where they fit; PHI text is omitted unless
  `capture_content=True`. Optional dependency: `pip install metaxu[otel]`;
  imported lazily so the core stays dependency-free. First of the adapter
  roadmap in [ADR 0002](docs/adr/0002-adapter-strategy.md).
- **CDS Hooks adapter** (`metaxu.adapters.cdshooks`): assurance for
  decision-support services at the EHR boundary. `begin_hook` turns a hook
  request into a session (prefetch → hashed provenance with validated
  codings; draft-order codes checked; `hookInstance` → correlation id);
  `finish_hook` records the cards as the answer and annotates the response
  with a `dev.metaxu` extension (artifact id + assurance summary), plus an
  optional visible assurance card when checks fail. The
  `assured_cds_service` decorator wraps a `handler(request, session) ->
  cards` function and can persist every artifact. `fhirAuthorization`
  bearer tokens are never recorded. Stdlib-only; see `examples/cdshooks/`.
- `docs/USE_CASES.md`: five hands-on use cases, each runnable from a fresh
  clone with exact commands and what-to-look-for notes.
- New `coding` event type; `terminology` artifact field.
- `py.typed` marker — the package now ships its inline type information.

### Changed
- Artifact schema → `0.3.0` (additive: `terminology` field and `coding`
  event type are optional, so `0.2.0` artifacts still validate).
- `link_evidence` accepts claims as well as resources (multi-hop chains);
  `set_answer` accepts `based_on` to name the claims an answer rests on.

## [0.2.0] — 2026-07-15

### Added
- **Multi-observer correlation and merge**: artifacts carry a `correlation`
  block; `metaxu merge` combines partial artifacts sharing an
  `interaction_id` by re-evaluating the engines over the union of
  observations.
- **Policy engine v2**: value conditions (`where`) and temporal conditions
  (`within_hours`), with a distinct `unmet` outcome bucket.
- **Governance engine** (`metaxu report`): aggregate metrics over an
  artifact store, a self-contained HTML dashboard, and a `--fail-on-review`
  CI gate.
- **Drift detection** (`metaxu drift`): environment, behavioral, answer, and
  source drift between two artifact cohorts, with `--fail-on-drift`.
- **Replay harness** (`metaxu replay`, `metaxu diff`): re-run a recorded
  interaction and diff it against the original.

### Changed
- Artifact schema → `0.2.0`. Unknown top-level fields and event types are
  tolerated, consistent with the extensibility rules in the spec.
- MCP proxy moved under `metaxu.adapters`.

### Fixed
- Errored tool calls no longer satisfy policy requirements.
- `verify_integrity()` compares the stored hash against a recomputation
  (previously a tautology that let post-load tampering pass).

## [0.1.0] — 2026-07-15

Initial development release.

### Added
- Assurance SDK core: event model, `AssuranceArtifact`, `AssuranceSession`,
  and the provenance, policy, safety, and trust engines (trust is reported
  per-dimension and never collapsed into a single score).
- `@assured_tool` instrumentation decorator.
- Transparent MCP assurance proxy (`metaxu mcp-proxy`).
- CLI: `inspect`, `validate`, `verify`.
- Assurance Artifact specification, event model, and JSON Schema.
- Anticoagulation and composition example scenarios.
- Apache License 2.0, `CONTRIBUTING.md`, and CI (test matrix + end-to-end
  demo verification).

[0.3.0]: https://github.com/Hefrock/metaxu/releases/tag/v0.3.0
[0.2.0]: https://github.com/Hefrock/metaxu/releases/tag/v0.2.0
[0.1.0]: https://github.com/Hefrock/metaxu/releases/tag/v0.1.0
