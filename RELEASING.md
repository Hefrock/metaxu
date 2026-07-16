# Releasing Metaxu

Metaxu publishes to PyPI via **Trusted Publishing** (OIDC): no API token
is stored in the repository. A release is cut by pushing a version tag;
the [`release.yml`](.github/workflows/release.yml) workflow builds,
validates, and publishes the distribution.

## One-time setup (maintainer, on PyPI)

Do this once before the first release:

1. Log in at <https://pypi.org> and go to **Your projects → Publishing**
   (or, before the project exists, **Account → Publishing → Add a pending
   publisher**).
2. Add a **GitHub** trusted publisher:
   - Owner: `Hefrock`
   - Repository: `metaxu`
   - Workflow name: `release.yml`
   - Environment: `pypi`
3. In the GitHub repo, create an **Environment** named `pypi`
   (Settings → Environments). Optionally add required reviewers so a human
   approves each publish.

No secrets are added anywhere — the workflow authenticates to PyPI with a
short-lived OIDC token minted per run.

## Cutting a release

1. Make sure `main` is green and the version is bumped in **both**
   `pyproject.toml` and `src/metaxu/__init__.py` (`__version__`), the
   `ARTIFACT_SCHEMA_VERSION` is correct if the schema changed, and
   `CHANGELOG.md` has an entry for the version.
2. Tag and push:
   ```bash
   git tag v0.3.0        # must match the package version exactly
   git push origin v0.3.0
   ```
3. The workflow runs: it builds the sdist and wheel, runs `twine check`,
   asserts the tag matches `metaxu.__version__`, and (after any required
   environment approval) publishes to PyPI.

## Verifying a build locally first

The published metadata uses PEP 639 (`License-Expression`). Validating it
requires reasonably current tooling — **`packaging >= 24.2`** and
**`twine >= 6`**; older `packaging` reports a spurious
"unrecognized field 'license-expression'" error even though the
distribution is correct. In a clean virtualenv:

```bash
python -m pip install --upgrade build "twine>=6" "packaging>=24.2"
python -m build
python -m twine check dist/*          # both files must say PASSED
python -m pip install dist/*.whl      # smoke test
metaxu --help
```

## Test PyPI (optional dry run)

To rehearse without touching the real index, add a second trusted
publisher on <https://test.pypi.org> and a workflow step targeting it, or
upload manually into a throwaway Test PyPI account:

```bash
python -m twine upload --repository testpypi dist/*
```

Do **not** point the automated `release.yml` at Test PyPI for real
releases — production releases go to PyPI only.
