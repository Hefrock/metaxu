"""Tests for the governance engine and HTML dashboard."""

import json
import os

from metaxu import (
    AssuranceSession,
    PolicyEngine,
    ProvenanceRecord,
    aggregate_artifacts,
    assured_tool,
    load_artifacts,
)
from metaxu.cli import main as cli_main
from metaxu.dashboard import render_html
from metaxu.governance import render_text

POLICY = PolicyEngine.from_document(
    {
        "policies": [
            {
                "name": "grounding",
                "trigger": {"always": True},
                "requires": ["patient_record_access"],
            }
        ]
    }
)


def good_artifact():
    """Grounded answer: retrieval, evidence-linked claim, policy passes."""
    engine = PolicyEngine.from_document(
        {"policies": [{"name": "grounding", "requires": ["patient_record_access"]}]}
    )

    @assured_tool(tags=["patient_record_access"])
    def get_labs(patient_id: str) -> dict:
        return {"value": 232}

    with AssuranceSession(question="Good Q?", policy_engine=engine) as session:
        get_labs("pat-001")
        prov = session.record_retrieval(
            ProvenanceRecord.for_resource(
                source_system="https://fhir.example.org",
                resource_type="Observation",
                resource_id="obs-1",
                content={"value": 232},
            ),
            tags=["patient_record_access"],
        )
        claim = session.record_claim("Value normal.")
        session.link_evidence(claim, [prov])
        session.set_answer("All good.")
    return session.artifact


def bad_artifact():
    """Unsupported claim, failed policy, errored tool, missing data."""
    engine = PolicyEngine.from_document(
        {"policies": [{"name": "grounding", "requires": ["patient_record_access"]}]}
    )

    @assured_tool(name="get_labs")
    def broken(patient_id: str) -> dict:
        raise ConnectionError("EHR down")

    with AssuranceSession(question="Bad Q?", policy_engine=engine) as session:
        try:
            broken("pat-001")
        except ConnectionError:
            pass
        session.record_claim("Renal function is normal.")  # unsupported
        session.record_missing_data("creatinine", reason="EHR down")
        session.set_answer("Proceed.")
    return session.artifact


def write_store(tmp_path, artifacts):
    store = tmp_path / "store"
    store.mkdir(parents=True)
    for i, artifact in enumerate(artifacts):
        artifact.save(str(store / f"artifact-{i}.json"))
    # Non-artifact JSON files must be skipped, not fatal.
    (store / "snapshots").mkdir()
    (store / "snapshots" / "Observation-obs-1.json").write_text('{"value": 232}')
    (store / "junk.json").write_text("not even json {")
    return str(store)


def test_aggregate_over_mixed_artifacts():
    report = aggregate_artifacts([good_artifact(), bad_artifact()])

    assert report["artifact_count"] == 2
    assert report["integrity"] == {"verified": 2, "failed": 0}
    assert report["roles"] == {"partial": 2}
    assert report["observers"] == {"sdk": 2}

    grounding = report["policies"]["grounding"]
    assert grounding["triggered"] == 2
    assert grounding["passed"] == 1
    assert grounding["pass_rate"] == 0.5
    assert "patient_record_access" in grounding["top_unsatisfied_requirements"]

    assert report["safety"]["unsupported_claim_rate"] == 0.5
    assert report["safety"]["hallucination_rate"] == 0.0
    assert report["safety"]["findings_by_check"]["unsupported_claims"] == 1

    labs = report["tools"]["get_labs"]
    assert labs["calls"] == 2
    assert labs["errors"] == 1
    assert labs["error_rate"] == 0.5

    assert report["missing_data"] == {"creatinine": 1}
    assert report["provenance"]["total_records"] == 1
    assert report["trust"]["provenance_coverage"]["mean"] == 0.5

    [review] = report["needs_review"]
    assert review["question"] == "Bad Q?"
    assert any("critical" in reason for reason in review["reasons"])
    assert any("grounding" in reason for reason in review["reasons"])


def test_aggregate_empty():
    report = aggregate_artifacts([])
    assert report["artifact_count"] == 0
    assert report["needs_review"] == []


def test_tampered_artifact_counted_and_flagged(tmp_path):
    artifact = good_artifact()
    store = write_store(tmp_path, [artifact])
    path = os.path.join(store, "artifact-0.json")
    data = json.loads(open(path).read())
    data["answer"] = "Tampered."
    open(path, "w").write(json.dumps(data))

    loaded = load_artifacts(store)
    report = aggregate_artifacts(loaded)
    assert report["integrity"]["failed"] == 1
    assert any(
        "integrity hash mismatch" in r for e in report["needs_review"] for r in e["reasons"]
    )


def test_load_artifacts_skips_non_artifacts(tmp_path):
    store = write_store(tmp_path, [good_artifact(), bad_artifact()])
    loaded = load_artifacts(store)
    assert len(loaded) == 2  # snapshot + junk skipped


def test_render_text_smoke():
    text = render_text(aggregate_artifacts([good_artifact(), bad_artifact()]))
    assert "Governance report over 2 artifact(s)" in text
    assert "grounding" in text
    assert "Needs review: 1 artifact(s)" in text


def test_render_html_escapes_and_reports(tmp_path):
    bad = bad_artifact()
    bad.question = 'Bad <script>alert("x")</script> Q?'
    html_out = render_html(aggregate_artifacts([good_artifact(), bad]))
    assert "<script>alert" not in html_out  # question text is escaped
    assert "&lt;script&gt;" in html_out
    assert "Needs review" in html_out
    assert "Trust dimensions" in html_out
    assert "prefers-color-scheme: dark" in html_out


def test_cli_report_text_json_html(tmp_path, capsys):
    store = write_store(tmp_path, [good_artifact(), bad_artifact()])

    assert cli_main(["report", store]) == 0
    assert "Governance report over 2" in capsys.readouterr().out

    assert cli_main(["report", store, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["artifact_count"] == 2

    html_path = str(tmp_path / "dash.html")
    assert cli_main(["report", store, "--html", html_path]) == 0
    capsys.readouterr()
    assert "Metaxu governance report" in open(html_path).read()


def test_cli_report_fail_on_review_gate(tmp_path, capsys):
    clean = write_store(tmp_path / "clean", [good_artifact()])
    dirty = write_store(tmp_path / "dirty", [good_artifact(), bad_artifact()])
    assert cli_main(["report", clean, "--fail-on-review"]) == 0
    capsys.readouterr()
    assert cli_main(["report", dirty, "--fail-on-review"]) == 1
