# ADR 0003: Calendar versioning for the package and the artifact schema

**Status:** accepted (2026-09-10)
**Context owner:** durable record of why Metaxu's version numbers changed
format and how schema compatibility is decided now that they no longer
have major/minor/patch structure. Read before bumping a version or
changing `SCHEMA_COMPATIBILITY`.

## Context

Through `0.3.0`, the package version and the Assurance Artifact
`schema_version` were versioned together under semver, per the original
design in [CHANGELOG.md](../../CHANGELOG.md). In practice the three
releases so far (`0.1.0`, `0.2.0`, `0.3.0`) were an arbitrary phase
counter, not semver's actual signal: every change has been additive, so
the leading digit never carried real meaning — it just incremented once
per development phase. The maintainer's read: that number is arbitrary
and a calendar date is more informative (readers can see *when* a release
shipped without cross-referencing the CHANGELOG).

Two version strings are affected, and they carry different
responsibilities:

- **Package version** (`pyproject.toml`, `metaxu.__version__`, PyPI, git
  tags) — pure release identity. No compatibility logic anywhere reads
  its structure. Safe to move to CalVer with no other changes.
- **Artifact `schema_version`** — genuinely load-bearing.
  `merge_artifacts()` used to refuse to merge two artifacts unless
  `schema_version.split(".")[0]` matched (`merge.py`, pre-migration), and
  [`spec/ARTIFACT.md`](../../spec/ARTIFACT.md) documented a
  semver-major-version compatibility contract for any consumer. A
  calendar date has no major/minor/patch structure, so that check could
  not simply be re-pointed at a new string format — the compatibility
  *mechanism* itself had to change, not just the version's spelling.

## Decision

**Both** move to calendar versioning, format `YYYY.M.D` (e.g. `2026.9.10`)
— no leading zeros. Leading zeros matter here for a concrete reason: PEP
440 normalizes `"2026.09.10"` to `"2026.9.10"` when a wheel is built, so
writing the zero-padded form in `pyproject.toml` while leaving
`__init__.py`'s `__version__` (a plain string literal, never normalized)
zero-padded would silently desync the two, and the git tag pushed for
release would disagree with what actually ends up on PyPI. Writing the
already-normalized form everywhere sidesteps this entirely — verified
directly: `packaging.version.Version("2026.09.10") == Version("2026.9.10")`
is `True`, and the built wheel's filename uses the normalized form
regardless of what's written in the source.

**Schema compatibility is decoupled from the version string's structure.**
`src/metaxu/artifact.py` now defines:

```python
SCHEMA_COMPATIBILITY: tuple[frozenset[str], ...] = (
    frozenset({"0.1.0", "0.2.0", "0.3.0", "2026.9.10"}),
)

def schema_era(version: str) -> frozenset[str] | None:
    return next((era for era in SCHEMA_COMPATIBILITY if version in era), None)
```

Every version string inside one `frozenset` is asserted to be mutually
additive-compatible — the same guarantee "same major version" used to
encode, just made explicit and code-maintained instead of derived by
parsing. `merge_artifacts()` now checks `schema_era()` membership instead
of splitting on `.`: artifacts merge only if every one of their
`schema_version`s falls in the *same* set, and an unrecognized version
(not in any set at all) is rejected outright rather than silently
assumed compatible.

The pre-CalVer semver versions and the first CalVer version share one
set: moving to a new *format* was not itself a *breaking schema change*
— every field from `0.1.0` onward is still present and means the same
thing. A future breaking change (a field removed, renamed, or retyped)
starts a **new** `frozenset` entry in `SCHEMA_COMPATIBILITY`; artifacts
whose versions land in different sets are never merged or replayed
against each other, with a clear error naming both versions.

## Consequences

- Package release version and artifact schema version stay coupled — one
  version bump covers both, per release, as before. Only the format
  changed.
- `spec/ARTIFACT.md`'s Versioning section no longer promises semver or a
  "major version" comparison; it documents `SCHEMA_COMPATIBILITY`
  instead. Any external consumer that was parsing `schema_version` as
  `major.minor.patch` needs to switch to checking membership in a known
  compatibility set — there is no drop-in numeric substitute, since a
  calendar date and a major version answer different questions.
- A real breaking schema change is now a two-part act, not just a number
  bump: add a new `frozenset` to `SCHEMA_COMPATIBILITY` *and* document
  why the change isn't additive in `spec/ARTIFACT.md` and the CHANGELOG.
  This is slightly more ceremony than incrementing a major version was,
  which is intentional — it has never actually happened in this
  project's history, and the old mechanism was never exercised (every
  schema version to date has had leading segment `"0"`, so the old
  "same major version" check was, in practice, always true).
- `RELEASING.md`'s tag-matching step in `.github/workflows/release.yml`
  needed no code change — it compares the git tag against
  `pyproject.toml`'s version as an opaque string via `tomllib`, which is
  format-agnostic.
