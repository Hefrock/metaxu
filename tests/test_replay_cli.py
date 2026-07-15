"""Tests for replay verification and the CLI."""

import json

from metaxu import (
    AssuranceSession,
    ProvenanceRecord,
    save_snapshot,
    snapshot_resolver,
    verify,
)
from metaxu.cli import main as cli_main

RESOURCE = {"resourceType": "Observation", "id": "obs-1", "value": 232}


def build_artifact(tmp_path, tamper_source=False):
    snapshots = str(tmp_path / "snapshots")
    with AssuranceSession(question="Q?") as session:
        prov = ProvenanceRecord.for_resource(
            source_system="https://fhir.example.org",
            resource_type="Observation",
            resource_id="obs-1",
            content=RESOURCE,
        )
        session.record_retrieval(prov)
        stored = dict(RESOURCE, value=999) if tamper_source else RESOURCE
        save_snapshot(snapshots, prov, stored)
        claim = session.record_claim("c")
        session.link_evidence(claim, [prov])
        session.set_answer("A")
    path = str(tmp_path / "artifact.json")
    session.artifact.save(path)
    return session.artifact, path, snapshots


def test_verify_clean_artifact(tmp_path):
    artifact, _, snapshots = build_artifact(tmp_path)
    report = verify(artifact, snapshot_resolver(snapshots))
    assert report.ok
    assert report.integrity_ok
    assert report.provenance_checked == 1
    assert report.provenance_matched == 1


def test_verify_detects_source_drift(tmp_path):
    artifact, _, snapshots = build_artifact(tmp_path, tamper_source=True)
    report = verify(artifact, snapshot_resolver(snapshots))
    assert report.integrity_ok
    assert not report.provenance_ok
    assert report.mismatches[0]["resource"] == "Observation/obs-1"


def test_verify_reports_unresolved(tmp_path):
    artifact, _, _ = build_artifact(tmp_path)
    report = verify(artifact, snapshot_resolver(str(tmp_path / "empty")))
    assert report.unresolved == [artifact.provenance[0].id]
    assert report.provenance_checked == 0


def test_cli_inspect(tmp_path, capsys):
    _, path, _ = build_artifact(tmp_path)
    assert cli_main(["inspect", path]) == 0
    out = capsys.readouterr().out
    assert "Question: Q?" in out
    assert "Trust dimensions:" in out
    assert "integrity:  ok" in out


def test_cli_validate(tmp_path, capsys):
    _, path, _ = build_artifact(tmp_path)
    assert cli_main(["validate", path]) == 0
    assert "valid" in capsys.readouterr().out


def test_cli_validate_rejects_missing_fields(tmp_path, capsys):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        json.dump({"question": "Q?"}, f)
    assert cli_main(["validate", path]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_cli_verify(tmp_path, capsys):
    _, path, snapshots = build_artifact(tmp_path)
    assert cli_main(["verify", path, "--snapshots", snapshots]) == 0
    out = capsys.readouterr().out
    assert "result:     ok" in out


def test_cli_verify_fails_on_drift(tmp_path, capsys):
    _, path, snapshots = build_artifact(tmp_path, tamper_source=True)
    assert cli_main(["verify", path, "--snapshots", snapshots]) == 1
    assert "CHANGED since retrieval" in capsys.readouterr().out
