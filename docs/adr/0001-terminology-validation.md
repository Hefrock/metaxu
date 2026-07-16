# ADR 0001: Terminology validation strategy

**Status:** accepted (2026-07-16)
**Context owner:** this document is the durable record of a design decision
discussed at length; read it before changing how Metaxu validates clinical
terminology codes (SNOMED CT, LOINC, RxNorm, UCUM, ICD).

## The two questions hiding under "terminology validation"

1. **Is this a well-formed code?** — does `2160-0` have the shape and check
   digit of a real LOINC code; is `123456789012` a structurally valid SNOMED
   identifier. Catches **hallucinated / malformed** codes. Needs **no data** —
   only public checksum algorithms.
2. **Is this the *right* code for the claim?** — the AI cites LOINC `2160-0`
   for "creatinine" and `2160-0` really does mean serum creatinine, and is
   still active. Catches **subtly wrong** codes. Needs the actual **code
   tables**, which is where licensing enters.

## Decision

Ship **A + B**; keep **C** as an optional, later, per-terminology path.

- **A — Format/checksum validation (all systems, no data).** Regex + check
  digits: LOINC (Luhn mod-10), SNOMED CT (Verhoeff + length), RxNorm (numeric
  RxCUI), UCUM (grammar subset), ICD-10-CM (pattern). Zero licensing exposure.
  This is `FormatResolver`, the built-in default.
- **B — Pluggable resolver interface (`TerminologyResolver`).** Metaxu ships
  the *check logic and interface*, never the *data*. Institutions plug in a
  terminology server they already have access to (a local server, NLM's free
  UMLS Terminology Services API, etc.). This is the architecturally correct
  long-term answer and the only clean path for SNOMED.
- **C — Bundled data (later, optional, `metaxu[terminology]`).** Legally
  plausible for **LOINC and RxNorm** (free to redistribute after their
  click-through). **SNOMED CT is excluded from any bundled path** — it is free
  only in "Member" countries, so shipping its tables in a public repo would
  expose users in non-member jurisdictions. SNOMED is instead served by a
  **local build step** (see below).

## Licensing summary (why the systems differ)

| System | Redistribution status |
|---|---|
| **UCUM** | Fully open — no license gate. |
| **LOINC** | Free; click-through license to download; redistribution-friendly. |
| **RxNorm** | Free; UMLS license (free account) to download; some source-vocabulary sub-restrictions. |
| **SNOMED CT** | Free only in Member countries; Affiliate License otherwise. **Never bundle for an unknown-jurisdiction audience.** |

## Versioning — the non-negotiable engineering discipline

Terminologies **change**, and not only by addition. The dangerous changes are
**retirement** (a code valid in 2024 is inactive in 2026) and **remapping**
(RxCUIs repointed). Cadence: LOINC ~2×/yr, RxNorm monthly, SNOMED ~2×/yr, UCUM
rare.

Two failures follow from validating against "whatever is current":
1. A historical artifact citing a since-retired code looks like a hallucination
   it never was.
2. Re-validating the same artifact later can yield a *different* verdict than it
   did originally — which violates the reproducibility guarantee the replay and
   drift engines are built on.

**Therefore every terminology check records the terminology version it
consulted**, exactly as provenance records a resource's `resource_version` and
reproducibility records `tool_versions`:

- `TerminologyResolver` exposes a `version` string; every `CodeValidation`
  carries `terminology_version`.
- That version flows into the artifact (the `terminology` block / check
  events), so it is auditable and `metaxu drift` can later flag "terminology
  version changed between cohorts" the same way it flags model/tool drift.
- Replay pins validation to the version recorded in the *original* artifact,
  never "latest", so re-verification stays deterministic.
- `FormatResolver.version` is `"format-check"` — algorithmic validation is not
  tied to a release; a data-backed resolver reports e.g. `"LOINC-2.78"`.

This seam is why B is designed first even though only format-checking ships
today: a future bundled index (C) or a user-built SNOMED index is just another
`TerminologyResolver` implementation — no rework.

## SNOMED "later" pattern: local build, never redistribution

`metaxu terminology build --snomed <national-release-files>` (future) compiles a
release file the user **already holds rights to** into a versioned local index,
stamped with the edition/date read from the file's own metadata. Metaxu never
distributes SNOMED bytes — it only indexes bytes the user legally holds. Legal
in every jurisdiction, and versioned for free because national releases are
themselves versioned artifacts. The same discipline applies if LOINC/RxNorm are
ever bundled: ship them as **versioned, replaceable data files** (not baked into
code) with a regeneration script and old versions retained, so historical
artifacts replay against the version they were actually checked against.

## Consequences

- The core stays stdlib-only; format validation adds no dependency.
- Full "right code for the claim" validation requires the institution to
  supply a resolver — consistent with Metaxu being model/agent/EHR-agnostic.
- Adding C for LOINC/RxNorm, or SNOMED local-build, is future work that slots
  into the existing interface without touching callers.
