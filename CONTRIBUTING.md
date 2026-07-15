# Contributing to Metaxu

Metaxu aims to become a shared standard, not one maintainer's opinion —
which means it improves fastest through outside review, especially from
people who run real clinical AI workflows, EHR integrations, or MCP
tooling. Contributions of every size are welcome: spec feedback, adapter
implementations, policy packs, bug reports, or just telling us where the
artifact schema doesn't fit your use case.

## Ways to contribute

- **Spec feedback** — open an issue against [`spec/ARTIFACT.md`](spec/ARTIFACT.md)
  or [`spec/EVENT_MODEL.md`](spec/EVENT_MODEL.md) if a field, versioning
  rule, or extensibility point doesn't work for your system. Spec changes
  are the highest-leverage contributions and the ones most worth
  discussing before code is written.
- **Adapters** — a new observer under `src/metaxu/adapters/` (OpenTelemetry,
  CDS Hooks, an LLM gateway, your own EHR integration) that translates a
  boundary into assurance events. See `src/metaxu/adapters/mcp.py` for
  the reference shape.
- **Policy packs** — declarative JSON/YAML policies for clinical
  workflows beyond the anticoagulation example, ideally with a benchmark
  scenario like `examples/anticoagulation/`.
- **Engine improvements** — new safety checks, trust dimensions, or
  terminology validation (SNOMED/LOINC/RxNorm/UCUM) in the policy, safety,
  or trust engines.
- **Bug reports** — especially ones with a minimal artifact or event
  sequence that reproduces the issue.

## Before you start on something large

For anything beyond a small fix — a new adapter, a spec change, a new
trust dimension — open an issue first describing what you want to do and
why. This project is young enough that direction is still being set, and
an issue avoids landing on a PR that conflicts with where things are
headed.

## Development setup

```bash
git clone https://github.com/Hefrock/metaxu.git
cd metaxu
pip install -e ".[dev]"
pytest
```

The core SDK (`src/metaxu/`) is stdlib-only by design — do not add a
required dependency there. YAML policy loading and full JSON Schema
validation are optional extras (`metaxu[yaml]`, `metaxu[schema]`); new
optional capabilities should follow the same pattern rather than becoming
hard requirements.

Run the examples as an end-to-end sanity check before opening a PR:

```bash
python examples/anticoagulation/run_demo.py
python examples/composition/run_demo.py
metaxu validate examples/anticoagulation/out/diligent-artifact.json
```

## Pull requests

- Keep PRs focused; separate spec changes from implementation changes
  where possible so each can be reviewed on its own terms.
- Add or update tests for any behavior change — the test suite is the
  actual specification of engine behavior today, more than any prose.
- If you change `src/metaxu/spec/assurance-artifact.schema.json`, update
  `spec/ARTIFACT.md` in the same PR, and bump `ARTIFACT_SCHEMA_VERSION`
  in `src/metaxu/artifact.py` per the versioning rules in `spec/ARTIFACT.md`
  (additive within a major version; breaking changes require a major bump).
- CI (`.github/workflows/ci.yml`) runs the test matrix and an end-to-end
  demo/validation job; both must pass.

## Code style

No enforced formatter yet. Match the surrounding code: type hints on
public functions, docstrings that explain *why* rather than *what*, and
no comments that just restate the code beneath them.

## Reporting security issues

Metaxu artifacts may carry PHI. If you find a way that the framework
could leak, mislabel, or misattribute patient data, or a way that a
crafted artifact/event stream could defeat the safety or policy engines,
please open an issue marked clearly as a security concern rather than a
routine bug, so it gets prioritized review.

## License

By contributing, you agree that your contributions are licensed under
the [Apache License 2.0](LICENSE), the same license as the rest of the
project (see `spec/ARTIFACT.md` and `NOTICE` for related details).
