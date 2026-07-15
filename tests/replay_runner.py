"""Deterministic runners used by the replay-harness tests (and the CLI
replay test, which imports this module by path as ``tests.replay_runner``)."""

from metaxu import ProvenanceRecord

RESOURCE = {"resourceType": "Observation", "id": "obs-1", "value": 232}
SOURCE = "https://fhir.example.org"


def build(question, session, value=232, answer="Platelets adequate; proceed."):
    resource = dict(RESOURCE, value=value)
    prov = session.record_retrieval(
        ProvenanceRecord.for_resource(
            source_system=SOURCE,
            resource_type="Observation",
            resource_id="obs-1",
            content=resource,
        ),
        tags=["platelet_count", "patient_record_access"],
    )
    claim = session.record_claim(f"Platelet value {resource['value']}.")
    session.link_evidence(claim, [prov])
    session.set_answer(answer)


def runner(question, session):
    """Faithful replay: identical workflow, identical data."""
    build(question, session)


def drifted_runner(question, session):
    """The source data changed: different value, different answer."""
    build(question, session, value=41, answer="Hold: platelets low.")


def lazy_runner(question, session):
    """Skips the retrieval and asserts without evidence."""
    session.record_claim("Platelet value 232.")
    session.set_answer("Platelets adequate; proceed.")
