# Changelog

All notable changes to Metaxu are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
versions the [Assurance Artifact schema](spec/ARTIFACT.md) and the Python
package together under [semantic versioning](https://semver.org/).

`0.3.0` is the first release published to PyPI. The `0.1.0` and `0.2.0`
entries record the pre-publication development history (the schema
versions artifacts were produced under).

## [0.3.0] — 2026-07-16

First public release.

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
