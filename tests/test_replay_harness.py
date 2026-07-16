"""Tests for the replay harness: diff_artifacts and replay_with_runner."""

from metaxu import (
    AssuranceSession,
    PolicyEngine,
    diff_artifacts,
    replay_with_runner,
    save_snapshot,
    snapshot_resolver,
)
from metaxu.cli import main as cli_main
from replay_runner import build, drifted_runner, lazy_runner, runner

QUESTION = "Start anticoagulation for pat-001?"


def make_original(**kwargs):
    with AssuranceSession(question=QUESTION) as session:
        build(QUESTION, session, **kwargs)
    return session.artifact


def test_faithful_replay_is_reproduced():
    original = make_original()
    replay_artifact, diff = replay_with_runner(original, runner)

    assert diff["reproduced"]
    assert diff["differences"] == []
    assert diff["answer"]["match"]
    assert diff["tool_calls"]["match"]
    assert diff["claims"]["match"]
    assert diff["provenance"]["shared_resources"] == 1
    assert diff["provenance"]["hash_mismatches"] == []

    assert replay_artifact.correlation["observer"] == "replay"
    assert (
        replay_artifact.correlation["interaction_id"]
        == original.correlation["interaction_id"]
    )
    assert replay_artifact.metadata["dev.metaxu/replay_of"] == original.id


def test_data_change_shows_hash_mismatch_and_answer_drift():
    original = make_original()
    _, diff = replay_with_runner(original, drifted_runner)

    assert not diff["reproduced"]
    assert not diff["answer"]["match"]
    assert not diff["claims"]["match"]
    [mismatch] = diff["provenance"]["hash_mismatches"]
    assert mismatch["resource"] == "Observation/obs-1"
    assert any("content differs" in d for d in diff["differences"])


def test_skipped_retrieval_detected():
    original = make_original()
    _, diff = replay_with_runner(original, lazy_runner)

    assert not diff["reproduced"]
    assert not diff["tool_calls"]["match"] or diff["provenance"]["only_in_original"]
    assert diff["provenance"]["only_in_original"] == ["Observation/obs-1"]
    # Same claim text and answer, so those still match — the diff localizes
    # the difference to the missing retrieval rather than shouting broadly.
    assert diff["answer"]["match"]
    assert diff["claims"]["match"]


def test_policy_outcome_difference_detected():
    doc = {"policies": [{"name": "grounding", "requires": ["patient_record_access"]}]}
    with AssuranceSession(
        question=QUESTION, policy_engine=PolicyEngine.from_document(doc)
    ) as session:
        build(QUESTION, session)
    original = session.artifact

    # Replay without evidence: grounding flips from passed to failed.
    _, diff = replay_with_runner(
        original, lazy_runner, policy_engine=PolicyEngine.from_document(doc)
    )
    assert not diff["policies"]["match"]
    assert any("policy 'grounding' outcome differs" in d for d in diff["differences"])


def test_replay_from_snapshots_pattern(tmp_path):
    """The recommended pattern: a runner wired to recorded snapshots."""
    snapshots = str(tmp_path / "snapshots")
    original = make_original()
    for record in original.provenance:
        save_snapshot(snapshots, record, dict(build.__globals__["RESOURCE"]))

    resolve = snapshot_resolver(snapshots)

    def snapshot_runner(question, session):
        from metaxu import ProvenanceRecord

        [record] = original.provenance
        content = resolve(record)
        prov = session.record_retrieval(
            ProvenanceRecord.for_resource(
                source_system=record.source_system,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                content=content,
            ),
            tags=["platelet_count", "patient_record_access"],
        )
        claim = session.record_claim(f"Platelet value {content['value']}.")
        session.link_evidence(claim, [prov])
        session.set_answer("Platelets adequate; proceed.")

    _, diff = replay_with_runner(original, snapshot_runner)
    assert diff["reproduced"]
    assert diff["provenance"]["hash_mismatches"] == []


def test_diff_artifacts_standalone_symmetric_resources():
    a = make_original()
    with AssuranceSession(question=QUESTION) as session:
        build(QUESTION, session)
        session.record_retrieval(
            __import__("metaxu").ProvenanceRecord.for_resource(
                source_system="https://other.example.org",
                resource_type="Observation",
                resource_id="obs-extra",
                content={"x": 1},
            )
        )
    b = session.artifact
    diff = diff_artifacts(a, b)
    assert diff["provenance"]["only_in_replay"] == ["Observation/obs-extra"]


def test_cli_diff(tmp_path, capsys):
    a_path, b_path = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    make_original().save(a_path)
    make_original().save(b_path)
    assert cli_main(["diff", a_path, b_path, "--fail-on-diff"]) == 0
    assert "reproduced: YES" in capsys.readouterr().out

    make_original(answer="Different answer.").save(b_path)
    assert cli_main(["diff", a_path, b_path, "--fail-on-diff"]) == 1
    out = capsys.readouterr().out
    assert "reproduced: NO" in out
    assert "answer differs" in out


def test_cli_replay_with_runner_entrypoint(tmp_path, capsys):
    artifact_path = str(tmp_path / "original.json")
    replay_path = str(tmp_path / "replay.json")
    make_original().save(artifact_path)

    code = cli_main(
        [
            "replay",
            artifact_path,
            "--runner",
            "tests.replay_runner:runner",
            "--out",
            replay_path,
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "reproduced: YES" in out

    from metaxu import AssuranceArtifact

    replayed = AssuranceArtifact.load(replay_path)
    assert replayed.correlation["observer"] == "replay"

    assert (
        cli_main(["replay", artifact_path, "--runner", "tests.replay_runner:drifted_runner"])
        == 1
    )
    assert "reproduced: NO" in capsys.readouterr().out


def test_cli_replay_bad_runner_spec(tmp_path, capsys):
    artifact_path = str(tmp_path / "original.json")
    make_original().save(artifact_path)
    assert cli_main(["replay", artifact_path, "--runner", "nonsense"]) == 2
    assert cli_main(["replay", artifact_path, "--runner", "no.such.module:fn"]) == 2
