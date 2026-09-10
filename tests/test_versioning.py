"""Guards that the triplicated version literal (pyproject.toml,
__init__.py, artifact.py) can't silently drift out of sync, and that a
bumped version was remembered in SCHEMA_COMPATIBILITY.

There is no single source of truth read at runtime for these three
values — see docs/adr/0003-calendar-versioning.md — so nothing else in
the suite would catch a release that bumped one and forgot another.
"""

import os
import sys

import pytest

import metaxu
from metaxu.artifact import schema_era

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is stdlib only from 3.11")
def test_pyproject_version_matches_package_version():
    import tomllib

    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        pyproject = tomllib.load(f)
    assert pyproject["project"]["version"] == metaxu.__version__


def test_artifact_schema_version_matches_package_version():
    # Not a technical requirement (they could diverge), but the project's
    # stated convention (CHANGELOG.md, ADR 0003) is to bump both together.
    assert metaxu.ARTIFACT_SCHEMA_VERSION == metaxu.__version__


def test_current_version_is_in_a_known_compatibility_set():
    # A version bump that forgets to add itself to SCHEMA_COMPATIBILITY
    # would make every freshly-produced artifact unmergeable with itself.
    assert schema_era(metaxu.ARTIFACT_SCHEMA_VERSION) is not None
