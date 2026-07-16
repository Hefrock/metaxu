"""End-to-end test over the anticoagulation example.

The example doubles as the first benchmark scenario: the diligent agent
must produce a clean artifact, the careless agent must be caught by the
policy, safety, and trust engines.
"""

import importlib.util
import os
import sys

import pytest

EXAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "examples", "anticoagulation")


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    sys.path.insert(0, EXAMPLE_DIR)
    spec = importlib.util.spec_from_file_location(
        "run_demo", os.path.join(EXAMPLE_DIR, "run_demo.py")
    )
    module = importlib.util.module_from_spec(spec)
    # Redirect outputs into a temp dir before the module-level dirs are used.
    spec.loader.exec_module(module)
    out = tmp_path_factory.mktemp("demo-out")
    module.OUT_DIR = str(out)
    module.SNAPSHOT_DIR = str(out / "snapshots")
    yield module
    sys.path.remove(EXAMPLE_DIR)


def run_agent(demo, agent):
    from metaxu import AssuranceSession, PolicyEngine

    engine = PolicyEngine.from_file(os.path.join(EXAMPLE_DIR, "policies.json"))
    with AssuranceSession(question=demo.QUESTION, policy_engine=engine) as session:
        agent(session)
    return session.artifact


def test_diligent_agent_passes_all_policies(demo):
    artifact = run_agent(demo, demo.diligent_agent)
    assert all(p["passed"] for p in artifact.policy_checks)
    assert artifact.safety_checks == []
    assert artifact.trust_scores["policy_compliance"]["score"] == 1.0
    assert artifact.trust_scores["provenance_coverage"]["score"] == 1.0
    assert artifact.trust_scores["safety"]["score"] == 1.0
    assert len(artifact.provenance) == 5  # patient, platelets, creatinine, allergy, guideline
    assert artifact.verify_integrity()


def test_careless_agent_is_caught(demo):
    artifact = run_agent(demo, demo.careless_agent)

    anticoag = next(
        p for p in artifact.policy_checks if p["policy"] == "before_anticoagulation"
    )
    assert anticoag["triggered"]
    assert not anticoag["passed"]
    assert set(anticoag["missing"]) == {"allergy_check", "pregnancy_status", "creatinine"}

    safety = {f["check"] for f in artifact.safety_checks}
    assert "unsupported_claims" in safety  # "Renal function is normal."

    assert artifact.trust_scores["policy_compliance"]["score"] < 1.0
    assert artifact.trust_scores["provenance_coverage"]["score"] == 0.5
    assert artifact.trust_scores["safety"]["score"] == 0.0


def test_replay_verifies_diligent_artifact(demo, tmp_path):
    from metaxu import snapshot_resolver, verify

    artifact = run_agent(demo, demo.diligent_agent)
    report = verify(artifact, snapshot_resolver(demo.SNAPSHOT_DIR))
    assert report.ok
    assert report.provenance_matched == report.provenance_checked == 5
