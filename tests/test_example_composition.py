"""The composition demo is itself a benchmark: partials fail, merge passes."""

import importlib.util
import os
import sys

import pytest

EXAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "examples", "composition")


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    spec = importlib.util.spec_from_file_location(
        "composition_demo", os.path.join(EXAMPLE_DIR, "run_demo.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT_DIR = str(tmp_path_factory.mktemp("composition-out"))
    yield module
    target = os.path.realpath(os.path.join(EXAMPLE_DIR, "..", "anticoagulation"))
    sys.path[:] = [p for p in sys.path if os.path.realpath(p) != target]


def anticoag(artifact):
    return next(
        p for p in artifact.policy_checks if p["policy"] == "before_anticoagulation"
    )


def test_each_partial_is_insufficient_but_merge_passes(demo):
    from metaxu import PolicyEngine, merge_artifacts

    sdk = demo.sdk_observer()
    proxy = demo.proxy_observer()

    assert anticoag(sdk)["triggered"] and not anticoag(sdk)["passed"]
    assert anticoag(sdk)["missing"] == ["allergy_check"]
    assert not anticoag(proxy)["triggered"]  # proxy never sees the answer

    merged = merge_artifacts(
        [sdk, proxy], policy_engine=PolicyEngine.from_file(demo.POLICY_FILE)
    )
    assert anticoag(merged)["passed"]
    assert merged.correlation["role"] == "merged"
    assert merged.answer == sdk.answer
    assert merged.verify_integrity()


def test_demo_main_writes_three_artifacts(demo, capsys):
    demo.main()
    out = capsys.readouterr().out
    assert "merged: before_anticoagulation PASS" in out
    written = sorted(os.listdir(demo.OUT_DIR))
    assert written == ["merged.json", "proxy-partial.json", "sdk-partial.json"]
